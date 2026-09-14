# Screenplay — three apps, three parts each

Record **three videos** (one per app) or one film with three chapters.
Each app is the same package: a paste-link, then Part 1, Part 2, Part 3.

| Piece | Audience | Voice |
|---|---|---|
| **The link** | Anyone who opens it | URL + four lines. Not spoken. Paste with the video. |
| **Part 1** | Lukas, engineering | What it does. How it is built. Show the contract. What the model must not do. |
| **Part 2** | Raphael, planning | You / we. Real buttons. A journey a planner can repeat. |
| **Part 3** | Raphael, vision | We. Week one. How a team takes it. Where the three meet. |

Do not say Lukas or Raphael. Title cards on screen are fine: *Part 1 — How it is built*, *Part 2 — Using it*, *Part 3 — Next*.
If a number on screen differs, say the screen. No recap at the end of a chapter.

**Runtime.** About 7 minutes per app (2–2.5 per part). Breath at each title card.

**Part 1 proof — show the file, do not narrate a stack.** Have these open in a second window:

| App | Open this | Point at |
|---|---|---|
| Urban Green | `rules/berlin_strassenbaeume_2024.json` then `/docs` | `carriageway_edge`: `source_ref`, `quote`, `must`. Then the same `/api/plan` in OpenAPI. |
| Recycling Walk | `data/placement/active.json` | `version` `public-47d55b23c676f576`, `stale: false`. Same id on the result. |
| City Lab | Stay in the world | Building tile **20260520**. Then **Original imagery** / **Clean streets**. |

**Tabs, already loaded.**

| Tab | App | Ready state |
|---|---|---|
| 1 | Urban Green | Langstrasse planned, aerial, **Years after planting** at 30 |
| 2 | Recycling Walk | Home. Glass example warmed once off-camera, then reloaded |
| 3 | City Lab | Already standing in the town centre |

Public world fallback: https://wonderful-rock-0c758e403.5.azurestaticapps.net/ — skip Poststrasse 9, walk Migros.

---

# 1 · Urban Green

## The link

**Urban Green** — http://localhost:8000  
*(Fly app name `urban-green` when you have a public host. Do not paste localhost as a hosted MVP.)*

Name a Zürich street, click one on the map, or open **Langstrasse**.
Green / amber / red is the verdict on each ring.
Drag **Years after planting** from 0 to 30, then **Show shade**.
**Duplicate plan**, change spacing or species, **Compare plans**, **Download map data**.
No login. The plan panel works without an API key.

---

## Part 1 — How it is built
*to Lukas · ~2:15*

**Title card.** Part 1 — How it is built

**Show.** Langstrasse. Year 30. Crowns on. Do not click yet.

This is Urban Green. Name a street. In under a second you get every legal trunk under a cited planting standard, and the canopy those trees throw in thirty years.

**Show.** Point at a green ring, an amber ring, a red ring. Hover the red.

Colour is the verdict. Green: no evaluated rule failed. Amber: a recommendation failed — the tree still counts toward canopy. Red: a required rule failed — it adds no canopy, no shade, no cooling. This popup is the measurement, the required metres, must or should, and the page: Berlin street-tree standard, September 2024, section 5.2. If the layer was estimated, it says so.

**Show.** Stay on the map.

The cut is deliberate. Above ground only. One sun: the fifteenth of July, three in the afternoon. Three distances are verified from the standard. The rest are tagged planning defaults — plantable surface, five metres to existing trees, ten metres to junctions, remaining sidewalk. No utilities. No pit size. No hourly shade. Every number here I can explain.

**Show.** Editor: `rules/berlin_strassenbaeume_2024.json`. Scroll to `carriageway_edge`. Hold `source_ref`, `quote`, `mode: must`. Then `building_crown` (`should`, assumption false) and one `assumption: true` rule.

This is the contract. Trunk to carriageway: half a metre, must, Abschnitt 5.2, quote from the Berlin page. Crown to building: one point five, should. The five-metre tree gap and the ten-metre junction are planning defaults — the file says so. No magic number in the engine. Shapely measures. The model never sees this GeoJSON.

**Show.** Browser `/docs`, `POST /api/plan`. Terminal: pytest already green.

Adapters in. Engine in the middle. Three fronts out: HTTP, server-sent events, MCP. Same `/api/plan` in curl, OpenAPI, and Claude Desktop. Tests go green without the network. One container. No key? The plan panel still works. That is a system, not a slide.

---

## Part 2 — Teach a planner
*to Raphael · ~2:30*

**Title card.** Part 2 — Using it

**Show.** Same tab. Do not reload. Open **Data sources**, then **Your tree plan**.

