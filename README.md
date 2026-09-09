# Urban Green

**Name a street. Explore proposed tree positions under a cited rule pack and estimate their canopy over 30 years.**

Urban Green is an agentic street-tree planner built as an overnight MVP for the goNEON
"Platform & Ecosystem Owner" exercise. A planner picks a city and a street (or draws one),
the engine proposes tree positions at a chosen spacing, checks each one against a planting
standard whose every distance carries its source, and projects crown growth, canopy cover
and one afternoon's shade. A chat agent drives exactly the same tools and explains the result
in planning terms. The same tools are exposed over HTTP, streamed server-sent events, and MCP,
so other agents and other teams can build on it.

Runs as one container. Works without any LLM key (the plan panel and the API do everything
the agent does; the chat panel just says the agent is offline).

## Quick start

```bash
# Python 3.11+ and uv (or pip)
uv venv .venv && uv pip install -p .venv/bin/python -e ".[dev]"
cp .env.example .env            # add OPENAI_API_KEY or ANTHROPIC_API_KEY to enable the agent
./run.sh                        # http://localhost:8000
.venv/bin/python -m pytest -q   # unit + API tests, no network needed
```

Open http://localhost:8000. Choose a city, then click a street on the map, enter a
street name, select an example, or use **Draw street** to mark a route. No street is loaded automatically.
Map selection snaps to an actual OpenStreetMap road within 30 m and creates a plan.
Use **Select street on map** to pick another street. Escape cancels selection;
at a distant zoom, the first click zooms in. The offline demo uses name/drawing only.
Drawing supports **Undo last point**, **Finish drawing** and **Cancel drawing**.
Follow **Choose a street → Adjust the trees → Review your plan → Compare temperatures**.
Expand the street selector to change location; adjust spacing, street sides or species, then
apply your changes to create another plan. The result explains the marker colors: green means no evaluated rule failed,
amber means a recommendation is not met, and red means a required rule failed.
Amber positions remain in the proposed count and canopy estimate; red positions
are excluded. The reason list separates amber and excluded counts and opens an
example position with its measured and recommended clearances. An independent
geometry safeguard excludes positions that coincide with an existing mapped trunk;
it uses numerical tolerance, not a new planting-clearance standard. A mandatory
10 m exclusion around mapped junctions and crossings also applies, even with
editable rules disabled. It checks both distance along the street and trunk
distance, preventing a wide crossing from pushing a candidate sideways past the
check. This is a conservative planning default, not a surveyed sight triangle;
unmapped crossings still need site review. Excluded positions add no canopy,
shade or cooling. Use the map's
year slider and shade toggle to explore growth, or compare at least two plans for
the same street and download GeoJSON. The comparison keeps the selected plan and
up to five recent alternatives, with further metrics under an expandable section.
API and assistant comparisons label streets and sources and do not rank different
street contexts against one another.

More planting options, planting rules and map layers expand when needed. Open
**Ask the planning assistant** for chat. Explanation requests use a read-only
`inspect_plan` tool, with actions available in an expandable trace. On mobile, **Show this plan on the map**
and **Back to settings** move between planning and the map. Plans are kept in the
page until refresh; data sources and estimation warnings remain available under
the selected street. The existing-tree card shows the actual source and count.
For Zürich, a cached OSM fallback is retried against the city register once when
loading a named street. If the register is still unavailable, the fallback stays
explicit and **Retry Zürich tree register** reloads the same street line and
creates a new plan. Distance from existing trees is currently a 5 m recommendation,
so conflicting candidates remain amber rather than being excluded.

**Compare temperatures** is a manual, exploratory air-temperature comparison of
existing trees versus the selected plan. Enter a reference or measured temperature
(the default 30°C is illustrative), tree age, and summer time/date. The card shows
the model's central value and sensitivity range, plus a separate before/after
tree-shade comparison. **Show this plan's shade on the map** applies that year and
time. Editing inputs or selecting another plan clears the previous comparison.
The model transfers a published Tacoma canopy association; it is not calibrated
for the selected street and is not a local forecast or pavement-temperature model.
See [temperature method and limitations](docs/temperature-method.md).

