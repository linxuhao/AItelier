"""State DAG through real host launch, HTTP, MCP, and internal driver entries.

No network/LLM is needed: fixtures execute real SkillFlow claims themselves.
All databases, seeds, Git checkouts and output artifacts live in tmp_path.
"""
import asyncio
import hashlib
import json
import subprocess
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition

from core.config_registry import ConfigRegistry
from core.db_manager import DBManager
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager


def spec(name, deps=None):
    return {"key": name, "goal": "Deliver " + name, "dependencies": deps or [], "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Deterministic behaviour verified"},
        {"id": "review", "kind": "review", "description": "Implementation and evidence reviewed"}]}


@pytest.fixture
def live(tmp_path, monkeypatch):
    import api.dependencies as deps
    import core.scheduler as scheduler
    from api import state_graph_routers as routes
    from api import mcp_router

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    for name, value in {"GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_NAME": "Test",
                        "GIT_AUTHOR_EMAIL": "test@localhost", "GIT_COMMITTER_EMAIL": "test@localhost"}.items():
        monkeypatch.setenv(name, value)
    # Host git helpers snapshot their environment at import time. Supply the
    # fixture identity there too, without changing any user's Git config.
    import core.workspace_manager as workspace_module
    monkeypatch.setattr(workspace_module, "_GIT_ENV", {**workspace_module._GIT_ENV,
        "GIT_AUTHOR_NAME": "Test", "GIT_COMMITTER_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@localhost", "GIT_COMMITTER_EMAIL": "test@localhost"})
    db = DBManager(str(tmp_path / "state.sqlite"))
    ws = WorkspaceManager(str(tmp_path / "ws"), str(tmp_path / "projects"))
    sf = SkillFlow(str(tmp_path / "skillflow.sqlite"), workspace_base=str(tmp_path / "ws"),
                   projects_base=str(tmp_path / "projects"))
    registry = ConfigRegistry()
    for name, mode in [("state_fixture", "none"), ("state_code_fixture", "code")]:
        graph = PipelineGraph(name=name, begin="work", steps=[StepNode(id="work", context=[
            {"source": {"config": name, "output": "plan.md", "required": True}}])])
        sf.register_graph(graph)
        registry.register_one(sf, name, hint_overrides={"scheduler_owned": True, "repo_mode": mode,
                               "seed_file": "plan.md", "output_step": "work"})
    monkeypatch.setattr(deps, "db_instance", db)
    monkeypatch.setattr(deps, "get_db_manager", lambda: db)
    monkeypatch.setattr(deps, "get_workspace_manager", lambda: ws)
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_config_registry", lambda: registry)
    monkeypatch.setattr(routes, "get_skillflow", lambda: sf)
    monkeypatch.setattr(routes, "get_config_registry", lambda: registry)
    monkeypatch.setattr(scheduler, "wake_scheduler", lambda *args, **kwargs: None)
    attached = []
    def attach(run_id, **kwargs):
        attached.append((run_id, kwargs))
        return True
    monkeypatch.setattr(mcp_router, "_start_driver", attach)
    service = StateService(db, ws, sf, registry, attach, actor="test-reviewer")
    service.create_project("game", "Long-running game")
    service.store.add_nodes("game", [spec("a"), spec("b", ["a"])])
    yield SimpleNamespace(db=db, ws=ws, sf=sf, registry=registry, service=service,
                          attached=attached, tmp=tmp_path)
    sf._conn.close()  # fixture teardown; the engine has no public close() in 1.5.67


def start(live, node_key="a", request_key="first", workflow="state_fixture", revision=1):
    return live.service.start_attempt("game", node_key, revision, workflow, request_key)


def finish(live, attempt, output="candidate output\n"):
    sf, ws = live.sf, live.ws
    rid = attempt["run_id"]
    assert rid
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    assert claimed is not None
    final = ws.get_final_path(attempt["execution_project_id"], "work", attempt["workflow"])
    final.mkdir(parents=True, exist_ok=True)
    (final / "result.txt").write_text(output)
    sf.confirm_step(claimed.token, StepResult(outputs={"result": output}))
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"
    return live.service.reconcile_attempt(attempt["attempt_id"])