Here is the path you would actually take. Sources first. Cadastre for the road, the sidewalk, the buildings — measured. Trees from the city register when it answers, otherwise OpenStreetMap, and the screen tells you. You always know what you are looking at.

Two numbers: how many trees are proposed, and how much cover at year thirty. Green you keep. Amber you discuss on site. Red is out.

**Show.** Open **Why are positions flagged?** Click one flagged ring.

A flagged tree gives you the rule, the metres, and the source. That is the conversation with the site.

**Show.** **More planting options** → **Fit around obstacles**. Wait for the update. **Years after planting** 0 → 30. **Show shade**.

Even spacing is a grid. Fit around obstacles slides each tree to the nearest legal spot and names the rule that made the gap. Now grow them. Zero to thirty. Show shade — one summer afternoon, not a climate model. Use it to talk.

**Show.** **Duplicate plan**. Change mature crown or species — London plane, a wide crown. **Compare plans**. **Download map data**.

Watch this. Duplicate first, so you keep a version. Wider crowns: more ambers. Restore if you want — that was a what-if, not a new law. Compare: same street, two futures. Download the GeoJSON. Opens in QGIS tonight.

If **Ask the planning assistant** is on, ask why trees dropped out. It inspects this plan. It does not invent another.

---

## Part 3 — Where this goes
*to Raphael · ~2:00*

**Title card.** Part 3 — Next

**Show.** Stay on year 30.

The prize is not this map. It is the rule pack. Drop in JSON with a page and a quote — same runner. Next: Grün Stadt Zürich, VSS, and an extractor that may only quote a page it actually read. A new city is one adapter class.

Then we stack engines on the same street. This one plants. The next tool places a collection point. A road-space engine can cut the section first. And then you stand under the year-thirty crowns — that is the third project.

The tools are the product. HTTP, events, MCP. Any agent. Nobody needs this UI.

Week one: cache the adapters, climate per species, pit and root once the data exists, packs behind a login. Then a real street, with a planner in the room.

**Show.** Cut to Recycling Walk, home screen.

Same honesty. Different question.

---

# 2 · Recycling Walk

## The link

**Recycling Walk** — http://127.0.0.1:5173  
*(This is the app tab, not the API port. Paste a public HTTPS URL once hosted.)*

Open the home screen. Choose **Try the glass example**.
Read the additional-buildings number.
Switch **Today** and **With this point**.
Choose **Review proposed site**, then **Download review brief**.
Open the HTML file in a browser. No login.

---

## Part 1 — How it is built
*to Lukas · ~2:15*

**Title card.** Part 1 — How it is built

**Show.** Home. Point at **Try the glass example**. Click it. Keep the wait.

This answers a different question. Kreis 4. Glass. Three hundred metres. Where would one new collection point bring more mapped buildings into a short walk? Prepared public data. No login.

It will not quietly swap in another district. Official glass stays put. First run of the day is slower — the network waking up, not a freeze.

**Show.** Split: spinner on the left. Editor on the right: `data/placement/active.json`. Point at `version` and `stale`.

One claim, replayable. Extra mapped buildings inside the walking limit. Official points stay where the city put them. No residents. No tonnes. No cost. No approval.

This file is the geography. Version `public-47d55b23c676f576`. Stale is false. LV95 inside, WGS84 on the wire. Fail a refresh and we keep the last good file and mark it stale. We never invent a street.

**Show.** Result is up. Point at the source version on screen. It must match the file. Then **Today** / **With this point** without clicking away.

Candidates come off the network, fifty metres apart. Every rejection keeps a reason. The solver maximises extra buildings. If it is not proven, we do not say optimal. Today and with this point use the same building IDs, counted twice. AI may draft settings. It may not invent a count. No key? This still runs. Same rule as the trees: the model talks, the metres are ours.

---

## Part 2 — Teach a planner
*to Raphael · ~2:30*

**Title card.** Part 2 — Using it

**Show.** Result is up. Point at the additional-buildings number. Blue pin. **Today** → **With this point**. Click a newly reached building if one is labelled.

The map opened for free exploration. I explicitly chose the glass example. The app does not substitute another area when walking data is missing.

The blue pin — the tip is the site. These buildings just came inside the limit. This walk used to be too far. Now it is not. Buildings, not people. Missing paths stay unknown. We do not fill them in.

**Show.** **Review proposed site**. Read the checklist: space, vehicles, access, permissions.

A pin is not permission. Nearby addresses are landmarks. The coordinates identify the proposed location. You still walk the site.

**Show.** **Download review brief**. Open the HTML file.

