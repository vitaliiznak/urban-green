"""Zürich adapter: canton cadastre land-cover polygons (measured carriageway,
sidewalks, buildings, green verges) plus the city tree cadastre WFS, which is
tried with a 6 s timeout and replaced by OpenStreetMap trees when it fails.
Junctions, cycle paths and the street axis always come from OpenStreetMap."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from typing import Any

from shapely import make_valid
from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep

from .. import crs
from ..schemas import Basemap, Basis, CityInfo
from .base import CityAdapter, ExistingTree, StreetContext, UpstreamUnavailable
from .osm import (CLIP_MARGIN_M, CORRIDOR_HALF_WIDTH_M, OSM_ATTRIBUTION, SIDEWALK_WIDTH_M, BBox,
                  OsmLayers, assemble_context, build_osm_layers, drawn_axis, fetch_area,
                  fetch_json, genus_from_species, impute_crown_d, parse_length, parse_year,
                  resolve_street_axis, union_polygons)

EPSG = 2056
ZURICH_BBOX: BBox = (8.44, 47.32, 8.63, 47.43)

CADASTRE_URL = "https://maps.zh.ch/wfs/OGDZHWFS"
CADASTRE_TYPENAME = "ms:ogd-0401_arv_basis_avzh_bodenbedeckung_f"
CADASTRE_COUNT = 5000
CADASTRE_ATTRIBUTION = "Amtliche Vermessung, Kanton Zürich (OGD)"

TREES_URL = "https://www.ogd.stadt-zuerich.ch/wfs/geoportal/Baumkataster"
TREES_TYPENAME = "baumkataster_baumstandorte"
TREES_TIMEOUT_S = 6.0
TREES_ATTRIBUTION = "Baumkataster Stadt Zürich (OGD)"
TREE_FALLBACK_WARNING = ("City tree cadastre unavailable, showing OpenStreetMap trees "
                         "(crown diameters imputed)")

# Cadastre `art` (land-cover class) -> Allee layer role.
LANDCOVER_CLASSES: dict[str, str] = {
    "Gebäude": "building",
    "Strasse, Weg": "carriageway",
    "Trottoir": "sidewalk",
    "humusierte Fläche": "green", "übrige humusierte": "green", "Gartenanlage": "green",
    "Acker, Wiese, Weide": "green", "Reben": "green", "übrige Intensivkultur": "green",
    "Hoch-, Flachmoor": "green",
    "befestigte Fläche": "paved", "übrige befestigte": "paved", "Verkehrsinsel": "paved",
}

ZURICH_INFO = CityInfo(
    id="zurich", name="Zürich", country="CH", epsg=EPSG, center=[8.5285, 47.3772], zoom=16,
    utc_offset_hours=2.0,
    basemaps=[
        Basemap(id="aerial", label="Aerial (swisstopo)",
                tiles=["https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissimage/default/current/3857/{z}/{x}/{y}.jpeg"],
                attribution="© swisstopo", max_zoom=20, default=True),
        Basemap(id="grey", label="Grey map (swisstopo)",
                tiles=["https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-grau/default/current/3857/{z}/{x}/{y}.jpeg"],
                attribution="© swisstopo", max_zoom=18),
    ],
    demo_streets=["Langstrasse", "Josefstrasse", "Hohlstrasse", "Badenerstrasse", "Weststrasse"],
    tree_source=f"{TREES_ATTRIBUTION}; fallback {OSM_ATTRIBUTION}",
    geometry_source=CADASTRE_ATTRIBUTION)


# ----------------------------------------------------------------------------- land cover
def classify_landcover(art: str | None) -> str | None:
    """Cadastre ``art`` value -> building | carriageway | sidewalk | green | paved | None."""
    if not art:
        return None
    role = LANDCOVER_CLASSES.get(art)
    if role is not None:
        return role
    low = art.lower()
    if "humusiert" in low or "wiese" in low or "garten" in low:
        return "green"
    if "gebäude" in low:
        return "building"
    if "trottoir" in low:
        return "sidewalk"
    if "strasse" in low:
        return "carriageway"
    if "befestigt" in low:
        return "paved"
    return None


@dataclass
class Landcover:
    """Cadastre polygons grouped by role, unioned and clipped (EPSG:2056)."""
    carriageway: BaseGeometry
    sidewalks: BaseGeometry
    buildings: BaseGeometry
    green: BaseGeometry
    paved: BaseGeometry
    feature_count: int

    @property
    def has_street(self) -> bool:
        return not (self.carriageway.is_empty and self.sidewalks.is_empty)


def parse_landcover(fc: dict[str, Any], clip: BaseGeometry) -> Landcover:
    """Cadastre GeoJSON FeatureCollection -> Landcover layers inside ``clip``."""
    groups: dict[str, list[BaseGeometry]] = {k: [] for k in ("carriageway", "sidewalk", "building", "green", "paved")}
    prepared = prep(clip)
    features = fc.get("features") or []
    for f in features:
        role = classify_landcover((f.get("properties") or {}).get("art"))
        if role is None or not f.get("geometry"):
            continue
        geom = make_valid(shape(f["geometry"]))
        if geom.is_empty or not prepared.intersects(geom):
            continue
        groups[role].append(geom)
    return Landcover(
        carriageway=union_polygons(groups["carriageway"], clip),
        sidewalks=union_polygons(groups["sidewalk"], clip),
        buildings=union_polygons(groups["building"], clip),
        green=union_polygons(groups["green"], clip),
        paved=union_polygons(groups["paved"], clip),
        feature_count=len(features))


async def fetch_landcover(bounds: tuple[float, float, float, float]) -> dict[str, Any]:
    """Cadastre land-cover polygons inside metric ``bounds`` (minx, miny, maxx, maxy)."""
    minx, miny, maxx, maxy = bounds
    params = {
        "service": "WFS", "version": "2.0.0", "request": "GetFeature",
        "typename": CADASTRE_TYPENAME, "outputFormat": "geojson", "srsname": f"EPSG:{EPSG}",
        "bbox": f"{minx:.1f},{miny:.1f},{maxx:.1f},{maxy:.1f},EPSG:{EPSG}",
        "count": CADASTRE_COUNT,
    }
    return await fetch_json(CADASTRE_URL, params=params)


# ----------------------------------------------------------------------------- city trees
def tree_from_city_feature(f: dict[str, Any], index: int, clip: BaseGeometry) -> ExistingTree | None:
    """One Baumkataster feature -> ExistingTree (None when outside the clip)."""
    geom = f.get("geometry") or {}
    if geom.get("type") != "Point":
        return None
    x, y = geom["coordinates"][:2]
    pt = Point(x, y) if abs(x) > 180 else crs.to_metric(Point(x, y), EPSG)
    if not clip.contains(pt):
        return None
    p = f.get("properties") or {}
    species = p.get("baumnamelat") or None
    genus = p.get("baumgattunglat") or genus_from_species(species)
    crown = parse_length(p.get("kronendurchmesser"))
    if crown is not None and crown <= 0:
        crown = None
    planted = parse_year(p.get("pflanzjahr"))
    age = date.today().year - planted if planted else None
    imputed = crown is None
    return ExistingTree(
        id=str(f.get("id") or p.get("baumnummer") or f"zh-{index}"), pt=pt, species=species,
        genus=genus, crown_d_m=impute_crown_d(genus, species, age) if imputed else crown,
        crown_imputed=imputed, height_m=parse_length(p.get("baumhoehe")), planted_year=planted,
        source="cadastre")


def parse_city_trees(fc: dict[str, Any], clip: BaseGeometry) -> list[ExistingTree]:
    """Baumkataster FeatureCollection -> trees inside ``clip``."""
    trees: list[ExistingTree] = []
    for i, f in enumerate(fc.get("features") or []):
        tree = tree_from_city_feature(f, i, clip)
        if tree is not None:
            trees.append(tree)
    return trees


async def fetch_city_trees(bounds: tuple[float, float, float, float]) -> dict[str, Any]:
    """City tree cadastre inside metric ``bounds``; 6 s timeout, raises UpstreamUnavailable."""
    minx, miny, maxx, maxy = bounds
    params = {
        "SERVICE": "WFS", "VERSION": "1.1.0", "REQUEST": "GetFeature", "TYPENAME": TREES_TYPENAME,
        "OUTPUTFORMAT": "GeoJSON", "SRSNAME": f"EPSG:{EPSG}",
        "BBOX": f"{minx:.1f},{miny:.1f},{maxx:.1f},{maxy:.1f},EPSG:{EPSG}",
    }
    payload = await fetch_json(TREES_URL, params=params, timeout=TREES_TIMEOUT_S)
    if not isinstance(payload, dict) or "features" not in payload:
        raise UpstreamUnavailable("Baumkataster returned no FeatureCollection")
    return payload


# ----------------------------------------------------------------------------- adapter
class ZurichAdapter(CityAdapter):
    """Zürich: measured cadastre geometry, city trees with OSM fallback."""

    info = ZURICH_INFO

    async def find_street(self, query: str) -> StreetContext:
        """Resolve a Zürich street name and build its context."""
        resolved = await resolve_street_axis(query, city_bbox=ZURICH_BBOX, epsg=EPSG,
                                             countrycodes="ch", city_label="Zürich")
        return await self._build(resolved.axis, resolved.name, resolved.way_ids, resolved.warnings)

    async def street_from_line(self, coords_wgs: list[tuple[float, float]], name: str | None = None) -> StreetContext:
        """Context around a user-drawn axis inside Zürich."""
        warnings: list[str] = []
        axis = drawn_axis(coords_wgs, EPSG, warnings)
        return await self._build(axis, name or "Drawn street", [], warnings)

    async def _build(self, axis: LineString, name: str, way_ids: list[int], warnings: list[str]) -> StreetContext:
        corridor = axis.buffer(CORRIDOR_HALF_WIDTH_M, cap_style="flat")
        clip = corridor.buffer(CLIP_MARGIN_M)
        osm_result, cadastre_result, trees_result = await asyncio.gather(
            fetch_area(axis, EPSG), fetch_landcover(clip.bounds), fetch_city_trees(clip.bounds),
            return_exceptions=True)

        osm_layers = self._osm_layers(osm_result, axis, corridor, way_ids, warnings)
        landcover = self._landcover(cadastre_result, clip, warnings)
        if osm_layers is None and landcover is None:
            raise UpstreamUnavailable("Neither the Zürich cadastre nor OpenStreetMap could be reached")

        basis: dict[str, Basis] = {}
        sources: dict[str, str] = {}
        if landcover is not None:
            carriageway, sidewalks, plantable, buildings = self._measured_layers(
                landcover, axis, clip, osm_layers, basis, sources, warnings)
        else:
            assert osm_layers is not None
            carriageway, sidewalks, plantable, buildings = (
                osm_layers.carriageway, osm_layers.sidewalks, osm_layers.plantable, osm_layers.buildings)
            basis.update({"carriageway": "estimated", "sidewalks": "estimated",
                          "plantable": "estimated", "buildings": "measured"})
            sources.update({k: OSM_ATTRIBUTION for k in ("carriageway", "sidewalks", "plantable", "buildings")})

        if osm_layers is not None:
            cycleways, junctions = osm_layers.cycleways, osm_layers.junctions
            basis.update({"cycleways": "estimated", "junctions": "measured"})
            sources.update({"cycleways": OSM_ATTRIBUTION, "junctions": OSM_ATTRIBUTION})
        else:
            cycleways, junctions = MultiLineString(), []
            basis.update({"cycleways": "unknown", "junctions": "unknown"})

        trees = self._trees(trees_result, clip, osm_layers, basis, sources, warnings)
        return assemble_context(
            city=self.info, epsg=EPSG, name=name, axis=axis, carriageway=carriageway,
            sidewalks=sidewalks, plantable=plantable, buildings=buildings, cycleways=cycleways,
            junctions=junctions, trees=trees, basis=basis, sources=sources, warnings=warnings,
            way_ids=osm_layers.axis_way_ids if osm_layers is not None else way_ids)

    @staticmethod
    def _osm_layers(result: Any, axis: LineString, corridor: BaseGeometry, way_ids: list[int],
                    warnings: list[str]) -> OsmLayers | None:
        if isinstance(result, BaseException):
            warnings.append(f"OpenStreetMap unavailable ({result}); junctions and cycle paths are missing")
            return None
        layers = build_osm_layers(axis, result, EPSG, axis_way_ids=way_ids, corridor=corridor)
        warnings.extend(layers.warnings)
        return layers

    @staticmethod
    def _landcover(result: Any, clip: BaseGeometry, warnings: list[str]) -> Landcover | None:
        if isinstance(result, BaseException):
            warnings.append(f"Cadastre unavailable ({result}); street geometry estimated from OpenStreetMap")
            return None
        landcover = parse_landcover(result, clip)
        if not landcover.has_street:
            warnings.append("Cadastre has no street polygons here; street geometry estimated from OpenStreetMap")
            return None
        if landcover.feature_count >= CADASTRE_COUNT:
            warnings.append(f"Cadastre response truncated at {CADASTRE_COUNT} polygons")
        return landcover

    @staticmethod
    def _measured_layers(landcover: Landcover, axis: LineString, clip: BaseGeometry,
                         osm_layers: OsmLayers | None, basis: dict[str, Basis], sources: dict[str, str],
                         warnings: list[str]) -> tuple[BaseGeometry, BaseGeometry, BaseGeometry, BaseGeometry]:
        carriageway, buildings = landcover.carriageway, landcover.buildings
        basis.update({"carriageway": "measured", "buildings": "measured"})
        sources.update({"carriageway": CADASTRE_ATTRIBUTION, "buildings": CADASTRE_ATTRIBUTION,
                        "sidewalks": CADASTRE_ATTRIBUTION, "plantable": CADASTRE_ATTRIBUTION})
        sidewalks = landcover.sidewalks
        if sidewalks.is_empty:
            half_width = osm_layers.half_width_m if osm_layers is not None else 3.25
            band = axis.buffer(half_width + SIDEWALK_WIDTH_M, cap_style="flat")
            sidewalks = union_polygons([band], clip).difference(carriageway).difference(buildings)
            sidewalks = make_valid(sidewalks)
            basis.update({"sidewalks": "estimated", "plantable": "estimated"})
            sources["sidewalks"] = OSM_ATTRIBUTION
            warnings.append("Cadastre has no sidewalk polygons here; sidewalks estimated "
                            f"{SIDEWALK_WIDTH_M:g} m beside the carriageway")
        else:
            basis.update({"sidewalks": "measured", "plantable": "measured"})
        plantable = union_polygons([sidewalks, landcover.green], clip).difference(buildings).difference(carriageway)
        return carriageway, sidewalks, make_valid(plantable), buildings

    @staticmethod
    def _trees(result: Any, clip: BaseGeometry, osm_layers: OsmLayers | None,
               basis: dict[str, Basis], sources: dict[str, str], warnings: list[str]) -> list[ExistingTree]:
        if not isinstance(result, BaseException):
            basis["existing_trees"] = "measured"
            sources["existing_trees"] = TREES_ATTRIBUTION
            return parse_city_trees(result, clip)
        warnings.append(TREE_FALLBACK_WARNING)
        sources["existing_trees"] = OSM_ATTRIBUTION
        if osm_layers is None:
            basis["existing_trees"] = "unknown"
            return []
        basis["existing_trees"] = "estimated"
        return osm_layers.trees
