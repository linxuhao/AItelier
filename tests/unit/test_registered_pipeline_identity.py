"""Real SDK history and the existing graph route, with owned race controls."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow import PipelineGraph, SkillFlow, graph_digest
from skillflow.graph import StepNode, Transition

from api import config_routers, dependencies
from core import addon_registry
from core.config_registry import ConfigRegistry


def graph(name="identity", step="work", secret="PRIVATE_CANARY_sk-test"):
    return PipelineGraph(name=name, begin=step, steps=[
        StepNode(id=step, step_type="tool", tool_name="owned_tool",
                 tool_params={"api_key": secret, "payload": {"system_prompt": secret}},
                 transitions=[Transition(to=None)])])


@contextmanager
def route(monkeypatch, sf, name):
    registry = ConfigRegistry()
    assert registry.register_one(sf, name, host_hints={}) is not None
    monkeypatch.setattr(dependencies, "get_skillflow", lambda: sf)
    app = FastAPI()
    app.include_router(config_routers.router)
    app.dependency_overrides[dependencies.get_config_registry] = lambda: registry
    with TestClient(app) as client:
        yield client


@pytest.fixture
def engine(tmp_path):
    sf = SkillFlow(str(tmp_path / "identity.sqlite"),
                   workspace_base=str(tmp_path / "workspace"), projects_base=str(tmp_path / "projects"))
    yield sf
    sf._conn.close()


def assert_identity(body, version, digest):
    assert body["registration_status"] == "available"
    assert body["graph_version"] == version
    assert body["graph_digest"] == digest


def assert_closed(body, status):
    assert body["registration_status"] == status
    assert body["graph_version"] is None and body["graph_digest"] is None


def test_existing_real_route_reports_same_registered_definition_and_no_private_values(engine, monkeypatch):
    definition = graph()
    version = engine.register_graph(definition)
    with route(monkeypatch, engine, definition.name) as client:
        response = client.get("/api/pipelines/identity/graph")
        assert response.status_code == 200
        body = response.json()
    assert_identity(body, version, graph_digest(definition.to_dict()))
    assert [step["id"] for step in body["steps"]] == ["work"]
    raw = json.dumps(body)
    assert "PRIVATE_CANARY" not in raw and "tool_params" not in raw and "system_prompt" not in raw
    assert "api_key" not in raw and "payload" not in raw and "created_at" not in raw


def test_content_change_changes_identity_and_preserves_historical_run_pin(engine, monkeypatch):
    first = graph()
    v1 = engine.register_graph(first)
    run = engine.create_run(first.name, project_id="owned")
    before = engine.graph_version_for_run(run)
    old = engine.get_graph_version(first.name, v1)
    second = graph(step="changed")
    v2 = engine.register_graph(second)
    with route(monkeypatch, engine, first.name) as client:
        body = client.get("/api/pipelines/identity/graph").json()
    assert v2 > v1
    assert_identity(body, v2, graph_digest(second.to_dict()))
    assert [step["id"] for step in body["steps"]] == ["changed"]
    after = engine.graph_version_for_run(run)
    assert after["version"] == before["version"] and after["digest"] == before["digest"]
    assert engine.get_graph_version(first.name, v1) == old


def test_same_content_reboot_keeps_content_identity(tmp_path, monkeypatch):
    database = tmp_path / "reboot.sqlite"
    first = SkillFlow(str(database))
    definition = graph()
    version = first.register_graph(definition)
    digest = graph_digest(definition.to_dict())
    first._conn.close()
    rebooted = SkillFlow(str(database))
    try:
        assert rebooted.register_graph(graph()) == version
        with route(monkeypatch, rebooted, definition.name) as client:
            body = client.get("/api/pipelines/identity/graph").json()
        assert_identity(body, version, digest)
        assert len(rebooted.list_graph_versions(definition.name)) == 1
    finally:
        rebooted._conn.close()


def test_real_addon_alias_identity_is_for_composed_definition(engine, monkeypatch):
    base = graph(name="base")
    engine.register_graph(base)
    engine.register_overlay("extra", {"name": "extra", "base": "base", "alias": "alias",
        "overlay": [{"insert_after": "work", "steps": [{"id": "added", "step_type": "gate", "transitions": [{"to": None}]}]}]})
    alias = engine.compose_config("base", ["extra"])
    assert alias == "alias"
    expected = engine._graphs[alias].to_dict()
    version = engine.list_graph_versions(alias)[0]["version"]
    with route(monkeypatch, engine, alias) as client:
        body = client.get("/api/pipelines/alias/graph").json()
    assert_identity(body, version, graph_digest(expected))
    assert body["base"] == "base" and body["addons"] == ["extra"]
    assert body["addon_steps"] == ["added"]
    assert body["graph_digest"] != graph_digest(base.to_dict())


def test_never_uses_latest_unrelated_definition(engine, monkeypatch):
    old = graph()
    v1 = engine.register_graph(old)
    new = graph(step="newer")
    engine.register_graph(new)
    # Mirrors the engine's own newest-matching-content run pin rule.
    engine._graphs[old.name] = old
    with route(monkeypatch, engine, old.name) as client:
        body = client.get("/api/pipelines/identity/graph").json()
    assert_identity(body, v1, graph_digest(old.to_dict()))
    assert body["begin"] == "work"


def test_absent_history_is_explicit_without_identity(engine, monkeypatch):
    definition = graph()
    engine.register_graph(definition)
    original = engine.list_graph_versions
    calls = []

    def absent(name):
        calls.append(name)
        assert original(name)
        return []

    monkeypatch.setattr(engine, "list_graph_versions", absent)
    with route(monkeypatch, engine, definition.name) as client:
        body = client.get("/api/pipelines/identity/graph").json()
    assert calls == [definition.name]
    assert_closed(body, "unavailable")


@pytest.mark.parametrize("broken", ["digest", "body", "version", "exception"])
def test_history_mismatch_or_read_failure_never_claims_available(engine, monkeypatch, broken):
    definition = graph()
    engine.register_graph(definition)
    original = engine.get_graph_version

    def altered(name, version):
        if broken == "exception":
            raise OSError("PRIVATE_CANARY_sk-test history outage")
        row = copy.deepcopy(original(name, version))
        if broken == "digest":
            row["digest"] = "sha256:" + "0" * 64
        elif broken == "version":
            row["version"] += 1
        else:
            row["graph"] = graph(step="wrong").to_dict()
        return row

    monkeypatch.setattr(engine, "get_graph_version", altered)
    with route(monkeypatch, engine, definition.name) as client:
        response = client.get("/api/pipelines/identity/graph")
    assert response.status_code == 200
    assert_closed(response.json(), "unavailable" if broken == "exception" else "conflict")
    assert "PRIVATE_CANARY" not in response.text


def test_actual_sdk_publication_before_history_commit_is_conflict(engine, monkeypatch):
    old = graph()
    engine.register_graph(old)
    entered, release = Event(), Event()
    original = engine._tx

    @contextmanager
    def held_transaction():
        entered.set()
        assert release.wait(10), "owned registration controller did not release"
        with original() as connection:
            yield connection

    new = graph(step="uncommitted")
    monkeypatch.setattr(engine, "_tx", held_transaction)
    with ThreadPoolExecutor(max_workers=1) as worker:
        registration = worker.submit(engine.register_graph, new)
        try:
            assert entered.wait(10)
            with route(monkeypatch, engine, old.name) as client:
                response = client.get("/api/pipelines/identity/graph")
            assert response.status_code == 200
            assert response.json()["begin"] == "uncommitted"
            assert_closed(response.json(), "conflict")
        finally:
            release.set()
        version = registration.result(timeout=10)
    with route(monkeypatch, engine, new.name) as client:
        body = client.get("/api/pipelines/identity/graph").json()
    assert_identity(body, version, graph_digest(new.to_dict()))


def test_definition_replaced_during_read_fails_closed(engine, monkeypatch):
    old = graph()
    engine.register_graph(old)
    original = engine.list_graph_versions
    newer = graph(step="replacement")

    def replaced(name):
        rows = original(name)
        engine.register_graph(newer)
        return rows

    monkeypatch.setattr(engine, "list_graph_versions", replaced)
    with route(monkeypatch, engine, old.name) as client:
        body = client.get("/api/pipelines/identity/graph").json()
    assert body["begin"] == "work"
    assert_closed(body, "conflict")


def test_private_content_changes_identity_without_widening_public_structure(engine, monkeypatch):
    first = graph(secret="PRIVATE_CANARY_sk-first")
    v1 = engine.register_graph(first)
    with route(monkeypatch, engine, first.name) as client:
        before = client.get("/api/pipelines/identity/graph").json()
        second = graph(secret="PRIVATE_CANARY_sk-second")
        v2 = engine.register_graph(second)
        after = client.get("/api/pipelines/identity/graph").json()
    assert_identity(before, v1, graph_digest(first.to_dict()))
    assert_identity(after, v2, graph_digest(second.to_dict()))
    assert before["graph_digest"] != after["graph_digest"]
    for name in ("graph_version", "graph_digest", "registration_status"):
        before.pop(name)
        after.pop(name)
    assert before == after
    assert "PRIVATE_CANARY" not in json.dumps(after)


def test_in_place_definition_change_during_read_fails_closed(engine, monkeypatch):
    definition = graph()
    engine.register_graph(definition)
    original = engine.list_graph_versions

    def mutated(name):
        rows = original(name)
        definition.steps[0].tool_params["api_key"] = "PRIVATE_CANARY_sk-mutated"
        return rows

    monkeypatch.setattr(engine, "list_graph_versions", mutated)
    with route(monkeypatch, engine, definition.name) as client:
        body = client.get("/api/pipelines/identity/graph").json()
    assert_closed(body, "conflict")
    assert "PRIVATE_CANARY" not in json.dumps(body)


def test_history_reads_are_scoped_and_load_at_most_one_body(engine, monkeypatch):
    for n in range(8):
        engine.register_graph(graph(secret=f"PRIVATE_CANARY_sk-{n}"))
    other = graph(name="other")
    engine.register_graph(other)
    list_original, get_original = engine.list_graph_versions, engine.get_graph_version
    calls = []

    def listed(name):
        calls.append(("list", name))
        return list_original(name)

    def fetched(name, version):
        calls.append(("get", name, version))
        return get_original(name, version)

    monkeypatch.setattr(engine, "list_graph_versions", listed)
    monkeypatch.setattr(engine, "get_graph_version", fetched)
    with route(monkeypatch, engine, "identity") as client:
        body = client.get("/api/pipelines/identity/graph").json()
    assert_identity(body, 8, graph_digest(engine._graphs["identity"].to_dict()))
    assert calls == [("list", "identity"), ("get", "identity", 8)]


def test_route_manifest_registration_race_does_not_claim_old_identity(engine, monkeypatch):
    old = graph()
    engine.register_graph(old)
    registry = ConfigRegistry()
    manifest = registry.register_one(engine, old.name, host_hints={})
    original = manifest.to_dict
    new = graph(step="manifest_race")

    def changed():
        engine.register_graph(new)
        return original()

    monkeypatch.setattr(manifest, "to_dict", changed)
    monkeypatch.setattr(dependencies, "get_skillflow", lambda: engine)
    app = FastAPI()
    app.include_router(config_routers.router)
    app.dependency_overrides[dependencies.get_config_registry] = lambda: registry
    with TestClient(app) as client:
        body = client.get("/api/pipelines/identity/graph").json()
    assert body["begin"] == "work"
    assert_closed(body, "conflict")
