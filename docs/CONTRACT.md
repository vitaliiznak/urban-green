# Allee — build contract

Allee is an agentic street-tree planner: a planner names a street (or draws one), the
engine proposes every legal tree position under a cited rule pack, projects the canopy
those trees will cast over 30 years, and a chat agent drives the same tools and explains
the result. Runs in one container, works without an LLM key (plan panel + HTTP API),
and exposes the same tools over HTTP, SSE and MCP.

Product shape copied from goNEON's N! demo: prompt -> parameters -> constraint check ->
valid / conditional / invalid layers -> compare -> export. LLM orchestrates and explains;
it never computes geometry. Rules are data with attribution, never constants in code.

Everything in this file is binding for every module. Read `server/schemas.py`,
`server/adapters/base.py`, `server/crs.py`, `rules/*.json` before writing code.

## Repository layout and ownership

```
server/
  schemas.py            Pydantic wire contracts (DONE, do not change field names)
  crs.py                to_metric / to_wgs / feature helpers (DONE)
  adapters/             [owner: ADAPTERS]
    base.py             StreetContext, ExistingTree, CityAdapter (DONE)
    osm.py              Overpass + Nominatim helpers, generic "osm" adapter (any city)
    zurich.py           canton cadastre polygons + city tree WFS (fallback OSM trees)
    berlin.py           Berlin tree cadastre WFS + OSM geometry
    serialize.py        StreetContext -> StreetResponse (GeoJSON in WGS84)
    __init__.py         ADAPTERS registry: dict[str, CityAdapter]; get_adapter(id)
  engine/               [owner: ENGINE]
    species.py          load_species, crown_d_at, height_at, impute_crown
    rules.py            load_rule_packs, apply_overrides, evaluate_site
    sites.py            plan_sites (grid + pack modes)
    canopy.py           canopy_metrics
    shade.py            sun_position, shade_polygons
    synthetic.py        synthetic_street() for tests and the offline "demo" city
  service.py            [owner: APP] store + use-cases shared by API, agent, MCP
  session.py, ratelimit.py, app.py (FastAPI + static)      [owner: APP]
  agent/providers.py, loop.py, tools.py, prompts.py         [owner: APP]
  mcp_server.py                                             [owner: APP]
web/index.html, app.js, style.css                           [owner: WEB]
rules/berlin_strassenbaeume_2024.json, rules/species.json   (DONE)
tests/                  each owner adds its own test file; fixtures in tests/fixtures/
Dockerfile, fly.toml, .dockerignore                         [owner: APP]
```

Python 3.12, venv at `.venv` (deps installed: fastapi, uvicorn, httpx, shapely 2, pyproj,
numpy, pydantic 2, openai, anthropic, python-dotenv, mcp, pytest, pytest-asyncio).
Run tests with `.venv/bin/python -m pytest -q`. Network tests must be skipped unless
`CANOPY_NETWORK_TESTS=1` is set.

## Geometry conventions

- Engine and adapters work in the city's metric CRS (`CityInfo.epsg`): Zürich 2056,
  Berlin 25833, generic OSM adapter: `crs.utm_epsg(lon, lat)`.
- `StreetContext.axis` is ONE merged LineString. Stations (`station_m`) are distances
  along it. "left"/"right" are relative to the axis direction (left = +90° from tangent).
- `corridor = axis.buffer(15.0, cap_style="flat")` (clip everything to `corridor.buffer(5)`
  when fetching; keep the context small).
- `plantable = (sidewalks ∪ green verges) − buildings − carriageway`. If a source has no
  sidewalk polygons, estimate: `axis.buffer(half_width + sidewalk_w) − axis.buffer(half_width)`
  and set `basis["sidewalks"] = "estimated"`.
- Layer keys (same everywhere): `axis, carriageway, sidewalks, plantable, buildings,
  cycleways, junctions, existing_trees, corridor`.
- GeoJSON on the wire is WGS84 lon/lat, coordinates rounded to 7 decimals (`crs.feature`).

## ADAPTERS

Data sources verified live on 2026-09-07/08 (fixtures of real responses in `tests/fixtures/`):

