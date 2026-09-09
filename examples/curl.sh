#!/usr/bin/env bash
# Urban Green HTTP API walkthrough. Usage: BASE=http://localhost:8000 ./examples/curl.sh
set -euo pipefail
BASE="${BASE:-http://localhost:8000}"
SID="example-$(date +%s)"
H=(-s -H "Content-Type: application/json" -H "X-Session-Id: $SID")

echo "# 1. What this server offers (cities, rule packs, species, agent status)"
curl "${H[@]}" "$BASE/api/config" | jq '{cities: [.cities[].id], packs: [.rule_packs[].id], species: (.species | length), agent}'

echo "# 2. Load a street (Zürich cadastre + tree cadastre, OSM fallback)"
STREET=$(curl "${H[@]}" -X POST "$BASE/api/street" -d '{"city":"zurich","query":"Langstrasse"}')
SID_STREET=$(echo "$STREET" | jq -r .street_id)
echo "$STREET" | jq '{street_id, name, length_m, stats, layer_basis, warnings}'

echo "# 3. Plan trees: 8 m spacing, both sides, small-leaved lime, grid mode"
A=$(curl "${H[@]}" -X POST "$BASE/api/plan" -d "{\"street_id\":\"$SID_STREET\",\"spacing_m\":8,\"side\":\"both\",\"species_id\":\"tilia_cordata\",\"mode\":\"grid\"}")
A_ID=$(echo "$A" | jq -r .scenario_id)
echo "$A" | jq '{scenario_id, label, summary, canopy30: (.canopy.years[] | select(.year==30))}'

echo "# 4. Same street, 12 m spacing, packed (greedy) placement"
B=$(curl "${H[@]}" -X POST "$BASE/api/plan" -d "{\"street_id\":\"$SID_STREET\",\"spacing_m\":12,\"side\":\"both\",\"species_id\":\"platanus_hispanica\",\"mode\":\"pack\"}")
B_ID=$(echo "$B" | jq -r .scenario_id)

echo "# 5. Compare"
curl "${H[@]}" -X POST "$BASE/api/compare" -d "{\"scenario_ids\":[\"$A_ID\",\"$B_ID\"]}" | jq '.rows[] | {label, planted, cover_corridor_pct_30, sidewalk_under_crown_pct_30}'

echo "# 6. Shade on 15 July at 15:00 local, year 30"
curl "${H[@]}" -X POST "$BASE/api/shade" -d "{\"scenario_id\":\"$A_ID\",\"year\":30}" | jq '{when, sun_elevation_deg, sun_azimuth_deg, shaded_sidewalk_pct, shaded_street_pct}'

echo "# 7. Tighten a rule for this session (carriageway edge 0.5 m -> 1.0 m) and re-plan"
curl "${H[@]}" -X PUT "$BASE/api/rules/berlin_strassenbaeume_2024/carriageway_edge" -d '{"min_distance_m":1.0}' | jq '.pack.rules[] | select(.id=="carriageway_edge")'
curl "${H[@]}" -X POST "$BASE/api/plan" -d "{\"street_id\":\"$SID_STREET\",\"spacing_m\":8}" | jq .summary
curl "${H[@]}" -X DELETE "$BASE/api/rules/overrides" >/dev/null

echo "# 8. Export"
curl -s -H "X-Session-Id: $SID" -o "canopy-$A_ID.geojson" "$BASE/api/export/$A_ID.geojson" && echo "wrote canopy-$A_ID.geojson"

echo "# 9. Ask the agent (SSE; needs an API key on the server)"
curl -N -s -H "Content-Type: application/json" -H "X-Session-Id: $SID" -X POST "$BASE/api/agent" \
  -d '{"message":"Plant as many trees as possible on Josefstrasse in Zürich, 8 m spacing, and tell me the 30-year canopy cover."}' | head -c 4000
echo
