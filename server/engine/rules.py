"""Rule packs and per-site rule evaluation.

Rules are data (``rules/*.json``) with attribution; this module only measures
distances in the street's metric CRS and compares them with the rule values.
An independent geometry safeguard excludes positions already occupied by a
mapped trunk, without treating numerical coincidence as a clearance standard.
``PreparedContext`` wraps a ``StreetContext`` with spatial indexes so that a
few thousand cadastre polygons can be queried per site in microseconds.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Callable, Optional

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, Point
from shapely.geometry.base import BaseGeometry

from ..adapters.base import StreetContext
from ..schemas import Basis, PlanParams, Rule, RuleOverride, RulePack, RuleResult, Species, Verdict
from .species import rules_dir

SIDEWALK_PROBE_HALF_M = 12.0
PLANTABLE_TOLERANCE_M = 0.05
ON_SEGMENT_TOLERANCE_M = 0.05
MEASURE_EPS = 1e-6

# --------------------------------------------------------------------------- loading
@lru_cache(maxsize=1)
def _packs() -> dict[str, RulePack]:
    packs: dict[str, RulePack] = {}
    for path in sorted(rules_dir().glob("*.json")):
        if path.name == "species.json":
            continue
        with path.open(encoding="utf-8") as fh:
            pack = RulePack(**json.load(fh))
        packs[pack.id] = pack
    return packs


def load_rule_packs() -> dict[str, RulePack]:
    """Every rule pack in the rules directory keyed by id (deep copies of a cached parse)."""
    return {pid: pack.model_copy(deep=True) for pid, pack in _packs().items()}


def apply_overrides(pack: RulePack, overrides: dict[str, RuleOverride]) -> RulePack:
    """Copy of ``pack`` with session/request overrides applied.

    Only fields set on the override change; every touched rule gets
    ``overridden=True``. Override ids that are not in the pack are ignored so a
    stale override never breaks planning.
    """
    out = pack.model_copy(deep=True)
    for rule in out.rules:
        ov = overrides.get(rule.id)
        if ov is None:
            continue
        changed = False
        if ov.min_distance_m is not None:
            rule.min_distance_m = ov.min_distance_m
            changed = True
        if ov.mode is not None:
            rule.mode = ov.mode
            changed = True
        if ov.enabled is not None:
            rule.enabled = ov.enabled
            changed = True
        if changed:
            rule.overridden = True
    return out


# --------------------------------------------------------------------------- geometry helpers
def flatten(geom: Optional[BaseGeometry]) -> list[BaseGeometry]:
    """Non-empty atomic parts of any geometry (collections flattened recursively)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type.startswith("Multi") or geom.geom_type == "GeometryCollection":
        out: list[BaseGeometry] = []
        for part in geom.geoms:
            out.extend(flatten(part))
        return out
    return [geom]


def axis_frame(axis: LineString, station: float) -> tuple[Point, tuple[float, float]]:
    """Axis point at ``station`` and the left-hand unit normal of the axis there.

    The tangent is taken between ``station - 0.5`` and ``station + 0.5``
    (clamped to the axis) so short segments and vertices do not produce
    noisy directions.
    """
    length = float(axis.length)
    d = min(max(float(station), 0.0), length)
    p = axis.interpolate(d)
    a = axis.interpolate(max(d - 0.5, 0.0))
    b = axis.interpolate(min(d + 0.5, length))
    tx, ty = b.x - a.x, b.y - a.y
    norm = math.hypot(tx, ty)
    if norm == 0.0:
        return p, (0.0, 1.0)
    return p, (-ty / norm, tx / norm)


class LayerIndex:
    """STRtree over the atomic parts of one layer for nearest-distance and line cuts."""

    def __init__(self, geom: Optional[BaseGeometry]):
        self.parts = np.array(flatten(geom), dtype=object)
        self.empty = len(self.parts) == 0
        self.tree = None if self.empty else STRtree(self.parts)

    def distance(self, pt: Point) -> Optional[float]:
        """Distance from ``pt`` to the nearest part (0 inside a polygon); None when empty."""
        if self.tree is None:
            return None
        _, dist = self.tree.query_nearest(pt, return_distance=True)
        if len(dist) == 0:
            return None
        return float(dist[0])

    def cut(self, line: LineString) -> list[LineString]:
        """LineString pieces of ``line`` that lie inside/on the layer's parts."""
        if self.tree is None:
            return []
        pieces: list[LineString] = []
        for i in self.tree.query(line, predicate="intersects"):
            for part in flatten(self.parts[i].intersection(line)):
                if isinstance(part, LineString) and part.length > 0.0:
                    pieces.append(part)
        return pieces


class PointIndex:
    """STRtree over points (existing trunks, junctions)."""

    def __init__(self, points: list[Point]):
        self.points = [p for p in points if p is not None and not p.is_empty]
        self.tree = STRtree(self.points) if self.points else None

    def min_distance(self, pt: Point) -> Optional[float]:
        """Distance to the nearest point, None when there are no points."""
        if self.tree is None:
            return None
        _, dist = self.tree.query_nearest(pt, return_distance=True)
        if len(dist) == 0:
            return None
        return float(dist[0])


