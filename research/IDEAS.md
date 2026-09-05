# Overnight MVP ideas for the goNEON take-home

Research date: 2026-09-07. Full cited report: `research/goneon-mvp-proposals.html`
(published at https://claude.ai/code/artifact/01927110-7b0e-4962-a18a-4526bea942f8).

Constraints: no underground topics; buildable in ONE Claude Code / Codex run; map-based and
very demoable; must have an ecosystem hook (API / MCP / rule packs / GeoJSON export).

## What the research changed about the brief

- goNEON is **goNEON Agentic Systems AG**, ETH Zürich spin-off (CEO Raphael Eder, CTO Lukas Ballo).
  Site is goneon.city (goneon.io 404s).
- Their product N! (try.goneon.city) is a **chat agent over a 3D Zürich map**:
  prompt -> parameter -> constraint check -> Valid / Invalid / Safety margin / Remaining width layers -> compare.
  Copy that shape.
- **Every public use case is above ground** (parking/curb compliance, cycling corridors scored on
  directness / gradient / turning angles / traffic exposure, loading zones, sight triangles).
  The sewer/water examples in the brief are hackathon prompts, not product scope.
- CTO's open-source toolkit **SNMan** (MIT) reallocates road space on OSM streets under design rules.
  Reference / "next engine", not a drop-in (city runs take hours).
- Agentic-GIS lesson (GISclaw, GISAgentBench): success is bounded by **tool design**, not model reasoning.
  Small set of deterministic, typed tools with injected rules; LLM orchestrates and explains, never computes geometry.
- The existing scaffold here (FastAPI + Shapely in LV95, swisstopo, canton Zürich WFS, city tree cadastre,
  A* alignment router in `server/engines/route.py`) already fits; Zürich is goNEON's home and demo city.

## Ranking (demo impact vs build risk, 0-10)

| Rank | Idea       | Risk | Impact | Why it sits there |
|-----:|------------|-----:|-------:|-------------------|
| 1 | Clearance  | 5   | 9   | 3D corridor conflicts are the most cinematic; A* router already exists; building heights sparse |
| 2 | Canopy     | 3   | 8.5 | Live tree WFS with CORS, three citable numbers, pure 2D math. Lowest risk |
| 3 | Rulebook   | 5.5 | 7.5 | Strongest platform story; PDF extraction flaky. Best as the hook on 1 or 2 |
| 4 | Corridor   | 7   | 7.5 | Most on-brand, but routing quality, gradient and width data add risk |
| 5 | One Street | 8.5 | 9.5 | Trees vs overhead lines is a great story, but two data domains in one night |
| 6 | Kerbside   | 4   | 5   | EV siting rules unverified; demo is a heat map, not geometry |

**Recommendation:** build **Clearance**, ship **Rulebook** as its ecosystem hook, keep **Canopy** as the
fallback. Checkpoint at ~hour 5: if real OSM lines are not drawing conflicts, switch the same agent loop,
UI, rule schema and export to Canopy (only the engine changes).

---

## 1. Clearance (recommended)

**Pitch:** Drop a 110 kV line on the map, see every building and tree inside its protective strip,
then let the agent bend it clear.

**60-second demo:** pick a real overhead line at the edge of Zürich. "Show the protective strip for this
line as an E.DIS 110 kV standard design" -> 46 m corridor sweeps along the route; buildings inside rise
in red with heights. "Realign to clear the school, at most two new angle towers" -> router redraws,
compare panel shows +11 % length, 0 conflicts. Export GeoJSON.

**Agent tools**
- `fetch_lines(bbox)` -> OSM `power=line` / `power=minor_line` with voltage and tower nodes
- `corridor(line_id, ruleset)` -> protective strip polygon
- `find_conflicts(corridor)` -> buildings, trees, masts with reasons
- `realign(line_id, constraints)` -> new alignment via least-cost A*
- `score(alignments)` -> length, angle towers, conflicts
- `fly_to`, `toggle_layer`, `compare`

**Data (verified):** OSM `power=line` and `minor_line` imply overhead, underground is `power=cable`;
excluding `location=underground` yields >99.8 % overhead lines. Overture buildings carry optional
`height`, `num_floors`, `min_height`, `roof_height`; they survive the DuckDB -> tippecanoe PMTiles
pipeline for MapLibre fill-extrusion.
**Data (scaffold, not re-verified):** canton Zürich cadastre, swisstopo heights.

**Rules (verified, E.DIS Netz guideline Jan 2022, basis DIN EN 50341):** 110 kV standard protective
strip 46 m (23 m each side), kept free of buildings; woody plants inside max 3.0 m final height;
trees outside placed so they cannot fall into the line; 15 m planting-free radius around each mast.
One operator's values -> editable rule pack with source attribution + operator selector. Never constants.

**Engine:** Shapely buffer/intersection in LV95 metres; realignment via the existing A* raster router
with the corridor as impassable buffer around buildings.

**Wow:** MapLibre 3D extrusions from static PMTiles, animated corridor sweep, conflict counter ticking
down while the realignment draws, reasoning trace streaming beside the map.

**Ecosystem hook:** rule packs as JSON per grid operator (each field with source + page); MCP server
exposing the same tools; GeoJSON + KML export.

**One-night plan (~9 h):** 1.0 Overpass fetch + PMTiles buildings for one bbox · 2.0 corridor /
conflict / score tools + tests · 1.5 wire realign to A* · 2.0 MapLibre UI (layers, compare, trace) ·
1.5 tool-calling loop + manual panel without API key · 1.0 deploy, instructions, MCP stub.

**Cut first:** realignment (check + drag the line is still end-to-end), then 3D -> 2D footprints.
**Risk:** city-centre lines are underground cables -> demo on suburban/rural bbox; Overture heights
sparse -> fallback `num_floors * 3` or default.

## 2. Canopy (fallback)

**Pitch:** Draw a street, get every legal tree position and the shade those trees will cast in 30 years.

**60-second demo:** "Plant as many street trees as possible on Langstrasse, Berlin standard clearances,
8 m spacing" -> loads existing trees, carriageway edges, cycle paths, building fronts; proposes positions;
crowns grow to mature diameter; invalid positions red with the rule that killed them.
"Compare 8 m and 10 m spacing" -> canopy cover per scenario. Export GeoJSON.

**Agent tools:** `query_trees(bbox)`, `street_edges(segment)`, `candidate_sites(segment, spacing)`,
`check_clearances(sites, rules)`, `canopy_gain(sites, species, years)`, `export_geojson`.

**Data (verified live):** Berlin tree cadastre WFS 2.0.0 (`https://gdi.berlin.de/services/wfs/baumbestand`,
layer `baumbestand:strassenbaeume`), 434,765 street trees, GeoJSON output, EPSG:4326 via SRSNAME,
CORS `*`; fields: species (`art_dtsch`/`art_bot`), genus, planting year, age, crown diameter
(`kronedurch`, ~20 % missing), trunk circumference, height. Always bbox-filter / page.
**Data (scaffold, not re-verified):** Zürich Baumkataster WFS -> same tool via a city adapter.

**Rules (verified, Berlin street-tree planting standard 09/2024, section 5.2):** >= 0.5 m carriageway
edge to trunk (mandatory); >= 0.25 m trunk to cycle path (recommended); >= 1.5 m mature crown edge to
multi-storey buildings (recommended). Render must vs should differently.

**Engine:** 2D buffers/intersections in Shapely; mature crown from a species table; optional shadow
polygon offset by height for one sun position.

**Wow:** crowns animating sapling -> mature, canopy % rising, July-afternoon shadow layer, side-by-side spacing.

**Ecosystem hook:** city adapters (one small class per WFS), species table plugin, same rule-pack
format as Clearance, MCP server.

**One-night plan (~8 h):** 1.0 WFS adapter + footprints + OSM ways · 2.0 sites / clearance / canopy +
tests · 2.0 map UI with growth animation and compare · 1.5 agent loop + manual panel · 1.0 deploy.
**Cut first:** shade, then species imputation (8 m default crown). **Risk:** low.

## 3. Rulebook (ecosystem hook, build on 1 or 2)

**Pitch:** Feed the agent a planning standard PDF; it writes the rule pack and runs it on the map in the same minute.

**Demo:** drop the Berlin tree standard or the E.DIS guideline -> agent extracts each distance with page
reference into a rule pack shown beside the PDF page; move the 0.5 m slider to 0.75 m and the map
re-validates; load a second pack and diff.

**Tools:** `extract_rules(pdf)` (schema-constrained LLM call), `validate_rules(pack)`,
`apply_rules(features, pack)`, `diff_rules(a, b)`, `explain_rule(id)` (quote + page).

**Rule schema:** subject, reference feature, min distance, mode (must/should), source doc + page,
operator/city scope. One generic buffer-check runner over GeoJSON serves both Clearance and Canopy.

**Wow:** split screen, PDF page with highlighted numbers left, map turning valid/invalid right.

**Ecosystem:** rule packs are the plugin format; each hackathon team submits one; registry lists them
with sources. Matches goNEON's own "machine-readable design rules" wording.

**Plan (+3 h):** 1.0 schema + validator + two hand-checked packs · 1.0 PDF extraction with citations ·
1.0 split-screen UI + diff. **Cut first:** live extraction (keep editable packs + diff).
**Risk:** extraction hallucinates numbers (the refuted 10/20/30 m claim is exactly that) -> every value shows its quote.

## 4. Corridor

**Pitch:** Three bike corridors between two points, scored the goNEON way: directness, gradient,
turning angles, traffic exposure.

**Demo:** click two points in Kreis 4 -> three alternatives, radar of scores, 2 m lane fit check per
segment with remaining road width. "Prefer the flattest" re-weights and re-ranks. Export GeoJSON.

**Tools:** `get_network(bbox)`, `alternatives(a, b, k)`, `score_corridor(path, weights)`,
`check_width(segment, lane_config)`, `compare(paths)`.

**Data:** OSM via Overpass/OSMnx; gradient from swisstopo profiles (scaffold `geo.py`, not re-verified);
exposure from highway class; OSM widths sparse. No verified cycling design values -> label as assumptions.

**Engine:** NetworkX penalised k-shortest paths + four scoring functions; SNMan as "next engine".
**Wow:** corridors drawing in parallel, street cross-section before/after, radar reshaping with weights.
**Ecosystem:** scoring functions as plugins with a weights file; SNMan integration path.

**Plan (~10 h):** 1.5 graph · 2.5 alternatives / scoring / width · 2.5 UI · 1.5 agent · 1.0 deploy.
**Cut first:** gradient, then cross-section. **Risk:** routing on messy graphs, DEM latency, missing
widths; invites the closest comparison with their real product.

## 5. One Street (stretch only)

**Pitch:** Two rulebooks, one street: the tree agent and the line agent negotiate until nothing conflicts.

**Demo:** suburban street with a distribution line; agent places trees, flags those inside the corridor
whose mature height > 3 m, swaps to a small species or shifts, keeps the 15 m mast radius clear, narrates.

**Tools:** union of Clearance + Canopy plus `resolve_conflicts(sites, corridors)`.
**Wow:** two constraint fields in different hatches, conflicts resolved one by one, zero red at the end.
**Ecosystem:** proof that rule packs compose.
**Plan:** ~12 h. Only if Clearance or Canopy works by midnight. **Risk:** high.

## 6. Kerbside (vision-video idea, not the MVP)

**Pitch:** Where can the city put EV chargers without blocking sight lines, trees, or loading zones?
Demand proxy from POI density lights up parking bays; agent rejects conflicts with trees, crossings,
loading zones. KEA-BW guideline fetched but no rule verified. Weak geometry, weak hook.

---

## Shared architecture

- LLM = orchestrator + explainer. Tools deterministic, typed, short observations. Rules = data with attribution.
- Backend: existing FastAPI + Shapely scaffold, one container. Tool schemas shared by HTTP API, agent loop, MCP server.
- Frontend: MapLibre + PMTiles from static hosting via `addProtocol` (needs Range + CORS, no tile server).
- Agent loop: Anthropic/OpenAI tool calling, streamed reasoning beside the map. Manual panel without a key
  is the "not a solo demo" safeguard.
- Prior-art UI pattern: OpenAssistant (GeoDaCenter, MIT, alpha) chat + tools over Vercel AI SDK.
  Borrow the pattern, not the package (its routing is Mapbox, no engineering geometry).
- Not verified: hosting/rate limits for 100 users, MCP specifics, the scaffold's Zürich endpoints.
  Defaults: one Fly.io/Railway container, key server-side, per-session token bucket + daily cap.

## Do not hardcode

- REFUTED (1-2): DIN EN 50341-2-4 horizontal distances 10 m (<=45 kV) / 20 m (<=110 kV) / 30 m (>110 kV).
- REFUTED (0-3): Overture "five themes as GeoParquet filterable by bbox in DuckDB" recipe as stated. Re-check docs.
- SPLIT (2-1): "tool design, not model reasoning, is the ceiling" (one paper's self-eval).

## Open questions (10 minutes before starting)

1. Does N! expose any API, export format or rule file the hook should mirror? Nothing public found.
2. Which 380/220 kV protective strips do Swissgrid / Axpo publish, for a Swiss rule pack in the Zürich demo?
3. Do the scaffold's Zürich WFS layers answer with CORS today? Berlin was probed; Switzerland was not.

## Key sources

- goneon.city · try.goneon.city · Startup Days 2026 profile · Tech.eu (2026-07-20)
- github.com/lukasballo/snman · Ballo, Raubal, Axhausen 2024
- OpenAssistant (github.com/geodacenter/openassistant) · GISclaw arXiv 2603.26845 · GISAgentBench arXiv 2608.01645
- Berlin trees WFS gdi.berlin.de/services/wfs/baumbestand · Berlin planting standard PDF (berlin.de/sen/uvk)
- OSM wiki power=line / power=minor_line / power=cable · E.DIS Netz guideline (amt-neuburg.de, bauleitplaene-mv.de)
- Overture building schema · Protomaps PMTiles for MapLibre · leafmap 3D PMTiles example
