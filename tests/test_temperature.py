"""Temperature transfer model invariants, without live weather or LLM calls."""
from dataclasses import replace
from statistics import mean

import pytest
import shapely
from fastapi.testclient import TestClient
from shapely.geometry import Point, Polygon, box, shape

from server.app import app
from server.crs import to_metric
from server.engine.rules import load_rule_packs
from server.engine.shade import shade_polygons
from server.engine.sites import plan_sites
from server.engine.species import species_or_default
from server.engine.synthetic import synthetic_street
from server.engine.temperature import temperature_comparison
from server.schemas import PlanParams, TemperatureRequest, TemperatureSampleProps


@pytest.fixture
def model():
    ctx = synthetic_street()
    species = species_or_default("tilia_cordata")
    sites, _ = plan_sites(ctx, PlanParams(), load_rule_packs()["berlin_strassenbaeume_2024"], species)
    return ctx, species, sites


def calculate(model, **kwargs):
    ctx, species, sites = model
    return temperature_comparison(ctx, sites, species, TemperatureRequest(scenario_id="test", **kwargs))


def test_comparison_uses_existing_baseline_and_ordered_sensitivity(model):
    result = calculate(model)
    assert 0 < result.existing_local_canopy_pct < result.proposed_local_canopy_pct <= 100
    assert result.sample_count >= 40
    assert 0 <= result.cooling_low_c <= result.cooling_c <= result.cooling_high_c
    assert result.proposed_air_low_c <= result.proposed_air_c <= result.proposed_air_high_c == 30
    assert result.proposed_air_c == round(30 - result.cooling_c, 2)
    assert result.existing_sidewalk_shade_pct <= result.proposed_sidewalk_shade_pct
    assert result.source_url.endswith("/tables/1")
    assert any("not calibrated" in note for note in result.limitations)
    assert any("not a statistical confidence" in note for note in result.limitations)


def test_samples_are_located_on_sidewalks_with_stable_ids(model):
    ctx, _, _ = model
    result = calculate(model)
    changed_inputs = calculate(model, year=0, reference_air_c=35, hour=9)
    assert result.samples.type == "FeatureCollection"
    assert result.sample_count == len(result.samples.features)
    assert {f["properties"]["side"] for f in result.samples.features} == {"left", "right"}
    assert len({f["properties"]["sample_id"] for f in result.samples.features}) == result.sample_count
    for sample, changed in zip(result.samples.features, changed_inputs.samples.features):
        assert sample["type"] == "Feature"
        assert sample["geometry"]["type"] == "Point"
        lon, lat = sample["geometry"]["coordinates"]
        assert 8 < lon < 9 and 47 < lat < 48  # WGS84, not the engine's Swiss metre coordinates
        point = to_metric(shape(sample["geometry"]), ctx.epsg)
        props = TemperatureSampleProps.model_validate(sample["properties"])
        assert ctx.sidewalks.buffer(0.02).covers(point)
        assert 0 <= props.station_m <= ctx.length_m
        assert props.station_m == pytest.approx(ctx.axis.project(point), abs=0.06)
        assert props.side == ("left" if point.y > ctx.axis.coords[0][1] else "right")
        assert props.reference_air_c == 30
        assert props.proposed_air_low_c <= props.proposed_air_c <= props.proposed_air_high_c == 30
        assert 0 <= props.cooling_low_c <= props.cooling_c <= props.cooling_high_c
        assert props.existing_local_canopy_pct <= props.proposed_local_canopy_pct
        assert sample["geometry"] == changed["geometry"]
        for key in ("sample_id", "station_m", "side"):
            assert sample["properties"][key] == changed["properties"][key]


def test_street_summary_is_the_sample_average(model):
    result = calculate(model)
    samples = [f["properties"] for f in result.samples.features]
    assert len({p["cooling_c"] for p in samples}) > 1
    # API values are rounded independently; aggregation uses unrounded values.
    for key in ("proposed_air_c", "proposed_air_low_c", "proposed_air_high_c", "cooling_c", "cooling_high_c"):
        assert getattr(result, key) == pytest.approx(mean(p[key] for p in samples), abs=0.01)
    for key in ("existing_local_canopy_pct", "proposed_local_canopy_pct"):
        assert getattr(result, key) == pytest.approx(mean(p[key] for p in samples), abs=0.1)


def test_point_cooling_is_strongest_near_added_canopy_and_varies_across_street(model):
    ctx, species, sites = model
    ctx.existing_trees = []
    x0, y0 = ctx.axis.coords[0]
    proposed = replace(next(s for s in sites if s.verdict == "valid"),
                       pt=Point(x0 + 175, y0 + 5.25), station_m=175, side="left")
    result = temperature_comparison(ctx, [proposed], species, TemperatureRequest(scenario_id="test"))
    points = {(f["properties"]["station_m"], f["properties"]["side"]): f["properties"]
              for f in result.samples.features}
    near = points[(175, "left")]
    opposite = points[(175, "right")]
    distant = points[(245, "left")]
    assert near["cooling_c"] > opposite["cooling_c"] > distant["cooling_c"] == 0
    assert near["proposed_air_c"] < opposite["proposed_air_c"] < distant["proposed_air_c"] == 30
    assert near["cooling_c"] > points[(165, "left")]["cooling_c"] > 0
    assert all(p["existing_local_canopy_pct"] == 0 for p in points.values())