class PreparedContext:
    """Spatial indexes of a StreetContext, built once per plan and reused per site."""

    def __init__(self, ctx: StreetContext):
        self.ctx = ctx
        self.carriageway = LayerIndex(ctx.carriageway)
        self.buildings = LayerIndex(ctx.buildings)
        self.cycleways = LayerIndex(ctx.cycleways)
        self.sidewalks = LayerIndex(ctx.sidewalks)
        self.trees = PointIndex([t.pt for t in ctx.existing_trees])
        self.junctions = PointIndex(list(ctx.junctions))
        self.junction_stations = [ctx.axis.project(p) for p in ctx.junctions]
        self.plantable_empty = ctx.plantable is None or ctx.plantable.is_empty
        self._plantable = None if self.plantable_empty else ctx.plantable.buffer(PLANTABLE_TOLERANCE_M)
        if self._plantable is not None:
            shapely.prepare(self._plantable)

    def basis(self, layer: str) -> Basis:
        """Basis recorded by the adapter for ``layer`` (``unknown`` when absent)."""
        return self.ctx.basis.get(layer, "unknown")

    def on_plantable(self, pt: Point) -> Optional[bool]:
        """Whether the trunk stands on the plantable surface (None when the layer is missing)."""
        if self._plantable is None:
            return None
        return bool(self._plantable.contains(pt))

    def sidewalk_width_through(self, pt: Point, normal: tuple[float, float]) -> Optional[float]:
        """Sidewalk width along the normal through ``pt``; None when ``pt`` is not on a sidewalk."""
        nx, ny = normal
        probe = LineString([
            (pt.x - nx * SIDEWALK_PROBE_HALF_M, pt.y - ny * SIDEWALK_PROBE_HALF_M),
            (pt.x + nx * SIDEWALK_PROBE_HALF_M, pt.y + ny * SIDEWALK_PROBE_HALF_M),
        ])
        pieces = self.sidewalks.cut(probe)
        if not pieces:
            return None
        merged = shapely.line_merge(MultiLineString(pieces)) if len(pieces) > 1 else pieces[0]
        segments = [g for g in flatten(merged) if isinstance(g, LineString)]
        if not segments:
            return None
        nearest = min(segments, key=lambda seg: seg.distance(pt))
        if nearest.distance(pt) > ON_SEGMENT_TOLERANCE_M:
            return None
        return float(nearest.length)


# --------------------------------------------------------------------------- measurements
# (measured_m, forced_passed, note, basis): forced_passed None means "compare measured
# with the rule value", otherwise it is the final outcome.
Measurement = tuple[Optional[float], Optional[bool], Optional[str], Basis]


def _measure_carriageway(pt: Point, prep: PreparedContext, *_: object) -> Measurement:
    dist = prep.carriageway.distance(pt)
    if dist is None:
        return None, None, "no carriageway mapped", "unknown"
    return dist, None, None, prep.basis("carriageway")


def _measure_cycleway(pt: Point, prep: PreparedContext, *_: object) -> Measurement:
    dist = prep.cycleways.distance(pt)
    if dist is None:
        return None, None, "no cycle path mapped", "unknown"
    return dist, None, None, prep.basis("cycleways")


def _measure_building(pt: Point, prep: PreparedContext, sp: Species, *_: object) -> Measurement:
    dist = prep.buildings.distance(pt)
    if dist is None:
        return None, None, "no buildings mapped", "unknown"
    return dist - sp.mature_crown_d_m / 2.0, None, None, prep.basis("buildings")


def _measure_existing_tree(pt: Point, prep: PreparedContext, *_: object) -> Measurement:
    dist = prep.trees.min_distance(pt)
    if dist is None:
        return None, True, "no existing trees nearby", prep.basis("existing_trees")
    return dist, None, None, prep.basis("existing_trees")


def _measure_junction(pt: Point, prep: PreparedContext, *_: object) -> Measurement:
    dist = prep.junctions.min_distance(pt)
    if dist is None:
        return None, True, "no junctions mapped", prep.basis("junctions")
    return dist, None, None, prep.basis("junctions")


def _measure_sidewalk_width(pt: Point, prep: PreparedContext, sp: Species, params: PlanParams,
                            normal: tuple[float, float]) -> Measurement:
    if prep.sidewalks.empty:
        return None, None, "no sidewalk mapped", "unknown"
    width = prep.sidewalk_width_through(pt, normal)
    if width is None:
        return None, None, "trunk not on a mapped sidewalk", prep.basis("sidewalks")
    return width - params.pit_width_m, None, None, prep.basis("sidewalks")


def _measure_plantable(pt: Point, prep: PreparedContext, *_: object) -> Measurement:
    inside = prep.on_plantable(pt)
    if inside is None:
        return None, None, "no plantable surface mapped", "unknown"
    note = None if inside else "trunk is not on a sidewalk or verge"
    return None, inside, note, prep.basis("plantable")


