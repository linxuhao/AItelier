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
from core.state_service import SEED_HEADING, StateService
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
    service = StateService(db, ws, sf, registry, attach, actor="test-reviewer",
                           project_read_trusted=True)
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
    body = json.dumps({"status": "completed", "settled": True, "usable": True,
                       "check": check, "criterion_id": check,
                       "artifact": attempt["artifact_ref"], "passed": True}).encode()
    report = live.tmp / (attempt["node_key"] + "-" + check + ".json")
    report.write_bytes(body)
    return live.service.record_evidence(attempt["attempt_id"], attempt["attempt_id"] + "-" + check,
        check, "pass", attempt["artifact_ref"], str(report), hashlib.sha256(body).hexdigest(), "Fixture verifier")


def failed_code_attempt(live):
    """One real isolated run with a committed half and an unpromoted draft."""
    from core import run_isolation
    repo = live.tmp / "failed-source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "feature.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    live.db.ensure_project("failed-source", name="Source", repo_type="existing", repo_path=str(repo))
    live.service.create_project("codegame", "Code game", source_project_id="failed-source")
    live.service.store.add_nodes("codegame", [spec("code")])
    attempt = live.service.start_attempt(
        "codegame", "code", 1, "state_code_fixture", "initial")
    rec = run_isolation.record(live.db, attempt["run_id"])
    tree = Path(rec["worktree_path"])
    (tree / "feature.py").write_text("VALUE = 2\n")
    subprocess.run(["git", "add", "feature.py"], cwd=tree, check=True)
    subprocess.run(["git", "commit", "-qm", "retained implementation"], cwd=tree, check=True)
    live.ws.write_draft(attempt["execution_project_id"], "work", "partial.txt",
                        "retained artifact draft\n", graph_name=attempt["workflow"])
    live.sf.trace(attempt["run_id"], "step", "turn_budget_exhausted", {
        "step_id": "work", "turns": 32, "max_turns": 32,
        "remaining_delivery": ["finish the focused test", "submit the immutable report"],
        "first_failure_run_id": attempt["run_id"],
    }, step_id="work", project_id=attempt["execution_project_id"])
    live.sf.fail_run(attempt["run_id"],
                     "Step work: native turn budget exhausted (32/32); no automatic retry")
    return live.service.reconcile_attempt(attempt["attempt_id"])


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


def test_state_service_refuses_missing_or_incomplete_evidence_and_retains_valid_bytes(live):
    attempt = finish(live, start(live, request_key="evidence-integrity"))
    missing = live.tmp / "missing-review.json"
    with pytest.raises(StateConflict, match="report|unavailable|inspectable"):
        live.service.record_evidence(
            attempt["attempt_id"], "missing-review", "review", "pass",
            attempt["artifact_ref"], str(missing), "1" * 64)

    incomplete = live.tmp / "incomplete-review.json"
    incomplete.write_text("{}", encoding="utf-8")
    with pytest.raises(StateConflict, match="complete|terminal|schema"):
        live.service.record_evidence(
            attempt["attempt_id"], "incomplete-review", "review", "pass",
            attempt["artifact_ref"], str(incomplete),
            hashlib.sha256(incomplete.read_bytes()).hexdigest())

    contradictory = live.tmp / "contradictory-review.json"
    contradictory.write_text(json.dumps({
        "status": "completed", "settled": True, "usable": True,
        "verdict": "fail", "criterion_id": "review",
    }), encoding="utf-8")
    with pytest.raises(StateConflict, match="verdict conflicts"):
        live.service.record_evidence(
            attempt["attempt_id"], "contradictory-review", "review", "pass",
            attempt["artifact_ref"], str(contradictory),
            hashlib.sha256(contradictory.read_bytes()).hexdigest())

    reports = []
    for criterion in ("behaviour", "review"):
        report = live.tmp / f"complete-{criterion}.json"
        report.write_text(json.dumps({
            "status": "completed", "settled": True, "usable": True,
            "verdict": "pass", "criterion_id": criterion,
        }), encoding="utf-8")
        reports.append(report)
        live.service.record_evidence(
            attempt["attempt_id"], f"complete-{criterion}", criterion, "pass",
            attempt["artifact_ref"], str(report),
            hashlib.sha256(report.read_bytes()).hexdigest())
    for report in reports:
        report.unlink()
    receipt = live.service.verify_node("game", "a", 1, attempt["attempt_id"])
    assert receipt["attempt_id"] == attempt["attempt_id"]