def test_point_shade_matches_the_shade_map_geometry(model):
    ctx, species, sites = model
    result = calculate(model, hour=12)
    shade_args = dict(year=30, month=7, day=15, hour=12, include_existing=True)
    baseline = shade_polygons(ctx, [], species, **shade_args)
    proposed = shade_polygons(ctx, sites, species, **shade_args)
    baseline_union = shapely.union_all([shape(f["geometry"]) for f in baseline.shadows.features])
    proposed_union = shapely.union_all([shape(f["geometry"]) for f in proposed.shadows.features])
    assert {f["properties"]["proposed_tree_shade"] for f in result.samples.features} == {True, False}
    for sample in result.samples.features:
        point = shape(sample["geometry"])
        assert sample["properties"]["existing_tree_shade"] == baseline_union.covers(point)
        assert sample["properties"]["proposed_tree_shade"] == proposed_union.covers(point)


def test_no_proposed_trees_means_no_added_cooling_or_shade(model):
    ctx, species, _ = model
    result = temperature_comparison(ctx, [], species, TemperatureRequest(scenario_id="test", reference_air_c=33.5))
    assert result.cooling_c == result.cooling_low_c == result.cooling_high_c == 0
    assert result.proposed_air_low_c == result.proposed_air_high_c == result.proposed_air_c == 33.5
    assert result.existing_sidewalk_shade_pct == result.proposed_sidewalk_shade_pct
    for sample in result.samples.features:
        props = sample["properties"]
        assert props["cooling_c"] == props["cooling_high_c"] == 0
        assert props["proposed_air_c"] == props["reference_air_c"] == 33.5
        assert props["existing_tree_shade"] == props["proposed_tree_shade"]


def test_excluded_sites_do_not_affect_temperature(model):
    ctx, species, sites = model
    rejected = [s for s in sites if s.verdict == "invalid"]
    assert rejected
    req = TemperatureRequest(scenario_id="test")
    assert temperature_comparison(ctx, rejected, species, req) == temperature_comparison(ctx, [], species, req)


def test_age_changes_canopy_and_baseline_input_only_shifts_absolute_temperature(model):
    young = calculate(model, year=0)
    mature = calculate(model, year=30)
    warmer = calculate(model, reference_air_c=35)
    assert mature.cooling_c > young.cooling_c
    assert mature.cooling_c == warmer.cooling_c
    assert warmer.proposed_air_c - mature.proposed_air_c == pytest.approx(5)


def test_time_changes_shade_not_empirical_air_coefficient(model):
    morning = calculate(model, hour=9)
    noon = calculate(model, hour=12)
    assert morning.cooling_c == noon.cooling_c
    assert morning.proposed_sidewalk_shade_pct != noon.proposed_sidewalk_shade_pct
    assert morning.when.endswith("09:00 local")
    assert [f["properties"]["cooling_c"] for f in morning.samples.features] == [
        f["properties"]["cooling_c"] for f in noon.samples.features]
    assert [f["properties"]["proposed_tree_shade"] for f in morning.samples.features] != [
        f["properties"]["proposed_tree_shade"] for f in noon.samples.features]


def test_representative_fallback_still_returns_a_located_sample(model):
    ctx, _, _ = model
    x0, y0 = ctx.axis.coords[0]
    ctx.sidewalks = box(x0 + 9, y0 + 4, x0 + 10, y0 + 6)
    result = calculate(model)
    assert result.sample_count == len(result.samples.features) == 1
    props = result.samples.features[0]["properties"]
    assert props["station_m"] == 9.5
    assert props["side"] == "left"
    assert result.cooling_c == props["cooling_c"]


def test_missing_sidewalk_and_invalid_date_are_explicit(model):
    with pytest.raises(ValueError):
        calculate(model, month=6, day=31)
    model[0].sidewalks = Polygon()
    with pytest.raises(ValueError, match="mapped sidewalks"):
        calculate(model)


def test_temperature_api_and_validation():
    with TestClient(app, headers={"X-Session-Id": "temperature-test"}) as client:
        street = client.post("/api/street", json={"city": "demo", "query": "temperature demo"}).json()
        scenario = client.post("/api/plan", json={"street_id": street["street_id"]}).json()
        body = {"scenario_id": scenario["scenario_id"], "reference_air_c": 32}
        response = client.post("/api/temperature", json=body)
        assert response.status_code == 200, response.text
        assert response.json()["reference_air_c"] == 32
        assert response.json()["scenario_id"] == scenario["scenario_id"]
        data = response.json()
        assert data["samples"]["type"] == "FeatureCollection"
        assert len(data["samples"]["features"]) == data["sample_count"]
        for sample in data["samples"]["features"]:
            assert sample["geometry"]["type"] == "Point"
            props = TemperatureSampleProps.model_validate(sample["properties"])
            assert props.reference_air_c == 32
        for invalid in ({"year": 31}, {"month": 1}, {"month": 6, "day": 31}, {"hour": 23}, {"reference_air_c": 70}):
            response = client.post("/api/temperature", json={**body, **invalid})
            assert response.status_code == 422
            assert response.json()["error"]["message"]
        assert client.post("/api/temperature", json={"scenario_id": "missing"}).status_code == 404