- Berlin tree cadastre WFS 2.0.0: `https://gdi.berlin.de/services/wfs/baumbestand`
  `typeNames=baumbestand:strassenbaeume&outputFormat=application/json&srsName=EPSG:4326
  &bbox=minlat,minlon,maxlat,maxlon,urn:ogc:def:crs:EPSG::4326&count=N` (NOTE: lat/lon
  axis order in bbox for 4326 on this server; the returned coordinates are [lon, lat]).
  Properties: `art_bot`, `art_dtsch`, `gattung`, `pflanzjahr`, `standalter`, `kronedurch`
  (metres, ~20 % null), `stammumfg`, `baumhoehe`, `strname`. There is also layer
  `baumbestand:anlagenbaeume` (park trees) — include it too, tagged source "park".
  CORS `*`. Attribution: "Baumbestand Berlin, Geoportal Berlin / SenMVKU (dl-de/by-2-0)".
- Zürich canton cadastre WFS 2.0.0: `https://maps.zh.ch/wfs/OGDZHWFS`
  `typename=ms:ogd-0401_arv_basis_avzh_bodenbedeckung_f&outputFormat=geojson
  &srsname=EPSG:2056&bbox=minx,miny,maxx,maxy,EPSG:2056&count=5000`.
  Property `art` values: "Gebäude" (building), "Strasse, Weg" (carriageway),
  "Trottoir" (sidewalk), "humusierte Fläche" / "übrige humusierte" / "Gartenanlage" /
  "Acker, Wiese, Weide" (green verges -> plantable), "befestigte Fläche" (paved: plantable
  only if adjacent to the street? treat as plantable=false, but not carriageway), water etc.
  Attribution: "Amtliche Vermessung, Kanton Zürich (OGD)". basis = measured.
- Zürich city tree cadastre WFS (official, but returned HTTP 500 on 2026-09-08 for every
  request): `https://www.ogd.stadt-zuerich.ch/wfs/geoportal/Baumkataster`
  `SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature&TYPENAME=baumkataster_baumstandorte
  &OUTPUTFORMAT=GeoJSON&SRSNAME=EPSG:2056&BBOX=...`. Try it with a 6 s timeout, on any
  failure fall back to OSM `natural=tree` nodes and add a warning
  "City tree cadastre unavailable, showing OpenStreetMap trees (crown diameters imputed)".
  Properties when it works: `baumnamelat`, `baumnamedeu`, `baumgattunglat`, `pflanzjahr`,
  `kronendurchmesser`, `baumhoehe`, `kategorie`.
- Overpass: `https://overpass-api.de/api/interpreter` POST `data=` QL. Use
  `[out:json][timeout:25]; ( way["highway"](bbox); way["building"](bbox);
  node["natural"="tree"](bbox); ); out tags geom;` — `geom` gives coordinates inline.
  Add a fallback mirror `https://overpass.kumi.systems/api/interpreter`.
  User-Agent header on every request: `allee/0.1 (street-tree planning demo)`.
- Nominatim: `https://nominatim.openstreetmap.org/search?q=<street>, <city>&format=jsonv2
  &polygon_geojson=1&limit=10` -> pick results with `osm_type=way` and `class=highway`.
  Then Overpass: all `way["highway"]["name"="<name>"]` inside the city bbox (use the
  `boundingbox` of the first Nominatim result of the city, or a fixed city bbox per adapter)
  -> `shapely.ops.linemerge` -> if several parts, take the longest and warn.
  Cap the axis at 2500 m (take the part around the middle, warn) to keep planning fast.

Street geometry from OSM (used by Berlin and generic adapters, and by Zürich when the
cadastre is empty): carriageway = axis.buffer(width/2) with width from tags
`width` > `lanes*3.0` > default by `highway` class (primary 11, secondary 9, tertiary 8,
residential 6.5, living_street 5, unclassified 6.5, service 4, pedestrian 0 -> no carriageway),
basis = estimated. Sidewalks: tags `sidewalk=both|left|right|no`, default both for
residential/tertiary/secondary/primary, width 2.5 m, basis = estimated. Cycleways:
`highway=cycleway` ways in bbox + `cycleway*=track|lane` on the axis -> offset lines at
carriageway edge, basis = estimated. Buildings: `way["building"]` polygons (closed ways),
basis = measured. Junctions: points where other highway ways (not footway/path/service
unless named) touch or cross the axis. Existing trees: OSM `natural=tree` nodes with
`diameter_crown`, `species`, `genus`, `height` when present; else imputed.