def test_authoritative_design_revision_is_frozen_through_real_launch_seed_and_claim(live):
    service = live.service
    service.create_project("ruling", "Authoritative ruling")
    service.store.add_nodes("ruling", [spec("work")])
    first = service.design.create_revision(
        "ruling", "rule", 0, "Rule", "Use the old rule.",
        "The old rule is the first approved decision.", [], {"mode": "all"},
        lifecycle_status="approved",
    )
    service.design.create_baseline(
        "ruling", "baseline-old", [{"design_id": "rule", "revision": first["revision"]}])
    service.design.bind_node(
        "ruling", "work", 1, "baseline-old",
        [{"design_id": "rule", "revision": 1, "purpose": "context",
          "coverage_scope": {"mode": "all"}}],
        "Bind the approved rule.",
    )
    old = service.start_attempt("ruling", "work", 2, "state_fixture", "old")
    from core.seed_publication import seed_dir
    old_payload = (seed_dir(live.sf, old["execution_project_id"], "state_fixture") / "plan.md").read_text()
    old_seed = json.loads(old_payload[len(SEED_HEADING):].strip())
    assert old_seed["design_context"]["baseline_id"] == "baseline-old"
    assert old["context"]["design_context"]["bindings"][0]["design"]["revision"] == 1
    finish(live, old)

    newer = service.design.create_revision(
        "ruling", "rule", 1, "Rule", "Use the new rule.",
        "The owner explicitly revised the decision.", [], {"mode": "all"},
        lifecycle_status="approved",
    )
    service.design.create_baseline(
        "ruling", "baseline-new", [{"design_id": "rule", "revision": newer["revision"]}],
        expected_baseline_id="baseline-old",
    )
    service.store.revise_node("ruling", "work", 2, "Adopt the revised rule.")
    service.design.bind_node(
        "ruling", "work", 3, "baseline-new",
        [{"design_id": "rule", "revision": 2, "purpose": "context",
          "coverage_scope": {"mode": "all"}}],
        "Bind the revised approved rule.",
    )
    current = service.start_attempt("ruling", "work", 4, "state_fixture", "new")
    current_payload = (seed_dir(live.sf, current["execution_project_id"], "state_fixture") / "plan.md").read_text()
    current_seed = json.loads(current_payload[len(SEED_HEADING):].strip())
    assert current_seed["design_context"]["baseline_id"] == "baseline-new"
    assert current["context"]["design_context"]["bindings"][0]["design"]["revision"] == 2
    assert service.attempts.get(old["attempt_id"])["context"]["design_context"]["baseline_id"] == "baseline-old"
    live.sf.advance_run(current["run_id"])
    claim = live.sf.claim_next_step(current["run_id"])
    assert claim is not None and claim.step_id == "work"


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


def _code_state_project(live, name="frozen"):
    repo = live.tmp / (name + "-repo")
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@localhost"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "kept.txt").write_text("unchanged\n")
    subprocess.run(["git", "add", "kept.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    live.db.ensure_project(name + "-source", name=name, repo_type="existing", repo_path=str(repo))
    live.service.create_project(name, name, source_project_id=name + "-source")
    live.service.store.add_nodes(name, [spec("work")])
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          text=True, capture_output=True).stdout.strip()
    return repo, head


def _frozen(*checks):
    return {"version": 1, "checks": list(checks)}


def _required(cid, probe, expected, **arguments):
    return {"id": cid, "probe": probe, "arguments": arguments,
            "expected": expected}


def test_director_chosen_base_provisions_real_attempt_from_differing_commit(live):
    from core import run_isolation
    from core.state_commands import describe, execute

    repo, chosen = _code_state_project(live, "chosen-base")
    (repo / "later.txt").write_text("source moved\n")
    subprocess.run(["git", "add", "later.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "later source head"], cwd=repo, check=True)
    source_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        text=True, capture_output=True).stdout.strip()
    assert source_head != chosen

    operation = describe()["operations"]["request_attempt_base"]
    assert operation["mutates"] is True
    assert describe()["operations"]["start_attempt"]["arguments"]["properties"]["base_sha"]
    arguments = {
        "project_id": "chosen-base", "node_key": "work", "expected_revision": 1,
        "workflow": "state_code_fixture", "request_key": "chosen-base",
        "base_sha": chosen,
    }
    launched = execute(live.service, "start_attempt", arguments, allow_write=True)
    assert launched["status"] == "running" and launched["run_id"]
    replayed = execute(live.service, "start_attempt", arguments, allow_write=True)
    assert replayed["attempt_id"] == launched["attempt_id"]
    assert replayed["run_id"] == launched["run_id"]
    with pytest.raises(StateConflict, match="request key already used"):
        execute(live.service, "start_attempt", {
            **arguments, "base_sha": source_head,
        }, allow_write=True)

    requested = run_isolation.requested_base(
        live.db, launched["execution_project_id"])
    assert requested["base_sha"] == chosen
    isolation = run_isolation.record(live.db, launched["run_id"])
    assert isolation["base_sha"] == chosen
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=isolation["worktree_path"], check=True,
        text=True, capture_output=True).stdout.strip() == chosen
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        text=True, capture_output=True).stdout.strip() == source_head


def test_director_chosen_unreachable_base_fails_closed_during_real_provision(live):
    from core import run_isolation
    from core.state_commands import execute

    repo, source_head = _code_state_project(live, "chosen-missing")
    missing = "f" * 40
    refused = execute(live.service, "start_attempt", {
        "project_id": "chosen-missing", "node_key": "work", "expected_revision": 1,
        "workflow": "state_code_fixture", "request_key": "chosen-missing",
        "base_sha": missing,
    }, allow_write=True)
    assert refused["status"] == "unknown" and refused["run_id"] is None
    assert "requested base" in refused["error"]
    assert "refusing to provision from HEAD instead" in refused["error"]
    engine_runs = live.sf.list_runs(project_id=refused["execution_project_id"])
    assert len(engine_runs) == 1
    assert run_isolation.record(live.db, engine_runs[0]["id"]) is None
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        text=True, capture_output=True).stdout.strip() == source_head


