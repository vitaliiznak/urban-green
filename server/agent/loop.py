"""One agent turn: context, provider call, tool dispatch and the SSE stream.

`run_turn` drives a provider and reports everything through `emit(event, data)`;
`sse_stream` wraps it for HTTP: events are queued and written out as
`event: <type>\\ndata: <json>\\n\\n`, with a `: ping` comment every 15 s while
the model is quiet.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable

from .. import service
from ..ratelimit import LIMITER
from ..schemas import AgentRequest
from ..session import Session, remember_message
from .prompts import ScenarioBrief, TurnContext, build_system
from .providers import Provider, ProviderError
from .tools import ToolContext, run_tool, tool_specs

log = logging.getLogger("canopy.agent")

MAX_TOOL_CALLS = 10
HEARTBEAT_S = 15.0
BUDGET_MESSAGE = ("Tool budget for this turn is exhausted (10 calls). Answer the planner with the results you already "
                  "have and suggest the next step.")

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


def turn_context(session: Session) -> TurnContext:
    """Describe the session for the system prompt using only what the store holds."""
    ctx = TurnContext()
    ctx.cities = {adapter.info.id: list(adapter.info.demo_streets) for adapter in service.ADAPTERS.values()}
    ctx.species_ids = list(service.load_species())
    packs = service.load_rule_packs()
    pack_id = service.PlanParams().rule_pack_id
    if session.last_street_id:
        try:
            street = service.STORE.get_street_response(session.last_street_id)
            ctx.city_id, ctx.city_name = street.city, street.city_name
            ctx.street_id, ctx.street_name = street.street_id, street.name
            ctx.street_length_m, ctx.existing_trees = street.length_m, street.stats.existing_trees
            ctx.warnings = list(street.warnings)
        except service.ApiError:
            session.last_street_id = None
    for sid in session.scenario_ids[-6:]:
        try:
            resp = service.scenario(sid)
        except service.ApiError:
            continue
        year30 = next((y for y in resp.canopy.years if y.year == 30), resp.canopy.years[-1])
        ctx.scenarios.append(ScenarioBrief(sid, resp.label, resp.summary.planted, year30.cover_corridor_pct,
                                           current=(sid == session.last_scenario_id)))
        pack_id = resp.rules_used.id
    ctx.overrides = dict(session.rule_overrides)
    if pack_id in packs:
        ctx.rule_pack_id = pack_id
        ctx.rule_ids = [rule.id for rule in packs[pack_id].rules]
    return ctx


def adopt_request_context(req: AgentRequest, session: Session) -> None:
    """The UI's current street/scenario become the agent's defaults when they exist."""
    if req.street_id and req.street_id in service.STORE.streets:
        session.last_street_id = req.street_id
    if req.scenario_id and req.scenario_id in service.STORE.scenarios:
        session.last_scenario_id = req.scenario_id
        if req.scenario_id not in session.scenario_ids:
            session.scenario_ids.append(req.scenario_id)


async def run_turn(req: AgentRequest, session: Session, provider: Provider, emit: Emit) -> None:
    """Run one user message through the provider, dispatching tools and emitting events."""
    adopt_request_context(req, session)
    system = build_system(turn_context(session))
    tool_ctx = ToolContext(session=session)
    usage = {"input_tokens": 0, "output_tokens": 0}
    calls = 0
    produced = False

    async def on_event(kind: str, data: dict[str, Any]) -> Any:
        nonlocal calls, produced
        if kind == "text":
            produced = True
            await emit("text", {"delta": data["delta"]})
        elif kind == "usage":
            usage["input_tokens"] += int(data.get("input_tokens", 0))
            usage["output_tokens"] += int(data.get("output_tokens", 0))
        elif kind == "tool_call":
            produced = True
            calls += 1
            if calls > MAX_TOOL_CALLS:
                return BUDGET_MESSAGE
            await emit("tool", {"id": data["id"], "name": data["name"], "args": data["args"]})
            result = await run_tool(data["name"], data["args"], tool_ctx)
            for ui_event in result.ui:
                await emit("ui", ui_event)
            await emit("result", {"id": data["id"], "name": data["name"], "ok": result.ok, "summary": result.summary,
                                  "payload_type": result.payload_type, "payload": result.payload})
            return result.summary
        return None

    user_item = {"role": "user", "content": req.message}
    history = [*session.history, user_item]
    try:
        new_items = await provider.run_turn(system, history, tool_specs(), on_event)
    except ProviderError as exc:
        log.warning("agent turn failed: %s", exc)
        await emit("error", {"message": str(exc)})
        return
    except Exception as exc:  # noqa: BLE001 - the stream must always end with an event
        log.exception("agent turn crashed")
        await emit("error", {"message": f"Agent failed: {exc.__class__.__name__}: {exc}"})
        return
    remember_message(session, [user_item, *new_items])
    if not produced:
        await emit("error", {"message": "The model returned no answer. Please try again."})
    await emit("done", {"usage": usage, "turns_remaining_hour": LIMITER.remaining("agent_hour", session.id)})


def format_sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


async def sse_stream(req: AgentRequest, session: Session, provider: Provider) -> AsyncIterator[str]:
    """Server-sent events for one turn, with heartbeats while the model thinks."""
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()

    async def emit(event: str, data: dict[str, Any]) -> None:
        await queue.put((event, data))

    async def worker() -> None:
        try:
            await run_turn(req, session, provider, emit)
        finally:
            await queue.put(None)

    task = asyncio.create_task(worker())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if item is None:
                break
            yield format_sse(*item)
    finally:
        if not task.done():
            task.cancel()