Side streets: the carriageway union must include side-street carriageways inside the
corridor (so the ray cast from the axis stops at the real edge) — buffer every highway
way in the bbox, not just the axis.

Each adapter exposes `info: CityInfo` with basemaps:
- Zürich: aerial `https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissimage/default/current/3857/{z}/{x}/{y}.jpeg`
  (attribution "© swisstopo", max_zoom 20, default) and grey map
  `https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.pixelkarte-grau/default/current/3857/{z}/{x}/{y}.jpeg`.
  center [8.5285, 47.3772] zoom 16, demo_streets ["Langstrasse", "Josefstrasse", "Hohlstrasse", "Badenerstrasse", "Weststrasse"].
- Berlin: aerial WMS `https://gdi.berlin.de/services/wms/truedop_2024?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=truedop_2024&STYLES=&CRS=EPSG:3857&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256&FORMAT=image/png`
  (attribution "© Geoportal Berlin / DOP 2024", default) and CARTO light
  `https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png` (attribution "© OpenStreetMap contributors © CARTO").
  center [13.4700, 52.5150] zoom 15.5, demo_streets ["Rigaer Straße", "Frankfurter Allee", "Bergmannstraße", "Karl-Marx-Allee", "Boxhagener Straße"].
- osm (generic, id "osm", name "Anywhere (OpenStreetMap)"): CARTO light + OSM standard
  `https://tile.openstreetmap.org/{z}/{x}/{y}.png`. `find_street` query must contain a
  comma: "street, city". center [8.5417, 47.3769] zoom 12, demo_streets ["Rue de Rivoli, Paris", "Karl-Marx-Allee, Berlin", "Marktgasse, Winterthur"].
All three: utc_offset_hours 2.0 (summer time), country codes CH/DE/"".

`serialize.py`: `serialize_street(ctx) -> StreetResponse`. Feature properties:
existing_trees: `{id, species, genus, crown_d_m, crown_imputed, height_m, planted_year, source}`;
junctions: `{id}`; cycleways/carriageway/sidewalks/plantable/buildings/corridor: `{layer}`;
axis: `{name, length_m}`. Multi-geometries may be emitted as one Feature.

`__init__.py`: `ADAPTERS = {"zurich": ZurichAdapter(), "berlin": BerlinAdapter(), "osm": OsmAdapter()}`
plus `"demo": DemoAdapter()` wrapping `engine.synthetic.synthetic_street` (no network; name
"Demo street (offline)"; a straight 400 m street in Zürich's CRS near [8.53, 47.38]; basemap
pixelkarte-grau). `get_adapter(city_id)` raises KeyError.

Cache every upstream response 15 min in-process keyed by URL+params. All fetches async
httpx with timeout 30 s (6 s for the Zürich tree WFS). Never raise on a missing optional
layer: leave it empty, set basis "unknown" and append a warning. Raise `StreetNotFound`
when the name cannot be resolved, `UpstreamUnavailable` when Overpass/Nominatim fail.

Tests (`tests/test_adapters.py`): parse the fixture files offline (Berlin tree props,
Zürich landcover classification, Overpass ways -> axis/carriageway/sidewalks/junctions,
Nominatim pick). Network tests behind `CANOPY_NETWORK_TESTS=1`.

## ENGINE

