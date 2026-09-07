# Exploratory air-temperature comparison

The planner compares the existing-tree baseline with the selected proposed plan
under identical weather. It does not retrieve weather measurements. The user
supplies a reference air temperature; 30°C is only the initial example input.

## Research basis

[Scientific Reports (2024), Table 1](https://www.nature.com/articles/s41598-024-51921-y/tables/1)
reports a canopy coefficient of −0.006°C per percentage point and a daytime
interaction of +0.001, from hourly measurements in South Tacoma during summer 2022.
The resulting daytime association is −0.005°C per percentage point of canopy.

The two reported marginal coefficient intervals are −0.008 to −0.003 and
−0.001 to +0.003. Combining their endpoints gives a sensitivity envelope of
−0.009 to 0°C per percentage point. This construction is **not a joint 95%
confidence interval**, a forecast interval, or a bound on cooling in other cities.
The table coefficients are used directly rather than the rounded values in the
article's prose.

## Calculation

1. Sample mapped sidewalk cross-sections along the street, approximately every
   10 m, capped at 400 stations. If no cross-section intersects mapped sidewalks,
   use one representative sidewalk point. Reject streets without sidewalk area.
2. At each point, calculate horizontal canopy coverage within a 10 m radius,
   matching the local scale of the study. Existing crowns come from the inventory;
   proposed crowns use the selected species and tree age. Union overlapping
   crowns before calculating percentages. Average across the sample points.
3. Compute added canopy, in percentage points, from existing trees to existing
   plus proposed trees. Excluded candidates contribute no crown. Amber candidates
   remain included, consistently with the rest of the planner.
4. Central cooling = added canopy × 0.005°C. Sensitivity envelope = added canopy
   × [0, 0.009]°C. Subtract those values from the user's baseline temperature.
5. Separately calculate current-tree and proposed-tree sidewalk shade using the
   same street, date and time. Date/time affect shade geometry, not the empirical
   daytime air coefficient. Summer inputs are limited to June–August, 06:00–18:00.

## Interpretation

This is an exploratory transfer of an observational association. It has not been
calibrated or validated for a Zürich street. Actual
cooling may fall outside the sensitivity envelope. It does not establish a
causal prediction for an individual planting intervention.

The existing inventory is held constant; age applies only to proposed trees.
Incomplete tree inventories and estimated crowns/sidewalks affect the result.
No future climate warming is included. The engine does not model building shade,
ventilation, humidity, soil moisture, tree mortality or species-specific
transpiration. Air temperature is distinct from pavement surface temperature and
human thermal comfort.

The temperature result is separate from the map's year slider. Its caption always
identifies its plan, age and date/time. The map action explicitly applies those
conditions to the shade view. Changed inputs or plan selection invalidate the
previous result; delayed responses cannot replace a newer comparison.

## Validation

`tests/test_temperature.py` verifies the existing-tree baseline, range ordering,
excluded candidates, tree-age behavior, baseline-temperature shifts, independent
shade timing, missing sidewalk data, invalid dates and API validation. These tests
verify implementation consistency, not local predictive accuracy.
