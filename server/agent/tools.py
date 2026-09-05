"""Agent tool registry: JSON schemas, handlers and compact observations.

Every handler calls the service layer and returns a ToolResult whose
`summary` is what the model reads (a few hundred characters at most) and
whose `payload` is the full typed response that the UI renders. Payloads
never enter the conversation history. The same handlers back the MCP server.
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from pydantic import ValidationError

from .. import service
from ..schemas import (
    YEARS,
    CanopyRequest,
    CanopyResult,
    CompareRequest,
    CompareResponse,
    PlanRequest,
    RuleOverride,
    RulePack,
    ScenarioResponse,
    ShadeRequest,
    ShadeResult,
    SiteProps,
    StreetRequest,
    StreetResponse,
)
from ..session import Session

OBSERVATION_LIMIT = 800
LAYERS = ["axis", "carriageway", "sidewalks", "plantable", "buildings", "cycleways", "junctions",
          "existing_trees", "corridor", "sites", "crowns", "shade"]


@dataclass
class ToolContext:
    session: Session


@dataclass
class ToolResult:
    summary: str
    ok: bool = True
    payload_type: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    ui: list[dict[str, Any]] = field(default_factory=list)


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler


TOOLS: dict[str, ToolSpec] = {}


def tool(name: str, description: str, parameters: dict[str, Any]) -> Callable[[Handler], Handler]:
    """Register a handler under `name` with its JSON-schema parameters."""
    schema = {"type": "object", "properties": parameters.get("properties", {}),
              "required": parameters.get("required", []), "additionalProperties": False}

    def register(fn: Handler) -> Handler:
        TOOLS[name] = ToolSpec(name=name, description=description, parameters=schema, handler=fn)
        return fn
    return register


def tool_specs() -> list[ToolSpec]:
    return list(TOOLS.values())


async def run_tool(name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Run one tool by name; failures become error observations, never exceptions."""
    spec = TOOLS.get(name)
    if spec is None:
        return ToolResult(summary=f"error: unknown tool '{name}'", ok=False)
    try:
        result = spec.handler(ctx, dict(args or {}))
        if inspect.isawaitable(result):
            result = await result
    except service.ApiError as exc:
        return ToolResult(summary=f"error ({exc.code}): {exc.message}", ok=False)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:3])
        return ToolResult(summary=f"error (invalid arguments): {problems}", ok=False)
    except (KeyError, ValueError, TypeError) as exc:
        return ToolResult(summary=f"error: {exc}", ok=False)
    result.summary = clip(result.summary)
    return result