```python
# species.py
load_species() -> dict[str, Species]            # from rules/species.json (cached)
species_or_default(species_id) -> Species       # KeyError on unknown id
crown_d_at(sp: Species, year: float) -> float   # sapling + (mature - sapling) * (1 - exp(-t/tau))
height_at(sp: Species, year: float) -> float    # same curve between sapling_height and mature_height
impute_crown(genus: str | None, species_name: str | None, age_years: int | None) -> float
    # genus table -> default 8.0; if age known scale with the growth curve (tau 15)

# rules.py
load_rule_packs() -> dict[str, RulePack]        # every rules/*.json except species.json (cached)
apply_overrides(pack: RulePack, overrides: dict[str, RuleOverride]) -> RulePack   # copies, sets overridden=True
evaluate_site(pt: Point, ctx: StreetContext, pack: RulePack, sp: Species, params: PlanParams,
              *, normal: tuple[float, float] | None = None) -> tuple[Verdict, list[RuleResult], list[str]]
```
Measurement per `reference` (all in metres, `passed = measured >= required`):
- carriageway: `pt.distance(ctx.carriageway)` (0 when inside). basis from ctx.basis["carriageway"].
- cycleway: `pt.distance(ctx.cycleways)`; if cycleways empty -> passed None, measured None, note "no cycle path mapped", basis "unknown".
- building (subject mature_crown): `pt.distance(ctx.buildings) - sp.mature_crown_d_m / 2`; empty -> None.
- existing_tree: min trunk distance; none -> passed True, measured None? No: measured None, passed True, note "no existing trees nearby".
- junction: min distance to ctx.junctions; none -> passed True with note.
- sidewalk_remaining_width (subject pit): width of `ctx.sidewalks` along the perpendicular
  through pt (segment of the ±12 m normal line ∩ sidewalks that contains pt) minus
  params.pit_width_m; if pt not inside sidewalks -> measured None, passed None, note.
- plantable_surface (boolean): passed = ctx.plantable.buffer(0.05).contains(pt); measured None.
Disabled rules (`enabled=False`) are skipped entirely. Verdict: any must failed -> invalid;
else any should failed -> conditional; else valid. `failed_rules` lists failing rule ids.

```python
# sites.py
@dataclass
class SiteGeom: site_id: str; pt: Point; station_m: float; side: Side; verdict: Verdict; props: SiteProps
plan_sites(ctx, params: PlanParams, pack: RulePack, sp: Species) -> tuple[list[SiteGeom], PlanSummary]
```
Grid mode: stations `spacing/2, 3*spacing/2, ...` along the axis; for each station and
requested side(s): tangent from `axis.interpolate(d ± 0.5)`, normal = left-hand
perpendicular (right side uses -normal). Ray from the axis point outward 40 m; find the
carriageway edge = end of the first `ray ∩ carriageway` segment that starts within 1.0 m
of the axis point (if the axis point is not inside the carriageway, edge_distance = 0 and
note it). Trunk = axis point + normal * (edge_distance + params.offset_from_edge_m).
Evaluate. `site_id = f"{'L' if left else 'R'}{int(round(d)):04d}"`.
Pack mode: walk each side independently from station 0 in 0.5 m steps; accept a position
when verdict != invalid AND (no accepted yet or d - last_accepted >= spacing). Emit only
accepted sites (they are valid or conditional); record `gaps` for any stretch > 2*spacing
without an accepted site (reason = most frequent failed must-rule in that stretch).
Props: `crown_d_by_year` / `height_by_year` for YEARS as string keys. Summary counts.

```python
# canopy.py
canopy_metrics(ctx, sites: list[SiteGeom], sp: Species, years=YEARS) -> CanopyResult
```
Planted sites = verdict in (valid, conditional). Existing crowns = circles from
`ExistingTree.crown_d_m` (already imputed by the adapter). For each year: new crowns =
circles `crown_d_at(sp, year)/2`; total = union(new ∪ existing) ∩ corridor. Areas:
corridor, street = (carriageway ∪ sidewalks) ∩ corridor, sidewalk = sidewalks ∩ corridor.
`existing_cover_corridor_pct` = existing ∩ corridor / corridor. Percentages rounded 1 dp.

