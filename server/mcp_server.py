"""Allee tools over the Model Context Protocol (stdio).

    python -m server.mcp_server

Exposes the same handlers as the chat agent so any MCP client (Claude
Desktop, Cursor, an IDE) can load a street, plan, explain, compare and export
without the web UI. One in-process session holds the rule overrides and the
latest street/scenario between calls.
"""
from __future__ import annotations

import json
from typing import Any, Optional

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError:  # mcp >= 2: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as FastMCP

from . import service
from .agent.tools import ToolContext, run_tool
from .session import Session

mcp = FastMCP("allee")
_CTX = ToolContext(session=Session(id="mcp-session"))


async def _call(name: str, **args: Any) -> str:
    result = await run_tool(name, {k: v for k, v in args.items() if v is not None}, _CTX)
    return result.summary


@mcp.tool()
async def load_street(city: str, query: str) -> str:
    """Load a street (city id: zurich, berlin, osm with 'street, city', or demo) and report its geometry and trees."""
    return await _call("load_street", city=city, query=query)


@mcp.tool()
async def plan_trees(spacing_m: float = 8.0, side: str = "both", species_id: str = "tilia_cordata", mode: str = "grid",
                     offset_from_edge_m: Optional[float] = None, label: Optional[str] = None) -> str:
    """Propose and evaluate tree positions on the loaded street; returns counts, top failing rules and 30-year canopy."""
    return await _call("plan_trees", spacing_m=spacing_m, side=side, species_id=species_id, mode=mode,
                       offset_from_edge_m=offset_from_edge_m, label=label)


@mcp.tool()
async def explain_site(site_id: str, scenario_id: Optional[str] = None) -> str:
    """Rule-by-rule explanation of one site (measured vs required, source)."""
    return await _call("explain_site", site_id=site_id, scenario_id=scenario_id)


@mcp.tool()
async def set_rule(rule_id: str, min_distance_m: Optional[float] = None, mode: Optional[str] = None,
                   enabled: Optional[bool] = None) -> str:
    """Override a rule (distance, must/should, enabled) for this session; re-run plan_trees to apply."""
    return await _call("set_rule", rule_id=rule_id, min_distance_m=min_distance_m, mode=mode, enabled=enabled)


@mcp.tool()
async def canopy_projection(scenario_id: Optional[str] = None) -> str:
    """Canopy cover by year for a scenario (defaults to the latest)."""
    return await _call("canopy_projection", scenario_id=scenario_id)


@mcp.tool()
async def shade(scenario_id: Optional[str] = None, year: int = 30, month: int = 7, day: int = 15, hour: float = 15.0) -> str:
    """Sun shadows at a local time and tree age with shaded shares of sidewalk and street."""
    return await _call("shade", scenario_id=scenario_id, year=year, month=month, day=day, hour=hour)


@mcp.tool()
async def compare_scenarios(scenario_ids: Optional[list[str]] = None) -> str:
    """Compare scenarios side by side (defaults to every scenario of this session)."""
    return await _call("compare_scenarios", scenario_ids=scenario_ids)


@mcp.tool()
async def export_geojson(scenario_id: Optional[str] = None) -> str:
    """The scenario as a GeoJSON FeatureCollection string: sites, mature crowns, axis."""
    sid = scenario_id or _CTX.session.last_scenario_id
    if not sid:
        return "error (no_scenario): No scenario yet. Run plan_trees first."
    try:
        return json.dumps(service.export_geojson(sid), ensure_ascii=False)
    except service.ApiError as exc:
        return f"error ({exc.code}): {exc.message}"


@mcp.tool()
async def list_species() -> str:
    """Available species with size class and mature crown diameter."""
    return await _call("list_species")


@mcp.tool()
async def list_rules() -> str:
    """The rule pack in force with each rule's mode, distance and source."""
    return await _call("list_rules")


def main() -> None:
    """Serve over stdio."""
    mcp.run("stdio")


if __name__ == "__main__":
    main()
