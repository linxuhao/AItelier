"""State Project browsing/migration guards and exact-run graph projection."""
import json
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition

from core.config_registry import ConfigRegistry
from core.db_manager import DBManager
from core.state_commands import execute
from core.state_graph import StateConflict, StateGraphError, StateGraphStore
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager
from core.run_graph_view import RunGraphUnavailable, pinned_run_graph


def node(k, deps=None):
    return {"key": k, "goal": "Goal " + k + "\nFull private requirement", "dependencies": deps or [],
            "acceptance": [{"id": "proof", "kind": "test", "description": "Explicit acceptance check"}]}


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "host.db"))
    ws = WorkspaceManager(str(tmp_path / "ws"), str(tmp_path / "projects"))
    sf = SkillFlow(str(tmp_path / "sf.db"))
    sf.register_graph(PipelineGraph(name="fixture", begin="work", steps=[StepNode(id="work")]))
    registry = ConfigRegistry()
    registry.register_one(sf, "fixture", hint_overrides={"repo_mode": "none", "seed_file": "plan.md", "output_step": "work", "scheduler_owned": True})
    service = StateService(db, ws, sf, registry, actor="test-verifier")
    service.create_project("game", "武虾传奇")
    service.store.add_nodes("game", [node("growth.proficiency"), node("month.actions", ["growth.proficiency"])])
    yield SimpleNamespace(service=service, db=db, ws=ws, sf=sf, tmp=tmp_path)
    sf._conn.close()


def reserve(live, k="growth.proficiency", request="one"):
    return live.service.attempts.reserve("game", k, 1, "fixture", request)


def make_repo(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    (path / "code.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "code.py"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "seed"], cwd=path, check=True)
    return path


def test_additive_schema_preserves_old_goals_and_defaults(live):
    reopened = StateGraphStore(DBManager(live.db.db_path))
    assert reopened.get_node("game", "growth.proficiency")["readiness"] == "ready"
    assert live.service.portfolio.overview("game")["policy"]["dispatch"] == "active"
    assert len(reopened.get_graph("game")["nodes"]) == 2


@pytest.mark.parametrize("policy", ["hold", "archive"])
def test_project_hold_blocks_frontier_reservation_and_dispatch(live, policy):
    a = reserve(live)
    live.service.portfolio.set_dispatch("game", policy, 0, "Migration not approved")
    assert live.service.store.frontier("game")["total"] == 0
    with pytest.raises(StateConflict, match="held"):
        live.service.attempts.claim_launch(a["attempt_id"])
    with pytest.raises(StateConflict, match="held"):
        live.service.attempts.reserve("game", "growth.proficiency", 1, "fixture", "new")
    assert live.sf.list_runs() == []
    live.service.portfolio.set_dispatch("game", "active", 1, "Explicit release")
    assert live.service.attempts.claim_launch(a["attempt_id"])


def test_held_project_refuses_before_executor_composition(live):
    live.service.portfolio.set_dispatch("game", "hold", 0, "Do not dispatch")
    calls = []
    def broken():
        calls.append(1)
        raise RuntimeError("should not be touched")
    offline = StateService(live.db, runtime_factory=broken)
    with pytest.raises(StateConflict, match="held"):
        offline.start_attempt("game", "growth.proficiency", 1, "fixture", "r")
    assert calls == []


def test_node_hold_does_not_change_facts_or_dependencies(live):
    before = live.service.store.get_node("game", "growth.proficiency")
    hold = live.service.set_node_hold("game", "growth.proficiency", True, 0, "Existing external worker")
    after = live.service.store.get_node("game", "growth.proficiency")
    assert after["readiness"] == "held" and after["hold"]["scope"] == "node"
    assert (before["status"], before["revision"], before["contract_hash"]) == (after["status"], after["revision"], after["contract_hash"])
    with pytest.raises(StateConflict):
        live.service.set_node_hold("game", "growth.proficiency", False, 0, "Stale client")
    live.service.set_node_hold("game", "growth.proficiency", False, hold["revision"], "Owner released")
    assert live.service.store.get_node("game", "growth.proficiency")["readiness"] == "ready"


def test_concurrent_hold_changes_require_cas(live):
    def change(mode):
        try:
            return live.service.portfolio.set_dispatch("game", mode, 0, mode)["revision"]
        except StateConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(change, ["hold", "archive"]))) == ["1", "conflict"]


def test_stable_source_not_tied_to_a_legacy_execution_project(live):
    repo = make_repo(live.tmp / "repo")
    binding = live.service.bind_source("game", str(repo), 0)
    assert binding["revision"] == 1
    assert live.service._source("game") == str(repo)
    assert live.service.store.get_project("game")["source_project_id"] is None
    assert live.service.portfolio.projects(repo_path=str(repo))["projects"][0]["project_id"] == "game"
    reserve(live)
    with pytest.raises(StateConflict, match="attempts exist"):
        live.service.bind_source("game", str(repo), 1)


