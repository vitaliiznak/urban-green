# One video — three projects, three registers

One take, or three cuts that join as one film. Never say anyone’s name. Never say “part two.” The people in the room change what you emphasise. They do not get announced.

| On screen (not spoken) | Voice | You sound like |
|---|---|---|
| **Build** | Engineering | Short. How it works. What the model must not do. |
| **Use** | Planning | You / we. Button names. Look at this. |
| **Next** | Vision | We. Week one. Where the three pieces meet. |

If a number on screen differs, say the screen.

**Runtime.** About 12 minutes. Breath at each tab change. No recap at the end.

---

## Tabs

| Tab | App | When you cut to it |
|---|---|---|
| 1 | Urban Green | Langstrasse already planned, aerial, year **30** |
| 2 | Recycling Walk | Home. Glass example warmed once off-camera, then reloaded |
| 3 | City Lab | Already standing in the town centre |

Explorer backup: https://wonderful-rock-0c758e403.5.azurestaticapps.net/ — skip Poststrasse 9, walk Migros.

## Link (paste with the video)

**Urban Green** — http://localhost:8000 (Fly when you have it)  
Zürich → Langstrasse. Green / amber / red. Year 0–30. Show shade. Compare. Download map data.

**Recycling Walk** — http://127.0.0.1:5174  
Home. Glass example. One app, one tab — not the API port.

**City Lab** — http://127.0.0.1:5173  
Already standing in the town centre.

Explorer is the public world if you need one.

---

# The film

Read top to bottom. *Italic* is the register — do not say it.

---

## Urban Green

*Build.*

**Show.** Langstrasse, year 30. Crowns on. Don’t click yet.

This is Urban Green. Name a street — in under a second you get every legal trunk under a cited rule pack, and the canopy those trees throw in thirty years.

**Show.** Point green, amber, red. Hover a red.

Colours are the verdict. Green: clean. Amber: a should-rule failed, the tree still counts. Red: a must-rule failed, it adds nothing. This popup is the measurement, the required metres, must or should, and the page — Berlin, September 2024, section 5.2. If the layer was estimated, it says so.

I cut hard on purpose. Above ground. One sun: 15 July, 15:00. Three verified distances, the rest tagged planning default. No utilities, no pit size, no hourly shade. Every number here I can explain.

**Show.** CONTRACT.md, five seconds. Back to the map.

Adapters in, engine in the middle, three fronts out: HTTP, SSE, MCP. Shapely does the metres. Rules are JSON with a source and a quote — never a magic number in code. The model only orchestrates. It never sees GeoJSON.

**Show.** `/docs`, then pytest already green.

Same tools in curl, OpenAPI, Claude Desktop. Tests green without the network. One container. No key? The plan panel still works. That’s a system, not a slide.

*Use.* Same tab. Don’t reload.

**Show.** **Data sources and limitations**, then **Review your plan**.

Here’s the path you’d actually take. Sources first. Cadastre for the road, the sidewalk, the buildings — measured. Trees from the city register when it answers, otherwise OSM, and it tells you. You always know what you’re looking at.

Two numbers: how many trees, and how much cover at year thirty. Green you keep. Amber you discuss. Red is out.

**Show.** **Why are positions flagged?** Click one.

A flagged tree gives you the rule, the metres, and the source. That’s the conversation with the site.

**Show.** **Fit around obstacles** → Apply. Year 0 → 30. **Show shade**.

Even spacing is a grid. Fit around obstacles slides each tree to the nearest legal spot and names the rule that made the gap. Now grow them. Zero to thirty. Show shade — one summer afternoon, not a climate model. Use it to talk.

**Show.** Crown-to-building 1.5 → 3. Restore. London plane, ~12 m, **Wide planes**. **Compare**. **Download map data**.

Watch this. Building rule to three metres — more ambers. Restore. That was a what-if, not a new law. Second plan: wider, planes. Compare — same street, two futures. Download the GeoJSON. Opens in QGIS tonight. If the assistant is on, ask why trees dropped out. It inspects this plan. It does not invent another.

*Next.* Stay on year 30.

The prize is not this map. It’s the rule pack. Drop in JSON with a page and a quote — same runner. Next: Grün Stadt Zürich, VSS, and an extractor that may only quote a page it actually read. A new city is one adapter class.

Then we stack engines on the same street. This one plants. The next tool places a collection point. A road-space engine can cut the section first. And then you stand under the year-thirty crowns — that’s the third project.

The tools are the product. HTTP, SSE, MCP. Any agent. Nobody needs this UI.

Week one: cache the adapters, climate per species, pit and root once the data exists, packs behind a login. Then a real street, with a planner in the room.

**Show.** Cut to Recycling Walk, home screen.

Same honesty. Different question.

---

## Recycling Walk

*Use*, then *Build* in the wait, then *Use* again. That’s the natural order here.

**Show.** Point at **Try the glass example**. Click it. Keep the wait.

