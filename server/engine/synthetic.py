"""Deterministic synthetic street for tests and the offline "demo" city.

A straight east-west street in EPSG:2056 with a carriageway, sidewalks on
both sides, building fronts, a cycle path on the south side, three existing
limes on the north side, a side street crossing at station 320 and junction
points at both ends. Every feature is placed so that specific rules fail in
known places (see the constants below).
"""
from __future__ import annotations

from typing import Optional

import shapely
from shapely.geometry import LineString, Point, box
from shapely.geometry.base import BaseGeometry

from ..adapters.base import ExistingTree, StreetContext
from ..schemas import Basemap, CityInfo

ORIGIN = (2682000.0, 1248000.0)
CORRIDOR_HALF_M = 15.0
FETCH_MARGIN_M = 5.0

SIDE_STREET_STATION_M = 320.0
SIDE_STREET_WIDTH_M = 6.0
SIDE_STREET_REACH_M = 30.0

BUILDING_SETBACK_M = 5.0            # normal fronts: this far behind the sidewalk edge
BUILDING_DEPTH_M = 12.0
BUILDING_GAP_NORTH = (150.0, 190.0)  # no building here (north side)
PUSHED_BUILDING_NORTH = (260.0, 300.0)
PUSHED_SETBACK_M = 0.8              # front only 0.8 m behind the sidewalk -> building_crown fails
BUILDING_MARGIN_AT_SIDE_STREET_M = 0.5

CYCLEWAY_OFFSET_M = 0.5             # outside the carriageway edge, south side
CYCLEWAY_SPAN = (0.0, 200.0)

EXISTING_TREE_STATIONS = (100.0, 108.0, 116.0)
EXISTING_TREE_CROWN_D_M = 9.0
EXISTING_TREE_HEIGHT_M = 12.0
EXISTING_TREE_PLANTED_YEAR = 1998
EXISTING_TREE_OFFSET_FROM_EDGE_M = 1.0

PIXELKARTE_GRAU_URL = ("https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-grau/default/current/"
                       "3857/{z}/{x}/{y}.jpeg")

LAYER_KEYS = ("axis", "carriageway", "sidewalks", "plantable", "buildings", "cycleways",
              "junctions", "existing_trees", "corridor")


def demo_city_info() -> CityInfo:
    """CityInfo of the offline demo city (Zürich CRS, grey swisstopo basemap)."""
    return CityInfo(
        id="demo",
        name="Demo street (offline)",
        country="CH",
        epsg=2056,
        center=[8.53, 47.38],
        zoom=16.5,
        utc_offset_hours=2.0,
        basemaps=[Basemap(id="swisstopo-grey", label="swisstopo grey map", tiles=[PIXELKARTE_GRAU_URL],
                          tile_size=256, attribution="© swisstopo", max_zoom=19, default=True)],
        demo_streets=["Demo street"],
        tree_source="synthetic",
        geometry_source="synthetic",
    )


def _span(x0: float, a: float, b: float, length_m: float) -> Optional[tuple[float, float]]:
    """Absolute x-range of stations [a, b] clamped to the street, None when outside."""
    lo, hi = max(a, 0.0), min(b, length_m)
    if hi <= lo:
        return None
    return x0 + lo, x0 + hi


def _boxes(x0: float, y0: float, length_m: float, blocks: list[tuple[float, float, float, float]]) -> list[BaseGeometry]:
    """Rectangles from (station_from, station_to, y_from, y_to) relative to the axis."""
    out: list[BaseGeometry] = []
    for a, b, y_from, y_to in blocks:
        span = _span(x0, a, b, length_m)
        if span is not None:
            out.append(box(span[0], y0 + y_from, span[1], y0 + y_to))
    return out


def _union(geoms: list[BaseGeometry]) -> BaseGeometry:
    return shapely.union_all(geoms) if geoms else shapely.Polygon()


