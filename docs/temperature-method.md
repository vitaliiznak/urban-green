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
   crowns before calculating percentages. Keep each point's local coverage.
3. Compute added canopy, in percentage points, from existing trees to existing
   plus proposed trees. Excluded candidates contribute no crown. Amber candidates
   remain included, consistently with the rest of the planner.
4. Central cooling = added canopy × 0.005°C. Sensitivity envelope = added canopy
   × [0, 0.009]°C. Calculate this separately at each sample and subtract from
   the same user-supplied reference temperature. The existing street summary
   remains the equally weighted sample average, calculated before rounding.
5. Separately calculate current-tree and proposed-tree sidewalk shade using the
   same street, date and time. Date/time affect shade geometry, not the empirical
   daytime air coefficient. Summer inputs are limited to June–August, 06:00–18:00.

## Point output

`TemperatureResult.samples` is a GeoJSON `FeatureCollection` of WGS84 points on
the sampled sidewalks. Each feature has these properties:

| Properties | Meaning |
| --- | --- |
| `sample_id`, `station_m`, `side` | Stable ID for the same street geometry, projected distance along the street axis in metres, and `left` or `right` relative to the axis direction. |
| `reference_air_c` | The same user-supplied air temperature at every point. |
| `proposed_air_c`, `proposed_air_low_c`, `proposed_air_high_c` | Central comparison and sensitivity envelope after added local canopy, in °C. |
| `cooling_c`, `cooling_low_c`, `cooling_high_c` | Positive reductions from the reference temperature, in °C. |
| `existing_local_canopy_pct`, `proposed_local_canopy_pct` | Local 10 m canopy coverage for the existing street and existing plus eligible proposed trees. |
| `existing_tree_shade`, `proposed_tree_shade` | Whether the sample lies within the existing-tree shadow geometry or existing plus eligible proposed-tree shadow geometry at the requested time. |

Shade membership uses the same WGS84 geometries as the shade map. An empty
shadow result produces `false`; when the sun is below the shade engine's usable
elevation, `when` states that no usable shadows are available. Shade membership
does not change the air-temperature coefficient. `sample_count` is the number of
returned features. Temperatures are rounded to two decimals, local canopy to
one decimal, and station distance to one decimal; rounded sample averages can
differ slightly from the independently rounded street summary.

## Interpretation

This is an exploratory transfer of an observational association. It has not been
calibrated or validated for a Zürich street. Actual
cooling may fall outside the sensitivity envelope. It does not establish a
causal prediction for an individual planting intervention.

Points close to added crowns can show more cooling than points farther along
the street or on the opposite sidewalk. These differences come only from the
modeled local canopy. The reference temperature remains uniform along the
street: this is not a measurement of existing spatial temperature differences.
Sample locations and displayed decimals do not imply measured microclimate
precision or capture wind, building geometry or heat stored in street materials.

The existing inventory is held constant; age applies only to proposed trees.
Incomplete tree inventories and estimated crowns/sidewalks affect the result.
No future climate warming is included. The engine does not model building shade,
ventilation, humidity, soil moisture, tree mortality or species-specific
transpiration. Air temperature is distinct from pavement surface temperature and
human thermal comfort.

The temperature result is separate from the tree map's year slider. Its caption
identifies the street and tree age; the separate shade reading includes date/time.
The cooling map uses a fixed 0–0.5°C scale and shows discrete sampled points, without
interpolating across unsampled space. The street profile previews up to 20 actual
samples per side at their distance along the street; the map and selector expose
all samples. Selecting a point updates its before/after comparison and nearby
canopy values. The shade action explicitly applies the comparison's age/date/time
to the shade view. Changed inputs, rules or plan selection invalidate the previous
result; delayed responses cannot replace a newer comparison. Tree settings and
restrictions apply automatically and refresh an existing cooling comparison,
retaining its selected point and map mode. Temperature input changes still need
an explicit recalculation.

## Validation

`tests/test_temperature.py` verifies the existing-tree baseline, range ordering,
excluded candidates, tree-age behavior, baseline-temperature shifts, spatial
variation near a single added crown and across sidewalks, valid point geometry,
stable sample IDs, aggregate consistency, shade-map membership, independent
shade timing, representative fallback, missing sidewalks, invalid dates and
API validation. These tests
verify implementation consistency, not local predictive accuracy.
