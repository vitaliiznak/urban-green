"""Temperature transfer model invariants, without live weather or LLM calls."""
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Polygon

from server.app import app
from server.engine.rules import load_rule_packs
from server.engine.sites import plan_sites
from server.engine.species import species_or_default
from server.engine.synthetic import synthetic_street
from server.engine.temperature import temperature_comparison
from server.schemas import PlanParams, TemperatureRequest


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


def test_no_proposed_trees_means_no_added_cooling_or_shade(model):
    ctx, species, _ = model
    result = temperature_comparison(ctx, [], species, TemperatureRequest(scenario_id="test", reference_air_c=33.5))
    assert result.cooling_c == result.cooling_low_c == result.cooling_high_c == 0
    assert result.proposed_air_low_c == result.proposed_air_high_c == result.proposed_air_c == 33.5
    assert result.existing_sidewalk_shade_pct == result.proposed_sidewalk_shade_pct


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
        for invalid in ({"year": 31}, {"month": 1}, {"month": 6, "day": 31}, {"hour": 23}, {"reference_air_c": 70}):
            response = client.post("/api/temperature", json={**body, **invalid})
            assert response.status_code == 422
            assert response.json()["error"]["message"]
        assert client.post("/api/temperature", json={"scenario_id": "missing"}).status_code == 404
