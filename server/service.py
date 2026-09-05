"""Use-cases shared by the HTTP API, the agent tools and the MCP server.

Holds the in-memory Store of street contexts and scenarios and wires the
adapters (street geometry) to the engine (sites, canopy, shade). Every
function here speaks in the wire models from `schemas.py`; geometry never
leaves this module in metric coordinates.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from . import crs
from .adapters import ADAPTERS, get_adapter
from .agent.providers import agent_info
from .adapters.base import StreetContext, StreetNotFound, UpstreamUnavailable
from .adapters.serialize import serialize_street
from .engine.canopy import canopy_metrics
from .engine.rules import apply_overrides, load_rule_packs
from .engine.shade import shade_polygons
from .engine.sites import SiteGeom, plan_sites
from .engine.species import load_species, species_or_default
from .schemas import (
    YEARS,
    CanopyRequest,
    CanopyResult,
    CompareRequest,
    CompareResponse,
    CompareRow,
    ConfigResponse,
    FeatureCollection,
    PlanParams,
    PlanRequest,
    RuleOverride,
    RulePack,
    ScenarioResponse,
    ShadeRequest,
    ShadeResult,
    SiteProps,
    Species,
    StreetRequest,
    StreetResponse,
)
from .session import Session

VERSION = "0.1.0"
STORE_TTL_S = 3 * 3600
STORE_MAX_ENTRIES = 2000

class ApiError(Exception):
    """Domain error that maps 1:1 to the JSON error envelope."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass
class Scenario:
    response: ScenarioResponse
    sites: list[SiteGeom]
    ctx_id: str
    session_id: str


class Store:
    """Street contexts and scenarios with a sliding 3 h TTL and a size cap.

    Reading an entry refreshes it, and reading a scenario also refreshes the
    street it depends on, so active work never expires under a planner."""

    def __init__(self, ttl_s: int = STORE_TTL_S, max_entries: int = STORE_MAX_ENTRIES) -> None:
        self.streets: dict[str, StreetContext] = {}
        self.scenarios: dict[str, Scenario] = {}
        self.street_responses: dict[str, StreetResponse] = {}
        self._expires: dict[str, float] = {}
        self._lookup: dict[tuple[str, str], str] = {}
        self._ttl = ttl_s
        self._max = max_entries
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ streets
    def put_street(self, ctx: StreetContext, response: StreetResponse, key: tuple[str, str] | None = None) -> None:
        with self._lock:
            self._evict()
            self.streets[ctx.street_id] = ctx
            self.street_responses[ctx.street_id] = response
            self._expires[ctx.street_id] = time.monotonic() + self._ttl
            if key is not None:
                self._lookup[key] = ctx.street_id

    def get_street(self, street_id: str) -> StreetContext:
        with self._lock:
            ctx = self.streets.get(street_id)
            if ctx is None or self._expired(street_id):
                raise ApiError(404, "unknown_street", f"Unknown street id '{street_id}'. Load the street first.")
            self._touch(street_id)
            return ctx

    def get_street_response(self, street_id: str) -> StreetResponse:
        self.get_street(street_id)
        return self.street_responses[street_id]

    def find_street(self, key: tuple[str, str]) -> Optional[str]:
        with self._lock:
            street_id = self._lookup.get(key)
            if street_id is None or street_id not in self.streets or self._expired(street_id):
                return None
            self._touch(street_id)
            return street_id

    # ---------------------------------------------------------------- scenarios
    def put_scenario(self, scenario: Scenario) -> None:
        with self._lock:
            self._evict()
            sid = scenario.response.scenario_id
            self.scenarios[sid] = scenario
            self._expires[sid] = time.monotonic() + self._ttl
            self._touch(scenario.ctx_id)

    def get_scenario(self, scenario_id: str) -> Scenario:
        with self._lock:
            scenario = self.scenarios.get(scenario_id)
            if scenario is None or self._expired(scenario_id):
                raise ApiError(404, "unknown_scenario", f"Unknown scenario id '{scenario_id}'.")
            self._touch(scenario_id)
            self._touch(scenario.ctx_id)
            return scenario

    def clear(self) -> None:
        with self._lock:
            self.streets.clear()
            self.scenarios.clear()
            self.street_responses.clear()
            self._expires.clear()
            self._lookup.clear()

    # ---------------------------------------------------------------- internals
    def _expired(self, key: str) -> bool:
        return self._expires.get(key, 0.0) < time.monotonic()

    def _touch(self, key: str) -> None:
        if key in self._expires:
            self._expires[key] = time.monotonic() + self._ttl

    def _evict(self) -> None:
        """Drop expired entries, then the oldest ones while over the cap."""
        now = time.monotonic()
        for key in [k for k, exp in self._expires.items() if exp < now]:
            self._drop(key)
        overflow = len(self._expires) - self._max + 1
        if overflow > 0:
            for key, _exp in sorted(self._expires.items(), key=lambda kv: kv[1])[:overflow]:
                self._drop(key)

    def _drop(self, key: str) -> None:
        self._expires.pop(key, None)
        self.streets.pop(key, None)
        self.street_responses.pop(key, None)
        self.scenarios.pop(key, None)
        for lookup_key in [k for k, v in self._lookup.items() if v == key]:
            del self._lookup[lookup_key]