Kreis 4. Glass. Three hundred metres. Where would one new point bring more buildings into a short walk? Prepared public data. No login. Let’s run it.

It will not quietly swap in another district. Official glass stays put. First run of the day is slower — the network waking up, not a freeze.

*Build.* Still on the spinner or the first result.

While that’s working: one claim, replayable. Extra mapped buildings inside the limit. Official points nailed down. No residents, no tonnes, no cost, no approval.

LV95 inside, WGS84 on the wire. Walking graph is a versioned snapshot. Fail a refresh and we keep the last good file and mark it stale. We never invent geography.

Candidates from the network, fifty metres apart, every rejection keeps a reason. Exact MIP. Max extra buildings. Thirty seconds. If it isn’t proven, we do not say optimal. Before and after are the same building IDs, counted twice.

AI may draft settings or extra questions. It may not invent a count. No key? This still runs. Same rule as the trees: the model talks, the metres are ours.

*Use.* Result is up.

**Show.** Big additional-buildings number. Blue **New point 1**. **This building**. **Before → After**.

Blue pin — the tip is the site. These buildings just came inside the limit. This walk used to be too far. Now it isn’t. Buildings, not people. Missing paths stay unknown. We don’t fill them in.

**Show.** **Compare 1 vs 2 points**. Keep one point.

A second site: more reach. Worth it? That’s the call — cost isn’t in here. I keep one point for the visit.

**Show.** **Prepare site review**. Note: “Narrow access. Check ownership.” **Save**. Open the brief.

A pin is not permission. Space, vehicles, access, ownership — you still walk it. Write the worry, save, download a brief the team can open without this app. Both options stay in the file.

*Next.* Stay on the brief.

Next is not a smarter chatbot. Next is more real districts, and a planner on a live street. New Swiss areas already prepare on demand, capped and checksummed.

If this point closes, that’s a different product: who loses the walk, where one replacement wins the most back. Different date, different number. Not this screen.

Compose. Does this sidewalk still take a tree? That’s Urban Green. Stand on the pin before the visit? That’s where we’re going.

**Show.** Cut to City Lab. Already walking.

The two tools stay on the map. The street is still missing.

---

## Adliswil

*Build* while you walk. Then *Use*. Then *Next*. Don’t stop walking unless the photo needs a pause.

**Show.** Walk five seconds. Hit a wall. Sprint once.

This is a world, not a form. React, Cesium, no ion key. Swiss terrain and imagery live from swisstopo. Buildings pinned to 20 May 2026 — same vintage as the game’s colliders. The city loads when you walk in.

Body is 42 centimetres. Steps max 18 — you cannot tunnel a wall at a run. Cleanup is a shader on mapped roads, not “we deleted the cars.”

**Show.** **Original imagery**, then **Clean streets**.

That’s the honesty switch. Same idea as estimated versus measured.

**Show.** A bus or a person if they’re there.

Buses and the S4 run in a worker on a saved timetable — not live delays. People and animals in another. We drop resolution before we drop frames. Regeneration reads the existing snapshots. No live cadastre at runtime.

*Use.* Keep walking.

You don’t plan in this window. You stand on the street you’d visit tomorrow. WASD walks, Shift runs, Q and R turn. Escape pauses. Site walk before the workshop.

**Show.** **From above**. Pan. Back down.

Centre, station, Sihl, cable, Felsenegg. Orient, then come back down. Aerial hides the street life on purpose. You don’t measure from the sky.

**Show.** A house-number plate if you see one.

Trust the building bodies and the town line — measured. House numbers come from official entrances; the plaque is drawn. Ordinary windows are illustration. Don’t count them and call it a survey.

**Show.** **Paint a house from a photo** → **Try Poststrasse 9** → **Compare**. Toggle once.

One building we actually reviewed: Poststrasse 9. Four storeys, the balcony stack, the shop, the set-back top. Flip original and enhanced. Chosen camera, not the photo’s pose. Flowers and signs are out. It’s a discussion model — and that’s enough to get a committee looking at the same house.

Address to official number to measured UUID. Vision is told: only what you see. No invented bays. Poststrasse 9 needs no key. A live upload does. The collision footprint does not change.

**Show.** **Back to exploring.** Stand on that sidewalk.

You leave with a shared picture, one honest facade, and a list of what’s real versus drawn. Shade and collection scores stay in the other two tools. After this walk, open them on the same street.

*Next.* Slow walk. Then black.

This is where the other two land. Legal trunks. Extra buildings. We don’t need another renderer. We need year-thirty crowns, a blue pin, and the facade the committee will actually see.

Week one: one provenance tree. Ten more facades, only what the photo shows. Detail is a budget. No interiors, no live timetable, no planning toolbar in this world until those two operations have been walked here.

Explorer is live today. This Cesium lab is next. Host it when a reviewer only needs: walk in, walk the street, open the house.

Three sharp operations. One street you can stand on. That’s the platform.

**Black.**