In **Adjust the trees**, species are listed from narrower to wider crowns. The
**Mature crown diameter** slider also lets you test a custom 2–25 m crown. This
is a scenario assumption; it does not change the species' height or guarantee
that a real tree will stay that size. Choosing a species restores its usual
crown diameter. Apply changes to recalculate crown clearances, growth, canopy,
shade and temperature estimates. Each saved plan retains its own size, shown
in the plan comparison; junction exclusions still apply to small trees.

Try the API: `BASE=http://localhost:8000 ./examples/curl.sh` (needs `jq`).
OpenAPI UI: http://localhost:8000/docs.

## What planners can do

| Action | Where | What happens |
|---|---|---|
| Load a street | click a street, city + name, or **Draw street** | adapter fetches carriageway, sidewalks, buildings, cycle paths, junctions, existing trees for the corridor (axis ± 15 m) |
| Plan trees | Plan card or the agent | candidate trunks at the chosen spacing, offset from the carriageway edge; every candidate evaluated against the rule pack; **valid / conditional / invalid** |
| Pack | mode = pack | greedy walk along the street: trees slide to the nearest legal position; gaps are reported with the rule that caused them |
| Grow | year slider 0–30 | crowns grow with a species curve; canopy % of corridor, of street space, of sidewalk |
| Shade | Shade toggle | ground shadow of each crown for 15 July, 15:00 local (editable via API); shaded sidewalk % |
| Edit a rule | Rules card | change a distance or must/should for your session; re-plan; overrides are labelled |
| Compare | Scenarios card | table across scenarios: planted count, 30-year cover, sidewalk under crown |
| Export | Export GeoJSON | sites with verdicts and rule results, mature crown polygons, the axis |

## Data and rules (what is measured, what is estimated)

| City | Street geometry | Existing trees | Basemap |
|---|---|---|---|
| Zürich | Canton cadastre (Amtliche Vermessung): carriageway, sidewalk, building polygons — *measured* | City tree cadastre WFS when it answers; otherwise OpenStreetMap trees with imputed crowns (a warning says so) | swisstopo SWISSIMAGE / grey map |
| Berlin | OpenStreetMap: carriageway width from tags or highway class, sidewalks 2.5 m — *estimated*; buildings measured | Berlin tree cadastre (Baumbestand, 434,765 street trees) with measured crown diameters | Berlin true orthophoto 2024 / CARTO |
| Anywhere (OSM) | OpenStreetMap, estimated | OpenStreetMap trees, imputed crowns | CARTO / OSM |
| Demo (offline) | synthetic 400 m street | synthetic | swisstopo grey map |

The UI's status strip shows the attribution and *measured / estimated* basis per layer, and
every rule result carries the same basis, so a planner always knows when a 0.4 m distance was
measured against a cadastre polygon and when it was measured against an estimated width.

**Rule pack** `rules/berlin_strassenbaeume_2024.json` — Berlin street-tree planting standard
(09/2024), section 5.2. Verified values: trunk ≥ 0.5 m from the carriageway edge (must),
≥ 0.25 m from a cycle path (should), mature crown ≥ 1.5 m from multi-storey buildings (should).
Four further rules (plantable surface, 5 m to existing trees, 10 m to junctions, 1.5 m remaining
sidewalk passage) are marked `assumption: true`: editable planning defaults, shown as
"planning default" in the UI, not part of the standard.

**Species** `rules/species.json` — twelve street species with mature crown diameter, height and
a growth time constant; typical literature values, approximate and editable. Trees without a
measured crown get a genus-based estimate (flagged `crown_imputed`).

## Architecture

```
web/            one static MapLibre page (no build step)
server/
  adapters/     one class per data source -> StreetContext (Shapely, metric CRS)
                zurich.py  berlin.py  osm.py (anywhere)  demo (synthetic)
  engine/       deterministic geometry: sites.py rules.py canopy.py shade.py species.py
  service.py    use-cases shared by the three fronts, in-memory store (TTL 3 h)
  app.py        FastAPI: /api/* JSON, /api/agent SSE, static UI, /docs
  agent/        tool registry + loop; providers: OpenAI (Responses API), Anthropic
  mcp_server.py the same tools over MCP (stdio)
rules/          rule packs and the species table (data with sources, never constants)
docs/CONTRACT.md  the binding module contract every part was built against
```

