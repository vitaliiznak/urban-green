"""Site generation along a street axis: grid and pack modes.

Grid mode puts a candidate at every ``spacing`` metres on each requested side
and reports its verdict. Pack mode walks each side in 0.5 m steps and keeps
the first legal position each time ``spacing`` metres have passed, so only
plantable sites are emitted and long unplantable stretches become ``gaps``.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterator, Optional

from shapely.geometry import LineString, Point

from ..adapters.base import StreetContext
from ..schemas import YEARS, Gap, PlanParams, PlanSummary, RulePack, RuleResult, Side, SiteProps, Species, Verdict
from .rules import LayerIndex, PreparedContext, apply_overrides, axis_frame, evaluate_site
from .species import crown_d_at, height_at

RAY_REACH_M = 40.0
EDGE_START_TOLERANCE_M = 1.0
INTERVAL_JOIN_M = 0.01
PACK_STEP_M = 0.5
STATION_EPS = 1e-9
AXIS_OUTSIDE_NOTE = "axis point is outside the carriageway; trunk offset measured from the axis"
NO_POSITION_REASON = "no legal position"


@dataclass
class SiteGeom:
    """One candidate trunk position with its evaluated properties (metric CRS)."""
    site_id: str
    pt: Point
    station_m: float
    side: Side
    verdict: Verdict
    props: SiteProps


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping / touching [start, end] intervals, sorted by start."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + INTERVAL_JOIN_M:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def carriageway_edge_distance(index: LayerIndex, origin: Point,
                              normal: tuple[float, float]) -> tuple[float, Optional[str]]:
    """Distance from ``origin`` along ``normal`` to the far edge of the carriageway.

    A 40 m ray is cut by the carriageway polygons; the edge is the end of the
    first cut segment that starts within 1 m of the origin. When the origin
    is not inside the carriageway the distance is 0 and a note says so.
    """
    ox, oy = origin.x, origin.y
    nx, ny = normal
    ray = LineString([(ox, oy), (ox + nx * RAY_REACH_M, oy + ny * RAY_REACH_M)])
    intervals: list[tuple[float, float]] = []
    for piece in index.cut(ray):
        along = [(x - ox) * nx + (y - oy) * ny for x, y in piece.coords]
        intervals.append((min(along), max(along)))
    merged = _merge_intervals(intervals)
    if merged and merged[0][0] <= EDGE_START_TOLERANCE_M:
        return merged[0][1], None
    return 0.0, AXIS_OUTSIDE_NOTE


def _join_note(existing: Optional[str], extra: str) -> str:
    return f"{existing}; {extra}" if existing else extra


class _Planner:
    """State shared by every site of one ``plan_sites`` call."""

    def __init__(self, ctx: StreetContext, params: PlanParams, pack: RulePack, sp: Species):
        self.ctx = ctx
        self.params = params
        self.pack = pack
        self.sp = sp
        self.prep = PreparedContext(ctx)
        self.length = ctx.length_m
        self.crown_by_year = {str(y): round(crown_d_at(sp, y), 2) for y in YEARS}
        self.height_by_year = {str(y): round(height_at(sp, y), 2) for y in YEARS}
        self.carriageway_rule_ids = {r.id for r in pack.rules if r.reference == "carriageway"}

    def _annotate_edge(self, rules: list[RuleResult], note: str) -> None:
        """Attach the ray-cast note to the carriageway rule (or the first rule)."""
        targets = [r for r in rules if r.rule_id in self.carriageway_rule_ids] or rules[:1]
        for r in targets:
            r.note = _join_note(r.note, note)

    def site_at(self, station: float, side: Side) -> SiteGeom:
        """Place and evaluate the trunk for ``station`` on ``side``."""
        origin, normal = axis_frame(self.ctx.axis, station)
        if side == "right":
            normal = (-normal[0], -normal[1])
        edge, edge_note = carriageway_edge_distance(self.prep.carriageway, origin, normal)
        reach = edge + self.params.offset_from_edge_m
        trunk = Point(origin.x + normal[0] * reach, origin.y + normal[1] * reach)
        verdict, rules, _ = evaluate_site(trunk, self.ctx, self.pack, self.sp, self.params,
                                          normal=normal, station_m=station, prepared=self.prep)
        if edge_note and rules:
            self._annotate_edge(rules, edge_note)
        site_id = f"{'L' if side == 'left' else 'R'}{int(round(station)):04d}"
        props = SiteProps(
            site_id=site_id,
            station_m=round(station, 2),
            side=side,
            verdict=verdict,
            edge_distance_m=round(edge, 2),
            rules=rules,
            failed_rules=[r.rule_id for r in rules if r.passed is False],
            species_id=self.sp.id,
            crown_d_mature_m=self.sp.mature_crown_d_m,
            height_mature_m=self.sp.mature_height_m,
            crown_d_by_year=dict(self.crown_by_year),
            height_by_year=dict(self.height_by_year),
        )
        return SiteGeom(site_id=site_id, pt=trunk, station_m=props.station_m, side=side,
                        verdict=verdict, props=props)

    def grid_stations(self) -> Iterator[float]:
        """spacing/2, 3*spacing/2, ... strictly inside the axis length."""
        spacing = self.params.spacing_m
        d = spacing / 2.0
        while d < self.length - STATION_EPS:
            yield d
            d += spacing

    def pack_side(self, side: Side) -> tuple[list[SiteGeom], list[Gap]]:
        """Walk one side from station 0 keeping the first legal site every ``spacing`` m."""
        spacing = self.params.spacing_m
        accepted: list[SiteGeom] = []
        rejected: list[tuple[float, list[str]]] = []
        d = 0.0
        while d <= self.length + STATION_EPS:
            site = self.site_at(d, side)
            if site.verdict != "invalid":
                accepted.append(site)
                d += spacing
            else:
                must_failed = [r.rule_id for r in site.props.rules if r.passed is False and r.mode == "must"]
                rejected.append((d, must_failed))
                d += PACK_STEP_M
            d = round(d, 3)
        return accepted, self._gaps(side, [s.station_m for s in accepted], rejected)

    def _gaps(self, side: Side, accepted: list[float],
              rejected: list[tuple[float, list[str]]]) -> list[Gap]:
        """Stretches longer than 2*spacing without an accepted site, with the dominant must-failure."""
        bounds = [0.0, *accepted, self.length]
        gaps: list[Gap] = []
        for a, b in zip(bounds, bounds[1:]):
            if b - a <= 2 * self.params.spacing_m:
                continue
            failures = Counter(rid for d, ids in rejected if a <= d <= b for rid in ids)
            reason = failures.most_common(1)[0][0] if failures else NO_POSITION_REASON
            gaps.append(Gap(side=side, station_from_m=round(a, 1), station_to_m=round(b, 1), reason=reason))
        return gaps


def summarize(sites: list[SiteGeom], gaps: list[Gap]) -> PlanSummary:
    """Verdict counts and failure statistics over ``sites``."""
    verdicts = Counter(s.verdict for s in sites)
    must = should = 0
    by_rule: Counter[str] = Counter()
    for site in sites:
        for r in site.props.rules:
            if r.passed is not False:
                continue
            by_rule[r.rule_id] += 1
            if r.mode == "must":
                must += 1
            else:
                should += 1
    valid, conditional = verdicts["valid"], verdicts["conditional"]
    return PlanSummary(
        total=len(sites),
        valid=valid,
        conditional=conditional,
        invalid=verdicts["invalid"],
        planted=valid + conditional,
        must_failures=must,
        should_failures=should,
        failures_by_rule=dict(by_rule.most_common()),
        gaps=gaps,
    )


def plan_sites(ctx: StreetContext, params: PlanParams, pack: RulePack,
               sp: Species) -> tuple[list[SiteGeom], PlanSummary]:
    """Propose and evaluate tree positions for ``ctx`` under ``pack`` and ``params``.

    ``params.rule_overrides`` are applied to ``pack`` (idempotent, so a pack
    that already carries them is fine). Grid mode returns every candidate with
    its verdict; pack mode returns only accepted (valid/conditional) sites
    plus the gaps between them.
    """
    active = apply_overrides(pack, params.rule_overrides) if params.rule_overrides else pack
    planner = _Planner(ctx, params, active, sp)
    sides: tuple[Side, ...] = ("left", "right") if params.side == "both" else (params.side,)
    gaps: list[Gap] = []
    if params.mode == "grid":
        sites = [planner.site_at(d, side) for d in planner.grid_stations() for side in sides]
    else:
        sites = []
        for side in sides:
            side_sites, side_gaps = planner.pack_side(side)
            sites.extend(side_sites)
            gaps.extend(side_gaps)
    return sites, summarize(sites, gaps)