def evidence(live, attempt, check):
    body = json.dumps({"check": check, "artifact": attempt["artifact_ref"], "passed": True}).encode()
    report = live.tmp / (attempt["node_key"] + "-" + check + ".json")
    report.write_bytes(body)
    return live.service.record_evidence(attempt["attempt_id"], attempt["attempt_id"] + "-" + check,
        check, "pass", attempt["artifact_ref"], str(report), hashlib.sha256(body).hexdigest(), "Fixture verifier")


def test_standard_launcher_persists_seed_attempt_run_and_output_artifact(live):
    a = start(live)
    assert a["run_id"] and a["status"] == "running"
    assert a["execution_project_id"] != "game"
    assert a["checkpoints"] == "ask"
    assert live.attached == [(a["run_id"], {"scheduler_owned": True, "auto_approve": False})]
    from core.seed_publication import seed_dir, published_generation
    seed = seed_dir(live.sf, a["execution_project_id"], "state_fixture")
    assert published_generation(seed)
    payload = (seed / "plan.md").read_text()
    assert "Deliver a" in payload and a["contract_hash"] in payload
    assert live.service.store.get_node("game", "a")["readiness"] == "in_progress"
    a = finish(live, a)
    assert a["status"] == "candidate" and len(a["artifact_ref"]) == 64
    assert live.service.store.get_node("game", "b")["readiness"] == "blocked"
    evidence(live, a, "behaviour")
    evidence(live, a, "review")
    live.service.verify_node("game", "a", 1, a["attempt_id"])
    assert live.service.store.get_node("game", "b")["readiness"] == "ready"
    b = start(live, "b")
    assert b["dependencies"]["a"]["verified_receipt"]
    assert b["run_id"] != a["run_id"]
    assert len(live.sf.list_runs()) == 2


def test_retry_does_not_create_a_second_workflow(live):
    a = start(live)
    again = start(live)
    assert a["attempt_id"] == again["attempt_id"]
    assert a["run_id"] == again["run_id"]
    assert len(live.sf.list_runs()) == 1
    assert len(live.attached) == 1


def test_concurrent_start_requests_share_a_single_run(live):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: start(live), [1, 2]))
    assert len({r["attempt_id"] for r in results}) == 1
    assert len(live.sf.list_runs()) == 1
    assert live.service.recover_attempt(results[0]["attempt_id"])["run_id"]


def test_lost_launcher_response_recovers_by_durable_execution_identity(live, monkeypatch):
    import core.run_launcher as launcher
    real = launcher.start_config_run
    calls = []
    def lost_response(*args, **kwargs):
        calls.append(1)
        real(*args, **kwargs)
        raise TimeoutError("response lost after run creation")
    monkeypatch.setattr(launcher, "start_config_run", lost_response)
    a = start(live)
    assert a["status"] == "unknown" and a["run_id"] is None
    assert len(live.sf.list_runs()) == 1
    recovered = live.service.recover_attempt(a["attempt_id"])
    assert recovered["run_id"] and recovered["status"] == "running"
    assert len(live.sf.list_runs()) == 1 and len(calls) == 1


def test_unknown_launch_without_run_is_not_blindly_retried(live, monkeypatch):
    import core.run_launcher as launcher
    calls = []
    def refused(*args, **kwargs):
        calls.append(1)
        raise OSError("uncertain transport failure")
    monkeypatch.setattr(launcher, "start_config_run", refused)
    a = start(live)
    recovered = live.service.recover_attempt(a["attempt_id"])
    assert recovered["recovery_required"] is True
    assert len(calls) == 1 and live.sf.list_runs() == []
    with pytest.raises(StateConflict):
        start(live, request_key="another")


def test_missing_or_unseeded_workflow_is_refused_before_attempt_creation(live):
    with pytest.raises(StateGraphError, match="unknown workflow"):
        start(live, workflow="not-registered")
    live.registry.get("state_fixture").seed_file = None
    with pytest.raises(StateGraphError, match="seed-file"):
        start(live)
    assert live.service.attempts.list("game", "a") == []
    assert live.sf.list_runs() == []


