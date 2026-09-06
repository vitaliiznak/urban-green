"""HTTP API tests against the offline "demo" city (no network, no LLM key)."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server import service
from server.agent import providers
from server.app import app
from server.ratelimit import LIMITER
from server.session import SESSIONS

SID = "test-session-0001"


@pytest.fixture(autouse=True)
def _clean_state():
    LIMITER.reset()
    SESSIONS.clear()
    service.STORE.clear()
    yield
    LIMITER.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, headers={"X-Session-Id": SID}, raise_server_exceptions=False)


def load_demo(client: TestClient) -> dict[str, Any]:
    r = client.post("/api/street", json={"city": "demo", "query": "Demo street"})
    assert r.status_code == 200, r.text
    return r.json()


def plan_demo(client: TestClient, **params: Any) -> dict[str, Any]:
    street = load_demo(client)
    body = {"street_id": street["street_id"], "spacing_m": 8, "side": "both", "species_id": "tilia_cordata", "mode": "grid"}
    body.update(params)
    r = client.post("/api/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.split("\n\n"):
        lines = [ln for ln in block.strip().splitlines() if ln and not ln.startswith(":")]
        if not lines:
            continue
        event = next(ln[len("event: "):] for ln in lines if ln.startswith("event: "))
        data = "".join(ln[len("data: "):] for ln in lines if ln.startswith("data: "))
        events.append((event, json.loads(data)))
    return events


# ------------------------------------------------------------------ meta
def test_health_and_config(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert set(health.json()["agent"]) == {"enabled", "provider", "model"}
    assert health.headers["X-Session-Id"] == SID

    cfg = client.get("/api/config").json()
    assert {c["id"] for c in cfg["cities"]} >= {"demo"}
    assert any(p["id"] == "berlin_strassenbaeume_2024" for p in cfg["rule_packs"])
    assert any(s["id"] == "tilia_cordata" for s in cfg["species"])
    assert cfg["defaults"]["spacing_m"] == 8.0
    assert cfg["agent"]["turns_per_hour"] == LIMITER.max_for("agent_hour")
    assert cfg["agent"]["example_prompts"]


def test_session_header_and_cookie() -> None:
    anon = TestClient(app)
    r = anon.get("/api/session")
    assert r.status_code == 200
    sid = r.headers["X-Session-Id"]
    assert len(sid) >= 8 and r.json()["session_id"] == sid
    assert anon.cookies.get("canopy_sid") == sid
    again = anon.get("/api/session")
    assert again.json()["session_id"] == sid          # cookie fallback keeps the session


def test_error_envelope(client: TestClient) -> None:
    r = client.get("/api/scenarios/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_scenario"
    assert "message" in r.json()["error"]
    r = client.post("/api/street", json={"city": "atlantis", "query": "x"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_city"
    r = client.post("/api/plan", json={"street_id": "missing"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_street"
    r = client.post("/api/plan", json={"spacing_m": 8})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


# ------------------------------------------------------------------ street & plan
def test_street_demo(client: TestClient) -> None:
    street = load_demo(client)
    assert len(street["street_id"]) == 8
    assert street["city"] == "demo" and street["length_m"] > 100
    assert set(street["layers"]) >= {"axis", "carriageway", "sidewalks", "existing_trees", "corridor"}
    assert street["stats"]["existing_trees"] >= 1
    assert len(street["bbox"]) == 4 and len(street["center"]) == 2
    cached = client.post("/api/street", json={"city": "demo", "query": "  demo STREET "}).json()
    assert cached["street_id"] == street["street_id"]


def test_plan_canopy_shade_site(client: TestClient) -> None:
    sc = plan_demo(client)
    assert len(sc["scenario_id"]) == 8
    assert sc["label"] == "8 m · Tilia cordata · 10 m crown · both · grid"
    assert sc["created_at"].endswith("Z")
    summary = sc["summary"]
    assert summary["total"] == len(sc["sites"]["features"]) > 10
    assert summary["planted"] == summary["valid"] + summary["conditional"]
    props = sc["sites"]["features"][0]["properties"]
    assert {"site_id", "station_m", "side", "verdict", "rules", "crown_d_by_year"} <= set(props)
    assert [y["year"] for y in sc["canopy"]["years"]] == [0, 5, 10, 20, 30]
    covers = [y["cover_corridor_pct"] for y in sc["canopy"]["years"]]
    assert covers == sorted(covers) and covers[-1] > 0

    got = client.get(f"/api/scenarios/{sc['scenario_id']}").json()
    assert got["scenario_id"] == sc["scenario_id"]

    canopy = client.post("/api/canopy", json={"scenario_id": sc["scenario_id"], "years": [0, 30]}).json()
    assert [y["year"] for y in canopy["years"]] == [0, 30]

    shade = client.post("/api/shade", json={"scenario_id": sc["scenario_id"], "year": 30, "month": 7, "day": 15, "hour": 15})
    assert shade.status_code == 200, shade.text
    body = shade.json()
    assert body["scenario_id"] == sc["scenario_id"]
    assert 0 < body["sun_elevation_deg"] < 90 and body["shaded_corridor_pct"] >= 0

    site_id = props["site_id"]
    site = client.get(f"/api/scenarios/{sc['scenario_id']}/sites/{site_id}").json()
    assert site["site_id"] == site_id and site["rules"]
    assert client.get(f"/api/scenarios/{sc['scenario_id']}/sites/ZZZ").status_code == 404


def test_compare_and_export(client: TestClient) -> None:
    a = plan_demo(client, spacing_m=8)
    b = plan_demo(client, spacing_m=12, label="wide")
    cmp = client.post("/api/compare", json={"scenario_ids": [a["scenario_id"], b["scenario_id"]]}).json()
    assert [r["scenario_id"] for r in cmp["rows"]] == [a["scenario_id"], b["scenario_id"]]
    assert cmp["rows"][1]["label"] == "wide"
    assert all(row["street_id"] == a["street_id"] for row in cmp["rows"])
    assert cmp["rows"][0]["street_name"] == "Demo street"
    assert cmp["rows"][0]["city"] == "demo"
    assert "synthetic" in cmp["rows"][0]["tree_source"].lower()
    assert cmp["best_by_cover"] in (a["scenario_id"], b["scenario_id"])

    r = client.get(f"/api/export/{a['scenario_id']}.geojson")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    fc = r.json()
    assert fc["type"] == "FeatureCollection"
    kinds = {f["properties"].get("kind") for f in fc["features"]}
    assert kinds == {"site", "mature_crown", "axis"}
    assert fc["properties"]["rule_pack_id"] == "berlin_strassenbaeume_2024"
    assert fc["properties"]["street"] and fc["properties"]["generated_at"]
    assert client.get("/api/export/nope.geojson").status_code == 404


def test_site_observation_distinguishes_axis_distance_from_trunk_clearance(client: TestClient) -> None:
    from server.agent.tools import site_observation

    scenario = plan_demo(client)
    site = service.explain_site(scenario["scenario_id"], "L0100")
    observation = site_observation(site, scenario["scenario_id"])
    assert site.edge_distance_m == 3.5
    clearance = next(rule for rule in site.rules if rule.rule_id == "carriageway_edge")
    assert clearance.measured_m == 1.0
    assert "axis-to-road-edge distance 3.50 m (not trunk clearance)" in observation
    assert "carriageway_edge (must): 1.00 m" in observation
    assert "trunk 3.50 m from the carriageway edge" not in observation
    assert observation.index("occupied_tree_position") < observation.index("carriageway_edge (must)")


def test_compare_different_streets_keeps_identity_without_ranking(client: TestClient) -> None:
    a = plan_demo(client, label="Same settings")
    street = client.post("/api/street", json={"city": "demo", "query": "Another street"}).json()
    response = client.post("/api/plan", json={"street_id": street["street_id"], "label": "Same settings"})
    assert response.status_code == 200, response.text
    b = response.json()

    response = client.post("/api/compare", json={"scenario_ids": [a["scenario_id"], b["scenario_id"]]})
    assert response.status_code == 200, response.text
    comparison = response.json()
    assert [row["street_id"] for row in comparison["rows"]] == [a["street_id"], street["street_id"]]
    assert [row["street_name"] for row in comparison["rows"]] == ["Demo street", "Another street"]
    assert all(row["city_name"] == street["city_name"] for row in comparison["rows"])
    assert comparison["best_by_cover"] is None


# ------------------------------------------------------------------ rules
def test_rule_override_flow(client: TestClient) -> None:
    baseline = plan_demo(client)
    rules = client.get("/api/rules").json()
    assert rules["overrides"] == {}
    pack = rules["packs"][0]
    assert all(not r["overridden"] for r in pack["rules"])

    r = client.put(f"/api/rules/{pack['id']}/carriageway_edge", json={"min_distance_m": 3.0})
    assert r.status_code == 200, r.text
    rule = next(x for x in r.json()["pack"]["rules"] if x["id"] == "carriageway_edge")
    assert rule["min_distance_m"] == 3.0 and rule["overridden"] is True

    rules = client.get("/api/rules").json()
    assert rules["overrides"]["carriageway_edge"]["min_distance_m"] == 3.0

    stricter = client.post("/api/plan", json={"street_id": baseline["street_id"]}).json()
    assert stricter["params"]["rule_overrides"]["carriageway_edge"]["min_distance_m"] == 3.0
    assert stricter["summary"]["invalid"] > baseline["summary"]["invalid"]
    used = next(x for x in stricter["rules_used"]["rules"] if x["id"] == "carriageway_edge")
    assert used["min_distance_m"] == 3.0

    assert client.put(f"/api/rules/{pack['id']}/no_such_rule", json={"enabled": False}).status_code == 404
    assert client.delete("/api/rules/overrides").json() == {"ok": True}
    assert client.get("/api/rules").json()["overrides"] == {}


# ------------------------------------------------------------------ agent
def test_agent_disabled_without_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert client.get("/api/health").json()["agent"]["enabled"] is False
    assert client.get("/api/config").json()["agent"]["enabled"] is False
    r = client.post("/api/agent", json={"message": "hello"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "agent_disabled"


class FakeProvider:
    """Scripted provider: prose, two tool calls, prose — no network."""

    name = "fake"
    model = "fake-1"

    async def run_turn(self, system, history, tools, on_event):
        assert "Allee" in system and "Current context" in system
        assert history[-1] == {"role": "user", "content": "plan it"}
        assert {t.name for t in tools} >= {"load_street", "plan_trees", "fly_to"}
        await on_event("text", {"delta": "Loading "})
        obs1 = await on_event("tool_call", {"id": "c1", "name": "load_street", "args": {"city": "demo", "query": "Demo street"}})
        obs2 = await on_event("tool_call", {"id": "c2", "name": "plan_trees",
                                             "args": {"spacing_m": 8, "side": "both", "species_id": "tilia_cordata", "mode": "grid"}})
        await on_event("tool_call", {"id": "c3", "name": "fly_to", "args": {"target": "street", "zoom": 17}})
        await on_event("text", {"delta": "done."})
        await on_event("usage", {"input_tokens": 12, "output_tokens": 3})
        assert len(obs1) <= 800 and len(obs2) <= 800 and "proposed" in obs2
        return [{"role": "assistant", "content": "Loading done."}]


def test_agent_sse_protocol(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "get_provider", lambda: FakeProvider())
    with client.stream("POST", "/api/agent", json={"message": "plan it"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        text = "".join(r.iter_text())
    events = parse_sse(text)
    names = [e for e, _ in events]
    assert names[0] == "text" and names[-1] == "done"
    assert names.count("tool") == 3 and names.count("result") == 3
    tool_names = [d["name"] for e, d in events if e == "tool"]
    assert tool_names == ["load_street", "plan_trees", "fly_to"]
    results = {d["name"]: d for e, d in events if e == "result"}
    assert results["load_street"]["payload_type"] == "street" and results["load_street"]["payload"]["layers"]
    assert results["plan_trees"]["payload_type"] == "scenario" and results["plan_trees"]["ok"] is True
    assert results["fly_to"]["summary"] == "ok" and results["fly_to"]["payload_type"] is None
    ui = [d for e, d in events if e == "ui"]
    assert {"action": "fly_to", "target": "street", "zoom": 17.0} in ui
    assert any(u["action"] == "select_scenario" for u in ui)
    done = events[-1][1]
    assert done["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert done["turns_remaining_hour"] == LIMITER.max_for("agent_hour") - 1

    session = SESSIONS.get_or_create(SID)
    assert session.last_scenario_id and session.last_street_id
    assert session.history[-1] == {"role": "assistant", "content": "Loading done."}
    assert not any("payload" in item for item in session.history)


class FailingProvider(FakeProvider):
    async def run_turn(self, system, history, tools, on_event):
        raise providers.ProviderError("model unavailable")


def test_agent_explanation_reads_current_plan_without_replanning(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    current = plan_demo(client)
    session = SESSIONS.get_or_create(SID)
    scenario_ids = list(session.scenario_ids)
    stored_ids = set(service.STORE.scenarios)
    selected_id = session.last_scenario_id

    class ExplanationProvider(FakeProvider):
        async def run_turn(self, system, history, tools, on_event):
            assert "call inspect_plan first" in system
            assert "not legal compliance or planting approval" in system
            observation = await on_event("tool_call", {"id": "explain", "name": "inspect_plan", "args": {}})
            assert current["scenario_id"] in observation
            assert f'{current["summary"]["planted"]} proposed' in observation
            rule_id, total = max(current["summary"]["failures_by_rule"].items(), key=lambda item: item[1])
            counts = {"conditional": 0, "invalid": 0}
            for site in current["sites"]["features"]:
                props = site["properties"]
                if props["verdict"] in counts and any(
                    check["rule_id"] == rule_id and check["passed"] is False for check in props["rules"]
                ):
                    counts[props["verdict"]] += 1
            assert f'{total} failures: {counts["conditional"]} amber, {counts["invalid"]} excluded' in observation
            await on_event("text", {"delta": "Amber positions do not meet a recommendation."})
            return [{"role": "assistant", "content": "Amber positions do not meet a recommendation."}]

    monkeypatch.setattr(providers, "get_provider", lambda: ExplanationProvider())
    response = client.post("/api/agent", json={
        "message": "Why are these positions amber?", "street_id": current["street_id"], "scenario_id": current["scenario_id"],
    })
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert not any(name in {"ui", "error"} for name, _ in events)
    result = next(data for name, data in events if name == "result")
    assert result["name"] == "inspect_plan" and result["ok"] is True
    assert result["payload"] is None and result["payload_type"] is None
    assert set(service.STORE.scenarios) == stored_ids
    assert session.scenario_ids == scenario_ids
    assert session.last_scenario_id == selected_id
    assert client.get(f'/api/scenarios/{selected_id}').json() == current


def test_agent_error_event(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "get_provider", lambda: FailingProvider())
    with client.stream("POST", "/api/agent", json={"message": "hi"}) as r:
        events = parse_sse("".join(r.iter_text()))
    assert events == [("error", {"message": "model unavailable"})]


@pytest.mark.parametrize("tool_name", ["shade", "explain_site"])
def test_agent_analysis_selects_requested_plan_before_display(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tool_name: str,
) -> None:
    older = plan_demo(client, spacing_m=8)
    current = plan_demo(client, spacing_m=12)
    site_id = older["sites"]["features"][0]["properties"]["site_id"]
    args = {"scenario_id": older["scenario_id"]}
    args.update({"year": 10, "month": 6, "day": 21, "hour": 12} if tool_name == "shade" else {"site_id": site_id})

    class AnalysisProvider(FakeProvider):
        async def run_turn(self, system, history, tools, on_event):
            assert "A should threshold is recommended" in system
            await on_event("tool_call", {"id": "analysis", "name": tool_name, "args": args})
            return [{"role": "assistant", "content": "Here is the requested earlier plan."}]

    monkeypatch.setattr(providers, "get_provider", lambda: AnalysisProvider())
    response = client.post("/api/agent", json={
        "message": "Show the earlier plan analysis", "street_id": current["street_id"], "scenario_id": current["scenario_id"],
    })
    assert response.status_code == 200
    events = parse_sse(response.text)
    assert not any(name == "error" for name, _ in events)
    expected_ui = [{"action": "select_scenario", "scenario_id": older["scenario_id"]}]
    if tool_name == "shade":
        expected_ui.extend([{"action": "set_year", "year": 10}, {"action": "show_layer", "layer": "shade", "visible": True}])
    else:
        expected_ui.append({"action": "fly_to", "target": site_id})
    assert [data for name, data in events if name == "ui"] == expected_ui
    result_index = next(i for i, (name, _) in enumerate(events) if name == "result")
    assert all(i < result_index for i, (name, _) in enumerate(events) if name == "ui")
    result = events[result_index][1]
    assert result["ok"] is True
    if tool_name == "shade":
        assert result["payload"]["scenario_id"] == older["scenario_id"]
        assert result["payload"]["year"] == 10
        assert "06-21 12:00" in result["payload"]["when"]
    else:
        assert result["payload"] == older["sites"]["features"][0]["properties"]
    assert len(service.STORE.scenarios) == 2


# ------------------------------------------------------------------ rate limits
def test_rate_limit_429(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLAN_REQUESTS_PER_HOUR", "2")
    street = load_demo(client)
    body = {"street_id": street["street_id"]}
    assert client.post("/api/plan", json=body).status_code == 200
    assert client.post("/api/plan", json=body).status_code == 200
    r = client.post("/api/plan", json=body)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1
    other = TestClient(app, headers={"X-Session-Id": "another-session-01"})
    assert other.post("/api/plan", json=body).status_code == 200      # per-session window


def test_agent_quota_429(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_TURNS_PER_HOUR", "1")
    monkeypatch.setattr(providers, "get_provider", lambda: FakeProvider())
    assert client.get("/api/session").json()["turns_remaining_hour"] == 1
    with client.stream("POST", "/api/agent", json={"message": "plan it"}) as r:
        assert r.status_code == 200
        "".join(r.iter_text())
    r = client.post("/api/agent", json={"message": "plan it"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert client.get("/api/session").json()["turns_remaining_hour"] == 0


class SlowProvider(FakeProvider):
    async def run_turn(self, system, history, tools, on_event):
        import asyncio

        await asyncio.sleep(0.25)
        await on_event("text", {"delta": "late"})
        return [{"role": "assistant", "content": "late"}]


def test_agent_heartbeat_while_waiting(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.agent import loop

    monkeypatch.setattr(loop, "HEARTBEAT_S", 0.05)
    monkeypatch.setattr(providers, "get_provider", lambda: SlowProvider())
    with client.stream("POST", "/api/agent", json={"message": "hi"}) as r:
        raw = "".join(r.iter_text())
    assert raw.count(": ping\n\n") >= 2
    assert [e for e, _ in parse_sse(raw)] == ["text", "done"]


def test_map_click_loads_city_context(client, monkeypatch):
    from server.adapters import ADAPTERS
    from server.engine.synthetic import synthetic_street
    seen = []
    async def from_point(point):
        seen.append(point)
        ctx = synthetic_street()
        ctx.city = ADAPTERS['zurich'].info
        ctx.name = 'Clicked street'
        return ctx
    monkeypatch.setattr(ADAPTERS['zurich'], 'street_from_point', from_point)
    response = client.post('/api/street', json={'city': 'zurich', 'point': [8.53, 47.38]})
    assert response.status_code == 200, response.text
    assert response.json()['name'] == 'Clicked street'
    assert seen == [(8.53, 47.38)]
    assert response.json()['layers']['axis']['features']


def test_map_click_demo_rejected_and_coordinate_validation(client):
    assert client.post('/api/street', json={'city': 'demo', 'point': [8.53, 47.38]}).status_code == 404
    assert client.post('/api/street', json={'city': 'zurich', 'point': [8.53]}).status_code == 422


def test_custom_crown_size_updates_geometry_checks_and_comparisons(client):
    street = load_demo(client)
    base = {'street_id': street['street_id'], 'species_id': 'tilia_cordata'}
    large = client.post('/api/plan', json=base).json()
    response = client.post('/api/plan', json={**base, 'crown_diameter_m': 4})
    assert response.status_code == 200, response.text
    small = response.json()
    assert small['params']['crown_diameter_m'] == 4
    assert small['species']['mature_crown_d_m'] == 4
    assert large['species']['mature_crown_d_m'] == 10
    assert service.species('tilia_cordata').mature_crown_d_m == 10
    by_large = {f['properties']['site_id']: f['properties'] for f in large['sites']['features']}
    for feature in small['sites']['features']:
        props = feature['properties']
        previous = by_large[props['site_id']]
        assert props['crown_d_mature_m'] == 4
        assert 1.5 < props['crown_d_by_year']['30'] < 4
        assert props['crown_d_by_year']['0'] == previous['crown_d_by_year']['0'] == 1.5
        checks = {r['rule_id']: r for r in props['rules']}
        old_checks = {r['rule_id']: r for r in previous['rules']}
        assert checks['building_crown']['measured_m'] == pytest.approx(old_checks['building_crown']['measured_m'] + 3)
        assert checks['junction_exclusion'] == old_checks['junction_exclusion']
    assert small['canopy']['years'][-1]['new_crown_area_m2'] < large['canopy']['years'][-1]['new_crown_area_m2']
    def temp(sc):
        result = client.post('/api/temperature', json={'scenario_id': sc['scenario_id']})
        assert result.status_code == 200, result.text
        return result.json()
    assert temp(small)['cooling_c'] < temp(large)['cooling_c']
    def shade(sc):
        result = client.post('/api/shade', json={'scenario_id': sc['scenario_id']})
        assert result.status_code == 200, result.text
        return result.json()
    assert shade(small)['shaded_corridor_pct'] < shade(large)['shaded_corridor_pct']
    comparison = client.post('/api/compare', json={'scenario_ids': [large['scenario_id'], small['scenario_id']]}).json()
    assert [r['crown_diameter_m'] for r in comparison['rows']] == [10, 4]
    exported = client.get(f"/api/export/{small['scenario_id']}.geojson").json()
    crowns = [f for f in exported['features'] if f['properties'].get('kind') == 'mature_crown']
    assert crowns and all(f['properties']['crown_d_m'] == 4 for f in crowns)
    assert service.scenario(large['scenario_id']).species.mature_crown_d_m == 10


@pytest.mark.parametrize('diameter', [1.5, 25.5, 'NaN', 'Infinity'])
def test_crown_size_validation(client, diameter):
    street = load_demo(client)
    response = client.post('/api/plan', json={'street_id': street['street_id'], 'crown_diameter_m': diameter})
    assert response.status_code == 422
