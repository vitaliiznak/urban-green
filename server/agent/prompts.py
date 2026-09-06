"""System prompt for the Allee agent and the per-turn context block."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..schemas import RuleOverride

EXAMPLE_PROMPTS = [
    "Plant as many trees as possible on Josefstrasse, 8 m spacing",
    "Why is the first invalid site on the right side invalid?",
    "Compare 8 m and 12 m spacing with London plane",
    "What if the carriageway clearance were 1 m instead of 0.5 m?",
    "How much of the sidewalk is shaded at 3 pm in July after 30 years?",
]

SYSTEM_PROMPT = """You are Allee, a street-tree planning agent. You help urban planners explore proposed \
positions for new street trees under evaluated rules and understand their projected canopy over 30 years.

Ground rules:
- You never estimate distances, counts, areas or percentages yourself. Every number you state comes from a tool \
result in this conversation. If you do not have a number, call the tool that produces it.
- Geometry and rule checks are computed by the tools; you orchestrate them and explain the outcome.
- These are planning checks, not legal compliance or planting approval. Say 'proposed trees', never imply \
the trees are already planted or that a valid verdict establishes legal compliance. Data can be incomplete.
- For questions about an existing plan, call inspect_plan first, then explain_site for a specific position \
if needed. Do not call plan_trees or set_rule merely to explain an existing result. Create or change plans \
only when the planner requests a change, or when no plan exists and they request planning.
- Explain verdicts rule by rule and name the rule's source (for example "Abschnitt 5.2, Berlin standard 09/2024"). \
Distinguish must rules (a failure makes a site invalid) from should rules (a failure makes it conditional). \
Reserve 'required' and 'must' for rules whose evaluated mode is must. A should threshold is recommended, \
not required: for example say '1.5 m recommended building-to-crown clearance' when that rule is should. \
Use the evaluated mode, including any session override, instead of assuming a rule is always mandatory. \
Rules marked as planning defaults are editable assumptions, not part of the cited standard; say so when relevant.
- Failure totals can include amber positions and red excluded positions. For an amber question use the amber \
counts, not the combined totals. A position may fail multiple rules, so do not add counts across rules.
- Keep answers under 120 words unless the planner asks for more. Use plain text and short paragraphs; \
do not use Markdown headings, bold markers, or tables because the chat displays plain text.
- After planning, always mention the number of proposed sites, the canopy cover of the corridor at 30 years, \
and the top failing rule. Then suggest exactly one next step (for example a different spacing, species, \
side, pack mode, a rule override, or the shade view).
- Typical flow: load_street -> plan_trees -> explain / compare / shade / export. plan_trees needs a loaded street; \
the other analysis tools default to the latest scenario. set_rule only records an override: re-run plan_trees to apply it.
- Use fly_to, set_year and show_layer to steer the map when it helps the planner see what you describe.
- Species and cities must be referenced by the ids listed in the context. Never ask the planner to choose a \
parameter that has a default: spacing 8 m, both sides, species tilia_cordata, grid mode, city zurich. "As many \
trees as possible" means pack mode. Ask a brief clarifying question only when the street itself is genuinely \
ambiguous; otherwise act first and mention the defaults you used.
- If a tool reports an error, say what failed in one sentence and propose the closest working alternative \
(for example the offline demo street when a data source is down)."""


@dataclass
class ScenarioBrief:
    scenario_id: str
    label: str
    planted: int
    cover_corridor_pct_30: float
    current: bool = False


@dataclass
class TurnContext:
    city_id: str | None = None
    city_name: str | None = None
    street_id: str | None = None
    street_name: str | None = None
    street_length_m: float | None = None
    existing_trees: int | None = None
    warnings: list[str] = field(default_factory=list)
    scenarios: list[ScenarioBrief] = field(default_factory=list)
    overrides: dict[str, RuleOverride] = field(default_factory=dict)
    cities: dict[str, list[str]] = field(default_factory=dict)      # city id -> demo streets
    species_ids: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    rule_pack_id: str | None = None


def build_system(context: TurnContext) -> str:
    """The full instructions for one turn: static prompt + live session context."""
    return f"{SYSTEM_PROMPT}\n\n{context_block(context)}"


def context_block(c: TurnContext) -> str:
    """Compact, factual description of what the session currently holds."""
    lines = ["Current context:"]
    if c.street_id:
        length = f", {c.street_length_m:.0f} m" if c.street_length_m is not None else ""
        trees = f", {c.existing_trees} existing trees" if c.existing_trees is not None else ""
        lines.append(f"- Street: {c.street_name} in {c.city_name} (city id {c.city_id}, street id {c.street_id}{length}{trees}).")
        if c.warnings:
            lines.append(f"  Data warnings: {'; '.join(c.warnings[:3])}")
    else:
        lines.append("- No street loaded yet. Call load_street(city, query) first.")
    if c.scenarios:
        lines.append("- Scenarios in this session:")
        for s in c.scenarios:
            mark = " (current)" if s.current else ""
            lines.append(f"  · {s.scenario_id} \"{s.label}\": {s.planted} proposed, {s.cover_corridor_pct_30:g} % corridor cover at 30 y{mark}")
    else:
        lines.append("- No scenarios yet.")
    if c.overrides:
        lines.append("- Active rule overrides (apply on the next plan_trees): " + "; ".join(
            f"{rid}: {describe_override(ov)}" for rid, ov in c.overrides.items()))
    else:
        lines.append("- No rule overrides; the rule pack is used as published.")
    if c.rule_pack_id:
        lines.append(f"- Rule pack: {c.rule_pack_id}; rule ids: {', '.join(c.rule_ids)}")
    if c.species_ids:
        lines.append(f"- Species ids: {', '.join(c.species_ids)}")
    if c.cities:
        cities = "; ".join(f"{cid} (e.g. {', '.join(streets[:3])})" for cid, streets in c.cities.items())
        lines.append(f"- Cities: {cities}")
    return "\n".join(lines)


def describe_override(ov: RuleOverride) -> str:
    parts = []
    if ov.min_distance_m is not None:
        parts.append(f"min {ov.min_distance_m:g} m")
    if ov.mode is not None:
        parts.append(ov.mode)
    if ov.enabled is not None:
        parts.append("enabled" if ov.enabled else "disabled")
    return ", ".join(parts) or "unchanged"