STORE = Store()
_inflight: dict[tuple[str, str], asyncio.Future] = {}


def new_id() -> str:
    """Short random id (8 hex chars) for streets and scenarios."""
    return secrets.token_hex(4)


def now_iso() -> str:
    """Current UTC time as ISO 8601 with a Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", query.strip()).casefold()


def species_short(sp: Species) -> str:
    """'Tilia cordata' from \"Tilia cordata 'Greenspire'\" — the label-friendly name."""
    return re.sub(r"\s*'[^']*'", "", sp.name_lat).strip()


def default_label(params: PlanParams, sp: Species) -> str:
    return f"{params.spacing_m:g} m · {species_short(sp)} · {params.side} · {params.mode}"


# ---------------------------------------------------------------------- streets
async def load_street(req: StreetRequest) -> StreetResponse:
    """Resolve a street through its city adapter and cache the context.

    Cached by (city, normalized query); drawn lines are always rebuilt."""
    try:
        adapter = get_adapter(req.city)
    except KeyError:
        raise ApiError(404, "unknown_city", f"Unknown city '{req.city}'. Known: {', '.join(ADAPTERS)}.") from None
    if req.line is not None:
        if len(req.line) < 2:
            raise ApiError(400, "bad_request", "A drawn street needs at least two points.")
        ctx = await _guard_upstream(adapter.street_from_line([(float(p[0]), float(p[1])) for p in req.line], req.name))
        return _register_street(ctx, None)
    if not req.query or not req.query.strip():
        raise ApiError(400, "bad_request", "Provide a street name in 'query' or a drawn 'line'.")
    key = (req.city, normalize_query(req.query))
    cached = STORE.find_street(key)
    if cached is not None:
        return STORE.get_street_response(cached)
    return await _load_shared(adapter, req, key)


async def _load_shared(adapter: Any, req: StreetRequest, key: tuple[str, str]) -> StreetResponse:
    """Coalesce concurrent loads of the same street into one upstream fetch."""
    pending = _inflight.get(key)
    if pending is not None:
        return await asyncio.shield(pending)
    task = asyncio.ensure_future(_fetch_and_register(adapter, req, key))
    _inflight[key] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _inflight.get(key) is task:
            del _inflight[key]


async def _fetch_and_register(adapter: Any, req: StreetRequest, key: tuple[str, str]) -> StreetResponse:
    ctx = await _guard_upstream(adapter.find_street(req.query.strip()))
    return _register_street(ctx, key)


