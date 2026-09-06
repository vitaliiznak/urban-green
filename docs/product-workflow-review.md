# Product workflow review — 6 September 2026

Reviewed the running app at http://localhost:8031/ in the browser, including a
390 CSS-pixel iframe for the responsive layout. Existing worktree changes were
preserved. This review covers the visible planning controls and their action
handlers; it does not establish uptime for every upstream city data service.

## Changes

- Show street selection first; reveal tree settings and results when usable.
  Keep plan history available when switching cities and identify its streets.
- Disable plan creation when the current settings are unchanged. Reverting an
  edit restores the up-to-date state; changed settings use **Apply changes**.
- Reduce the map toolbar to its current task and a compact color key. Keep
  detailed symbol explanations in Map layers & key.
- Expand flagged-position details and temperature inputs on demand. The review
  shortcut opens the temperature section and focuses its reference input.
- Remove the duplicate shade checkbox, repeated planning-default labels, and
  duplicate drawing controls while drawing. Shade says Show shade / Hide shade.
- Hide real-street search and map selection in the synthetic demo; explain its
  example/drawing options. Keep a clear distinction between real and demo data.
- Bring errors into view and restore the street picker after failed search.
  Back navigation targets street selection when no plan exists.
- Version local script and stylesheet URLs to prevent stale cached controls
  from being combined with an updated document.

## Verification

| Control or workflow | Evidence |
| --- | --- |
| Initial screen | Browser shows city, search, drawing and examples; later stages and growth controls stay hidden until ready. |
| Example / named search | Langstrasse loads 37 existing trees and produces 144 proposed trees under the default rules; unknown street returns a readable error and reopens selection. |
| Click street | Clicking the real map selects Ankerstrasse, 240 m, with 10 existing trees and 26 proposed trees. |
| Drawing | Two points enable Finish; Undo returns it to disabled; finishing produces a 20 m drawn section and a plan. Cancel drawing and Cancel street selection were clicked and preserve the plan; Escape is wired to the same exit handlers. |
| Plan settings | Spacing edit enables Apply changes; reverting disables it. Choosing a 6 m hornbeam creates a second plan with 12.1% cover, versus 25.5% for 10 m lime. Final check also applies a custom 4 m crown with Fit around obstacles and the name Smaller crowns: 156 proposed trees and 7.6% cover. Left/right/both-side controls were exercised and reverting restored the up-to-date state. |
| Review and site inspection | Review shortcut reaches the result; expanded conflict reason opens the relevant tree popup with its failed checks and sources. |
| Temperature and shade | With a 32°C reference, the lime plan gives an illustrative 31.85°C result and 6.0% → 45.9% sidewalk shade. The linked map reports the same 45.9% for July 15 at 15:00. |
| Map controls | All ten layer checkboxes toggle; aerial/grey switching renders the selected basemap; existing-tree, plan-map and recenter actions work. Shade toggles its state and label. Growth plays, pauses and responds to the year slider. |
| Alternatives / comparison | Two plans appear in the comparison; pressing Enter on a row selects that plan and closes the dialog. Comparison remains limited to the current street. |
| Rules | Keyboard edit marks a threshold edited and recalculates; checkbox changes active-rule count; must/should switches work; Restore default rules resets them. |
| Export | Download link targets the selected scenario. Verified HTTP 200, attachment filename and a valid GeoJSON FeatureCollection with 455 features for the reviewed lime plan. |
| Assistant | Open, Send and Close work. A live explanation uses the existing plan and preserves the number of alternatives. Example prompts and trace disclosure have connected handlers. |
| Recovery / city changes | Demo creates an illustrative plan; changing city hides its settings while retaining history with the street name. Default rules were restored after verification. |
| Responsive layout | At 390 CSS pixels, street selection and result controls fit; Review → Show plan on map → Back to settings works using clicks. This is a responsive browser-frame check, not physical-device testing. |

`node --check web/app.js` and `git diff --check` pass. Backend regression suite:
**97 passed, 3 skipped**. The skipped tests require optional upstream access.
Conditional retry of Zürich's tree inventory retains its existing handler; the
live inventory was available during this review, so that fallback was checked
in code rather than triggered by a simulated outage.

Final browser console check: no errors. Left Langstrasse open with the default
10 m crown plan and the named 4 m crown alternative. Removed the temporary
responsive test page after verification.