def test_director_chosen_base_surfaces_existing_isolation_record(live, monkeypatch):
    from core import run_isolation
    from core.run_launcher import start_config_run
    from core.state_commands import execute

    repo, base = _code_state_project(live, "chosen-already-bound")
    original = run_isolation.request_base

    def competing_provision(db, project_id, base_sha, note=""):
        competing = start_config_run(
            live.db, live.ws, "state_code_fixture", project_id,
            seed_text="competing launch", repo_type="existing", repo_path=str(repo))
        assert competing["status"] == "started"
        return original(db, project_id, base_sha, note=note)

    monkeypatch.setattr(run_isolation, "request_base", competing_provision)

    with pytest.raises(StateConflict, match="already has an isolation record.*would never apply"):
        execute(live.service, "start_attempt", {
            "project_id": "chosen-already-bound", "node_key": "work",
            "expected_revision": 1, "workflow": "state_code_fixture",
            "request_key": "chosen-already-bound", "base_sha": base,
        }, allow_write=True)
    attempt = live.service.attempts.list("chosen-already-bound", "work")[0]
    assert attempt["status"] == "reserved" and attempt["run_id"] is None


def test_director_chosen_base_refuses_external_and_past_dispatch_window(live):
    from core.state_commands import execute

    external = live.service.start_external_attempt(
        "game", "a", 1, "codex", "external-1", "external-base")
    with pytest.raises(StateConflict, match="external attempt.*no execution project"):
        execute(live.service, "request_attempt_base", {
            "attempt_id": external["attempt_id"], "base_sha": "a" * 40,
        }, allow_write=True)

    _code_state_project(live, "chosen-past-window")
    running = execute(live.service, "start_attempt", {
        "project_id": "chosen-past-window", "node_key": "work", "expected_revision": 1,
        "workflow": "state_code_fixture", "request_key": "already-dispatched",
    }, allow_write=True)
    with pytest.raises(StateConflict, match="past the dispatch window.*status=running"):
        execute(live.service, "request_attempt_base", {
            "attempt_id": running["attempt_id"], "base_sha": "a" * 40,
        }, allow_write=True)


def test_frozen_base_mismatch_refuses_exact_regression_before_launch_claim_or_later_probe(
        live, monkeypatch):
    import core.frozen_attempt as frozen
    import core.run_isolation as isolation
    import core.run_launcher as launcher
    from aitelier.runner import AgentStepRunner

    repo, before = _code_state_project(live)
    original = frozen._source_head
    monkeypatch.setattr(frozen, "_source_head", lambda source: lambda args:
                        "c6446bd6146a70704a94a57356c6f4dabbad4298")
    calls = {"launch": 0, "claim": 0, "model": 0, "gate": 0,
             "capability": 0, "base_pin": 0}
    monkeypatch.setattr(launcher, "start_config_run", lambda *a, **k:
                        calls.__setitem__("launch", calls["launch"] + 1))
    monkeypatch.setattr(live.sf, "claim_next_step", lambda *a, **k:
                        calls.__setitem__("claim", calls["claim"] + 1))
    async def model_execute(*args, **kwargs):
        calls["model"] += 1
    monkeypatch.setattr(AgentStepRunner, "execute", model_execute)
    monkeypatch.setattr(live.service.attempts, "claim_launch", lambda *a, **k:
                        calls.__setitem__("gate", calls["gate"] + 1))
    monkeypatch.setattr(live.sf, "capability_identity", lambda name:
                        calls.__setitem__("capability", calls["capability"] + 1) or {})
    monkeypatch.setattr(isolation, "request_base", lambda *a, **k:
                        calls.__setitem__("base_pin", calls["base_pin"] + 1))
    attempt = live.service.start_attempt(
        "frozen", "work", 1, "state_code_fixture", "base-mismatch",
        frozen_prerequisites=_frozen(
            _required("source-base", "source_head",
                      "91621acd58b7cf16f20aaae15c35a34001e826c5"),
            _required("expensive-runtime", "runtime_capability", None, name="late"),
        ),
    )
    assert attempt["status"] == "failed" and attempt["run_id"] is None
    assert calls == {"launch": 0, "claim": 0, "model": 0, "gate": 0,
                     "capability": 0, "base_pin": 0}
    assert live.sf.list_runs(project_id=attempt["execution_project_id"]) == []
    assert (repo / "kept.txt").read_text() == "unchanged\n"
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          text=True, capture_output=True).stdout.strip() == before
    event = [e for e in live.service.store.events("frozen")
             if e["event_type"] == "attempt_preflight_failed"][-1]
    check = event["payload"]["report"]["checks"][0]
    assert check["required"] == "91621acd58b7cf16f20aaae15c35a34001e826c5"
    assert check["actual"] == "c6446bd6146a70704a94a57356c6f4dabbad4298"
    monkeypatch.setattr(frozen, "_source_head", original)


@pytest.mark.parametrize("case", ["input-hash", "missing-capability", "unknown-shape"])
def test_frozen_inputs_capabilities_and_unknown_shapes_fail_before_launch(live, monkeypatch, case):
    import core.run_launcher as launcher

    repo, head = _code_state_project(live, "frozen-" + case)
    report = live.tmp / (case + ".json")
    report.write_text("immutable input\n")
    actual_hash = hashlib.sha256(report.read_bytes()).hexdigest()
    launches = []
    monkeypatch.setattr(launcher, "start_config_run", lambda *a, **k: launches.append(1))
    if case == "input-hash":
        checks = [_required("input", "sha256_file", "0" * 64, path=str(report))]
    elif case == "missing-capability":
        checks = [_required("runtime", "runtime_capability",
                            {"name": "absent", "tools": [], "briefing": "", "owner": "host"},
                            name="absent")]
    else:
        checks = [{**_required("source", "source_head", head), "extra": True}]
    attempt = live.service.start_attempt(
        "frozen-" + case, "work", 1, "state_code_fixture", case,
        frozen_prerequisites=_frozen(*checks),
    )
    assert attempt["status"] == "failed" and not launches
    assert live.sf.list_runs(project_id=attempt["execution_project_id"]) == []
    assert (repo / "kept.txt").read_text() == "unchanged\n"
    if case == "input-hash":
        event = [e for e in live.service.store.events("frozen-" + case)
                 if e["event_type"] == "attempt_preflight_failed"][-1]
        got = event["payload"]["report"]["checks"][0]
        assert got["required"] == "0" * 64 and got["actual"] == actual_hash


