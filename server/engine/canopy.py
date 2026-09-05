"""Canopy cover projection: crown circles per growth year, unioned with the
existing trees and clipped to the analysis corridor."""
from __future__ import annotations

from typing import Iterable

import numpy as np
import shapely
from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from ..adapters.base import ExistingTree, StreetContext
from ..schemas import YEARS, CanopyResult, CanopyYear, Species
from .sites import SiteGeom
from .species import crown_d_at, impute_crown

CIRCLE_QUAD_SEGS = 8
PLANTED = ("valid", "conditional")


def existing_crown_d(tree: ExistingTree) -> float:
    """Crown diameter of an existing tree, imputed from genus/species when unmeasured."""
    if tree.crown_d_m is not None and tree.crown_d_m > 0:
        return float(tree.crown_d_m)
    return impute_crown(tree.genus, tree.species, None)


def crown_union(points: list[Point], radii: list[float]) -> BaseGeometry:
    """Union of circles of the given radii (metres) around ``points``."""
    if not points:
        return shapely.Polygon()
    circles = shapely.buffer(np.array(points, dtype=object), np.array(radii, dtype=float),
                             quad_segs=CIRCLE_QUAD_SEGS)
    return shapely.union_all(circles)


def clip_to(geom: BaseGeometry | None, corridor: BaseGeometry) -> BaseGeometry:
    """``geom`` clipped to ``corridor`` (empty polygon for a missing layer)."""
    if geom is None or geom.is_empty:
        return shapely.Polygon()
    return geom.intersection(corridor)


def pct(part: float, whole: float) -> float:
    """Percentage of ``part`` in ``whole`` rounded to one decimal, 0 when ``whole`` is 0."""
    return round(100.0 * part / whole, 1) if whole > 0 else 0.0


def canopy_metrics(ctx: StreetContext, sites: list[SiteGeom], sp: Species,
                   years: Iterable[int] = YEARS) -> CanopyResult:
    """Crown area and cover shares per year for the planted sites of a scenario.

    Planted sites are the valid and conditional ones. Existing crowns come
    from the adapter's (already imputed) crown diameters. All areas are
    clipped to ``ctx.corridor``; the denominators are the corridor, the
    street (carriageway + sidewalks) and the sidewalks alone.
    """
    corridor = ctx.corridor
    corridor_area = float(corridor.area)
    sidewalk = clip_to(ctx.sidewalks, corridor)
    street = clip_to(shapely.union_all([g for g in (ctx.carriageway, ctx.sidewalks)
                                      if g is not None and not g.is_empty]), corridor)
    existing = clip_to(crown_union([t.pt for t in ctx.existing_trees],
                                 [existing_crown_d(t) / 2.0 for t in ctx.existing_trees]), corridor)
    planted = [s.pt for s in sites if s.verdict in PLANTED]

    rows: list[CanopyYear] = []
    for year in years:
        radius = crown_d_at(sp, year) / 2.0
        new = clip_to(crown_union(planted, [radius] * len(planted)), corridor)
        total = shapely.union_all([new, existing])
        rows.append(CanopyYear(
            year=int(year),
            new_crown_area_m2=round(float(new.area), 1),
            total_crown_area_m2=round(float(total.area), 1),
            cover_corridor_pct=pct(total.area, corridor_area),
            cover_street_pct=pct(total.intersection(street).area, street.area),
            sidewalk_under_crown_pct=pct(total.intersection(sidewalk).area, sidewalk.area),
        ))
    return CanopyResult(
        corridor_area_m2=round(corridor_area, 1),
        street_area_m2=round(float(street.area), 1),
        sidewalk_area_m2=round(float(sidewalk.area), 1),
        existing_cover_corridor_pct=pct(existing.area, corridor_area),
        years=rows,
    )