def test_private_state_queries_require_writer_authorization(live, monkeypatch):
    from api import authz, state_graph_routers as routes
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: live.service
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *_: None)
    with TestClient(app) as client:
        assert client.get("/api/state/projects").status_code == 403
        assert client.get("/api/state/projects/game").status_code == 403
        assert client.post("/api/state/query/frontier", json={"project_id": "game"}).status_code == 403
        assert client.post("/api/state/commands/add_nodes", json={"project_id": "game", "nodes": [spec("secret")]}).status_code == 403
    assert len(live.service.store.get_graph("game")["nodes"]) == 2


def test_rest_uses_typed_commands_and_never_accepts_status_assignment(live):
    from api import state_graph_routers as routes
    app = FastAPI()
    app.state._test_mode = True
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: live.service
    with TestClient(app) as client:
        assert client.get("/api/state/schema").json()["operations"]["verify_node"]
        assert client.get("/api/state/projects/game/frontier").json()["nodes"][0]["node_key"] == "a"
        assert client.post("/api/state/query/revise_node", json={}).status_code == 422
        assert client.post("/api/state/commands/set_status", json={"status": "VERIFIED"}).status_code == 422
        bad = client.post("/api/state/commands/start_attempt", json={"project_id": "game", "node_key": "a",
            "expected_revision": True, "workflow": "state_fixture", "request_key": "r"})
        assert bad.status_code == 422
        a = client.post("/api/state/commands/start_attempt", json={"project_id": "game", "node_key": "a",
            "expected_revision": 1, "workflow": "state_fixture", "request_key": "r"}).json()
        assert a["run_id"]
        assert client.get("/api/state/attempts/" + a["attempt_id"]).json()["run_id"] == a["run_id"]
        assert client.get("/api/state/projects/unknown").status_code == 404
        assert client.post("/api/state/commands/revise_node", json={"project_id": "game", "node_key": "a",
            "expected_revision": 99, "reason": "old read"}).status_code == 409
        assert client.post("/api/state/commands/record_evidence", json={"reviewer": "spoofed"}).status_code == 422


def rpc(client, method, params):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                       headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})


def test_mcp_real_wire_uses_same_graph_and_marks_domain_errors(live, client, monkeypatch):
    # Positive wire tests must explicitly authenticate; a real deployment can
    # enable its auth gate through .env even when the generic HTTP fixture is
    # in test mode. Separate refusal tests below keep anonymous access closed.
    from api import mcp_router
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda request: True)
    handshake = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "state-test", "version": "1"}})
    assert handshake.status_code == 200
    result = rpc(client, "tools/call", {"name": "state_graph_read", "arguments": {
        "action": "frontier", "arguments": {"project_id": "game"}}}).json()["result"]
    assert not result.get("isError")
    assert json.loads(result["content"][0]["text"])["result"]["total"] == 1
    bad = rpc(client, "tools/call", {"name": "state_graph_write", "arguments": {
        "action": "add_nodes", "arguments": {"project_id": "game", "nodes": [spec("loop", ["loop"])]}}}).json()["result"]
    assert bad["isError"] is True
    good = rpc(client, "tools/call", {"name": "state_graph_write", "arguments": {
        "action": "start_attempt", "arguments": {"project_id": "game", "node_key": "a", "expected_revision": 1,
        "workflow": "state_fixture", "request_key": "mcp-1"}}}).json()["result"]
    assert not good.get("isError"), good
    a = json.loads(good["content"][0]["text"])["result"]
    assert a["run_id"] and a["checkpoints"] == "ask"
    assert live.service.attempts.get(a["attempt_id"])["run_id"] == a["run_id"]


def test_mcp_state_read_is_not_an_anonymous_read(live, monkeypatch):
    from api import mcp_router
    mcp_router.build_mcp()
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    with pytest.raises(mcp_router.ToolDenied):
        mcp_router._authorize("state_graph_read", None)
    mcp_router._authorize("list_pipelines", None)