def test_matching_frozen_prerequisites_launch_success_control(live):
    repo, head = _code_state_project(live, "frozen-ok")
    report = live.tmp / "input-ok.json"
    report.write_text("bound report\n")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    live.sf.register_capability("review", tools=[], briefing="review inputs", owner="host")
    expected_capability = {"name": "review", "tools": [],
                           "briefing": "review inputs", "owner": "host",
                           "available": True, "tool_schema_sha256": {}}
    attempt = live.service.start_attempt(
        "frozen-ok", "work", 1, "state_code_fixture", "ok",
        frozen_prerequisites=_frozen(
            _required("source", "source_head", head),
            _required("input", "sha256_file", digest, path=str(report)),
            _required("runtime", "runtime_capability", expected_capability, name="review"),
        ),
    )
    assert attempt["status"] == "running" and attempt["run_id"]
    from core import run_isolation
    isolation = run_isolation.record(live.db, attempt["run_id"])
    assert isolation["base_sha"] == head
    events = [e for e in live.service.store.events("frozen-ok")
              if e["event_type"] == "attempt_preflight_passed"]
    assert len(events) == 1 and events[0]["payload"]["report"]["passed"] is True
    assert [c["actual"] for c in events[0]["payload"]["report"]["checks"]] == [
        head, digest, expected_capability]
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=isolation["worktree_path"],
                          check=True, text=True, capture_output=True).stdout.strip() == head
    live.sf.advance_run(attempt["run_id"])
    claim = live.sf.claim_next_step(attempt["run_id"])
    assert claim is not None and claim.step_id == "work"


def test_frozen_source_pin_survives_head_swap_before_worktree_creation(live, monkeypatch):
    from core import run_isolation

    repo, head = _code_state_project(live, "frozen-head-swap")
    live.sf.register_capability("review", tools=[], briefing="review inputs", owner="host")
    expected_capability = {"name": "review", "tools": [],
                           "briefing": "review inputs", "owner": "host",
                           "available": True, "tool_schema_sha256": {}}
    original_request_base = run_isolation.request_base
    moved = {}

    def pin_then_move(db, project_id, base_sha, note=""):
        result = original_request_base(db, project_id, base_sha, note=note)
        (repo / "later.txt").write_text("new source head after successful preflight\n")
        subprocess.run(["git", "add", "later.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "move source after preflight"], cwd=repo, check=True)
        moved["head"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                         text=True, capture_output=True).stdout.strip()
        return result

    monkeypatch.setattr(run_isolation, "request_base", pin_then_move)
    attempt = live.service.start_attempt(
        "frozen-head-swap", "work", 1, "state_code_fixture", "head-swap",
        frozen_prerequisites=_frozen(
            _required("source", "source_head", head),
            _required("runtime", "runtime_capability", expected_capability, name="review"),
        ),
    )

    assert attempt["status"] == "running" and attempt["run_id"]
    assert moved["head"] != head
    isolation = run_isolation.record(live.db, attempt["run_id"])
    assert isolation["base_sha"] == head
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=isolation["worktree_path"],
                          check=True, text=True, capture_output=True).stdout.strip() == head
    event = [e for e in live.service.store.events("frozen-head-swap")
             if e["event_type"] == "attempt_preflight_passed"][-1]
    assert event["payload"]["report"]["checks"][0] == {
        "id": "source", "probe": "source_head", "required": head,
        "actual": head, "passed": True,
    }
    live.sf.advance_run(attempt["run_id"])
    claim = live.sf.claim_next_step(attempt["run_id"])
    assert claim is not None and claim.step_id == "work"


@pytest.mark.parametrize("invalid", [
    None,
    {"name": "review", "tools": ["inspect"], "briefing": "", "owner": "host",
     "available": False, "tool_schema_sha256": {"inspect": None}},
    "malformed",
    {"name": "review", "available": True},
])
def test_mirrored_invalid_runtime_capability_refuses_before_every_side_effect(
        live, monkeypatch, invalid):
    import core.run_isolation as isolation
    import core.run_launcher as launcher
    from aitelier.runner import AgentStepRunner

    repo, before = _code_state_project(live, "invalid-capability")
    calls = {"launch": 0, "claim": 0, "model": 0, "gate": 0, "base_pin": 0}
    monkeypatch.setattr(live.sf, "capability_identity", lambda name: invalid)
    monkeypatch.setattr(launcher, "start_config_run", lambda *a, **k:
                        calls.__setitem__("launch", calls["launch"] + 1))
    monkeypatch.setattr(live.sf, "claim_next_step", lambda *a, **k:
                        calls.__setitem__("claim", calls["claim"] + 1))
    async def model_execute(*args, **kwargs):
        calls["model"] += 1
    monkeypatch.setattr(AgentStepRunner, "execute", model_execute)
    monkeypatch.setattr(live.service.attempts, "claim_launch", lambda *a, **k:
                        calls.__setitem__("gate", calls["gate"] + 1))
    monkeypatch.setattr(isolation, "request_base", lambda *a, **k:
                        calls.__setitem__("base_pin", calls["base_pin"] + 1))

    attempt = live.service.start_attempt(
        "invalid-capability", "work", 1, "state_code_fixture",
        "mirrored-invalid-" + str(type(invalid).__name__),
        frozen_prerequisites=_frozen(
            _required("runtime", "runtime_capability", invalid, name="review")),
    )

    assert attempt["status"] == "failed" and attempt["run_id"] is None
    assert calls == {"launch": 0, "claim": 0, "model": 0, "gate": 0,
                     "base_pin": 0}
    assert live.sf.list_runs(project_id=attempt["execution_project_id"]) == []
    assert (repo / "kept.txt").read_text() == "unchanged\n"
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          text=True, capture_output=True).stdout.strip() == before
    event = [e for e in live.service.store.events("invalid-capability")
             if e["event_type"] == "attempt_preflight_failed"][-1]
    check = event["payload"]["report"]["checks"][0]
    assert check["required"] == invalid and check["actual"] == invalid
    assert check["passed"] is False