def clip(text: str, limit: int = OBSERVATION_LIMIT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------- observations
def street_observation(r: StreetResponse) -> str:
    s = r.stats
    basis = ", ".join(f"{k} {v}" for k, v in r.layer_basis.items() if k in ("carriageway", "sidewalks", "buildings"))
    text = (f"Loaded {r.name} ({r.city_name}), street_id {r.street_id}: axis {s.length_m:.0f} m, "
            f"{s.existing_trees} existing trees ({s.existing_trees_crown_imputed} crowns imputed), "
            f"{s.buildings} buildings, {s.cycleway_m:.0f} m cycleway, {s.junctions} junctions; "
            f"carriageway {s.carriageway_m2:.0f} m², sidewalk {s.sidewalk_m2:.0f} m², plantable {s.plantable_m2:.0f} m². "
            f"Basis: {basis}.")
    if r.warnings:
        text += " Warnings: " + "; ".join(r.warnings[:3])
    return text


def _rule_lookup(pack: RulePack) -> dict[str, Any]:
    return {rule.id: rule for rule in pack.rules}


def scenario_observation(r: ScenarioResponse) -> str:
    s, p = r.summary, r.params
    year30 = next((y for y in r.canopy.years if y.year == 30), r.canopy.years[-1])
    rules = _rule_lookup(r.rules_used)
    top = sorted(s.failures_by_rule.items(), key=lambda kv: -kv[1])[:3]
    failing = []
    for rid, n in top:
        rule = rules.get(rid)
        if rule is None:
            failing.append(f"{rid} ×{n}")
            continue
        req = f"{rule.min_distance_m:g} m" if rule.min_distance_m is not None else "boolean"
        src = rule.source_ref or ("planning default" if rule.assumption else "no source")
        failing.append(f"{rid} ({rule.mode}, {req}, {src}) ×{n}")
    text = (f"Scenario {r.scenario_id} \"{r.label}\": {s.valid} valid, {s.conditional} conditional, {s.invalid} invalid "
            f"of {s.total} sites → {s.planted} planted ({p.spacing_m:g} m, {p.side}, {p.mode}, {service.species_short(r.species)}). "
            f"Canopy at 30 y: {year30.cover_corridor_pct:g} % of corridor, {year30.cover_street_pct:g} % of street, "
            f"{year30.sidewalk_under_crown_pct:g} % of sidewalk (existing cover {r.canopy.existing_cover_corridor_pct:g} %). ")
    text += "Top failing rules: " + ("; ".join(failing) if failing else "none") + "."
    if s.gaps:
        gap = s.gaps[0]
        text += f" {len(s.gaps)} gap(s), e.g. {gap.side} {gap.station_from_m:.0f}–{gap.station_to_m:.0f} m ({gap.reason})."
    text += " Site ids = side letter + station in m; " + "; ".join(
        f"{verdict}: {_example_ids(r, verdict)}" for verdict in ("conditional", "invalid")) + "."
    return text


def _example_ids(r: ScenarioResponse, verdict: str, limit: int = 4) -> str:
    ids = [f["properties"]["site_id"] for f in r.sites.features if f.get("properties", {}).get("verdict") == verdict]
    if not ids:
        return "none"
    more = f" (+{len(ids) - limit} more)" if len(ids) > limit else ""
    return ", ".join(ids[:limit]) + more


def site_observation(p: SiteProps, scenario_id: str) -> str:
    parts = []
    for rr in p.rules:
        mark = "?" if rr.passed is None else ("✓" if rr.passed else "✗")
        measured = "n/a" if rr.measured_m is None else f"{rr.measured_m:.2f} m"
        required = "boolean" if rr.required_m is None else f"≥ {rr.required_m:g} m"
        tag = " [planning default]" if rr.assumption else ""
        note = f" ({rr.note})" if rr.note and rr.passed is not True else ""
        parts.append(f"{mark} {rr.rule_id} ({rr.mode}): {measured} vs {required}, {rr.basis}{tag}{note}")
    edge = "n/a" if p.edge_distance_m is None else f"{p.edge_distance_m:.2f} m"
    return (f"Site {p.site_id} in {scenario_id}: station {station(p.station_m)}, {p.side} side, verdict {p.verdict}, "
            f"trunk {edge} from the carriageway edge, mature crown {p.crown_d_mature_m:g} m. Rules: " + "; ".join(parts))


def station(m: float) -> str:
    """0+240 style chainage."""
    return f"{int(m // 1000)}+{int(round(m % 1000)):03d}"


def rules_observation(pack: RulePack, overrides: dict[str, RuleOverride]) -> str:
    parts = []
    for rule in pack.rules:
        value = "boolean" if rule.min_distance_m is None else f"{rule.min_distance_m:g} m"
        src = rule.source_ref or ("planning default" if rule.assumption else "no source")
        flags = ("" if rule.enabled else ", disabled") + (", overridden" if rule.overridden else "")
        parts.append(f"{rule.id} {rule.mode} {value} ({src}{flags})")
    text = f"Rule pack {pack.id} — {pack.name}: " + "; ".join(parts) + "."
    if overrides:
        text += " Overrides pending until the next plan_trees: " + ", ".join(overrides)
    return text


def canopy_observation(c: CanopyResult, scenario_id: str) -> str:
    rows = "; ".join(f"year {y.year}: {y.cover_corridor_pct:g} % corridor, {y.cover_street_pct:g} % street, "
                     f"{y.sidewalk_under_crown_pct:g} % sidewalk, new crowns {y.new_crown_area_m2:.0f} m²" for y in c.years)
    return (f"Canopy projection for {scenario_id} (corridor {c.corridor_area_m2:.0f} m², existing cover "
            f"{c.existing_cover_corridor_pct:g} %): {rows}.")


def shade_observation(s: ShadeResult) -> str:
    n_new = sum(1 for f in s.shadows.features if f.get("properties", {}).get("kind") == "new")
    n_existing = len(s.shadows.features) - n_new
    return (f"Shade for {s.scenario_id} at {s.when}, year {s.year}: sun elevation {s.sun_elevation_deg:.1f}°, "
            f"azimuth {s.sun_azimuth_deg:.0f}°; shaded sidewalk {s.shaded_sidewalk_pct:g} %, street {s.shaded_street_pct:g} %, "
            f"corridor {s.shaded_corridor_pct:g} % ({n_new} new + {n_existing} existing shadows).")


def compare_observation(c: CompareResponse) -> str:
    rows = "; ".join(f"{r.scenario_id} \"{r.label}\": {r.planted} planted ({r.valid} valid/{r.conditional} cond./{r.invalid} inv.), "
                     f"{r.cover_corridor_pct_30:g} % corridor, {r.cover_street_pct_30:g} % street, "
                     f"{r.sidewalk_under_crown_pct_30:g} % sidewalk at 30 y" for r in c.rows)
    return f"Compared {len(c.rows)} scenarios: {rows}. Best corridor cover: {c.best_by_cover}."


# -------------------------------------------------------------------- helpers
def _scenario_id(ctx: ToolContext, args: dict[str, Any]) -> str:
    sid = args.get("scenario_id") or ctx.session.last_scenario_id
    if not sid:
        raise service.ApiError(409, "no_scenario", "No scenario yet. Run plan_trees first.")
    service.STORE.get_scenario(sid)
    return sid


def _street_id(ctx: ToolContext) -> str:
    sid = ctx.session.last_street_id
    if not sid:
        raise service.ApiError(409, "no_street", "No street loaded. Call load_street(city, query) first.")
    service.STORE.get_street(sid)
    return sid


def _find_rule_pack(rule_id: str, ctx: ToolContext) -> RulePack:
    """The pack holding `rule_id`, preferring the pack of the current scenario."""
    packs = service.load_rule_packs()
    preferred = None
    if ctx.session.last_scenario_id:
        try:
            preferred = service.scenario(ctx.session.last_scenario_id).rules_used.id
        except service.ApiError:
            preferred = None
    ordered = sorted(packs.values(), key=lambda p: p.id != preferred)
    for pack in ordered:
        if any(rule.id == rule_id for rule in pack.rules):
            return pack
    known = ", ".join(rule.id for pack in packs.values() for rule in pack.rules)
    raise service.ApiError(404, "unknown_rule", f"Unknown rule '{rule_id}'. Known rules: {known}.")


# -------------------------------------------------------------------- handlers
@tool("load_street", "Load a street's geometry and existing trees for a city. Use city id 'zurich', 'berlin', "
      "'osm' (query 'street, city') or 'demo' (offline). Must run before plan_trees.",
      {"properties": {"city": {"type": "string", "description": "city adapter id"},
                      "query": {"type": "string", "description": "street name, e.g. 'Langstrasse'"}},
       "required": ["city", "query"]})
async def load_street(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    resp = await service.load_street(StreetRequest(city=args["city"], query=args["query"]))
    ctx.session.last_street_id = resp.street_id
    return ToolResult(summary=street_observation(resp), payload_type="street", payload=resp.model_dump(),
                      ui=[{"action": "fly_to", "target": "street"}])


@tool("plan_trees", "Propose tree positions on the loaded street and evaluate every one against the rule pack. "
      "Grid mode places one candidate per spacing step; pack mode packs as many legal trees as possible.",
      {"properties": {"spacing_m": {"type": "number", "minimum": 3, "maximum": 40,
                                    "description": "trunk spacing in metres (default 8)"},
                      "side": {"type": "string", "enum": ["both", "left", "right"], "description": "default both"},
                      "species_id": {"type": "string", "description": "species id from list_species; "
                                                                      "default tilia_cordata when the planner names none"},
                      "mode": {"type": "string", "enum": ["grid", "pack"],
                               "description": "grid = one candidate per step, every verdict shown (default); "
                                              "pack = as many legal trees as fit"},
                      "offset_from_edge_m": {"type": "number", "minimum": 0.3, "maximum": 5.0,
                                             "description": "carriageway edge to trunk centre, default 1.0"},
                      "label": {"type": "string", "maxLength": 60}},
       "required": ["spacing_m"]})
async def plan_trees(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    street_id = _street_id(ctx)
    fields = {k: v for k, v in args.items() if v is not None}
    req = PlanRequest(street_id=street_id, **fields)
    resp = await asyncio.to_thread(service.plan, req, ctx.session)
    return ToolResult(summary=scenario_observation(resp), payload_type="scenario", payload=resp.model_dump(),
                      ui=[{"action": "select_scenario", "scenario_id": resp.scenario_id}, {"action": "set_year", "year": 30}])


@tool("explain_site", "Explain one proposed site: every rule with measured vs required distance and its source.",
      {"properties": {"site_id": {"type": "string", "description": "e.g. 'L0120' or 'R0240'"},
                      "scenario_id": {"type": "string", "description": "defaults to the latest scenario"}},
       "required": ["site_id"]})
async def explain_site(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    sid = _scenario_id(ctx, args)
    props = service.explain_site(sid, args["site_id"])
    return ToolResult(summary=site_observation(props, sid), payload_type="site", payload=props.model_dump(),
                      ui=[{"action": "fly_to", "target": props.site_id}])


@tool("set_rule", "Override a rule for this session (distance, must/should, enabled). Takes effect on the next plan_trees.",
      {"properties": {"rule_id": {"type": "string"},
                      "min_distance_m": {"type": "number", "minimum": 0, "maximum": 50},
                      "mode": {"type": "string", "enum": ["must", "should"]},
                      "enabled": {"type": "boolean"}},
       "required": ["rule_id"]})
async def set_rule(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rule_id = args["rule_id"]
    pack = _find_rule_pack(rule_id, ctx)
    override = RuleOverride(min_distance_m=args.get("min_distance_m"), mode=args.get("mode"), enabled=args.get("enabled"))
    if override.min_distance_m is None and override.mode is None and override.enabled is None:
        raise service.ApiError(400, "bad_request", "Give at least one of min_distance_m, mode or enabled.")
    ctx.session.rule_overrides[rule_id] = merge_override(ctx.session.rule_overrides.get(rule_id), override)
    applied = service.rule_pack(pack.id, ctx.session.rule_overrides)
    rule = _rule_lookup(applied)[rule_id]
    value = "boolean" if rule.min_distance_m is None else f"{rule.min_distance_m:g} m"
    summary = (f"Rule {rule_id} now {rule.mode}, {value}, {'enabled' if rule.enabled else 'disabled'} "
               f"(session override; original source: {rule.source_ref or 'planning default'}). "
               f"Re-run plan_trees to apply it.")
    return ToolResult(summary=summary, payload_type="rules", payload=applied.model_dump())


def merge_override(current: RuleOverride | None, new: RuleOverride) -> RuleOverride:
    """Field-wise merge so a later override of one field keeps earlier ones."""
    if current is None:
        return new
    return RuleOverride(
        min_distance_m=new.min_distance_m if new.min_distance_m is not None else current.min_distance_m,
        mode=new.mode if new.mode is not None else current.mode,
        enabled=new.enabled if new.enabled is not None else current.enabled,
    )


@tool("canopy_projection", "Canopy cover of a scenario by year (0, 5, 10, 20, 30): corridor, street and sidewalk shares.",
      {"properties": {"scenario_id": {"type": "string", "description": "defaults to the latest scenario"}}, "required": []})
async def canopy_projection(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    sid = _scenario_id(ctx, args)
    result = await asyncio.to_thread(service.canopy, CanopyRequest(scenario_id=sid, years=list(YEARS)))
    return ToolResult(summary=canopy_observation(result, sid), payload_type=None, payload=None)


@tool("shade", "Sun shadows of the trees at a given local time and tree age, with shaded shares of sidewalk and street.",
      {"properties": {"scenario_id": {"type": "string"},
                      "year": {"type": "integer", "minimum": 0, "maximum": 60, "description": "tree age, default 30"},
                      "month": {"type": "integer", "minimum": 1, "maximum": 12},
                      "day": {"type": "integer", "minimum": 1, "maximum": 31},
                      "hour": {"type": "number", "minimum": 0, "maximum": 24, "description": "local civil time"}},
       "required": []})
async def shade(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    sid = _scenario_id(ctx, args)
    fields = {k: v for k, v in args.items() if k in ("year", "month", "day", "hour") and v is not None}
    result = await asyncio.to_thread(service.shade, ShadeRequest(scenario_id=sid, **fields))
    return ToolResult(summary=shade_observation(result), payload_type="shade", payload=result.model_dump(),
                      ui=[{"action": "set_year", "year": result.year}, {"action": "show_layer", "layer": "shade", "visible": True}])


@tool("compare_scenarios", "Compare scenarios side by side: planted counts and canopy shares at 30 years. "
      "Defaults to every scenario of this session (max 6).",
      {"properties": {"scenario_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 6}}, "required": []})
async def compare_scenarios(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    ids = list(args.get("scenario_ids") or ctx.session.scenario_ids[-6:])
    if not ids:
        raise service.ApiError(409, "no_scenario", "No scenarios to compare yet. Run plan_trees first.")
    result = service.compare(CompareRequest(scenario_ids=ids))
    return ToolResult(summary=compare_observation(result), payload_type="compare", payload=result.model_dump(),
                      ui=[{"action": "open_compare", "scenario_id": result.best_by_cover}])


@tool("export_geojson", "Give the planner a download link for a scenario as GeoJSON (sites, mature crowns, axis).",
      {"properties": {"scenario_id": {"type": "string"}}, "required": []})
async def export_geojson(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    sid = _scenario_id(ctx, args)
    url = f"/api/export/{sid}.geojson"
    return ToolResult(summary=f"GeoJSON export for scenario {sid} is ready at {url} (sites with verdicts and rules, "
                              f"mature crown polygons, street axis).", payload_type="export", payload={"url": url, "scenario_id": sid})


@tool("list_species", "List the available tree species with size class and mature crown diameter.",
      {"properties": {}, "required": []})
async def list_species(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    rows = "; ".join(f"{sp.id}: {service.species_short(sp)} ({sp.size_class}, crown {sp.mature_crown_d_m:g} m)"
                     for sp in service.load_species().values())
    return ToolResult(summary=f"Species: {rows}.")


@tool("list_rules", "List the rule pack in force: each rule's mode, distance and source, plus session overrides.",
      {"properties": {}, "required": []})
async def list_rules(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pack_id = PlanRequest.model_fields["rule_pack_id"].default
    if ctx.session.last_scenario_id:
        try:
            pack_id = service.scenario(ctx.session.last_scenario_id).rules_used.id
        except service.ApiError:
            pass
    pack = service.rule_pack(pack_id, ctx.session.rule_overrides)
    return ToolResult(summary=rules_observation(pack, ctx.session.rule_overrides), payload_type="rules", payload=pack.model_dump())


@tool("fly_to", "Move the map to the street, to a site id, or to [lon, lat].",
      {"properties": {"target": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}],
                                 "description": "'street', a site id such as 'R0240', or [lon, lat]"},
                      "zoom": {"type": "number", "minimum": 10, "maximum": 21}},
       "required": ["target"]})
async def fly_to(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    event = {"action": "fly_to", "target": args["target"]}
    if args.get("zoom") is not None:
        event["zoom"] = float(args["zoom"])
    return ToolResult(summary="ok", ui=[event])


@tool("set_year", "Set the map's year slider (0–30) so crowns show their size at that age.",
      {"properties": {"year": {"type": "integer", "minimum": 0, "maximum": 30}}, "required": ["year"]})
async def set_year(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    year = max(0, min(30, int(args["year"])))
    return ToolResult(summary="ok", ui=[{"action": "set_year", "year": year}])


@tool("show_layer", "Show or hide a map layer.",
      {"properties": {"layer": {"type": "string", "enum": LAYERS}, "visible": {"type": "boolean"}},
       "required": ["layer", "visible"]})
async def show_layer(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    if args["layer"] not in LAYERS:
        raise service.ApiError(400, "bad_request", f"Unknown layer '{args['layer']}'. Known: {', '.join(LAYERS)}.")
    return ToolResult(summary="ok", ui=[{"action": "show_layer", "layer": args["layer"], "visible": bool(args["visible"])}])
