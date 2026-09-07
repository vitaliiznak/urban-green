"""Adapter tests: offline parsing of real upstream fixtures, plus live network
checks that only run with CANOPY_NETWORK_TESTS=1."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, box

from server import crs
from server.adapters import ADAPTERS, DemoAdapter, get_adapter, serialize_street
from server.adapters import osm, zurich
from server.adapters.base import CityAdapter, StreetContext, StreetNotFound
from server.schemas import CityInfo, StreetResponse

FIXTURES = Path(__file__).parent / "fixtures"
EVERYWHERE = box(-1e9, -1e9, 1e9, 1e9)
NETWORK = os.environ.get("CANOPY_NETWORK_TESTS") == "1"
network = pytest.mark.skipif(not NETWORK, reason="set CANOPY_NETWORK_TESTS=1 to run live upstream tests")


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def area_data():
    return load("overpass_langstrasse_area.json")


@pytest.fixture(scope="module")
def langstrasse(area_data):
    """Axis merged from the Langstrasse ways in the area fixture plus its OSM layers."""
    ways = [e for e in area_data["elements"] if e["type"] == "way" and e["tags"].get("name") == "Langstrasse"]
    warnings: list[str] = []
    axis = osm.merge_axis([osm.way_line(e, 2056) for e in ways], warnings)
    layers = osm.build_osm_layers(axis, area_data, 2056, axis_way_ids=[e["id"] for e in ways])
    return axis, layers, warnings


# ----------------------------------------------------------------------------- Zürich land cover
def test_zurich_landcover_classification():
    assert zurich.classify_landcover("Gebäude") == "building"
    assert zurich.classify_landcover("Strasse, Weg") == "carriageway"
    assert zurich.classify_landcover("Trottoir") == "sidewalk"
    for art in ("humusierte Fläche", "übrige humusierte", "Gartenanlage", "Acker, Wiese, Weide"):
        assert zurich.classify_landcover(art) == "green"
    assert zurich.classify_landcover("befestigte Fläche") == "paved"
    assert zurich.classify_landcover("Verkehrsinsel") == "paved"
    assert zurich.classify_landcover("Gewässer") is None
    assert zurich.classify_landcover(None) is None


def test_zurich_landcover_layers_from_fixture():
    fc = load("zurich_landcover_wfs.json")
    clip = box(2682085, 1248378, 2682583, 1248916)
    lc = zurich.parse_landcover(fc, clip)
    assert lc.feature_count == 50 and lc.has_street
    assert lc.carriageway.area > 5000 and lc.sidewalks.area > 3000
    assert lc.buildings.area > 5000 and lc.green.area > 1000 and lc.paved.area > 1000
    # roles are disjoint in the cadastre
    assert lc.carriageway.intersection(lc.sidewalks).area < 1.0
    assert lc.buildings.intersection(lc.carriageway).area < 1.0
    # paved yards are neither carriageway nor plantable
    plantable = lc.sidewalks.union(lc.green).difference(lc.buildings).difference(lc.carriageway)
    assert plantable.intersection(lc.paved).area < 1.0
    assert plantable.area == pytest.approx(lc.sidewalks.area + lc.green.area, rel=0.02)


# ----------------------------------------------------------------------------- OSM tag estimation
def test_carriageway_width_by_class_and_tags():
    ways = {e["id"]: e["tags"] for e in load("overpass_zurich_ways.json")["elements"]}
    assert osm.carriageway_width(ways[4533225]) == 6.5          # residential default
    assert osm.carriageway_width(ways[4743773]) == 5.0          # living_street
    assert osm.carriageway_width(ways[26675841]) == 4.0         # service
    assert osm.carriageway_width(ways[4533221]) == 0.0          # pedestrian -> no carriageway
    assert osm.carriageway_width(ways[15217437]) == 0.0         # path
    assert osm.carriageway_width({"highway": "primary"}) == 11.0
    assert osm.carriageway_width({"highway": "secondary_link"}) == 9.0
    assert osm.carriageway_width({"highway": "tertiary", "lanes": "3"}) == 9.0
    assert osm.carriageway_width({"highway": "residential", "width": "7.5 m", "lanes": "1"}) == 7.5
    assert osm.carriageway_width({"highway": "residential", "width": "6,5"}) == 6.5


def test_sidewalk_sides_from_tags():
    assert osm.sidewalk_sides({"highway": "residential", "sidewalk": "both"}) == (True, True)
    assert osm.sidewalk_sides({"highway": "residential", "sidewalk": "left"}) == (True, False)
    assert osm.sidewalk_sides({"highway": "residential", "sidewalk": "right"}) == (False, True)
    assert osm.sidewalk_sides({"highway": "living_street", "sidewalk": "no"}) == (False, False)
    assert osm.sidewalk_sides({"highway": "residential", "sidewalk:both": "separate"}) == (True, True)
    assert osm.sidewalk_sides({"highway": "residential", "sidewalk:left": "yes", "sidewalk:right": "no"}) == (True, False)
    assert osm.sidewalk_sides({"highway": "residential"}) == (True, True)     # default both
    assert osm.sidewalk_sides({"highway": "primary"}) == (True, True)
    assert osm.sidewalk_sides({"highway": "service"}) == (False, False)
    assert osm.sidewalk_sides({"highway": "pedestrian"}) == (True, True)


def test_cycle_lane_sides_from_tags():
    assert osm.cycle_lane_sides({"cycleway:left": "lane", "cycleway:right": "no"}) == (True, False)
    assert osm.cycle_lane_sides({"cycleway:both": "track"}) == (True, True)
    assert osm.cycle_lane_sides({"cycleway": "lane"}) == (True, True)
    assert osm.cycle_lane_sides({"cycleway": "opposite_lane"}) == (True, False)
    assert osm.cycle_lane_sides({"cycleway:both": "shared_lane"}) == (False, False)
    assert osm.cycle_lane_sides({"cycleway": "opposite"}) == (False, False)


def test_parking_sides_from_tags():
    assert osm.parking_side_kind({"parking:both": "no"}, "left") is None
    assert osm.parking_side_kind({"parking:both": "no"}, "right") is None
    assert osm.parking_side_width({"parking:left": "lane", "parking:right": "no"}, "left") == 2.0
    assert osm.parking_side_width({"parking:left": "lane", "parking:right": "no"}, "right") is None
    assert osm.parking_side_width({"parking:both": "street_side", "parking:both:orientation": "diagonal"},
                                  "left") == 4.8
    assert osm.parking_side_width({"parking:lane:right": "perpendicular"}, "right") == 5.0
    assert osm.parking_side_width({"parking:both": "lane", "parking:both:width": "2.4"}, "right") == 2.4
    assert osm.parking_side_kind({"parking:both": "separate"}, "left") is None


def test_osm_layers_from_area_fixture(langstrasse):
    axis, layers, warnings = langstrasse
    assert warnings == []
    assert 380 < axis.length < 400
    assert layers.half_width_m == pytest.approx(3.25)
    mid = axis.interpolate(0.5, normalized=True)
    assert layers.carriageway.contains(mid)
    assert layers.carriageway.area > 2500
    # side streets crossing the corridor add their own carriageway polygons
    side = next(e for e in load("overpass_langstrasse_area.json")["elements"]
                if e["type"] == "way" and e["tags"].get("name") == "Brauerstrasse")
    side_line = osm.way_line(side, 2056)
    inside = side_line.intersection(axis.buffer(20))
    assert not inside.is_empty and layers.carriageway.intersection(inside.buffer(1.0)).area > 10
    # sidewalks sit beside the carriageway, never on it or inside buildings
    assert layers.sidewalks.area > 1000
    assert layers.sidewalks.intersection(layers.carriageway).area < 1e-6
    assert layers.sidewalks.intersection(layers.buildings).area < 1e-6
    assert layers.plantable.area == pytest.approx(layers.sidewalks.area)
    assert layers.parking.is_empty
    assert layers.buildings.geom_type == "MultiPolygon" and len(layers.buildings.geoms) >= 5
    assert len(layers.junctions) >= 3
    for pt in layers.junctions:
        assert pt.distance(axis) < 0.6
    stations = [axis.project(p) for p in layers.junctions]
    assert stations == sorted(stations)


def test_cycleways_from_axis_tags_and_cycleway_ways():
    data = load("overpass_zurich_ways.json")
    brauer = [e for e in data["elements"] if e["type"] == "way" and e["tags"].get("name") == "Brauerstrasse"]
    warnings: list[str] = []
    axis = osm.merge_axis([osm.way_line(e, 2056) for e in brauer], warnings)
    layers = osm.build_osm_layers(axis, data, 2056, axis_way_ids=[e["id"] for e in brauer])
    assert not layers.cycleways.is_empty          # cycleway:right=lane / cycleway:left=lane
    assert layers.cycleways.length > 50
    assert layers.cycleways.distance(axis) == pytest.approx(3.25, abs=0.3)
    assert len(layers.junctions) >= 2
    # closed building ways parse into valid footprints (they lie outside this corridor's clip)
    footprints = [osm.way_polygon(e, 2056) for e in data["elements"] if "building" in e["tags"]]
    assert len(footprints) == 4 and all(f is not None and f.is_valid and f.area > 200 for f in footprints)
    assert layers.buildings.is_empty


def test_osm_parking_lane_is_removed_from_plantable(langstrasse):
    axis, layers, _ = langstrasse
    data = load("overpass_langstrasse_area.json")
    way_ids = [e["id"] for e in data["elements"]
               if e.get("type") == "way" and e.get("tags", {}).get("name") == "Langstrasse"]
    for el in data["elements"]:
        if el.get("id") in way_ids:
            el.setdefault("tags", {})["parking:both"] = "lane"
            el["tags"]["parking:both:orientation"] = "parallel"
    parked = osm.build_osm_layers(axis, data, 2056, axis_way_ids=way_ids)
    assert not parked.parking.is_empty
    assert parked.parking.area > 200
    assert parked.plantable.intersection(parked.parking).area < 1e-3
    assert parked.sidewalks.intersection(parked.parking).area < 1e-3
    assert parked.plantable.area < layers.plantable.area


def test_osm_parking_space_polygon_is_excluded():
    axis = LineString([(2682000.0, 1248000.0), (2682040.0, 1248000.0)])
    stall_wgs = crs.to_wgs(box(2682008.0, 1247994.0, 2682020.0, 1247998.0), 2056)
    ring = list(stall_wgs.exterior.coords)
    stall = {
        "type": "way", "id": 1, "tags": {"amenity": "parking_space"},
        "geometry": [{"lon": x, "lat": y} for x, y in ring],
    }
    layers = osm.build_osm_layers(axis, {"elements": [stall]}, 2056)
    assert not layers.parking.is_empty
    assert layers.parking.area > 10
    assert layers.plantable.intersection(layers.parking).area < 1e-3


def test_tree_nodes_measured_and_imputed(area_data):
    trees = osm.trees_from_elements([e for e in area_data["elements"] if e["type"] == "node"], 2056, EVERYWHERE)
    assert len(trees) == 66
    measured = [t for t in trees if not t.crown_imputed]
    assert len(measured) == 8 and all(t.crown_d_m and t.crown_d_m > 0 for t in measured)
    platanus = next(t for t in trees if t.species == "Platanus x hispanica")
    assert platanus.genus == "Platanus" and platanus.crown_imputed is False
    imputed = [t for t in trees if t.crown_imputed]
    assert imputed and all(t.crown_d_m and t.crown_d_m > 0 for t in imputed)
    dated = next(t for t in trees if t.planted_year)
    assert dated.planted_year >= 2000
    assert all(t.id.startswith("osm-") and t.source == "osm" for t in trees)


def test_tree_nodes_without_coordinates_are_skipped():
    tags_only = load("overpass_zurich_trees.json")["elements"]
    assert osm.trees_from_elements(tags_only, 2056, EVERYWHERE) == []


def test_impute_crown_fallback_never_raises():
    assert osm.impute_crown_d(None, None, None) > 0
    assert osm.impute_crown_d("Tilia", "Tilia cordata", 20) > 0
    assert osm.genus_from_species("Platanus x hispanica") == "Platanus"
    assert osm.genus_from_species(None) is None


# ----------------------------------------------------------------------------- Nominatim & axis
def test_nominatim_pick_prefers_highway_ways():
    results = load("nominatim_langstrasse.json")
    hit = osm.pick_nominatim_street(results, "Langstrasse")
    assert hit is not None and hit["osm_type"] == "way" and hit["osm_id"] == 83115253
    assert osm.pick_nominatim_street([results[0]], "Langstrasse") is None      # relation only
    emergency = dict(results[1], category="emergency")
    assert osm.pick_nominatim_street([results[0], emergency], "Langstrasse") is None
    assert osm.nominatim_bbox(results[0]) == (8.4976185, 47.3688505, 8.5384124, 47.3913741)


def test_merge_axis_picks_longest_part_and_caps_length():
    warnings: list[str] = []
    long = LineString([(0, 0), (1000, 0)])
    short = LineString([(0, 500), (200, 500)])
    axis = osm.merge_axis([short, long], warnings)
    assert axis.length == pytest.approx(1000) and len(warnings) == 1 and "2 disconnected parts" in warnings[0]
    warnings.clear()
    anchored = osm.merge_axis([short, long], warnings, anchor=Point(100, 500))
    assert anchored.length == pytest.approx(200)
    warnings.clear()
    chained = osm.merge_axis([LineString([(0, 0), (10, 0)]), LineString([(10, 0), (20, 0)])], warnings)
    assert chained.length == pytest.approx(20) and warnings == []
    huge = osm.merge_axis([LineString([(0, 0), (4000, 0)])], warnings)
    assert huge.length == pytest.approx(2500)
    assert huge.bounds == pytest.approx((750, 0, 3250, 0)) and "middle 2500 m" in warnings[0]
    with pytest.raises(StreetNotFound):
        osm.merge_axis([], warnings)


def test_overpass_queries_and_bboxes():
    q = osm.overpass_name_query('Rue "A" B', (8.5, 47.3, 8.6, 47.4))
    assert '["name"="Rue \\"A\\" B"]' in q and "(47.300000,8.500000,47.400000,8.600000)" in q
    axis = osm.crs.to_metric(LineString([(8.52, 47.37), (8.53, 47.385)]), 2056)
    boxes = osm.corridor_bboxes(axis, 2056)
    assert len(boxes) == 4                       # ~1.9 km -> four 600 m chunks
    for minlon, minlat, maxlon, maxlat in boxes:
        assert 8.51 < minlon < maxlon < 8.54 and 47.36 < minlat < maxlat < 47.39
    area_q = osm.overpass_area_query(boxes)
    assert area_q.count('way["highway"]') == 4 and area_q.count('node["natural"="tree"]') == 4
    assert area_q.endswith("out tags geom;")


def test_cache_ttl(monkeypatch):
    osm.cache_clear()
    monkeypatch.setattr(osm, "DISK_CACHE_DIR", "")  # memory tier only: the disk tier has its own 7-day TTL
    key = osm.cache_key("https://example.test", {"b": 1, "a": 2})
    assert key == osm.cache_key("https://example.test", {"a": 2, "b": 1})
    osm.cache_set(key, {"x": 1})
    assert osm.cache_get(key) == {"x": 1}
    real_monotonic = time.monotonic
    monkeypatch.setattr(osm.time, "monotonic", lambda: real_monotonic() + osm.CACHE_TTL_S + 1)
    assert osm.cache_get(key) is None


# ----------------------------------------------------------------------------- serialization
def test_serialize_street_output_shape(langstrasse):
    axis, layers, warnings = langstrasse
    ctx = osm.context_from_osm_layers(zurich.ZURICH_INFO, 2056, "Langstrasse", axis, layers, warnings)
    resp = serialize_street(ctx)
    assert isinstance(resp, StreetResponse)
    round_trip = StreetResponse.model_validate_json(resp.model_dump_json())
    assert round_trip.street_id == ctx.street_id and round_trip.city == "zurich"
    assert set(resp.layers) == {"axis", "carriageway", "sidewalks", "plantable", "buildings", "cycleways",
                                "parking", "junctions", "existing_trees", "corridor"}
    assert resp.layers["axis"].features[0]["properties"] == {"name": "Langstrasse", "length_m": resp.length_m}
    assert resp.layers["carriageway"].features[0]["properties"] == {"layer": "carriageway"}
    assert len(resp.layers["buildings"].features) == resp.stats.buildings
    assert len(resp.layers["junctions"].features) == resp.stats.junctions
    assert resp.layers["junctions"].features[0]["properties"] == {"id": "J01"}
    tree = resp.layers["existing_trees"].features[0]["properties"]
    assert set(tree) >= {"id", "species", "genus", "crown_d_m", "crown_imputed", "height_m", "planted_year", "source"}
    assert tree["crown_r"] == pytest.approx(tree["crown_d_m"] / 2)
    lon, lat = resp.layers["existing_trees"].features[0]["geometry"]["coordinates"]
    assert 8.52 < lon < 8.53 and 47.37 < lat < 47.38
    assert resp.bbox[0] < resp.center[0] < resp.bbox[2] and resp.bbox[1] < resp.center[1] < resp.bbox[3]
    assert resp.layer_basis["carriageway"] == "estimated" and resp.layer_basis["buildings"] == "measured"
    assert set(resp.layer_basis) == set(resp.layers)
    assert resp.stats.length_m == pytest.approx(axis.length, abs=0.1)
    assert resp.stats.carriageway_m2 > 0 and resp.stats.sidewalk_m2 > 0 and resp.stats.plantable_m2 > 0
    assert resp.stats.existing_trees == len(ctx.existing_trees)
    coords = resp.layers["axis"].features[0]["geometry"]["coordinates"][0]
    assert all(round(c, 7) == c for c in coords)


# ----------------------------------------------------------------------------- registry
def test_registry_and_city_infos():
    assert list(ADAPTERS) == ["zurich", "demo"]
    for city_id, adapter in ADAPTERS.items():
        assert isinstance(adapter, CityAdapter)
        assert isinstance(adapter.info, CityInfo) and adapter.info.id == city_id
        assert sum(1 for b in adapter.info.basemaps if b.default) == 1
        assert adapter.info.utc_offset_hours == 2.0
    assert get_adapter("zurich").info.epsg == 2056
    assert zurich.ZURICH_INFO.demo_streets[0] == "Langstrasse"
    with pytest.raises(KeyError):
        get_adapter("atlantis")


async def test_demo_adapter_offline():
    adapter = get_adapter("demo")
    assert isinstance(adapter, DemoAdapter) and adapter.info.name == "Demo street (offline)"
    drawn = await adapter.street_from_line([(8.53, 47.38), (8.535, 47.38)], name="Sketch")
    assert isinstance(drawn, StreetContext) and drawn.name == "Sketch" and drawn.epsg == 2056
    assert 370 < drawn.length_m < 390 and drawn.carriageway.contains(drawn.axis.interpolate(0.5, normalized=True))
    assert not drawn.sidewalks.is_empty and drawn.basis["sidewalks"] == "estimated"
    resp = serialize_street(drawn)
    assert resp.city == "demo" and resp.stats.carriageway_m2 > 0
    pytest.importorskip("server.engine.synthetic")
    ctx = await adapter.find_street("Demo street")
    assert isinstance(ctx, StreetContext) and ctx.city.id == "demo"
    assert 390 < ctx.length_m < 410 and len(ctx.existing_trees) >= 1
    serialize_street(ctx)


# ----------------------------------------------------------------------------- network
@network
async def test_network_zurich_langstrasse():
    ctx = await get_adapter("zurich").find_street("Langstrasse")
    resp = serialize_street(ctx)
    print(f"\nzurich Langstrasse: length={resp.length_m} trees={resp.stats.existing_trees} "
          f"imputed={resp.stats.existing_trees_crown_imputed} buildings={resp.stats.buildings} "
          f"junctions={resp.stats.junctions} warnings={resp.warnings}")
    assert 1000 < resp.length_m <= 2500
    assert resp.layer_basis["carriageway"] == "measured" and resp.layer_basis["sidewalks"] == "measured"
    assert resp.stats.buildings > 20 and resp.stats.carriageway_m2 > 5000 and resp.stats.plantable_m2 > 1000
    assert resp.stats.junctions >= 5
    assert resp.stats.existing_trees > 0
    assert zurich.TREE_FALLBACK_WARNING in resp.warnings or resp.layer_basis["existing_trees"] == "measured"
    assert resp.layer_sources["carriageway"] == zurich.CADASTRE_ATTRIBUTION


def test_mapped_foot_crossing_is_a_junction_but_overpass_is_not():
    axis = LineString([(0, 0), (100, 0)])
    road = ({"id": 1, "tags": {"highway": "residential"}}, axis)
    crossing = ({"id": 2, "tags": {"highway": "footway", "footway": "crossing"}},
                LineString([(30, -10), (30, 10)]))
    bridge = ({"id": 3, "tags": {"highway": "primary", "layer": "1", "bridge": "yes"}},
              LineString([(70, -10), (70, 10)]))
    found = osm.find_junctions(axis, [road, crossing, bridge], {1}, EVERYWHERE)
    assert len(found) == 1 and found[0].equals(Point(30, 0))