def test_missing_or_unseeded_workflow_is_refused_before_attempt_creation(live):
    with pytest.raises(StateGraphError, match="unknown workflow"):
        start(live, workflow="not-registered")
    live.registry.get("state_fixture").seed_file = None
    with pytest.raises(StateGraphError, match="seed-file"):
        start(live)
    assert live.service.attempts.list("game", "a") == []
    assert live.sf.list_runs() == []


def test_anonymous_reader_sees_the_graph_and_the_notebook_but_not_the_mailbox(live, monkeypatch):
    """The split this replaces the blanket writer gate with.

    Anonymous reads of the graph/attempts/issues are open — that is the point of
    building in public — and since the owner's ruling of 2026-09-22 the note reads
    are public actions; this fixture's service is read-trusted so the project
    half passes even though `game` is never opened here. The director
    mailbox stays writer-only, and every write stays refused. Its pole is proved
    with the refusal's own words: a READ request must not be answered "to make
    changes".
    """
    from api import authz, state_graph_routers as routes
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: live.service
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *_: None)
    with TestClient(app) as client:
        assert client.get("/api/state/projects").status_code == 200
        assert client.get("/api/state/projects/game").status_code == 200
        assert client.get("/api/state/projects/game/overview").status_code == 200
        assert client.post("/api/state/query/frontier",
                           json={"project_id": "game"}).status_code == 200
        assert client.get("/api/state/projects/game/driver-note").status_code == 200
        assert client.post("/api/state/query/driver_note_index",
                           json={"project_id": "game"}).status_code == 200
        refused = client.post("/api/state/query/list_director_messages",
                              json={"project_id": "game"})
        assert refused.status_code == 403
        assert "to make changes" not in refused.text
        assert refused.headers["X-AItelier-Denial"] == authz.READ_DENIED_NOT_AUTHENTICATED
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


def test_rest_exposes_one_exclusive_failed_attempt_disposition(live):
    from api import state_graph_routers as routes
    failed = failed_code_attempt(live)
    app = FastAPI()
    app.state._test_mode = True
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_service] = lambda: live.service
    with TestClient(app) as client:
        operation = client.get("/api/state/schema").json()["operations"]["disposition_failed_attempt"]
        enum = operation["arguments"]["properties"]["disposition"]["enum"]
        assert enum == ["continue-workflow", "handoff-external", "leave-stopped"]
        bad = client.post("/api/state/commands/disposition_failed_attempt", json={
            "attempt_id": failed["attempt_id"], "disposition": "handoff-external",
            "request_key": "missing-owner", "relay_digest": failed["relay_inventory"]["digest"]})
        assert bad.status_code == 422
        response = client.post("/api/state/commands/disposition_failed_attempt", json={
            "attempt_id": failed["attempt_id"], "disposition": "handoff-external",
            "request_key": "rest-handoff", "relay_digest": failed["relay_inventory"]["digest"],
            "instruction": "finish reported remainder", "harness": "rest-subagents",
            "external_id": "rest/job-1"})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["handoff"]["relay_inventory"] == failed["relay_inventory"]
        assert result["attempt"]["context_hash"] == live.service.attempts.get(
            result["attempt"]["attempt_id"])["context_hash"]
        assert client.get("/api/state/attempts/" + failed["attempt_id"]).json()["status"] == "failed"
        assert live.service.attempts.evidence(failed["attempt_id"]) == []


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