Principles: the LLM orchestrates and explains, it never computes geometry; tools are small,
typed and deterministic; observations sent to the model are short text, never GeoJSON;
rules are data with attribution; every layer knows whether it is measured or estimated.

## HTTP API (all JSON, all under `/api`)

```
GET  /api/config                      cities, rule packs, species, defaults, agent status
POST /api/street                      {city, query} or {city, line:[[lon,lat],...]} -> StreetResponse (GeoJSON layers)
POST /api/plan                        {street_id, spacing_m, side, species_id, mode, ...} -> ScenarioResponse
POST /api/canopy                      {scenario_id, years?}
POST /api/shade                       {scenario_id, year, month, day, hour}
POST /api/temperature                 {scenario_id, reference_air_c, year, month, day, hour}
POST /api/compare                     {scenario_ids: [...]}
GET  /api/scenarios/{id}              GET /api/scenarios/{id}/sites/{site_id}
GET  /api/export/{id}.geojson
GET  /api/rules   PUT /api/rules/{pack}/{rule}   DELETE /api/rules/overrides
POST /api/agent                       {message} -> text/event-stream (text, tool, result, ui, done, error)
```

Send an `X-Session-Id` header (any UUID) to keep rule overrides, scenarios and agent history
together. Rate limits per session and per day are set with `AGENT_TURNS_PER_HOUR` and
`AGENT_TURNS_PER_DAY`.

## MCP

The same tools for Claude Desktop, Claude Code or any MCP client:

```json
{ "mcpServers": { "urban-green": { "command": "/path/to/.venv/bin/python", "args": ["-m", "server.mcp_server"], "cwd": "/path/to/urban-green" } } } }
```

Tools: `load_street`, `plan_trees`, `inspect_plan`, `explain_site`, `set_rule`, `canopy_projection`, `shade`,
`compare_scenarios`, `export_geojson`, `list_species`, `list_rules`.

## Deploy (Fly.io, one container)

```bash
fly launch --no-deploy --copy-config --name urban-green   # uses fly.toml
fly secrets set OPENAI_API_KEY=...                            # or ANTHROPIC_API_KEY
fly deploy
```

Any Docker host works: `docker build -t urban-green . && docker run -p 8000:8000 --env-file .env urban-green`.
Keys stay server-side; the browser never sees them. One shared-CPU machine handles a
hackathon room: planning a 1.3 km street takes well under a second and upstream responses
are cached for 15 minutes.

## How to build on it

- **Add a city:** subclass `CityAdapter` in `server/adapters/`, return a `StreetContext`
  (see `berlin.py`: about 150 lines, one WFS call plus the shared OSM helpers), register it in
  `ADAPTERS`. The UI, agent and MCP pick it up from `/api/config`.
- **Add a rule pack:** drop a JSON file in `rules/` following `berlin_strassenbaeume_2024.json`
  (every rule: subject, reference, distance, must/should, source_ref, quote, assumption flag).
  The generic runner in `server/engine/rules.py` applies it; new `reference` kinds need one
  measurement function there.
- **Add a species:** one object in `rules/species.json`.
- **Add a tool:** one JSON schema + handler in `server/agent/tools.py`; it appears in the
  agent, the SSE trace and the MCP server at once.

## Scope cut on purpose

Above ground only. One sun position, not a day. A genus table, not a species database.
No routing, no utilities, no root-space or pit-size rules (the standard has them; they need
data this MVP does not fetch). Every cut is visible: rules marked `assumption`, layers marked
`estimated`, and warnings in the status strip rather than silent defaults.

## Research

`research/IDEAS.md` and `research/goneon-mvp-proposals.html` hold the six proposals this
build was chosen from, with the verified sources for every rule and endpoint used here.