Download the brief. The team can open it without this app. Proposed location, map link, access counts, sources, site checks. It remains a draft for municipal review.

If you have twenty seconds: **Change settings**, **All collection types**, **See what's missing**. Glass, metal, oil, textiles compared separately. Missing collection records stay visible as a data gap.

---

## Part 3 — Where this goes
*to Raphael · ~2:00*

**Title card.** Part 3 — Next

**Show.** Stay on the brief.

Next is not a smarter chatbot. Next is more real districts, and a planner on a live street. New Swiss areas already prepare on demand, capped and checksummed.

If this point closes, that is a different operation: who loses the walk, where one replacement wins the most back. Different date, different number. Not this screen.

Compose. Does this sidewalk still take a tree? That is Urban Green. Stand on the pin before the visit? That is where we are going.

**Show.** Cut to City Lab. Already walking.

The two tools stay on the map. The street is still missing.

---

# 3 · Adliswil · City Lab

## The link

**City Lab** — http://127.0.0.1:5173  
Public world if a reviewer only needs to walk: https://wonderful-rock-0c758e403.5.azurestaticapps.net/

Choose **Enter the world**. WASD walks, Shift runs.
Toggle **Clean streets** and **Original imagery**.
**From above** to orient, then **On foot**.
Paint a house → **Try Poststrasse 9 · instant example**.
Toggle **Show original 3D** / **Show painted 3D**. **Back to exploring**.

---

## Part 1 — How it is built
*to Lukas · ~2:15*

**Title card.** Part 1 — How it is built

**Show.** Walk five seconds. Hit a wall. Sprint once.

This is a world, not a form. React, Cesium, no ion key. Swiss terrain and imagery live from swisstopo. Buildings are pinned to tile **20260520** — twentieth of May, twenty twenty-six — same vintage as the game’s colliders. The city loads when you walk in.

The body is forty-two centimetres. Steps max eighteen — you cannot tunnel a wall at a run. Nearby furniture blocks you once it is ready and visible. We drop resolution before we drop frames.

**Show.** **Original imagery**, hold two seconds, then **Clean streets**.

Cleanup is a shader on mapped roads, not “we deleted the cars.” That is the honesty switch. Same idea as estimated versus measured.

**Show.** A bus or a person if they are there. Do not open a file tree.

Buses and the S4 tick in one worker on a saved timetable — not live delays. People and animals in another, five updates a second. Regeneration reads the existing snapshots. No live cadastre at runtime. The model is not in this loop.

---

## Part 2 — Teach a planner
*to Raphael · ~2:30*

**Title card.** Part 2 — Using it

**Show.** Keep walking. Do not stop unless the photo needs a pause.

You do not plan in this window. You stand on the street you would visit tomorrow. WASD walks. Shift runs. Drag or Q and R turn. Escape pauses. Site walk before the workshop.

**Show.** **From above**. Pan. **On foot**.

Town centre, station, Sihl, cable, Felsenegg. Orient, then come back down. Aerial hides the street life on purpose. You do not measure from the sky.

**Show.** A house-number plate if you see one.

Trust the building bodies and the town line — measured. House numbers come from official entrances; the plaque is drawn. Ordinary windows are illustration. Do not count them and call it a survey.

**Show.** Paint a house from a photo → **Try Poststrasse 9 · instant example**. Toggle **Show original 3D** / **Show painted 3D** once.

One building we actually reviewed: Poststrasse 9. Four storeys, the balcony stack, the shop, the set-back top. Flip original and painted. Chosen camera, not the photo’s pose. Flowers and signs are out. It is a discussion model — and that is enough to get a committee looking at the same house.

Address to official number to measured UUID. Vision is told: only what you see. No invented bays. Poststrasse 9 needs no key. A live upload does. The collision footprint does not change.

**Show.** **Back to exploring.** Stand on that sidewalk.

You leave with a shared picture, one honest façade, and a list of what is real versus drawn. Shade and collection scores stay in the other two tools. After this walk, open them on the same street.

---

## Part 3 — Where this goes
*to Raphael · ~2:00*

**Title card.** Part 3 — Next

**Show.** Slow walk. Then black.

This is where the other two land. Legal trunks. Extra buildings. We do not need another renderer. We need year-thirty crowns, a blue pin, and the façade the committee will actually see.

Week one: one provenance tree. Ten more façades, only what the photo shows. Detail is a budget. No interiors, no live timetable, no planning toolbar in this world until those two operations have been walked here.

Explorer is live today. This Cesium lab is next. Host it when a reviewer only needs: walk in, walk the street, open the house.

Three sharp operations. One street you can stand on. That is the platform.

**Black.**