def test_mcp_failed_attempt_disposition_uses_same_digest_guard_and_is_idempotent(
        live, client, monkeypatch):
    from api import mcp_router
    failed = failed_code_attempt(live)
    monkeypatch.setattr(mcp_router.authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(mcp_router.authz, "request_can_write", lambda request: True)
    rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "state-disposition-test", "version": "1"}})
    arguments = {
        "attempt_id": failed["attempt_id"], "disposition": "continue-workflow",
        "request_key": "mcp-continue", "relay_digest": failed["relay_inventory"]["digest"],
        "instruction": "finish exact retained remainder",
    }
    call = lambda args: rpc(client, "tools/call", {"name": "state_graph_write", "arguments": {
        "action": "disposition_failed_attempt", "arguments": args}}).json()["result"]
    first = call(arguments)
    assert not first.get("isError"), first
    result = json.loads(first["content"][0]["text"])["result"]
    assert result["attempt"]["run_id"] != failed["run_id"]
    assert result["attempt"]["context"]["relay"]["staged_files"] == failed["relay_inventory"]["staged_files"]
    second = call(arguments)
    repeated = json.loads(second["content"][0]["text"])["result"]
    assert repeated["idempotent"] is True
    assert repeated["attempt"]["attempt_id"] == result["attempt"]["attempt_id"]
    bad = call({**arguments, "request_key": "mcp-stale", "relay_digest": "0" * 64})
    assert bad["isError"] is True
    assert live.service.attempts.get(failed["attempt_id"])["status"] == "failed"
    assert len(live.sf.list_runs()) == 2


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


def test_service_disposition_continues_as_a_distinct_fresh_run_without_rewriting_failure(live):
    failed = failed_code_attempt(live)
    before_trace = live.sf.get_trace(failed["run_id"])
    before_events = live.service.store.events("codegame", after=0, limit=500)

    stopped = live.service.disposition_failed_attempt(
        failed["attempt_id"], "leave-stopped")
    assert stopped["attempt"] is None and stopped["dispatchable"] is False
    assert stopped["automatic_retry"] is False
    assert len(live.sf.list_runs()) == 1
    assert live.service.disposition_failed_attempt(
        failed["attempt_id"], "leave-stopped") == stopped
    with pytest.raises(StateGraphError, match="cannot carry dispatch"):
        live.service.disposition_failed_attempt(
            failed["attempt_id"], "leave-stopped", request_key="must-not-be-ignored")
    with pytest.raises(StateGraphError, match="external harness"):
        live.service.disposition_failed_attempt(
            failed["attempt_id"], "continue-workflow", request_key="bad-shape",
            relay_digest=failed["relay_inventory"]["digest"], harness="wrong")

    result = live.service.disposition_failed_attempt(
        failed["attempt_id"], "continue-workflow", request_key="continue-once",
        relay_digest=failed["relay_inventory"]["digest"],
        instruction="finish only the reported remaining delivery")
    successor = result["attempt"]
    assert result["disposition"] == "continue-workflow" and result["dispatchable"] is True
    assert successor["attempt_id"] != failed["attempt_id"]
    assert successor["run_id"] != failed["run_id"]
    assert successor["context"]["relay"]["digest"] == failed["relay_inventory"]["digest"]
    assert successor["context"]["relay"]["base_sha"] == failed["relay_inventory"]["head_sha"]
    assert successor["context"]["relay"]["staged_files"] == failed["relay_inventory"]["staged_files"]
    assert not any(r["event"] == "turn_budget_exhausted"
                   for r in live.sf.get_trace(successor["run_id"])), "new run has a fresh role turn"

    duplicate = live.service.disposition_failed_attempt(
        failed["attempt_id"], "continue-workflow", request_key="continue-once",
        relay_digest=failed["relay_inventory"]["digest"],
        instruction="finish only the reported remaining delivery")
    assert duplicate["idempotent"] is True
    assert duplicate["attempt"]["attempt_id"] == successor["attempt_id"]
    assert len(live.sf.list_runs()) == 2
    source = live.service.attempts.get(failed["attempt_id"])
    assert source["status"] == "failed" and source["error"] == failed["error"]
    assert live.sf.get_trace(failed["run_id"]) == before_trace
    assert live.service.attempts.evidence(failed["attempt_id"]) == []
    assert before_events == live.service.store.events(
        "codegame", after=0, limit=500)[:len(before_events)]


def test_service_external_handoff_freezes_complete_relay_and_rejects_drift(live):
    failed = failed_code_attempt(live)
    inventory = failed["relay_inventory"]
    result = live.service.disposition_failed_attempt(
        failed["attempt_id"], "handoff-external", request_key="handoff-once",
        relay_digest=inventory["digest"], instruction="finish the retained delivery",
        harness="director-subagents", external_id="subagent/attempt-7")
    external = result["attempt"]
    handoff = result["handoff"]
    assert result["dispatchable"] is True and external["execution_kind"] == "external"
    assert external["run_id"] is None and external["context"]["relay_handoff"] == handoff
    assert handoff["source_attempt_id"] == failed["attempt_id"]
    assert handoff["source_execution_project_id"] == failed["execution_project_id"]
    assert handoff["source_context_hash"] == failed["context_hash"]
    assert handoff["relay_inventory"]["base_sha"] == inventory["base_sha"]
    assert handoff["relay_inventory"]["head_sha"] == inventory["head_sha"]
    assert handoff["relay_inventory"]["staged_files"] == {
        "work": {"partial.txt": hashlib.sha256(b"retained artifact draft\n").hexdigest()}}
    assert handoff["remaining_delivery"] == [
        "finish the focused test", "submit the immutable report"]
    assert handoff["original_first_failure"]["attempt_id"] == failed["attempt_id"]
    assert handoff["original_first_failure"]["run_id"] == failed["run_id"]
    assert handoff["original_first_failure"]["trace"]["event"] == "turn_budget_exhausted"
    assert handoff["authority"]["checkpoint_policy"] == "ask"
    assert live.service.attempts.get(failed["attempt_id"])["status"] == "failed"
    assert len(live.sf.list_runs()) == 1, "external handoff never starts another workflow"

    duplicate = live.service.disposition_failed_attempt(
        failed["attempt_id"], "handoff-external", request_key="handoff-once",
        relay_digest=inventory["digest"], instruction="finish the retained delivery",
        harness="director-subagents", external_id="subagent/attempt-7")
    assert duplicate["idempotent"] is True
    assert duplicate["attempt"]["attempt_id"] == external["attempt_id"]
    assert len(live.service.attempts.list("codegame", "code")) == 2

    draft = live.ws._draft_dir(failed["execution_project_id"], "work", failed["workflow"])
    (draft / "partial.txt").write_text("drifted after inspection\n")
    with pytest.raises(StateConflict, match="changed since it was read"):
        live.service.disposition_failed_attempt(
            failed["attempt_id"], "handoff-external", request_key="handoff-after-drift",
            relay_digest=inventory["digest"], instruction="do not dispatch drift",
            harness="director-subagents", external_id="subagent/attempt-8")
    assert len(live.service.attempts.list("codegame", "code")) == 2


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
    service = StateService(live.db, live.ws, runtime_factory=unavailable, project_read_trusted=True)
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
            body = json.dumps({"status": "completed", "settled": True, "usable": True,
                               "verdict": "pass", "criterion_id": check}).encode()
            report = live.tmp / ("rest-" + check + ".json")
            report.write_bytes(body)
            out = command("record_evidence", {"attempt_id": a["attempt_id"], "evidence_id": "rest-" + check,
                "criterion_id": check, "verdict": "pass", "artifact": a["artifact_ref"],
                "report_ref": str(report), "report_sha256": hashlib.sha256(body).hexdigest()})
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