```python
# shade.py
sun_position(lat, lon, year, month, day, hour_local, utc_offset_hours) -> tuple[elev_deg, az_deg]   # NOAA algorithm, azimuth from north clockwise
shade_polygons(ctx, sites, sp, *, year, month, day, hour, include_existing) -> ShadeResult
```
Crown approximated as a sphere of diameter D at centre height `h - D/2` (h from
`height_at`, existing: `height_m` or `max(D, 1.5*D)`). Shadow = ellipse: centre displaced
from the trunk by `hc / tan(elev)` in direction `azimuth + 180°`, semi-axis across = D/2,
along = (D/2) / sin(elev) (cap displacement at 60 m, elev < 5° -> return empty with note).
Union, clip to corridor, compute shaded % of sidewalk / street / corridor. `when` uses
the city's utc_offset. Use `ctx.city.center` for lat/lon.

```python
# synthetic.py
synthetic_street(length_m=400, road_w=7.0, sidewalk_w=3.5, name="Demo street") -> StreetContext
```
EPSG 2056, axis from (2682000, 1248000) eastwards. Buildings: fronts 1.0 m behind the
sidewalk on both sides, with a 40 m gap at station 150-190 (north) and one building
pushed to 0.8 m from the sidewalk edge at station 260-300 (north) so `building_crown`
fails there. One cycleway line 1.0 m outside the carriageway edge on the south side for
stations 0-200 (so `cycleway` fails for trunks placed within 0.25 m of it only if
offset makes it so — keep offset 1.0 so it passes; tests may set offset 0.4). Existing
trees: three Tilia at stations 100/108/116 north side with crown 9 m. Junction points at
stations 0 and 400 and a side street crossing at station 320 (carriageway polygon 6 m wide
crossing north-south). `city` = CityInfo id "demo" (see ADAPTERS). Deterministic.

Tests (`tests/test_engine.py`): grid count = expected for 400 m / 8 m / both;
south side all valid except near junctions; north side conditional at 260-300 (building_crown)
and invalid where the side street crosses (carriageway_edge / plantable); pack mode count
>= grid valid count; canopy cover monotonic in years and existing_cover > 0; shade at
July 15 15:00 has elevation 40-60° and azimuth 220-260°, shadow displaced to the NE;
apply_overrides flips outcomes (set carriageway_edge to 3.0 -> more invalid); species
curve at 30 y within [0.85, 0.9] of mature.

Performance target: Langstrasse (1.3 km, ~330 grid sites, ~2000 cadastre polygons) plans
in < 2 s. Prepare `ctx` unions once (adapters already union); use `shapely.prepared` or
STRtree for point-to-layer distances if needed.

## APP (service, API, agent, MCP)

`service.py`:
```python
class Store: streets: dict[str, StreetContext]; scenarios: dict[str, Scenario]; TTL 3 h; max 2000 entries
@dataclass class Scenario: response: ScenarioResponse; sites: list[SiteGeom]; ctx_id: str; session_id: str
async def load_street(req: StreetRequest) -> StreetResponse     # via ADAPTERS; caches ctx by (city, normalized query)
def plan(req: PlanRequest, session: Session) -> ScenarioResponse # merges session.rule_overrides with req.rule_overrides (request wins)
def canopy(req: CanopyRequest) -> CanopyResult
def shade(req: ShadeRequest) -> ShadeResult
def compare(req: CompareRequest) -> CompareResponse
def export_geojson(scenario_id) -> dict                          # FeatureCollection: sites (+ verdict, rules), mature crown polygons, axis
def explain_site(scenario_id, site_id) -> SiteProps
def config() -> ConfigResponse
```
Scenario labels default to `f"{spacing:g} m · {species short} · {side} · {mode}"`.
`created_at` ISO 8601 UTC.

`session.py`: session id from header `X-Session-Id` (client-generated UUID) else cookie
`canopy_sid` else new UUID (returned in response header `X-Session-Id`). Session holds
`rule_overrides: dict[str, RuleOverride]`, `history: list` (agent messages, last 16),
`scenario_ids`, `last_street_id`, `last_scenario_id`.
`ratelimit.py`: sliding-window counters per session (`AGENT_TURNS_PER_HOUR`, default 30)
and global per day (`AGENT_TURNS_PER_DAY`, default 3000); HTTP 429 with a JSON message.
Also cap `POST /api/street` at 60/h/session and `POST /api/plan` at 300/h/session.

