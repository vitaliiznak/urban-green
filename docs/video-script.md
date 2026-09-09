# Video script (three parts, ~8 minutes total)

Record with the app open on the Zürich default street, aerial basemap, year slider at 30.

## Part 1 — to Lukas / engineering (≈3 min): what it does and how it is built

1. **One sentence.** "Urban Green is an agentic street-tree planner: name a street, get every legal
   tree position under a cited rule pack, and the canopy those trees cast in 30 years."
2. **Scope cut, and why.** Above-ground only, one engine (2D clearances + canopy + one sun
   position), three verified rules, three city adapters. Cut: shade for every hour, species
   imputation beyond a genus table, routing, any utility. Reason: an MVP a hundred people can
   use tonight beats a feature list; every number on screen must be explainable.
3. **Architecture on one screen.** Show `docs/CONTRACT.md` layout block:
   adapters (data) → engine (deterministic Shapely in a metric CRS) → service → three fronts
   (HTTP API, SSE agent loop, MCP server) → one static MapLibre page. The LLM orchestrates and
   explains; it never computes geometry. Rules are JSON with source and page, never constants.
4. **Show the engine being honest.** Hover a red site: "0.0 m measured vs 0.5 m required, must,
   § 5.2 Berlin 09/2024". Hover an amber one: a should-rule. Open the Rules card: `assumption`
   rules are labelled "planning default".
5. **Show it is not a solo demo.** Run `examples/curl.sh`; show `/docs` (OpenAPI); show the MCP
   config in the README; show `pytest` green and the Dockerfile.

## Part 2 — to Raphael / planning (≈3 min): a real tutorial

1. Open the link. Pick **Zürich → Josefstrasse** (or type any street name).
2. Read the status strip: which data is measured (cadastre) and which is estimated (OSM).
3. Plan: 8 m, both sides, Winterlinde, grid. Read the counts: valid / conditional / invalid.
4. Click an invalid site: read the rule that killed it. Switch to **pack** mode: the engine
   slides trees to the nearest legal position and reports gaps with the reason.
5. Drag the year slider 0 → 30; press play. Read the canopy % of the corridor and of the
   sidewalk. Toggle **Shade** (15 July, 15:00).
6. Change the rule "Mature crown to building" from 1.5 m to 3 m; re-plan; watch conditionals
   appear. Reset overrides.
7. Plan a second scenario (12 m, plane trees). **Compare**. **Export GeoJSON** and open it in
   QGIS or geojson.io.
8. Ask the agent the same thing in one sentence and watch it call the same tools.

## Part 3 — to Raphael / vision (≈2 min): where it goes and how a team takes it there

- **Rule packs are the plugin format.** A city or a hackathon team adds a JSON pack with
  sources; the same runner applies it. Next: a Zürich pack (Grün Stadt Zürich planting
  standard), Swiss VSS norms, and an LLM-assisted extractor that quotes the page it read.
- **City adapters are one class each.** Zürich (cadastre), Berlin (tree cadastre), anywhere
  (OSM). Next: Basel, Wien, any WFS with a tree layer — an afternoon per city.
- **Engines compose.** The same site list can be checked against a second rule pack
  (overhead lines, sight triangles at junctions, loading zones) — the N! pattern of layered
  constraints. Next engine candidates: SNMan road-space reallocation as the upstream that
  produces the street cross-section Urban Green plants into.
- **Ecosystem surface.** HTTP + SSE + MCP today; the tool schemas are the API. A partner can
  drive Urban Green from their own agent, or a planner from Claude Desktop, without touching the UI.
- **What a team does in week one.** Harden the adapters (retry, caching to disk), add a
  species table with climate suitability per city, run the Berlin standard's remaining
  rules (pit size, root space), and put the rule editor behind a login so packs are shared.