async def _guard_upstream(coro: Any) -> StreetContext:
    """Translate adapter failures into API errors."""
    try:
        return await coro
    except StreetNotFound as exc:
        raise ApiError(404, "street_not_found", str(exc) or "Street not found.") from exc
    except UpstreamUnavailable as exc:
        raise ApiError(503, "upstream_unavailable", str(exc) or "Upstream data source unavailable.") from exc
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        raise ApiError(503, "upstream_unavailable", f"Upstream data source unavailable: {exc}") from exc


def _register_street(ctx: StreetContext, key: tuple[str, str] | None) -> StreetResponse:
    ctx.street_id = new_id()
    response = serialize_street(ctx)
    STORE.put_street(ctx, response, key)
    return response


# --------------------------------------------------------------------- planning
def merged_overrides(session: Session, request_overrides: dict[str, RuleOverride]) -> dict[str, RuleOverride]:
    """Session overrides with the request's on top (request wins per rule)."""
    merged = dict(session.rule_overrides)
    merged.update(request_overrides)
    return merged


def rule_pack(pack_id: str, overrides: dict[str, RuleOverride] | None = None) -> RulePack:
    """A rule pack by id with the given overrides applied (copy)."""
    packs = load_rule_packs()
    if pack_id not in packs:
        raise ApiError(404, "unknown_rule_pack", f"Unknown rule pack '{pack_id}'. Known: {', '.join(packs)}.")
    pack = packs[pack_id]
    relevant = {rid: ov for rid, ov in (overrides or {}).items() if any(r.id == rid for r in pack.rules)}
    return apply_overrides(pack, relevant) if relevant else pack.model_copy(deep=True)


def species(species_id: str) -> Species:
    try:
        return species_or_default(species_id)
    except KeyError:
        raise ApiError(404, "unknown_species", f"Unknown species '{species_id}'. Known: {', '.join(load_species())}.") from None


def plan(req: PlanRequest, session: Session) -> ScenarioResponse:
    """Plan a scenario for a loaded street and store it."""
    ctx = STORE.get_street(req.street_id)
    overrides = merged_overrides(session, req.rule_overrides)
    pack = rule_pack(req.rule_pack_id, overrides)
    sp = species(req.species_id)
    params = PlanParams(**req.model_dump(exclude={"street_id", "rule_overrides", "label"}), rule_overrides=overrides)
    params.label = req.label or default_label(params, sp)
    sites, summary = plan_sites(ctx, params, pack, sp)
    canopy_result = canopy_metrics(ctx, sites, sp)
    response = ScenarioResponse(
        scenario_id=new_id(),
        street_id=ctx.street_id,
        label=params.label,
        created_at=now_iso(),
        params=params,
        rules_used=pack,
        species=sp,
        sites=FeatureCollection(features=[crs.feature(s.pt, ctx.epsg, s.props.model_dump()) for s in sites]),
        summary=summary,
        canopy=canopy_result,
    )
    STORE.put_scenario(Scenario(response=response, sites=sites, ctx_id=ctx.street_id, session_id=session.id))
    session.last_street_id = ctx.street_id
    session.last_scenario_id = response.scenario_id
    session.scenario_ids.append(response.scenario_id)
    return response


def scenario(scenario_id: str) -> ScenarioResponse:
    return STORE.get_scenario(scenario_id).response


def canopy(req: CanopyRequest) -> CanopyResult:
    """Canopy metrics for a scenario, optionally for a custom list of years."""
    sc = STORE.get_scenario(req.scenario_id)
    ctx = STORE.get_street(sc.ctx_id)
    years = tuple(sorted({int(y) for y in req.years})) or YEARS
    return canopy_metrics(ctx, sc.sites, sc.response.species, years=years)


def shade(req: ShadeRequest) -> ShadeResult:
    """Sun-shadow polygons for a scenario at one moment."""
    sc = STORE.get_scenario(req.scenario_id)
    ctx = STORE.get_street(sc.ctx_id)
    result = shade_polygons(
        ctx, sc.sites, sc.response.species,
        year=req.year, month=req.month, day=req.day, hour=req.hour, include_existing=req.include_existing,
    )
    result.scenario_id = req.scenario_id
    return result