HTTP API (`app.py`, FastAPI, all under `/api`, JSON; errors `{"error": {"message", "code"}}`):
```
GET  /api/health              -> {ok, version, agent: {enabled, provider, model}}
GET  /api/config              -> ConfigResponse
GET  /api/session             -> QuotaInfo
POST /api/street              StreetRequest -> StreetResponse           (404 street_not_found, 503 upstream_unavailable)
POST /api/plan                PlanRequest -> ScenarioResponse           (404 unknown street)
POST /api/canopy              CanopyRequest -> CanopyResult
POST /api/shade               ShadeRequest -> ShadeResult
POST /api/compare             CompareRequest -> CompareResponse
GET  /api/scenarios/{id}      -> ScenarioResponse
GET  /api/scenarios/{id}/sites/{site_id} -> SiteProps
GET  /api/export/{id}.geojson -> FeatureCollection (Content-Disposition attachment)
GET  /api/rules               -> {packs: [RulePack], overrides: session overrides}
PUT  /api/rules/{pack_id}/{rule_id}   RuleOverride -> {pack: RulePack (with overrides applied)}
DELETE /api/rules/overrides   -> {ok}
POST /api/agent               AgentRequest -> text/event-stream (see below)   (429 quota, 503 agent_disabled)
GET  /                        web/index.html;  /static/* -> web/*
GET  /docs                    FastAPI OpenAPI UI (keep enabled)
```
CORS: allow all origins (hackathon API use). Serve `web/` with `StaticFiles` at `/static`
and `index.html` at `/`. Load `.env` with python-dotenv at import.

SSE protocol for `POST /api/agent` (each event: `event: <type>\ndata: <json>\n\n`):
```
event: text     {"delta": "..."}                                   streamed assistant prose
event: tool     {"id": "...", "name": "plan_trees", "args": {...}}   a tool call started
event: result   {"id": "...", "name": "...", "ok": true, "summary": "41 valid, 6 conditional, 3 invalid …",
                 "payload_type": "street"|"scenario"|"shade"|"compare"|"rules"|"site"|"export"|null,
                 "payload": {...}}                                  payload = the full StreetResponse / ScenarioResponse / ShadeResult / CompareResponse / RulePack / SiteProps / {url}
event: ui       {"action": "fly_to"|"set_year"|"show_layer"|"select_scenario"|"open_compare",
                 "target": ..., "year": ..., "layer": ..., "visible": ..., "scenario_id": ...}
event: done     {"usage": {"input_tokens": n, "output_tokens": n}, "turns_remaining_hour": n}
event: error    {"message": "..."}
```
Send a heartbeat comment line `: ping` every 15 s while waiting on the model.

Agent tools (`agent/tools.py`; JSON schemas + Python handlers; the LLM only ever sees
short observations, never GeoJSON):
```
load_street(city, query)                       -> summary text + payload street
plan_trees(spacing_m, side, species_id, mode, offset_from_edge_m?, label?)  -> summary + payload scenario (uses session.last_street_id)
explain_site(site_id, scenario_id?)            -> rule results as text + payload site
set_rule(rule_id, min_distance_m?, mode?, enabled?)  -> updated rule + payload rules; note "re-run plan_trees to apply"
canopy_projection(scenario_id?)                -> years table text
shade(scenario_id?, year=30, month=7, day=15, hour=15)  -> summary + payload shade
compare_scenarios(scenario_ids?)               -> table text + payload compare (default: all session scenarios)
export_geojson(scenario_id?)                   -> {url} payload export
list_species() / list_rules()                  -> text
fly_to(target: "street"|site_id|[lon,lat], zoom?) ; set_year(year) ; show_layer(layer, visible)   -> emit ui events, return "ok"
```
Observations are compact strings (< 800 chars): counts, top 3 failing rules with the
rule's required distance and source_ref, canopy % at 30 y, warnings. Max 10 tool calls
per turn; history trimmed to the last 16 messages; tool payloads are NOT kept in history.