@pytest.mark.asyncio
async def test_internal_driver_uses_same_persistent_contracts(live):
    from core.meta_agent import MetaAgent, TOOL_DEFINITIONS
    agent = SimpleNamespace(db=live.db, ws=live.ws, owner_email="driver@local", mode="butler")
    names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert names.count("state_graph_help") == names.count("state_graph_read") == names.count("state_graph_write") == 1
    help_result = await MetaAgent._execute_tool(agent, "state_graph_help", {})
    assert "start_attempt" in help_result["operations"]
    result = await MetaAgent._execute_tool(agent, "state_graph_read", {"action": "get_node",
        "arguments": {"project_id": "game", "node_key": "a"}})
    assert result["result"]["node"]["goal"] == "Deliver a"
    launched = await MetaAgent._execute_tool(agent, "state_graph_write", {"action": "start_attempt", "arguments": {
        "project_id": "game", "node_key": "a", "expected_revision": 1, "workflow": "state_fixture", "request_key": "driver-1"}})
    assert launched["result"]["run_id"]
    refused = await MetaAgent._execute_tool(agent, "state_graph_read", {"action": "create_project",
        "arguments": {"project_id": "unauthorized-through-read", "title": "No"}})
    assert "error" in refused


def test_driver_attachment_is_deduplicated_and_can_resume_after_completion(live, monkeypatch):
    # Test the actual helper, not the fixture's callback spy.
    import importlib.util
    from api import mcp_router
    original_module = importlib.util.spec_from_file_location("state_driver_test_original", mcp_router.__file__)
    module = importlib.util.module_from_spec(original_module)
    original_module.loader.exec_module(module)
    pending = []
    monkeypatch.setattr(module, "_MAIN_LOOP", object())
    def schedule(coroutine, loop):
        coroutine.close()
        future = Future()
        pending.append(future)
        return future
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", schedule)
    assert module._start_driver("run1", scheduler_owned=True, auto_approve=False)
    assert module._start_driver("run1", scheduler_owned=True, auto_approve=False)
    assert len(pending) == 1
    pending[0].set_result(None)
    assert module._start_driver("run1", scheduler_owned=True, auto_approve=False)
    assert len(pending) == 2
    pending[1].set_result(None)
    assert not module._DRIVERS_BY_RUN and not module._DRIVERS


def test_legacy_import_keeps_original_tasks_and_never_inherits_verified(live):
    live.db.ensure_project("legacy", name="Legacy", repo_type="none")
    with live.db.get_connection() as conn:
        first = conn.execute("INSERT INTO tasks(project_id,prompt,status,dependencies) VALUES('legacy','old A','completed','[]')").lastrowid
        second = conn.execute("INSERT INTO tasks(project_id,prompt,status,dependencies) VALUES('legacy','old B','pending',?)", (json.dumps([first]),)).lastrowid
        conn.commit()
    result = live.service.import_tasks("game", "legacy")
    assert result["verified"] == 0
    assert live.service.store.get_node("game", "legacy-" + str(first))["status"] == "CANDIDATE"
    assert live.service.store.get_node("game", "legacy-" + str(second))["readiness"] == "blocked"
    with live.db.get_connection() as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (first,)).fetchone()[0] == "completed"
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id='legacy'").fetchone()[0] == 2


def test_legacy_dangling_dependency_import_rolls_back(live):
    live.db.ensure_project("legacy", name="Legacy", repo_type="none")
    with live.db.get_connection() as conn:
        conn.execute("INSERT INTO tasks(project_id,prompt,status,dependencies) VALUES('legacy','broken','pending','[999999]')")
        conn.commit()
    before = live.service.store.get_graph("game")
    with pytest.raises(StateGraphError, match="dependency"):
        live.service.import_tasks("game", "legacy")
    assert live.service.store.get_graph("game") == before