def test_binding_cannot_be_subdirectory_or_repointed_silently(live):
    repo = make_repo(live.tmp / "repo")
    (repo / "sub").mkdir()
    with pytest.raises(StateGraphError, match="root"):
        live.service.bind_source("game", str(repo / "sub"))
    live.service.bind_source("game", str(repo))
    a = reserve(live)
    with pytest.raises(StateConflict, match="binding changed"):
        live.service.attempts.pin_host_contract(a["attempt_id"], {"source_repo": str(live.tmp / "other"), "seed_file": "plan.md",
            "output_step": "work", "scheduler_owned": True, "repo_mode": "code"})


def test_reference_does_not_adopt_or_verify_legacy_run(live):
    rid = live.sf.create_run("fixture", project_id="legacy-recovery")
    live.sf.start_run(rid)
    live.sf.pause_run(rid)
    ref = live.service.add_reference("game", "growth.proficiency", "legacy-run", "run", rid, "Paused recovery", "Codex")
    assert ref["observed_status"] == "paused" and ref["protection"] == 1
    assert live.service.store.get_node("game", "growth.proficiency")["readiness"] == "held"
    assert live.service.attempts.list("game", "growth.proficiency") == []
    assert live.sf.get_run(rid)["project_id"] == "legacy-recovery"
    owners = live.service.portfolio.run_owners(rid)["links"]
    assert owners[0]["relation"] == "reference" and owners[0]["node_key"] == "growth.proficiency"
    assert not live.service.portfolio.overview("game")["counts"].get("VERIFIED", 0)
    hold = live.service.store.get_node("game", "growth.proficiency")["node_hold"]
    with pytest.raises(StateConflict, match="external run"):
        live.service.set_node_hold("game", "growth.proficiency", False, hold["revision"], "Not yet")
    live.sf.fail_run(rid, "Owner cancellation in isolated fixture")
    live.service.set_node_hold("game", "growth.proficiency", False, hold["revision"], "Run ended and drained")
    assert live.service.store.get_node("game", "growth.proficiency")["readiness"] == "ready"


def test_protected_reference_added_while_held_changes_hold_revision(live):
    live.service.set_node_hold("game", "growth.proficiency", True, 0, "Hold")
    live.service.portfolio.add_reference("game", "growth.proficiency", "external", "note", "external-worker",
        "Still working", "Claude", protect=True)
    with pytest.raises(StateConflict):
        live.service.set_node_hold("game", "growth.proficiency", False, 1, "Stale release")


def test_reference_retry_is_idempotent_and_history_cannot_be_rewritten(live):
    kwargs = dict(project_id="game", node_key="growth.proficiency", reference_id="report1", kind="report",
                  ref="reports/first-failure.json", label="Failure evidence", provenance_actor="tester", report_sha256="f" * 64)
    one = live.service.add_reference(**kwargs)
    assert live.service.add_reference(**kwargs) == one
    with pytest.raises(StateConflict):
        live.service.add_reference(**(kwargs | {"label": "Claimed success"}))
    with live.db.get_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM state_history_links")
    assert live.service.store.get_node("game", "growth.proficiency")["status"] == "OPEN"


def test_compact_overview_has_separate_fact_readiness_attempt_and_evidence(live):
    a = reserve(live)
    view = live.service.portfolio.overview("game")
    n = next(n for n in view["nodes"] if n["node_key"] == "growth.proficiency")
    assert n["status"] == "OPEN" and n["readiness"] == "in_progress"
    assert n["latest_attempt"]["attempt_id"] == a["attempt_id"]
    assert n["latest_attempt"]["status"] == "reserved"
    assert n["domain"] == "growth" and "Full private requirement" not in n["title"]
    assert "acceptance" not in n and "context" not in n["latest_attempt"]
    before = live.service.store.events("game")
    live.service.portfolio.overview("game")
    assert live.service.store.events("game") == before


def test_overview_counts_ready_actions_without_changing_readiness_counts(live):
    live.service.store.add_nodes("game", [node("art.open"), node("release.stale")])
    with live.db.get_connection() as conn:
        conn.execute("UPDATE state_nodes SET status='CANDIDATE' "
                     "WHERE project_id='game' AND node_key='growth.proficiency'")
        conn.execute("UPDATE state_nodes SET status='STALE' "
                     "WHERE project_id='game' AND node_key='release.stale'")
        conn.commit()

    view = live.service.portfolio.overview("game")
    nodes = {n["node_key"]: n for n in view["nodes"]}
    assert view["readiness_counts"] == {"ready": 3, "blocked": 1}
    assert view["ready_action_counts"] == {"candidate_review": 1, "new_attempt": 2}
    assert nodes["growth.proficiency"]["next_action"] == "candidate_review"
    assert nodes["art.open"]["next_action"] == "new_attempt"
    assert nodes["release.stale"]["next_action"] == "new_attempt"
    assert nodes["month.actions"]["next_action"] is None