`agent/providers.py`: `OpenAIProvider` (Responses API, `client.responses.create(model,
instructions, input, tools, stream=True, reasoning={"effort": "low"})`; tool schema shape
`{"type":"function","name","description","parameters","strict":False}`; stream events:
`response.output_text.delta` (delta), `response.output_item.done` with `item.type ==
"function_call"` (`item.name`, `item.arguments`, `item.call_id`), `response.completed`
(`response.usage.input_tokens/output_tokens`). Tool results go back as input items
`{"type":"function_call","name","arguments","call_id"}` followed by
`{"type":"function_call_output","call_id","output"}`. VERIFIED 2026-09-08 with model
`gpt-5.6-luna` — the Chat Completions endpoint rejects tools for that model, do not use it).
`AnthropicProvider` (`client.messages.stream` with `tools`, `tool_use`/`tool_result`
blocks, model `ANTHROPIC_MODEL` default `claude-sonnet-5`, max_tokens 2000).
Provider selection: `OPENAI_API_KEY` -> OpenAI (`OPENAI_MODEL` default `gpt-5.6-luna`),
else `ANTHROPIC_API_KEY` -> Anthropic, else agent disabled (`/api/agent` -> 503 and
`config.agent.enabled=false`). Both providers implement
`async def run_turn(system, history, tools, on_event) -> new_history_items` so `loop.py`
is provider-agnostic.

`agent/prompts.py`: system prompt: you are Allee, a street-tree planning agent; you
never estimate distances yourself, every number comes from a tool; explain verdicts by
rule with its source; must vs should; keep answers under 120 words unless asked; after
planning, mention canopy at 30 years and the top failing rule; suggest one next step.
Include the active city, street, scenarios and rule overrides as context each turn.

`mcp_server.py`: `FastMCP("allee")` exposing load_street, plan_trees, explain_site,
set_rule, canopy_projection, shade, compare_scenarios, export_geojson (returns GeoJSON
string), list_species, list_rules — same handlers as the agent tools, stdio transport,
`python -m server.mcp_server`.

Deploy: `Dockerfile` (python:3.12-slim, copy pyproject + server + web + rules, `pip install .`,
`CMD uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000}`), `fly.toml` (app
"allee-planner", region fra, 1 shared-cpu 512 MB, internal_port 8000, http_service
force_https, auto_stop_machines off, min_machines_running 1), `.dockerignore`.
`run.sh`: `.venv/bin/uvicorn server.app:app --reload --port ${PORT:-8000}`.

Tests (`tests/test_api.py`): TestClient with city "demo": config, street, plan, canopy,
shade, compare, export, rules override flow, agent 503 when no key (monkeypatch env),
rate-limit 429 after N calls with a tiny limit.

## WEB (single page, no build step)

Files: `web/index.html`, `web/app.js`, `web/style.css`. MapLibre GL JS 4.7.1 from
`https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/maplibre-gl.js` and its CSS from
the same path (`maplibre-gl.css`), Google Fonts: `Instrument Serif` (display),
`IBM Plex Sans` (UI), `IBM Plex Mono` (numbers/stations). No framework.

Page name: **Allee**. One committed dark theme (aerial imagery under dark chrome), painted
explicitly: ground `#0F1512`, panel `#17201B` (92 % alpha over the map), line `#2A3630`,
ink `#E8EDE6`, muted `#9AA89E`, accent (canopy) `#7FD069`, mature crown fill `#2F8F4E`,
valid `#58C97A`, conditional `#F2B84B`, invalid `#F0574F`, shade `#7C9CFF` at 35 %,
cycleway `#4FA3E0`, existing trees `#A3C69C`. Verdict colour is never the only encoding:
site markers also carry a ring (valid solid, conditional dashed, invalid crossed) and the
legend names them.

Layout (desktop first, works at 1280×800; below 900 px the panels stack under the map):
- Map full-bleed. Top-left floating header: wordmark "Allee" (Instrument Serif) +
  subtitle "street-tree planning agent", city select, street search input with
  demo-street chips, "Draw street" button (click points on the map, double-click to finish,
  uses `POST /api/street` with `line`).