def test_missing_empty_or_symlink_output_is_never_accepted(live):
    a = start(live)
    live.sf.advance_run(a["run_id"])
    claimed = live.sf.claim_next_step(a["run_id"])
    live.sf.confirm_step(claimed.token, StepResult())
    live.sf.advance_run(a["run_id"])
    a = live.service.reconcile_attempt(a["attempt_id"])
    assert a["artifact_pending"] is True and a["artifact_ref"] is None
    final = live.ws.get_final_path(a["execution_project_id"], "work", a["workflow"])
    final.mkdir(parents=True, exist_ok=True)
    outside = live.tmp / "outside.txt"
    outside.write_text("not an artifact")
    (final / "result.txt").symlink_to(outside)
    a = live.service.reconcile_attempt(a["attempt_id"])
    assert a["artifact_pending"] is True and a["artifact_ref"] is None
    assert live.service.store.get_node("game", "a")["status"] != "VERIFIED"


def test_code_launch_uses_a_run_owned_tree_and_actual_commit(live):
    from core import run_isolation
    repo = live.tmp / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "feature.py").write_text("def value(): return 1\n")
    subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    initial = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    live.db.ensure_project("source", name="Source", repo_type="existing", repo_path=str(repo))
    live.service.create_project("codegame", "Code game", source_project_id="source")
    live.service.store.add_nodes("codegame", [spec("code")])
    a = live.service.start_attempt("codegame", "code", 1, "state_code_fixture", "code-1")
    rec = run_isolation.record(live.db, a["run_id"])
    assert rec["mode"] == "worktree" and rec["source_repo"] == str(repo)
    wt = Path(rec["worktree_path"])
    assert wt != repo
    (wt / "feature.py").write_text("def value(): return 2\n")
    subprocess.run(["git", "add", "feature.py"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "implementation"], cwd=wt, check=True)
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=wt, text=True).strip()
    a = finish(live, a)
    assert a["artifact_ref"] == expected != initial
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == initial


def test_output_contract_is_pinned_even_if_current_manifest_changes(live):
    a = start(live)
    assert a["context"]["host_contract"]["output_step"] == "work"
    live.registry.get("state_fixture").output_step = "different-output"
    a = finish(live, a)
    assert a["artifact_ref"] and not a.get("artifact_pending")
    assert a["context"]["host_contract"]["output_step"] == "work"


def test_read_only_query_cannot_retire_or_verify(live):
    from core.state_commands import execute
    a = live.service.attempts.reserve("game", "a", 1, "state_fixture", "reserve-only")
    with pytest.raises(StateGraphError, match="read surface"):
        execute(live.service, "retire_reservation", {"attempt_id": a["attempt_id"], "reason": "No"})
    result = execute(live.service, "retire_reservation", {"attempt_id": a["attempt_id"], "reason": "Change workflow"}, allow_write=True)
    assert result["status"] == "superseded"
    assert live.service.store.get_node("game", "a")["readiness"] == "ready"


def test_state_inspection_does_not_require_a_working_executor(live):
    from core.state_commands import execute
    calls = []
    def unavailable():
        calls.append(1)
        raise RuntimeError("executor unavailable")
    service = StateService(live.db, live.ws, runtime_factory=unavailable)
    assert execute(service, "frontier", {"project_id": "game"})["total"] == 1
    execute(service, "add_nodes", {"project_id": "game", "nodes": [spec("offline-plan")]}, allow_write=True)
    assert calls == []
    with pytest.raises(StateConflict, match="runtime is unavailable"):
        service.start_attempt("game", "a", 1, "state_fixture", "not-started")
    assert len(calls) == 1
    assert service.attempts.list("game", "a") == []


def test_real_checkpoint_stays_paused_under_state_reconciliation(live):
    # SkillFlow applies a checkpoint before a transition; a terminal node
    # with no successor completes instead. Exercise a real review boundary.
    graph = PipelineGraph(name="checkpoint_fixture", begin="work", steps=[
        StepNode(id="work", checkpoint=True, transitions=[Transition(to="after_review")]),
        StepNode(id="after_review")])
    live.sf.register_graph(graph)
    live.registry.register_one(live.sf, graph.name, hint_overrides={"scheduler_owned": True,
        "repo_mode": "none", "seed_file": "plan.md", "output_step": "work"})
    a = start(live, workflow="checkpoint_fixture")
    live.sf.advance_run(a["run_id"])
    claim = live.sf.claim_next_step(a["run_id"])
    live.sf.confirm_step(claim.token, StepResult(outputs={"done": "candidate only"}))
    live.sf.advance_run(a["run_id"])
    assert live.sf.get_run(a["run_id"])["status"] == "paused"
    assert live.service.reconcile_attempt(a["attempt_id"])["status"] == "paused"
    with pytest.raises(StateConflict):
        live.service.verify_node("game", "a", 1, a["attempt_id"])
    assert live.sf.get_run(a["run_id"])["status"] == "paused"