def test_mcp_external_token_reads_private_state_it_can_write(live, client, monkeypatch):
    from api import mcp_router
    from api import authz

    live.service.driver_notes.update("game", "permanent", "PRIVATE-MCP-NOTE", 0, "test")
    monkeypatch.setattr(mcp_router, "_EXTERNAL_TOKEN", "external-test-token")
    monkeypatch.setattr(client.app.state, "_test_mode", False)
    from api import main as main_module
    monkeypatch.setattr(main_module, "_ALLOW_EXTERNAL", True)
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "admin-test-token")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)

    def call(action, headers):
        response = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "state_graph_read", "arguments": {
                "action": action, "arguments": (
                    {} if action == "list_projects" else {"project_id": "game"})}},
        }, headers={"Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream", **headers})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    public = {"Cf-Ray": "edge-test", "X-AItelier-MCP-External-Token": "external-test-token"}
    listed = call("list_projects", public)
    assert listed.get("isError") is not True, listed
    assert [p["project_id"] for p in json.loads(listed["content"][0]["text"])["result"]] == ["game"]
    note = call("get_driver_note", public)
    assert note.get("isError") is not True, note
    assert "PRIVATE-MCP-NOTE" in note["content"][0]["text"]

    for headers in ({"Cf-Ray": "edge-test", "X-AItelier-MCP-External-Token": "wrong"},
                    {"Cf-Ray": "edge-test"},
                    {"X-AItelier-MCP-External-Token": "external-test-token"}):
        refused = call("get_driver_note", headers)
        assert refused["isError"] is True
        assert "PRIVATE-MCP-NOTE" not in refused["content"][0]["text"]


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