def compare(req: CompareRequest) -> CompareResponse:
    """Side-by-side table of scenarios; best = highest corridor cover at 30 years."""
    rows = [compare_row(STORE.get_scenario(sid).response) for sid in req.scenario_ids]
    best = max(rows, key=lambda r: r.cover_corridor_pct_30).scenario_id if rows else None
    return CompareResponse(rows=rows, best_by_cover=best)


def compare_row(resp: ScenarioResponse) -> CompareRow:
    year30 = _year_entry(resp, 30)
    return CompareRow(
        scenario_id=resp.scenario_id,
        label=resp.label,
        spacing_m=resp.params.spacing_m,
        side=resp.params.side,
        species_id=resp.params.species_id,
        mode=resp.params.mode,
        planted=resp.summary.planted,
        valid=resp.summary.valid,
        conditional=resp.summary.conditional,
        invalid=resp.summary.invalid,
        new_crown_area_30_m2=year30.new_crown_area_m2,
        cover_corridor_pct_30=year30.cover_corridor_pct,
        cover_street_pct_30=year30.cover_street_pct,
        sidewalk_under_crown_pct_30=year30.sidewalk_under_crown_pct,
    )


def _year_entry(resp: ScenarioResponse, year: int):
    for entry in resp.canopy.years:
        if entry.year == year:
            return entry
    return resp.canopy.years[-1]


def explain_site(scenario_id: str, site_id: str) -> SiteProps:
    """Rule-by-rule result for one proposed site."""
    sc = STORE.get_scenario(scenario_id)
    for site in sc.sites:
        if site.site_id == site_id:
            return site.props
    ids = [site.site_id for site in sc.sites]
    hint = ", ".join(ids[:8]) + (f" … ({len(ids)} sites)" if len(ids) > 8 else "")
    raise ApiError(404, "unknown_site", f"Scenario {scenario_id} has no site '{site_id}'. "
                                        f"Site ids are side letter + station in metres: {hint}.")


def export_geojson(scenario_id: str) -> dict[str, Any]:
    """FeatureCollection: every site (all props), mature crowns of planted sites, the axis."""
    sc = STORE.get_scenario(scenario_id)
    ctx = STORE.get_street(sc.ctx_id)
    resp = sc.response
    radius = resp.species.mature_crown_d_m / 2
    features: list[dict[str, Any]] = []
    for site in sc.sites:
        features.append(crs.feature(site.pt, ctx.epsg, {"kind": "site", **site.props.model_dump()}))
    for site in sc.sites:
        if site.verdict in ("valid", "conditional"):
            features.append(crs.feature(site.pt.buffer(radius, 24), ctx.epsg, {
                "kind": "mature_crown", "site_id": site.site_id, "verdict": site.verdict,
                "crown_d_m": resp.species.mature_crown_d_m, "year": 30,
            }))
    features.append(crs.feature(ctx.axis, ctx.epsg, {"kind": "axis", "name": ctx.name, "length_m": round(ctx.length_m, 1)}))
    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "street": ctx.name,
            "street_id": ctx.street_id,
            "city": ctx.city.id,
            "scenario_id": resp.scenario_id,
            "label": resp.label,
            "params": resp.params.model_dump(),
            "rule_pack_id": resp.rules_used.id,
            "rule_pack_source": resp.rules_used.source.title,
            "species": resp.species.name_lat,
            "summary": resp.summary.model_dump(),
            "generated_at": now_iso(),
            "generator": f"Allee {VERSION}",
        },
    }


def config() -> ConfigResponse:
    """Everything the front-end needs on first load."""
    return ConfigResponse(
        cities=[adapter.info for adapter in ADAPTERS.values()],
        rule_packs=list(load_rule_packs().values()),
        species=list(load_species().values()),
        defaults=PlanParams(),
        agent=agent_info(),
        version=VERSION,
    )
