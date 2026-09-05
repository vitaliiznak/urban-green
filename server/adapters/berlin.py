"""Berlin adapter: street geometry estimated from OpenStreetMap, existing trees
from the Berlin tree cadastre WFS (street trees + park trees)."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry

from .. import crs
from ..schemas import Basemap, CityInfo
from .base import CityAdapter, ExistingTree, StreetContext
from .osm import (CLIP_MARGIN_M, OSM_ATTRIBUTION, BBox, drawn_axis, fetch_json, genus_from_species,
                  impute_crown_d, osm_street_context, parse_int, parse_length, parse_year,
                  resolve_street_axis)

EPSG = 25833
BERLIN_BBOX: BBox = (13.08, 52.33, 13.77, 52.68)

WFS_URL = "https://gdi.berlin.de/services/wfs/baumbestand"
TREE_LAYERS: tuple[tuple[str, str], ...] = (
    ("baumbestand:strassenbaeume", "street"),
    ("baumbestand:anlagenbaeume", "park"),
)
TREE_COUNT = 5000
TREES_ATTRIBUTION = "Baumbestand Berlin, Geoportal Berlin / SenMVKU (dl-de/by-2-0)"
TREE_FALLBACK_WARNING = ("Berlin tree cadastre unavailable, showing OpenStreetMap trees "
                         "(crown diameters imputed)")

BERLIN_INFO = CityInfo(
    id="berlin", name="Berlin", country="DE", epsg=EPSG, center=[13.4700, 52.5150], zoom=15.5,
    utc_offset_hours=2.0,
    basemaps=[
        Basemap(id="aerial", label="Aerial (DOP 2024)",
                tiles=["https://gdi.berlin.de/services/wms/truedop_2024?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap"
                       "&LAYERS=truedop_2024&STYLES=&CRS=EPSG:3857&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256"
                       "&FORMAT=image/png"],
                attribution="© Geoportal Berlin / DOP 2024", max_zoom=20, default=True),
        Basemap(id="light", label="CARTO light",
                tiles=["https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"],
                attribution="© OpenStreetMap contributors © CARTO", max_zoom=19),
    ],
    demo_streets=["Rigaer Straße", "Frankfurter Allee", "Bergmannstraße", "Karl-Marx-Allee", "Boxhagener Straße"],
    tree_source=TREES_ATTRIBUTION, geometry_source=OSM_ATTRIBUTION)


# ----------------------------------------------------------------------------- tree cadastre
def tree_from_feature(f: dict[str, Any], epsg: int, source: str) -> ExistingTree | None:
    """One WFS feature (coordinates [lon, lat]) -> ExistingTree in ``epsg``."""
    geom = f.get("geometry") or {}
    if geom.get("type") != "Point":
        return None
    lon, lat = geom["coordinates"][:2]
    p = f.get("properties") or {}
    species = p.get("art_bot") or None
    genus = p.get("gattung") or genus_from_species(species)
    crown = parse_length(p.get("kronedurch"))
    if crown is not None and crown <= 0:
        crown = None
    planted = parse_year(p.get("pflanzjahr"))
    age = parse_int(p.get("standalter"))
    if age is None and planted is not None:
        age = date.today().year - planted
    imputed = crown is None
    return ExistingTree(
        id=str(p.get("gisid") or f.get("id") or f"berlin-{lon:.6f}-{lat:.6f}"),
        pt=crs.to_metric(Point(float(lon), float(lat)), epsg), species=species, genus=genus,
        crown_d_m=impute_crown_d(genus, species, age) if imputed else crown, crown_imputed=imputed,
        height_m=parse_length(p.get("baumhoehe")), planted_year=planted, source=source)


def parse_trees(fc: dict[str, Any], epsg: int, clip: BaseGeometry, source: str) -> list[ExistingTree]:
    """Tree cadastre FeatureCollection -> trees inside ``clip``."""
    trees: list[ExistingTree] = []
    for f in fc.get("features") or []:
        tree = tree_from_feature(f, epsg, source)
        if tree is not None and clip.contains(tree.pt):
            trees.append(tree)
    return trees


async def fetch_trees(typename: str, bbox_wgs: BBox, count: int = TREE_COUNT) -> dict[str, Any]:
    """GetFeature for one tree layer; the bbox is lat/lon ordered as this server expects."""
    minlon, minlat, maxlon, maxlat = bbox_wgs
    params = {
        "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": typename,
        "OUTPUTFORMAT": "application/json", "SRSNAME": "EPSG:4326",
        "BBOX": f"{minlat:.6f},{minlon:.6f},{maxlat:.6f},{maxlon:.6f},urn:ogc:def:crs:EPSG::4326",
        "COUNT": count,
    }
    return await fetch_json(WFS_URL, params=params)


# ----------------------------------------------------------------------------- adapter
class BerlinAdapter(CityAdapter):
    """Berlin: OSM street geometry, cadastre trees (street + park layers)."""

    info = BERLIN_INFO

    async def find_street(self, query: str) -> StreetContext:
        """Resolve a Berlin street name and build its context."""
        resolved = await resolve_street_axis(query, city_bbox=BERLIN_BBOX, epsg=EPSG,
                                             countrycodes="de", city_label="Berlin")
        return await self._build(resolved.axis, resolved.name, resolved.way_ids, resolved.warnings)

    async def street_from_line(self, coords_wgs: list[tuple[float, float]], name: str | None = None) -> StreetContext:
        """Context around a user-drawn axis inside Berlin."""
        warnings: list[str] = []
        axis = drawn_axis(coords_wgs, EPSG, warnings)
        return await self._build(axis, name or "Drawn street", [], warnings)

    async def _build(self, axis: LineString, name: str, way_ids: list[int], warnings: list[str]) -> StreetContext:
        ctx = await osm_street_context(self.info, EPSG, axis, name, way_ids, warnings)
        clip = ctx.corridor.buffer(CLIP_MARGIN_M)
        bbox_wgs = crs.to_wgs(clip, EPSG).bounds
        results = await asyncio.gather(*(fetch_trees(typename, bbox_wgs) for typename, _ in TREE_LAYERS),
                                       return_exceptions=True)
        trees: list[ExistingTree] = []
        failures: list[str] = []
        for (typename, source), result in zip(TREE_LAYERS, results):
            if isinstance(result, BaseException):
                failures.append(typename)
                continue
            trees.extend(parse_trees(result, EPSG, clip, source))
            if (result.get("numberMatched") or 0) > (result.get("numberReturned") or 0):
                ctx.warnings.append(f"Tree cadastre layer {typename} truncated at {TREE_COUNT} trees")
        if len(failures) == len(TREE_LAYERS):
            ctx.warnings.append(TREE_FALLBACK_WARNING)
            return ctx
        if failures:
            ctx.warnings.append(f"Tree cadastre layer {failures[0]} unavailable; only {trees[0].source if trees else 'no'} trees shown")
        ctx.existing_trees = trees
        ctx.basis["existing_trees"] = "measured"
        ctx.sources["existing_trees"] = TREES_ATTRIBUTION
        return ctx