def test_project_attempt_pagination_and_run_reverse_link(live):
    for i in range(3):
        a = reserve(live, request="request" + str(i))
        rid = live.sf.create_run("fixture", project_id=a["execution_project_id"])
        live.service.attempts.bind_run(a["attempt_id"], rid, live.sf)
        live.sf.fail_run(rid, "retry fixture")
        live.service.attempts.reconcile(a["attempt_id"], live.sf)
    page = live.service.portfolio.project_attempts("game", limit=2)
    assert len(page["attempts"]) == 2 and page["next_after"]
    rest = live.service.portfolio.project_attempts("game", page["next_after"], 2)
    assert len(rest["attempts"]) == 1 and rest["next_after"] is None
    assert live.service.portfolio.run_owners(rid)["links"][0]["attempt_id"] == a["attempt_id"]
    assert live.service.portfolio.attempt_detail(a["attempt_id"])["evidence"] == []


def test_explicit_refresh_observes_only_and_retains_errors(live, monkeypatch):
    a = reserve(live)
    rid = live.sf.create_run("fixture", project_id=a["execution_project_id"])
    live.sf.start_run(rid)
    live.service.attempts.bind_run(a["attempt_id"], rid, live.sf)
    live.sf.pause_run(rid)
    report = live.service.refresh_project("game")
    assert report["results"][0]["status"] == "paused"
    assert live.sf.get_run(rid)["status"] == "paused"
    assert len(live.sf.list_runs()) == 1
    monkeypatch.setattr(live.service, "reconcile_attempt", lambda _: (_ for _ in ()).throw(OSError("unavailable")))
    assert live.service.refresh_project("game")["results"][0]["error"] == "OSError"
    assert live.service.attempts.get(a["attempt_id"])["status"] == "paused"


def test_all_new_state_reads_remain_private_and_commands_strict(live, monkeypatch):
    from api import state_graph_routers as routes, authz
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: live.service
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *_: None)
    urls = ["/projects", "/projects/game/overview", "/projects/game/nodes/growth.proficiency",
            "/projects/game/attempts", "/projects/game/references", "/runs/unknown/owners"]
    with TestClient(app) as client:
        for url in urls:
            response = client.get("/api/state" + url)
            assert response.status_code == 403, (url, response.text)
            assert "Full private requirement" not in response.text
    app.state._test_mode = True
    with TestClient(app) as client:
        assert client.get("/api/state/projects/game/overview").status_code == 200
        assert client.get("/api/state/projects/game/attempts?limit=10000").status_code == 422
        assert client.post("/api/state/commands/set_dispatch", json={"project_id": "game", "dispatch": "hold", "expected_revision": 0, "reason": "Review"}).status_code == 200
        assert client.post("/api/state/query/set_dispatch", json={}).status_code == 422
    with pytest.raises(StateGraphError):
        execute(live.service, "add_reference", {"status": "VERIFIED"}, allow_write=True)


def test_pinned_run_graph_does_not_follow_new_config(live):
    rid = live.sf.create_run("fixture", project_id="old-run")
    old = pinned_run_graph(live.sf, rid)
    live.sf.register_graph(PipelineGraph(name="fixture", begin="new_work", steps=[StepNode(id="new_work")]))
    view = pinned_run_graph(live.sf, rid)
    assert view == old
    assert view["steps"][0]["id"] == "work" and view["graph_version"] == 1
    assert view["run_id"] == rid
    with pytest.raises(KeyError):
        pinned_run_graph(live.sf, "old-run")  # project alias is not an exact run


@pytest.mark.parametrize("missing", [None, {"digest": "sha256:" + "0" * 64, "graph": {}}])
def test_missing_graph_version_is_not_rendered_as_current(live, monkeypatch, missing):
    rid = live.sf.create_run("fixture", project_id="old")
    monkeypatch.setattr(live.sf, "get_graph_version", lambda *_: missing)
    with pytest.raises(RunGraphUnavailable):
        pinned_run_graph(live.sf, rid)


def test_run_graph_projection_excludes_private_payloads(live):
    secret = "PRIVATE-DO-NOT-RENDER"
    graph = PipelineGraph(name="private", begin="one", description=secret, steps=[
        StepNode(id="one", config={"prompt": secret}, tool_params={"token": secret},
                 context=[{"source": {"value": secret}}], transitions=[Transition(to="two", match={"value": secret})]),
        StepNode(id="two")])
    live.sf.register_graph(graph)
    rid = live.sf.create_run("private", {"goal": secret}, project_id="private-project")
    view = pinned_run_graph(live.sf, rid)
    assert secret not in json.dumps(view)
    assert "tool_params" not in json.dumps(view) and "description" not in view
    assert view["steps"][0]["transitions"][0]["match"] == {"condition": "value"}


def test_pinned_graph_http_entry_uses_exact_identity(live, monkeypatch):
    from api import run_routers
    monkeypatch.setattr(run_routers, "get_skillflow", lambda: live.sf)
    app = FastAPI()
    app.include_router(run_routers.router)
    rid = live.sf.create_run("fixture", project_id="legacy-execution")
    with TestClient(app) as client:
        view = client.get(f"/api/runs/{rid}/graph")
        assert view.status_code == 200 and view.json()["run_id"] == rid
        assert client.get("/api/runs/legacy-execution/graph").status_code == 404
        monkeypatch.setattr(live.sf, "get_graph_version", lambda *_: None)
        assert client.get(f"/api/runs/{rid}/graph").status_code == 409
