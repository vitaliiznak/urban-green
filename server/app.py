"""Urban Green HTTP API and static front-end (FastAPI).

Everything JSON lives under /api; errors use one envelope
`{"error": {"message": ..., "code": ...}}`; the single-page app in `web/`
is served at `/` and `/static`. `.env` is loaded at import so one key
turns the agent on.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

ROOT = Path(os.environ.get("CANOPY_ROOT") or Path(__file__).resolve().parent.parent)
load_dotenv(ROOT / ".env")

from . import service  # noqa: E402  (after dotenv so provider selection sees the keys)
from .agent import providers  # noqa: E402
from .agent.loop import sse_stream  # noqa: E402
from .agent.tools import merge_override  # noqa: E402
from .ratelimit import LIMITER, RateLimited  # noqa: E402
from .schemas import (  # noqa: E402
    AgentRequest,
    CanopyRequest,
    CanopyResult,
    CompareRequest,
    CompareResponse,
    ConfigResponse,
    PlanRequest,
    QuotaInfo,
    RuleOverride,
    ScenarioResponse,
    ShadeRequest,
    ShadeResult,
    SiteProps,
    StreetRequest,
    StreetResponse,
    TemperatureRequest,
    TemperatureResult,
)
from .session import SESSION_HEADER, Session, current_session, session_middleware  # noqa: E402

log = logging.getLogger("canopy")
WEB_DIR = ROOT / "web"

app = FastAPI(
    title="Urban Green",
    version=service.VERSION,
    description="Agentic street-tree planner: every legal tree position under a cited rule pack, "
                "30-year canopy projection, shade, and an agent that drives the same tools.",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
    expose_headers=[SESSION_HEADER, "Content-Disposition"],
)
app.middleware("http")(session_middleware)


# ------------------------------------------------------------------ errors
def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "code": code}}, status_code=status, headers=headers)


_HTTP_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
               405: "method_not_allowed", 409: "conflict", 422: "validation_error", 429: "rate_limited"}


@app.exception_handler(service.ApiError)
async def _api_error(_request: Request, exc: service.ApiError) -> JSONResponse:
    return error_response(exc.status, exc.code, exc.message)


@app.exception_handler(RateLimited)
async def _rate_limited(_request: Request, exc: RateLimited) -> JSONResponse:
    return error_response(429, "rate_limited", exc.message, {"Retry-After": str(exc.retry_after)})


@app.exception_handler(HTTPException)
async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return error_response(exc.status_code, _HTTP_CODES.get(exc.status_code, "error"), detail, exc.headers)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()[:4])
    return error_response(422, "validation_error", f"Invalid request: {problems}")


@app.exception_handler(Exception)
async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return error_response(500, "internal_error", f"{exc.__class__.__name__}: {exc}")


# ------------------------------------------------------------------ meta
@app.get("/api/health")
def health() -> dict[str, Any]:
    info = providers.agent_info()
    return {"ok": True, "version": service.VERSION,
            "agent": {"enabled": info.enabled, "provider": info.provider, "model": info.model}}


@app.get("/api/config", response_model=ConfigResponse)
def config() -> ConfigResponse:
    return service.config()


@app.get("/api/session", response_model=QuotaInfo)
def session_info(session: Session = Depends(current_session)) -> QuotaInfo:
    return QuotaInfo(session_id=session.id,
                     turns_remaining_hour=LIMITER.remaining("agent_hour", session.id),
                     turns_remaining_day=LIMITER.remaining("agent_day", session.id))


# ------------------------------------------------------------------ streets & planning
@app.post("/api/street", response_model=StreetResponse)
async def street(req: StreetRequest, session: Session = Depends(current_session)) -> StreetResponse:
    LIMITER.consume("street_hour", session.id)
    resp = await service.load_street(req)
    session.last_street_id = resp.street_id
    return resp


@app.post("/api/plan", response_model=ScenarioResponse)
def plan(req: PlanRequest, session: Session = Depends(current_session)) -> ScenarioResponse:
    LIMITER.consume("plan_hour", session.id)
    return service.plan(req, session)


@app.post("/api/canopy", response_model=CanopyResult)
def canopy(req: CanopyRequest) -> CanopyResult:
    return service.canopy(req)


@app.post("/api/shade", response_model=ShadeResult)
def shade(req: ShadeRequest) -> ShadeResult:
    return service.shade(req)


@app.post("/api/compare", response_model=CompareResponse)
def compare(req: CompareRequest) -> CompareResponse:
    return service.compare(req)


@app.post("/api/temperature", response_model=TemperatureResult)
def temperature(req: TemperatureRequest) -> TemperatureResult:
    return service.temperature(req)


@app.get("/api/scenarios/{scenario_id}", response_model=ScenarioResponse)
def scenario(scenario_id: str) -> ScenarioResponse:
    return service.scenario(scenario_id)


@app.get("/api/scenarios/{scenario_id}/sites/{site_id}", response_model=SiteProps)
def site(scenario_id: str, site_id: str) -> SiteProps:
    return service.explain_site(scenario_id, site_id)


@app.get("/api/export/{scenario_id}.geojson")
def export(scenario_id: str) -> JSONResponse:
    collection = service.export_geojson(scenario_id)
    return JSONResponse(collection, media_type="application/geo+json",
                        headers={"Content-Disposition": f'attachment; filename="canopy-{scenario_id}.geojson"'})


# ------------------------------------------------------------------ rules
def _rule_state(overrides: dict[str, RuleOverride]) -> dict[str, Any]:
    packs = [service.rule_pack(pack_id, overrides) for pack_id in service.load_rule_packs()]
    return {"packs": [pack.model_dump() for pack in packs],
            "overrides": {rid: ov.model_dump() for rid, ov in overrides.items()}}


@app.get("/api/rules")
def rules(session: Session = Depends(current_session)) -> dict[str, Any]:
    return _rule_state(dict(session.rule_overrides))


@app.put("/api/rules/overrides")
def replace_overrides(overrides: dict[str, RuleOverride],
                      session: Session = Depends(current_session)) -> dict[str, Any]:
    """Restore an exact override snapshot without exposing a partially restored pack."""
    rule_ids = {rule.id for pack in service.load_rule_packs().values() for rule in pack.rules}
    unknown = next((rule_id for rule_id in overrides if rule_id not in rule_ids), None)
    if unknown is not None:
        raise service.ApiError(404, "unknown_rule", f"Unknown rule '{unknown}'.")
    replacement = dict(overrides)
    response = _rule_state(replacement)
    session.rule_overrides = replacement
    return response


@app.put("/api/rules/{pack_id}/{rule_id}")
def put_rule(pack_id: str, rule_id: str, override: RuleOverride,
             session: Session = Depends(current_session)) -> dict[str, Any]:
    pack = service.rule_pack(pack_id)
    if not any(rule.id == rule_id for rule in pack.rules):
        raise service.ApiError(404, "unknown_rule", f"Rule pack {pack_id} has no rule '{rule_id}'.")
    session.rule_overrides[rule_id] = merge_override(session.rule_overrides.get(rule_id), override)
    return {"pack": service.rule_pack(pack_id, session.rule_overrides).model_dump()}


@app.delete("/api/rules/overrides")
def clear_overrides(session: Session = Depends(current_session)) -> dict[str, Any]:
    session.rule_overrides.clear()
    return {"ok": True}


# ------------------------------------------------------------------ agent
@app.post("/api/agent")
async def agent(req: AgentRequest, session: Session = Depends(current_session)) -> StreamingResponse:
    provider = providers.get_provider()
    if provider is None:
        raise service.ApiError(503, "agent_disabled", "Agent offline on this server (no OPENAI_API_KEY or "
                               "ANTHROPIC_API_KEY). The plan panel and the HTTP API do everything the agent does.")
    LIMITER.consume_agent(session.id)
    return StreamingResponse(
        sse_stream(req, session, provider), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", SESSION_HEADER: session.id},
    )


# ------------------------------------------------------------------ static front-end
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    index_html = WEB_DIR / "index.html"
    if not index_html.is_file():
        raise service.ApiError(404, "not_found", "web/index.html is missing; the API is available under /api and /docs.")
    return FileResponse(index_html, media_type="text/html")


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# ------------------------------------------------------------------ cache pre-warm
PREWARM = [("zurich", "Langstrasse"), ("zurich", "Josefstrasse")]


async def _prewarm() -> None:
    """Fetch the demo streets once so the first planners hit warm caches (disk cache keeps them across restarts)."""
    import asyncio

    for city, query in PREWARM:
        try:
            resp = await service.load_street(StreetRequest(city=city, query=query))
            log.info("prewarmed %s/%s (%d m)", city, query, int(resp.length_m))
        except Exception as exc:  # upstream hiccups must never affect startup
            log.warning("prewarm %s/%s failed: %s", city, query, exc)
        await asyncio.sleep(1.0)


@app.on_event("startup")
async def _start_prewarm() -> None:
    import asyncio

    if os.environ.get("CANOPY_PREWARM", "1") != "0":
        asyncio.create_task(_prewarm())