- Left column (320 px) under the header, three cards: **Plan** (spacing slider 4–20 m with
  live value, side segmented control, species select showing size class + mature crown,
  mode toggle grid/pack, "Plan trees" button, label input), **Rules** (list of rules from
  the active pack: label, must/should badge, editable value, `assumption` tag rendered as
  "planning default", source_ref shown as "§ 5.2 Berlin 09/2024"; editing calls
  `PUT /api/rules/...` and re-runs the current plan), **Scenarios** (chips per scenario with
  planted count + cover_corridor_pct_30; click = show on map; "Compare" opens a table
  modal built from `POST /api/compare`; "Export GeoJSON" link to `/api/export/{id}.geojson`).
- Bottom-centre dock over the map: **year slider 0–30** with play/pause (animates 0→30 in
  ~4 s, crowns grow), readout "Year 12 · canopy 18.4 % of corridor · 41 trees", and the
  **Shade** toggle (calls `POST /api/shade` for the current year; re-fetch when the year
  changes while enabled, debounced 250 ms).
- Right column (360 px): **Agent** chat: message list (assistant prose as text, each tool
  call as a compact trace row "plan_trees · spacing 8 m · both" with a spinner then a
  one-line result summary), input + send, example prompt chips from config. When
  `config.agent.enabled` is false: show "Agent offline on this server (no API key). The plan
  panel and the HTTP API do everything the agent does." and hide the input.
  SSE via `fetch` + `ReadableStream` parsing (POST), apply `result` payloads to the map,
  handle `ui` events.
- Legend (bottom-left): existing tree, valid/conditional/invalid site, mature crown, shade,
  carriageway, sidewalk, cycleway, building. Layer toggles beside it.
- Status strip in the header: source attributions from `layer_sources` and `warnings`
  (e.g. cadastre unavailable) — visible, not hidden in a tooltip.

Map layers (MapLibre): raster basemap (default from city; switcher for the others),
`corridor` (line only, dashed muted), `carriageway` (fill rgba(255,255,255,0.07) + line),
`sidewalks` (fill accent 12 %), `plantable` (hidden by default), `buildings` (fill 16 % +
outline), `cycleways` (line cycleway colour, dashed), `junctions` (small circles), `existing_trees`
(circle layer with radius in metres: use `["interpolate",["exponential",2],["zoom"],
0, 0, 22, ["*",["get","crown_r"], K]]` where K = 2^22 * pixels-per-metre at zoom 0 ≈
`0.000163 * 4194304`... simpler: compute pixels per metre for the current zoom and map
centre latitude on `zoom` events and set the circle radius expression with
`["*", ["get","crown_r"], ppm]`), `crowns` (same technique on the sites source, radius from
`crown_d_by_year[year]/2` interpolated linearly between YEARS keys, fill mature crown 35 %),
`sites` (circle 5 px, stroke by verdict), `shade` (fill shade colour). Hover a site -> popup:
site id, station "0+240", side, verdict, each rule as "✓/✗ label — measured vs required
(must/should, § ref)". Click an existing tree -> species, crown, planted year, source.

First frame: on load fetch `/api/config`, select the first city, load its first demo street,
run a plan with defaults, fit bounds, year = 30. If `POST /api/street` fails (upstream down),
fall back to city "demo" and say so in the status strip. Everything visible at rest,
`prefers-reduced-motion` disables the growth animation (jumps to the final year).
Keyboard: Enter sends chat, Escape closes modals. Visible focus states. Keep `app.js`
in one file with small functions; no `alert()`s; errors go to the status strip.
Session id: `crypto.randomUUID()` stored in `localStorage` (try/catch), sent as
`X-Session-Id` on every request.

## Definition of done (whole product)

`./run.sh` serves `/`; opening it shows Langstrasse with existing trees, proposed sites
coloured by verdict, crowns growing with the slider; the agent answers "Plant as many trees
as possible on Josefstrasse, 8 m spacing" by loading the street, planning, and reporting
counts and 30-year cover; `/api/export/{id}.geojson` downloads; `pytest` green;
`docker build .` succeeds; `README.md` explains run, deploy, API, MCP, and how to add a
city adapter, a rule pack and a species.
