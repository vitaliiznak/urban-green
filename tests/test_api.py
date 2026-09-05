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
    assert sc["label"] == "8 m · Tilia cordata · both · grid"
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
        assert len(obs1) < 800 and len(obs2) < 800 and "planted" in obs2
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


def test_agent_error_event(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers, "get_provider", lambda: FailingProvider())
    with client.stream("POST", "/api/agent", json={"message": "hi"}) as r:
        events = parse_sse("".join(r.iter_text()))
    assert events == [("error", {"message": "model unavailable"})]


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