MeasureFn = Callable[[Point, PreparedContext, Species, PlanParams, tuple[float, float]], Measurement]

MEASUREMENTS: dict[str, MeasureFn] = {
    "carriageway": _measure_carriageway,
    "cycleway": _measure_cycleway,
    "building": _measure_building,
    "existing_tree": _measure_existing_tree,
    "junction": _measure_junction,
    "sidewalk_remaining_width": _measure_sidewalk_width,
    "plantable_surface": _measure_plantable,
}


def _evaluate_rule(rule: Rule, pt: Point, prep: PreparedContext, sp: Species, params: PlanParams,
                   normal: tuple[float, float]) -> RuleResult:
    measure = MEASUREMENTS.get(rule.reference)
    if measure is None:
        measured, passed, note, basis = None, None, f"unsupported reference '{rule.reference}'", "unknown"
    else:
        measured, passed, note, basis = measure(pt, prep, sp, params, normal)
    if passed is None and measured is not None:
        if rule.min_distance_m is None:
            note = "rule has no distance value"
        elif basis == "estimated" and rule.reference == "sidewalk_remaining_width":
            # An OSM default width (2.5 m) would fail every site; report the estimate, do not judge on it.
            note = "sidewalk width estimated from OSM tags, not measured; passage not evaluated"
        else:
            passed = measured + MEASURE_EPS >= rule.min_distance_m
    return RuleResult(
        rule_id=rule.id,
        label=rule.label,
        mode=rule.mode,
        required_m=rule.min_distance_m,
        measured_m=None if measured is None else round(measured, 2),
        passed=passed,
        basis=basis,
        assumption=rule.assumption,
        note=note,
    )


def verdict_for(results: list[RuleResult]) -> Verdict:
    """invalid when any must-rule fails, conditional when only should-rules fail, else valid."""
    failed_modes = {r.mode for r in results if r.passed is False}
    if "must" in failed_modes:
        return "invalid"
    if "should" in failed_modes:
        return "conditional"
    return "valid"


def _occupied_tree_position(pt: Point, prep: PreparedContext) -> RuleResult:
    """Reject duplicate mapped trunk positions independently of editable clearances.

    MEASURE_EPS absorbs floating-point geometry noise; it is not a planting
    distance. The wider existing-tree spacing recommendation remains editable.
    """
    distance = prep.trees.min_distance(pt)
    occupied = distance is not None and distance <= MEASURE_EPS
    return RuleResult(
        rule_id="occupied_tree_position",
        label="Position occupied by an existing tree",
        mode="must",
        required_m=None,
        measured_m=distance,
        passed=not occupied,
        basis=prep.basis("existing_trees"),
        assumption=True,
        note=("This position coincides with a mapped existing trunk and is excluded. "
              "Geometry safeguard using numerical tolerance, not a cited clearance standard."
              if occupied else None),
    )


def evaluate_site(pt: Point, ctx: StreetContext, pack: RulePack, sp: Species, params: PlanParams,
                  *, normal: tuple[float, float] | None = None,
                  station_m: float | None = None,
                  prepared: PreparedContext | None = None) -> tuple[Verdict, list[RuleResult], list[str]]:
    """Evaluate every enabled rule of ``pack`` for a trunk at ``pt``.

    ``normal`` is the unit vector across the street at the site (used for the
    sidewalk width probe); when omitted it is derived from the axis. Pass a
    ``PreparedContext`` built from ``ctx`` when evaluating many sites.
    Returns the verdict, one RuleResult per enabled rule plus the occupied-trunk
    geometry safeguard, and the collected notes.
    """
    prep = prepared if prepared is not None and prepared.ctx is ctx else PreparedContext(ctx)
    if normal is None:
        _, normal = axis_frame(ctx.axis, ctx.axis.project(pt))
    results = [_evaluate_rule(rule, pt, prep, sp, params, normal) for rule in pack.rules if rule.enabled]
    results.append(_occupied_tree_position(pt, prep))
    # A side-street can send the carriageway ray far sideways. Measure the
    # station as well as trunk distance so this cannot escape the exclusion.
    station = ctx.axis.project(pt) if station_m is None else station_m
    separation = min((abs(station - s) for s in prep.junction_stations), default=None)
    distance = prep.junctions.min_distance(pt)
    if distance is not None:
        separation = min(separation, distance)
    results.append(RuleResult(
        rule_id="junction_exclusion", label="Junction and crossing exclusion",
        mode="must", required_m=10.0,
        measured_m=None if separation is None else round(separation, 2),
        passed=separation is None or separation + MEASURE_EPS >= 10.0,
        basis=prep.basis("junctions"), assumption=True,
        note=("No junctions mapped; exclusion could not be checked." if separation is None else
              "Mandatory 10 m planning exclusion around mapped junctions and crossings, measured along the street or to the trunk. Not a surveyed sight triangle."
              if separation < 10.0 else None),
    ))
    notes = list(dict.fromkeys(r.note for r in results if r.note))
    return verdict_for(results), results, notes