def _source_repo(live, name="source"):
    repo = live.tmp / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "feature.py").write_text("def value(): return 1\n")
    subprocess.run(["git", "add", "feature.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "unrelated work by somebody else"], cwd=repo, check=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    live.db.ensure_project(name, name="Source", repo_type="existing", repo_path=str(repo))
    return repo, head


def test_read_only_attempt_pins_its_own_findings_not_the_head_it_read(live):
    """An investigate-shaped attempt must not pin the repo HEAD it read.

    Measured defect: `investigate` runs (declared repo_mode code by default)
    were handed a worktree, committed nothing, and their artifact_ref became
    the source HEAD at launch time -- a commit holding none of the findings.
    record_evidence binds a verdict to (attempt, artifact_ref), so the review
    was an attestation about an unrelated tree.
    """
    from core import run_isolation
    repo, head = _source_repo(live)
    live.service.create_project("readgame", "Read-only game", source_project_id="source")
    live.service.store.add_nodes("readgame", [spec("look")])
    a = live.service.start_attempt("readgame", "look", 1, "state_fixture", "look-1")
    rec = run_isolation.record(live.db, a["run_id"])
    assert rec["mode"] == run_isolation.MODE_READ_SNAPSHOT
    assert rec["base_sha"] == head
    a = finish(live, a, output="findings: the growth curve is flat\n")
    assert a["status"] == "candidate"
    assert a["artifact_ref"] != head
    assert len(a["artifact_ref"]) == 64          # digest of its own output, not a commit
    # And the findings are what a reviewer's evidence binds to.
    evidence(live, a, "behaviour")
    evidence(live, a, "review")
    live.service.verify_node("readgame", "look", 1, a["attempt_id"])
    assert live.service.store.get_node("readgame", "look")["status"] == "VERIFIED"


def test_read_only_attempt_without_findings_is_refused_not_pinned_to_the_head(live):
    """No deliverable => no artifact. Never fall back to the commit it read."""
    from core import run_isolation
    repo, head = _source_repo(live, "source2")
    live.service.create_project("emptygame", "Read-only game", source_project_id="source2")
    live.service.store.add_nodes("emptygame", [spec("look")])
    a = live.service.start_attempt("emptygame", "look", 1, "state_fixture", "look-1")
    assert run_isolation.record(live.db, a["run_id"])["mode"] == run_isolation.MODE_READ_SNAPSHOT
    live.sf.advance_run(a["run_id"])
    claimed = live.sf.claim_next_step(a["run_id"])
    live.sf.confirm_step(claimed.token, StepResult())
    live.sf.advance_run(a["run_id"])
    a = live.service.reconcile_attempt(a["attempt_id"])
    assert a["artifact_pending"] is True and a["artifact_ref"] is None
    assert head not in json.dumps(a["note"])
    body = b'{"status":"completed","settled":true,"usable":true,"verdict":"pass","criterion_id":"review"}'
    report = live.tmp / "empty-evidence.json"
    report.write_bytes(body)
    with pytest.raises(StateConflict, match="pinned artifact"):
        live.service.record_evidence(a["attempt_id"], "ev-empty", "review", "pass", head,
                                     str(report), hashlib.sha256(body).hexdigest())
    assert live.service.store.get_node("emptygame", "look")["status"] != "VERIFIED"


def test_code_attempt_that_committed_nothing_is_refused_not_pinned_to_its_base(live):
    """A code-declared run whose worktree HEAD never moved delivered nothing."""
    from core import run_isolation
    repo, head = _source_repo(live, "source3")
    live.service.create_project("nocommit", "Code game", source_project_id="source3")
    live.service.store.add_nodes("nocommit", [spec("code")])
    a = live.service.start_attempt("nocommit", "code", 1, "state_code_fixture", "code-1")
    rec = run_isolation.record(live.db, a["run_id"])
    assert rec["mode"] == run_isolation.MODE_WORKTREE and rec["base_sha"] == head
    a = finish(live, a)                       # writes the output step, commits no code
    assert a["artifact_pending"] is True and a["artifact_ref"] is None
    assert "committed nothing" in a["note"]
    with pytest.raises(StateConflict, match="pinned artifact"):
        live.service.verify_node("nocommit", "code", 1, a["attempt_id"])


def test_investigate_config_declares_that_it_owns_no_repository(live):
    """The shipped investigate pipeline is read-only; declaring it code-producing
    is what routed it to a worktree and made its artifact a foreign commit."""
    import yaml
    root = Path(__file__).resolve().parents[2]
    hints = yaml.safe_load((root / "configs" / "investigate.yaml").read_text())["x-aitelier"]
    assert hints["repo_mode"] == "none"
    assert hints["output_step"] == "investigate"


def test_successor_restores_owner_cursor_and_pending_checkpoint_without_duplicate_dispatch(live):
    """An isolated successor rereads durable handoff facts before acting."""
    graph = PipelineGraph(name="handoff_checkpoint_fixture", begin="work", steps=[
        StepNode(id="work", checkpoint=True, transitions=[Transition(to="after_review")]),
        StepNode(id="after_review")])
    live.sf.register_graph(graph)
    live.registry.register_one(live.sf, graph.name, hint_overrides={"scheduler_owned": True,
        "repo_mode": "none", "seed_file": "plan.md", "output_step": "work"})
    live.service.store.add_nodes("game", [spec("running")])
    running = start(live, "running")
    checkpoint = start(live, "a", workflow=graph.name, request_key="checkpoint-handoff")
    live.sf.advance_run(checkpoint["run_id"])
    claim = live.sf.claim_next_step(checkpoint["run_id"])
    live.sf.confirm_step(claim.token, StepResult(outputs={"candidate": "pending decision"}))
    live.sf.advance_run(checkpoint["run_id"])
    assert live.sf.get_run(checkpoint["run_id"])["status"] == "paused"

    cursor = live.service.portfolio.overview("game")["event_seq"]
    owners = live.service.portfolio.run_owners(checkpoint["run_id"])
    assert owners["links"] == [{
        "project_id": "game", "title": "Long-running game", "node_key": "a",
        "attempt_id": checkpoint["attempt_id"], "relation": "attempt",
    }]

    successor = StateService(live.db, live.ws, live.sf, live.registry, live.service.attach_driver,
                             actor="successor", project_read_trusted=True)
    recovered = successor.recover_attempt(checkpoint["attempt_id"])
    assert recovered["attempt_id"] == checkpoint["attempt_id"]
    assert recovered["run_id"] == checkpoint["run_id"]
    assert recovered["status"] == "paused"
    snapshot = successor.portfolio.overview("game")
    assert snapshot["event_seq"] >= cursor
    by_node = {node["node_key"]: node for node in snapshot["nodes"]}
    assert by_node["running"]["latest_attempt"]["attempt_id"] == running["attempt_id"]
    assert by_node["running"]["latest_attempt"]["status"] == "running"
    assert by_node["a"]["latest_attempt"]["attempt_id"] == checkpoint["attempt_id"]
    assert by_node["a"]["latest_attempt"]["status"] == "paused"

    duplicate = successor.start_attempt("game", "a", 1, graph.name, "checkpoint-handoff")
    assert duplicate["attempt_id"] == checkpoint["attempt_id"]
    assert duplicate["run_id"] == checkpoint["run_id"]
    assert duplicate["status"] == "paused"
    assert len(live.sf.list_runs(project_id=checkpoint["execution_project_id"])) == 1
    with pytest.raises(StateConflict):
        successor.verify_node("game", "a", 1, checkpoint["attempt_id"])
    assert live.sf.get_run(checkpoint["run_id"])["status"] == "paused"
