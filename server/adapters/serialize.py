"""StreetContext (metric Shapely) -> StreetResponse (GeoJSON in WGS84)."""
from __future__ import annotations

from shapely.geometry.base import BaseGeometry

from .. import crs
from ..schemas import Basis, FeatureCollection, StreetResponse, StreetStats
from .base import StreetContext

LAYER_KEYS: tuple[str, ...] = ("axis", "carriageway", "sidewalks", "plantable", "buildings",
                               "cycleways", "parking", "junctions", "existing_trees", "corridor")


def _parts(geom: BaseGeometry) -> list[BaseGeometry]:
    """Non-empty single parts of a (multi) geometry."""
    if geom is None or geom.is_empty:
        return []
    if hasattr(geom, "geoms"):
        return [g for g in geom.geoms if not g.is_empty]
    return [geom]


def _single_feature(geom: BaseGeometry, epsg: int, key: str) -> list[dict]:
    """One Feature carrying the whole (multi) geometry, or nothing when empty."""
    if geom is None or geom.is_empty:
        return []
    return [crs.feature(geom, epsg, {"layer": key})]


def _area_in(geom: BaseGeometry, clip: BaseGeometry) -> float:
    if geom is None or geom.is_empty:
        return 0.0
    return round(float(geom.intersection(clip).area), 1)


def street_stats(ctx: StreetContext) -> StreetStats:
    """Counts and corridor-clipped areas summarising a street context."""
    return StreetStats(
        length_m=round(ctx.length_m, 1),
        existing_trees=len(ctx.existing_trees),
        existing_trees_crown_imputed=sum(1 for t in ctx.existing_trees if t.crown_imputed),
        buildings=len(_parts(ctx.buildings)),
        cycleway_m=round(float(ctx.cycleways.length), 1) if ctx.cycleways is not None and not ctx.cycleways.is_empty else 0.0,
        junctions=len(ctx.junctions),
        carriageway_m2=_area_in(ctx.carriageway, ctx.corridor),
        sidewalk_m2=_area_in(ctx.sidewalks, ctx.corridor),
        plantable_m2=_area_in(ctx.plantable, ctx.corridor))


def serialize_street(ctx: StreetContext) -> StreetResponse:
    """Every layer as a WGS84 FeatureCollection plus attribution, basis and stats."""
    epsg = ctx.epsg
    layers: dict[str, FeatureCollection] = {
        "axis": FeatureCollection(features=[
            crs.feature(ctx.axis, epsg, {"name": ctx.name, "length_m": round(ctx.length_m, 1)})]),
        "carriageway": FeatureCollection(features=_single_feature(ctx.carriageway, epsg, "carriageway")),
        "sidewalks": FeatureCollection(features=_single_feature(ctx.sidewalks, epsg, "sidewalks")),
        "plantable": FeatureCollection(features=_single_feature(ctx.plantable, epsg, "plantable")),
        "buildings": FeatureCollection(features=[
            crs.feature(g, epsg, {"layer": "buildings"}) for g in _parts(ctx.buildings)]),
        "cycleways": FeatureCollection(features=[
            crs.feature(g, epsg, {"layer": "cycleways"}) for g in _parts(ctx.cycleways)]),
        "parking": FeatureCollection(features=_single_feature(ctx.parking, epsg, "parking")),
        "junctions": FeatureCollection(features=[
            crs.feature(pt, epsg, {"id": f"J{i + 1:02d}"}) for i, pt in enumerate(ctx.junctions)]),
        "existing_trees": FeatureCollection(features=[
            crs.feature(t.pt, epsg, {
                "id": t.id, "species": t.species, "genus": t.genus, "crown_d_m": t.crown_d_m,
                "crown_r": round(t.crown_d_m / 2.0, 2) if t.crown_d_m else None,
                "crown_imputed": t.crown_imputed, "height_m": t.height_m,
                "planted_year": t.planted_year, "source": t.source})
            for t in ctx.existing_trees]),
        "corridor": FeatureCollection(features=_single_feature(ctx.corridor, epsg, "corridor")),
    }
    minlon, minlat, maxlon, maxlat = crs.to_wgs(ctx.corridor, epsg).bounds
    centre = crs.to_wgs(ctx.axis.interpolate(0.5, normalized=True), epsg)
    basis: dict[str, Basis] = {key: ctx.basis.get(key, "unknown") for key in LAYER_KEYS}
    sources = {key: ctx.sources[key] for key in LAYER_KEYS if ctx.sources.get(key)}
    return StreetResponse(
        street_id=ctx.street_id, city=ctx.city.id, city_name=ctx.city.name, name=ctx.name,
        epsg=epsg, length_m=round(ctx.length_m, 1),
        bbox=[round(v, 7) for v in (minlon, minlat, maxlon, maxlat)],
        center=[round(centre.x, 7), round(centre.y, 7)],
        layers=layers, layer_sources=sources, layer_basis=basis, stats=street_stats(ctx),
        warnings=list(ctx.warnings))