def synthetic_street(length_m: float = 400, road_w: float = 7.0, sidewalk_w: float = 3.5,
                     name: str = "Demo street") -> StreetContext:
    """Build the deterministic demo street (EPSG:2056, axis eastwards from ORIGIN)."""
    x0, y0 = ORIGIN
    half = road_w / 2.0
    outer = half + sidewalk_w
    axis = LineString([(x0, y0), (x0 + length_m, y0)])
    corridor = axis.buffer(CORRIDOR_HALF_M, cap_style="flat")
    fetch_area = corridor.buffer(FETCH_MARGIN_M)

    side_half = SIDE_STREET_WIDTH_M / 2.0
    side_from, side_to = SIDE_STREET_STATION_M - side_half, SIDE_STREET_STATION_M + side_half
    carriageway = _union(_boxes(x0, y0, length_m, [
        (0.0, length_m, -half, half),
        (side_from, side_to, -SIDE_STREET_REACH_M, SIDE_STREET_REACH_M),
    ]))

    sidewalk_boxes = _boxes(x0, y0, length_m, [
        (0.0, length_m, half, outer),
        (0.0, length_m, -outer, -half),
        (side_from - sidewalk_w, side_from, -SIDE_STREET_REACH_M, SIDE_STREET_REACH_M),
        (side_to, side_to + sidewalk_w, -SIDE_STREET_REACH_M, SIDE_STREET_REACH_M),
    ])
    sidewalks = _union(sidewalk_boxes).difference(carriageway)

    front = outer + BUILDING_SETBACK_M
    back = front + BUILDING_DEPTH_M
    gap_from = side_from - sidewalk_w - BUILDING_MARGIN_AT_SIDE_STREET_M
    gap_to = side_to + sidewalk_w + BUILDING_MARGIN_AT_SIDE_STREET_M
    pushed_front = outer + PUSHED_SETBACK_M
    buildings = _union(_boxes(x0, y0, length_m, [
        (0.0, BUILDING_GAP_NORTH[0], front, back),
        (BUILDING_GAP_NORTH[1], PUSHED_BUILDING_NORTH[0], front, back),
        (PUSHED_BUILDING_NORTH[0], PUSHED_BUILDING_NORTH[1], pushed_front, back),
        (PUSHED_BUILDING_NORTH[1], gap_from, front, back),
        (gap_to, length_m, front, back),
        (0.0, gap_from, -back, -front),
        (gap_to, length_m, -back, -front),
    ]))

    cycle_span = _span(x0, CYCLEWAY_SPAN[0], CYCLEWAY_SPAN[1], length_m)
    cycle_y = y0 - half - CYCLEWAY_OFFSET_M
    cycleways: BaseGeometry = (LineString([(cycle_span[0], cycle_y), (cycle_span[1], cycle_y)])
                               if cycle_span else LineString())

    carriageway = carriageway.intersection(fetch_area)
    sidewalks = sidewalks.intersection(fetch_area)
    buildings = buildings.intersection(fetch_area)
    plantable = sidewalks.difference(buildings).difference(carriageway)

    junction_stations = [0.0, length_m] + ([SIDE_STREET_STATION_M] if SIDE_STREET_STATION_M < length_m else [])
    junctions = [Point(x0 + s, y0) for s in junction_stations]

    tree_y = y0 + half + EXISTING_TREE_OFFSET_FROM_EDGE_M
    existing_trees = [
        ExistingTree(id=f"demo-tree-{i + 1}", pt=Point(x0 + s, tree_y), species="Tilia cordata", genus="Tilia",
                     crown_d_m=EXISTING_TREE_CROWN_D_M, crown_imputed=False, height_m=EXISTING_TREE_HEIGHT_M,
                     planted_year=EXISTING_TREE_PLANTED_YEAR, source="synthetic")
        for i, s in enumerate(EXISTING_TREE_STATIONS) if s < length_m
    ]

    slug = "-".join(name.strip().lower().split()) or "street"
    return StreetContext(
        street_id=f"demo:{slug}",
        city=demo_city_info(),
        name=name,
        epsg=2056,
        axis=axis,
        carriageway=carriageway,
        sidewalks=sidewalks,
        plantable=plantable,
        buildings=buildings,
        cycleways=cycleways,
        junctions=junctions,
        existing_trees=existing_trees,
        corridor=corridor,
        basis={key: "measured" for key in LAYER_KEYS},
        sources={key: "synthetic" for key in LAYER_KEYS},
        warnings=[],
        osm_way_ids=[],
    )