def test_superseded_legacy_task_is_not_reopened_by_import(live):
    live.db.ensure_project("legacy", name="Legacy", repo_type="none")
    with live.db.get_connection() as conn:
        tid = conn.execute("INSERT INTO tasks(project_id,prompt,status,dependencies) VALUES('legacy','obsolete','superseded','[]')").lastrowid
        conn.commit()
    live.service.import_tasks("game", "legacy")
    assert live.service.store.get_node("game", "legacy-" + str(tid))["status"] == "SUPERSEDED"


def test_end_to_end_rest_acceptance_and_requirement_revision(live):
    from api import state_graph_routers as routes
    app = FastAPI()
    app.state._test_mode = True
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: live.service
    with TestClient(app) as client:
        a = finish(live, start(live))
        def command(action, args):
            return client.post("/api/state/commands/" + action, json=args)
        target = {"project_id": "game", "node_key": "a", "expected_revision": 1, "attempt_id": a["attempt_id"]}
        assert command("verify_node", target).status_code == 409
        for check in ["behaviour", "review"]:
            out = command("record_evidence", {"attempt_id": a["attempt_id"], "evidence_id": "rest-" + check,
                "criterion_id": check, "verdict": "pass", "artifact": a["artifact_ref"],
                "report_ref": "test-reports/" + check + ".json", "report_sha256": "f" * 64})
            assert out.status_code == 200
            assert out.json()["reviewer"] == "test-reviewer"
        assert command("verify_node", target).status_code == 200
        assert client.get("/api/state/projects/game/frontier").json()["nodes"][0]["node_key"] == "b"
        assert command("revise_node", {"project_id": "game", "node_key": "a", "expected_revision": 1,
            "reason": "New requirement", "goal": "Changed rule"}).status_code == 200
        assert command("verify_node", target).status_code == 409
        assert live.service.store.get_node("game", "b")["readiness"] == "blocked"


def test_offline_executable_demo_runs_real_red_then_green_tests(live):
    import sys
    source = Path(__file__).resolve().parents[2]
    report = live.tmp / "demo-result.json"
    out = subprocess.run([sys.executable, "examples/state_graph_demo.py", "--report", str(report)],
                         cwd=source, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    result = json.loads(report.read_text())
    assert result["result"] == "PASS"
    assert [r["verdict"] for r in result["actual_test_verdicts"]] == ["fail", "pass", "pass"]
    assert result["workflow_runs"] == 3
    assert result["requirement_change_invalidated_downstream"] is True
    assert result["production_data_used"] is False


def test_sha256_git_format_is_not_confused_with_output_bundle(live, monkeypatch):
    from core import run_isolation
    a = start(live)
    a = finish(live, a)
    rec = {"mode": run_isolation.MODE_WORKTREE}
    monkeypatch.setattr(run_isolation, "record", lambda *_: rec)
    monkeypatch.setattr(run_isolation, "resolve_for_resolver", lambda *_: str(live.tmp))
    monkeypatch.setattr(live.service, "_git", lambda path, *args: "" if args[0] == "status" else "b" * 64)
    with pytest.raises(StateConflict, match="SHA-1-format"):
        live.service._artifact(a)


def test_mcp_private_state_denial_is_an_error_without_goal_disclosure(live, client, monkeypatch):
    from api import mcp_router
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda request: False)
    rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "state-anonymous-test", "version": "1"}})
    result = rpc(client, "tools/call", {"name": "state_graph_read", "arguments": {
        "action": "get_graph", "arguments": {"project_id": "game"}}}).json()["result"]
    assert result["isError"] is True
    body = result["content"][0]["text"]
    assert "denied:" in body and "Deliver a" not in body
    assert len(live.service.store.get_graph("game")["nodes"]) == 2
