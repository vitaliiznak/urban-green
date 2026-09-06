# Critical review — 6 September 2026

Three independent agents reviewed the running Allee app: one used Browser for
interactive workflow checks, one used Computer Use in Microsoft Edge for visual
and accessibility review, and one traced client/server behavior in the code.
Existing worktree changes were preserved. The updated app runs at
http://localhost:8031/.

## Findings and changes

| Finding | Change | Evidence |
|---|---|---|
| Comparisons mixed Zürich and synthetic demo plans without identifying streets, and ranked unrelated study areas. | Normal comparisons use the current street; API rows identify street, city and tree source; different street contexts are not ranked. | Browser comparison includes only the two Demo alternatives; API mixed-street regression passes. |
| Main comparison measure was outside the visible table. | Six primary columns include tree cover; settings and other measures expand separately. | Computer Use review saw all six desktop columns and both alternatives' cover values. |
| An exact existing-trunk position counted as a proposed tree. | Independent geometry safeguard rejects numerical coincidences; the wider editable spacing recommendation retains its behavior. | Demo changes from 100 to 97 proposed trees and 3 excluded positions; Browser inspected the explicit safeguard; engine regressions cover grid, pack, canopy and shade. |
| Explanation requests created duplicate plans and implied trees were already planted or approved. | Read-only `inspect_plan` tool; proposed-tree language; explicit planning-estimate and must/should instructions. | Repeated live explanation used `inspect_plan` and preserved one plan; scripted SSE regression asserts no new scenarios or UI mutations. |
| Assistant and result panel used unexplained different failure totals. | Both distinguish amber and excluded positions for each reason. | Computer Use verified 211 amber + 8 excluded building conflicts, and 69 amber + 2 excluded junction conflicts on the reviewed Langstrasse plan. |
| Assistant suggestions, technical traces and literal formatting crowded the answer. | Suggestions hide after the first request; actions collapse; safe inline bold rendering; quota-only footer. | Computer Use verified a readable complete answer and expandable action trace. |
| Ready results were below the sidebar fold. | Compact result summary links directly to the review section. | Computer Use verified first-view visibility and the jump. |
| Popups and delayed shade responses could refer to a previous selection. | Scenario changes remove pinned popups; shade invalidates old work and retains custom date/time. | Browser verified popup removal and June 15 noon shade remaining selected when the year changed. |
| Rule changes could overlap or leave a historical plan looking current. | Lock controls through rule mutation/replanning and show explicit pending-rule action and explanation. | Mutation ordering checked in code; Browser verified the explicit pending-rule explanation and Apply rules action when selecting a historical plan. |
| Assistant comparison events could overwrite an explicitly requested subset; older rows could fail to select. | Stream events are awaited in order, compare payload is authoritative, and rows fetch missing scenarios before selection. | Code review of event ordering; scenario and comparison API regressions. |
| Shade and site explanations for an older plan could target the active plan's map. | Tool events select the requested scenario before changing year, layer or focus. | Parameterized SSE regressions and live Browser checks passed: earlier lime-plan shade showed 21.7% sidewalk shading instead of the maple plan's 27.6%, and its site popup showed the lime's 10 m crown. |
| Site tool text mislabeled axis-to-road-edge distance as trunk clearance. | Corrected the geometry label and prioritized failed checks in the observation. | Regression checks distinguish the demo's 3.50 m axis distance from its 1.00 m trunk clearance. |

## Validation

- `python -m pytest -q`: **70 passed, 3 skipped**. Skipped adapter tests remain skipped;
  this result does not establish availability of every upstream data service.
- `node --check web/app.js` and `git diff --check`: passed.
- Desktop Browser and Computer Use re-reviews passed the inspected workflows.
- At an effective 390 CSS-pixel width, Browser verified no document horizontal
  overflow and working review/map/back navigation. The external browser's
  screenshot/zoom mismatch prevented a complete narrow-screen visual assessment.
- [Desktop comparison screenshot](../output/critical-review/comparison-desktop.jpeg).

These checks cover the reported issues and representative workflows, not every
possible model response or future upstream-data condition. Plans remain temporary
and the planner's existing site-assessment limitations still apply.
