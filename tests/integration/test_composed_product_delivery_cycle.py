"""One delivery cycle through real host and SkillFlow APIs."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import skillflow as skillflow_package
from skillflow.core import StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader

from aitelier.runner import AgentStepRunner
from core import run_isolation, run_resources
from core.db_manager import DBManager
from core.dpe_pipeline import NativeTurnBudgetExhausted, PipelineEngine
from core.skillflow_host import AItelierSkillFlow
from core.state_attempts import StateAttempts
from core.state_database import StateDatabase
from core.state_external import ExternalAttempts
from core.state_graph import StateGraphStore
from core.write_scope import scope_from_context
from core.workspace_manager import WorkspaceManager

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args, check=True):
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def seed_repo(repo):
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "app.py").write_text('VALUE = "old"\n')
    (repo / "known.gd").write_text("extends Node\n# KNOWN_DIAGNOSTIC\n")
    (repo / "verify.py").write_text(
        "import sys\nfrom app import VALUE\n"
        "assert VALUE == sys.argv[1], f'{VALUE} != {sys.argv[1]}'\n")
    git(repo, "add", "--", "app.py", "known.gd", "verify.py")
    git(repo, "commit", "-qm", "baseline")
    return git(repo, "rev-parse", "HEAD")


def behavior(repo, expected):
    return subprocess.run(
        [sys.executable, "-B", "verify.py", expected], cwd=repo,
        text=True, capture_output=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def independent_report(directory, label, repo, test_log):
    """A separate verifier process reads the commit and real test receipt."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{label}.json"
    script = """
import hashlib,json,subprocess,sys
from pathlib import Path
repo,out,log=map(Path,sys.argv[1:4])
candidate=subprocess.run(['git','rev-parse','HEAD'],cwd=repo,check=True,
 text=True,capture_output=True).stdout.strip()
payload={'verdict':'pass','candidate':candidate,'test_report':str(log),
 'test_report_sha256':hashlib.sha256(log.read_bytes()).hexdigest(),
 'reviewer':'independent-subprocess'}
out.write_text(json.dumps(payload,sort_keys=True,indent=2)+'\\n')
"""
    subprocess.run([sys.executable, "-c", script, str(repo), str(target),
                    str(test_log)], check=True)
    return target, sha(target)


def adjudicate(state, node, candidate, report, report_sha):
    attempts = StateAttempts(StateGraphStore(state))
    external = ExternalAttempts(attempts, "fixture-controller")
    attempt = external.register("delivery", node, 1, "product-cycle",
                                f"worker-{node}", f"request-{node}")
    worker_report = report.with_name(f"{node}-candidate.json")
    # The terminal envelope a real external harness writes: an explicit
    # terminal status, the settled/usable declaration State requires before it
    # retains the bytes, and the artifact the observation is about.
    worker_report.write_text(json.dumps({
        "status": "candidate", "settled": True, "usable": True,
        "artifact": candidate, "producer": f"worker-{node}"}) + "\n")
    observed = external.observe(
        attempt["attempt_id"], f"candidate-{node}", 0, attempt["context_hash"],
        "candidate", str(worker_report), sha(worker_report), quiescent=True,
        artifact=candidate, artifact_kind="git-sha1")
    for criterion in ("test", "review"):
        attempts.record_evidence(
            attempt["attempt_id"], f"{criterion}-{node}", criterion, "pass",
            candidate, str(report), report_sha, "independent-subprocess",
            "separate process checked real commit and test receipt")
    assert observed["artifact_ref"] == candidate
    return attempts.verify("delivery", node, 1, attempt["attempt_id"],
                           "authorized-director")


class GodotHandler(BaseHTTPRequestHandler):
    requests = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(body["files"])
        results = []
        for name in body["files"]:
            text = Path(name).read_text()
            marker = ("new diagnostic" if "NEW_DIAGNOSTIC" in text else
                      "known diagnostic" if "KNOWN_DIAGNOSTIC" in text else "")
            results.append({"file": name, "passed": not marker,
                            "error_message": f"SCRIPT ERROR: {marker}" if marker else ""})
        data = json.dumps({"all_passed": all(x["passed"] for x in results),
                           "results": results}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


class ModelHandler(BaseHTTPRequestHandler):
    actions = []
    requests = []
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            self.requests.append(body)
            action = self.actions.pop(0)
            call_id = len(self.requests)
        tool = {"id": f"call-{call_id}", "type": "function",
                "function": {"name": action[0],
                             "arguments": json.dumps(action[1])}}
        data = json.dumps({
            "id": f"response-{call_id}", "object": "chat.completion",
            "created": 1, "model": "fixture-model",
            "choices": [{"index": 0, "message": {
                "role": "assistant", "content": None, "tool_calls": [tool]},
                "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                      "total_tokens": 11}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args):
        return


def install_zg(directory):
    """Executable boundary fixture; IndexControl invokes it as a subprocess."""
    directory.mkdir()
    log = directory / "zg.log"
    exe = directory / "zg"
    exe.write_text(
        f"#!{sys.executable}\nimport json,shutil,sys\nfrom pathlib import Path\n"
        f"log=Path({str(log)!r})\nargs=sys.argv[1:]; root=Path(args[1])\n"
        "log.open('a').write(json.dumps(args)+'\\n')\n"
        "if args[0]=='index' and '--drop' not in args:\n"
        " d=root/'.zvec-grep'; (d/'files.zvec').mkdir(parents=True,exist_ok=True);"
        " (d/'index.zvec').mkdir(exist_ok=True); (d/'manifest.json').write_text('{}')\n"
        "elif args[0]=='index':\n"
        " d=root/'.zvec-grep'; shutil.rmtree(d/'files.zvec',ignore_errors=True);"
        " shutil.rmtree(d/'index.zvec',ignore_errors=True);"
        " (d/'manifest.json').unlink(missing_ok=True)\n")
    exe.chmod(0o755)
    return log


def graph(name, handoff=False):
    steps = [StepNode(
        id="implement", output_mode="write", output_target="code",
        config={"extra_tools": ["apply_patch"]},
        context=[{"from": "repository", "mode": "tool"}],
        transitions=[Transition(to="knowledge" if handoff else None)])]
    if handoff:
        steps += [
            StepNode(id="knowledge", step_type="tool", tool_name="knowledge_sync",
                     tool_params={"out_dir": "$STEP_DIR"},
                     transitions=[Transition(to="next_code")]),
            StepNode(id="next_code", output_mode="write", output_target="code",
                     config={"extra_tools": ["apply_patch"]},
                     context=[{"from": "repository", "mode": "tool"}],
                     transitions=[Transition(to=None)])]
    return PipelineGraph(name=name, begin="implement", steps=steps)


def budget_graph(max_turns):
    return PipelineGraph(name="budget_cycle", begin="implement", steps=[
        StepNode(id="implement", output_mode="write", output_target="code",
                 agent_config="budget_agent", max_tool_turns=max_turns,
                 config={"extra_tools": ["apply_patch"]},
                 context=[{"from": "repository", "mode": "tool"}],
                 transitions=[Transition(to=None)])])


def runtime(tmp_path, db):
    loader = ToolLoader(Path(skillflow_package.__file__).parent / "tools")
    loader.add_tools_dir(ROOT / "aitelier/tools")
    native = loader.is_native
    loader.is_native = lambda name: name == "knowledge_sync" or native(name)
    return AItelierSkillFlow(
        str(tmp_path / "skillflow.sqlite"), tool_loader=loader,
        workspace_base=str(tmp_path / "workspaces"),
        projects_base=str(tmp_path / "projects"),
        code_path_resolver=lambda _pid, run_id=None:
        run_isolation.resolve_for_resolver(db, run_id) if run_id else None)


def host(sf, claim, root, context, artifact):
    engine = PipelineEngine(registry=sf.agent_registry)
    engine._write_scope = scope_from_context(context)
    engine._scope_violations = []
    engine._scope_claim_history = []
    engine._output_fixed = {}
    engine._output_target = "code"
    engine._tool_schemas = claim.inputs["_tool_schemas"]
    engine._artifact_dir = str(artifact)
    engine._code_path = str(root)
    engine._run_id = claim.token.run_id
    engine._current_step = claim.step_id
    engine._write_scope_step_id = claim.step_id
    engine._step_instance_id = claim.token.step_instance_id
    engine._claim_epoch = claim.token.claim_epoch
    return engine


def patch(old, new, gd=False):
    extra = ("*** Update File: known.gd\n@@\n-# KNOWN_DIAGNOSTIC\n"
             "+# KNOWN_DIAGNOSTIC\n+# touched safely\n" if gd else "")
    return ("*** Begin Patch\n*** Update File: app.py\n@@\n"
            f'-VALUE = "{old}"\n+VALUE = "{new}"\n{extra}*** End Patch\n')


@pytest.mark.asyncio
async def test_real_product_delivery_cycle(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("AITELIER_HOME", str(home))
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "1")
    monkeypatch.setenv("AITELIER_ZVEC_PREPARE_WAIT_SECONDS", "0")
    zg_log = install_zg(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])

    source = tmp_path / "source"
    original = seed_repo(source)
    db = DBManager(str(tmp_path / "host.sqlite"))
    for project in ("delivery-a", "delivery-b", "budget", "paused", "dirty", "unknown"):
        db.ensure_project(project, name=project, repo_type="existing",
                          repo_path=str(source))
    sf = runtime(tmp_path, db)
    sf.register_graph(graph("cycle_a", handoff=True))
    sf.register_graph(graph("cycle_b"))
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    monkeypatch.setattr("api.dependencies.get_db_manager", lambda: db)

    run_a = sf.create_run("cycle_a", project_id="delivery-a")
    rec_a = run_isolation.ensure_for_run(
        db, run_id=run_a, project_id="delivery-a", config_name="cycle_a")
    a = Path(rec_a["worktree_path"])
    sf.start_run(run_a)
    ctl = run_resources.control()
    ready = ctl.request(rec_a, "ready")
    assert ready["run_id"] == run_a and ready["root"] == str(a)
    assert ctl.process_once() == [{"run_id": run_a, "outcome": "ready"}]
    assert ctl.settled(ctl.get(run_a), "ready")
    (a / ".zvec-grep/index.zvec/INTERNAL_ONLY").write_text(
        "DERIVED_INDEX_NEEDLE")

    sf.advance_run(run_a)
    claim_a = sf.claim_next_step(run_a)
    assert claim_a and claim_a.step_id == "implement"
    context_a = {"[current_task]": "a",
                 "cards/tasks/a.json": {"owns": ["app.py", "known.gd"]}}
    host_a = host(sf, claim_a, a, context_a, tmp_path / "scope-a")
    with run_resources.search_lease(str(a)):
        searched = host_a._exec_tool({"tool": "search", "params": {
            "pattern": "VALUE|DERIVED_INDEX_NEEDLE", "max_results": 20}})
    searched_files = {m["file"] for m in searched["matches"]}
    assert "app.py" in searched_files
    assert not any(".zvec-grep" in name for name in searched_files)
    first_patch = host_a._exec_tool({"tool": "apply_patch", "params": {
        "patch": patch("old", "middle", gd=True)}})
    assert first_patch.get("applied"), first_patch

    # Actual gdscript_check transport: identical HEAD failure is accepted, a
    # new diagnostic is rejected. New file creation/deletion still goes through
    # the strict host write path and declared task authority.
    GodotHandler.requests = []
    godot = ThreadingHTTPServer(("127.0.0.1", 0), GodotHandler)
    thread = threading.Thread(target=godot.serve_forever, daemon=True)
    thread.start()
    try:
        import aitelier.tools.gdscript_check.impl as gd
        monkeypatch.setattr(gd, "_BUILDER_URL",
                            f"http://127.0.0.1:{godot.server_port}")
        known = gd.gdscript_check(files=["known.gd"], workspace_root=str(a))
        assert known["all_passed"] and known["results"][0]["preexisting"]
        add_bad = ("*** Begin Patch\n*** Add File: new_bad.gd\n"
                   "+extends Node\n+# NEW_DIAGNOSTIC\n*** End Patch\n")
        refusal = host_a._exec_tool(
            {"tool": "apply_patch", "params": {"patch": add_bad}})
        assert refusal["scope_violation"] and not (a / "new_bad.gd").exists()
        context_a["cards/tasks/a.json"]["owns"].append("new_bad.gd")
        host_a._write_scope = scope_from_context(context_a)
        assert host_a._exec_tool(
            {"tool": "apply_patch", "params": {"patch": add_bad}})["applied"]
        introduced = gd.gdscript_check(
            files=["new_bad.gd"], workspace_root=str(a))
        assert not introduced["all_passed"]
        assert "new diagnostic" in introduced["results"][0]["error_message"]
        assert host_a._exec_tool({"tool": "apply_patch", "params": {"patch":
            "*** Begin Patch\n*** Delete File: new_bad.gd\n*** End Patch\n"
        }})["applied"]
        assert len(GodotHandler.requests) >= 3
    finally:
        godot.shutdown()
        thread.join(timeout=5)
        godot.server_close()

    test_a = behavior(a, "middle")
    assert test_a.returncode == 0, test_a.stderr
    reports = tmp_path / "reports"
    reports.mkdir()
    test_a_log = reports / "a-test.json"
    test_a_log.write_text(json.dumps({
        "returncode": test_a.returncode, "stdout": test_a.stdout,
        "tree": git(a, "write-tree")}) + "\n")
    sf.confirm_step(claim_a.token, StepResult())
    candidate_a = git(a, "rev-parse", "HEAD")
    assert candidate_a != original and git(a, "status", "--porcelain") == ""

    state = StateDatabase(str(tmp_path / "state.sqlite"))
    store = StateGraphStore(state)
    store.create_project("delivery", "fixture delivery")
    store.add_nodes("delivery", [
        {"key": "a", "goal": "deliver A", "acceptance": [
            {"id": "test", "kind": "test", "description": "real behavior"},
            {"id": "review", "kind": "review", "description": "independent"}]},
        {"key": "b", "goal": "deliver B", "dependencies": ["a"],
         "acceptance": [
            {"id": "test", "kind": "test", "description": "real behavior"},
            {"id": "review", "kind": "review", "description": "independent"}]},
    ])
    report_a, report_a_sha = independent_report(
        reports, "a-independent", a, test_a_log)
    receipt_a = adjudicate(state, "a", candidate_a, report_a, report_a_sha)
    assert receipt_a["artifact_ref"] == candidate_a
    git(source, "merge", "--ff-only", candidate_a)
    assert git(source, "rev-parse", "HEAD") == candidate_a

    # Real knowledge tool publication then makes the next code step claimable.
    graph_dir = tmp_path / "workspaces/delivery-a/cycle_a"
    (graph_dir / "2").mkdir(parents=True)
    (graph_dir / "2/step2_design.md").write_text(
        "## Overview\nValue transition contract\n")
    (graph_dir / "5/final").mkdir(parents=True)
    (graph_dir / "5/final/verify_report.json").write_text(json.dumps({
        "all_goals_met": True, "ready_for_deploy": True,
        "verified_subtasks": ["middle value"], "issues": []}))
    for _ in range(3):
        sf.advance_run(run_a)
        if sf.get_run(run_a).get("current_node") == "next_code":
            break
    next_claim = sf.claim_next_step(run_a)
    assert next_claim and next_claim.step_id == "next_code"
    knowledge = a / ".aitelier/knowledge.md"
    assert knowledge.is_file() and "middle value" in knowledge.read_text()
    assert git(a, "status", "--porcelain") == ""
    sf.confirm_step(next_claim.token, StepResult())
    sf.advance_run(run_a)
    assert sf.get_run(run_a)["status"] == "completed"

    # B is pinned to the exact authorized A integration and goes through the
    # same scoped write, real test, commit, independent evidence and merge path.
    run_isolation.request_base(db, "delivery-b", candidate_a,
                               note="authorized A integration")
    run_b = sf.create_run("cycle_b", project_id="delivery-b")
    rec_b = run_isolation.ensure_for_run(
        db, run_id=run_b, project_id="delivery-b", config_name="cycle_b")
    b = Path(rec_b["worktree_path"])
    assert rec_b["base_sha"] == candidate_a
    assert behavior(b, "middle").returncode == 0
    sf.start_run(run_b)
    sf.advance_run(run_b)
    claim_b = sf.claim_next_step(run_b)
    context_b = {"[current_task]": "b",
                 "cards/tasks/b.json": {"owns": ["app.py"]}}
    host_b = host(sf, claim_b, b, context_b, tmp_path / "scope-b")
    known_bytes = (b / "known.gd").read_bytes()
    refused = host_b._exec_tool({"tool": "apply_patch", "params": {"patch":
        "*** Begin Patch\n*** Delete File: known.gd\n*** End Patch\n"}})
    assert refused["scope_violation"]
    assert (b / "known.gd").read_bytes() == known_bytes
    assert host_b._exec_tool({"tool": "apply_patch", "params": {
        "patch": patch("middle", "new")}})["applied"]
    test_b = behavior(b, "new")
    assert test_b.returncode == 0, test_b.stderr
    test_b_log = reports / "b-test.json"
    test_b_log.write_text(json.dumps({
        "returncode": test_b.returncode, "base": rec_b["base_sha"],
        "tree": git(b, "write-tree")}) + "\n")
    sf.confirm_step(claim_b.token, StepResult())
    sf.advance_run(run_b)
    candidate_b = git(b, "rev-parse", "HEAD")
    report_b, report_b_sha = independent_report(
        reports, "b-independent", b, test_b_log)
    receipt_b = adjudicate(state, "b", candidate_b, report_b, report_b_sha)
    assert receipt_b["artifact_ref"] == candidate_b
    git(source, "merge", "--ff-only", candidate_b)
    assert git(source, "rev-parse", "HEAD") == candidate_b
    old = tmp_path / "old-base"
    git(source, "worktree", "add", "--detach", str(old), original)
    old_failure = behavior(old, "new")
    assert old_failure.returncode and "old != new" in old_failure.stderr
    git(source, "worktree", "remove", str(old))

    # A real AgentStepRunner reaches the actual AIGateway HTTP boundary. The
    # first claim writes a verified partial change and then exhausts its two
    # turns. An authorized graph repin raises the budget; a fresh runner rebuilds
    # the retained trace, makes another model/tool call, and completes.
    ModelHandler.requests = []
    ModelHandler.actions = [
        ("apply_patch", {"patch":
            "*** Begin Patch\n*** Update File: app.py\n@@\n"
            '-VALUE = "new"\n+VALUE = "partial"\n*** End Patch\n'}),
        ("search", {"pattern": "VALUE", "max_results": 5}),
        ("apply_patch", {"patch":
            "*** Begin Patch\n*** Update File: app.py\n@@\n"
            '-VALUE = "partial"\n+VALUE = "resumed"\n*** End Patch\n'}),
        ("finish_step", {"summary": "resumed delivery complete"})]
    model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    model_thread = threading.Thread(target=model.serve_forever, daemon=True)
    model_thread.start()
    providers = tmp_path / "providers.json"
    providers.write_text(json.dumps({"fixture": {
        "base_url": f"http://127.0.0.1:{model.server_port}",
        "api_key_env": "FIXTURE_MODEL_KEY"}}))
    monkeypatch.setenv("FIXTURE_MODEL_KEY", "fixture-secret")
    monkeypatch.setenv("AITELIER_LLM_STREAM", "0")
    import core.model_routes as model_routes
    real_config = model_routes.config_or_example
    monkeypatch.setattr(model_routes, "config_or_example",
                        lambda name: str(providers) if name == "llm_providers.json"
                        else real_config(name))
    sf.register_agent_config_from_dict("budget_agent", {
        "model": "fixture/model", "tools": ["apply_patch"],
        "system_prompt": "Make the exact requested fixture edit.",
        "native_tool_calling": True, "fallback_to_json_mode": False,
        "max_tool_turns": 2})
    sf.register_graph(budget_graph(2))
    budget_run = sf.create_run("budget_cycle", project_id="budget")
    budget_rec = run_isolation.ensure_for_run(
        db, run_id=budget_run, project_id="budget", config_name="budget_cycle")
    budget_tree = Path(budget_rec["worktree_path"])
    protected_bytes = (b / "app.py").read_bytes()
    source_before_budget = git(source, "rev-parse", "HEAD")
    sf.start_run(budget_run)
    sf.advance_run(budget_run)
    budget_claim = sf.claim_next_step(budget_run)
    workspace = WorkspaceManager(base_path=str(tmp_path / "workspaces"))
    runner = AgentStepRunner(db, workspace)
    try:
        with pytest.raises(NativeTurnBudgetExhausted):
            await runner.execute(budget_claim)
        assert (budget_tree / "app.py").read_text() == 'VALUE = "partial"\n'
        draft_receipts = list((tmp_path / "workspaces").rglob("code_changes.json"))
        assert draft_receipts
        retained_receipt_sha = sha(draft_receipts[0])
        trace = sf.get_trace(
            budget_run, step_instance_id=budget_claim.token.step_instance_id)
        assert any(row["event"] == "turn_budget_exhausted" for row in trace)
        assert any(row["event"] == "prompt_delta" for row in trace)
        sf.fail_step(budget_claim.token, "authorized budget continuation",
                     retryable=True)
        new_version = sf.register_graph(budget_graph(4))
        repinned = sf.repin_run(budget_run, new_version)
        assert sf.get_run(budget_run)["graph_version"] == new_version, repinned
        sf.advance_run(budget_run)
        resumed_claim = sf.claim_next_step(budget_run)
        resumed = await AgentStepRunner(db, workspace).execute(resumed_claim)
        sf.confirm_step(resumed_claim.token, resumed)
        sf.advance_run(budget_run)
        assert sf.get_run(budget_run)["status"] == "completed"
        assert (budget_tree / "app.py").read_text() == 'VALUE = "resumed"\n'
        resumed_trace = sf.get_trace(
            budget_run, step_instance_id=resumed_claim.token.step_instance_id)
        assert any(row["event"] == "resumed_from_trace" for row in resumed_trace)
        assert len(ModelHandler.requests) == 4
        assert retained_receipt_sha
        assert git(source, "rev-parse", "HEAD") == source_before_budget
        assert (b / "app.py").read_bytes() == protected_bytes
    finally:
        model.shutdown()
        model_thread.join(timeout=5)
        model.server_close()

    # A's concrete index identity is released before its integrated, clean
    # worktree is reaped. Reports and refs live outside that checkout.
    assert run_resources.reconcile(db, sf, run_a)["requested"] == [run_a]
    assert ctl.process_once() == [{"run_id": run_a, "outcome": "released"}]
    reaped = run_isolation.reap_released_worktrees(db, sf)
    assert [item["run_id"] for item in reaped["removed"]] == [run_a]
    assert not a.exists()
    assert report_a.is_file() and sha(report_a) == report_a_sha
    assert git(source, "cat-file", "-t", candidate_a) == "commit"

    # Time by itself cannot authorize discard. A paused real run and native
    # resource stay retained after more than 24 hours of synthetic clock age.
    paused_run = sf.create_run("cycle_b", project_id="paused")
    paused_rec = run_isolation.ensure_for_run(
        db, run_id=paused_run, project_id="paused", config_name="cycle_b")
    sf.start_run(paused_run)
    sf.pause_run(paused_run)
    ctl.request(paused_rec, "ready")
    ctl.process_once()
    ledger = ctl.get(paused_run)
    eligible, _ = run_resources.idle_eligible(
        paused_rec, sf.get_run(paused_run), ledger,
        now=ledger["activity_at"] + 90000, minimum=86400)
    assert eligible
    disposable, why = run_isolation.is_disposable(
        db, paused_run, run_status="paused", admitted_ops=0)
    assert not disposable and "not terminal" in why
    assert Path(paused_rec["worktree_path"]).is_dir()

    # Dirty terminal work is released from active demand but cannot be reaped.
    dirty_run = sf.create_run("cycle_b", project_id="dirty")
    dirty_rec = run_isolation.ensure_for_run(
        db, run_id=dirty_run, project_id="dirty", config_name="cycle_b")
    dirty = Path(dirty_rec["worktree_path"])
    sf.start_run(dirty_run)
    sf.advance_run(dirty_run)
    dirty_claim = sf.claim_next_step(dirty_run)
    dirty_host = host(sf, dirty_claim, dirty, context_b, tmp_path / "scope-dirty")
    assert dirty_host._exec_tool({"tool": "apply_patch", "params": {
        "patch": patch("new", "dirty")}})["applied"]
    sf.stop_run(dirty_run, "fixture retains unfinished work")
    ctl.request(dirty_rec, "ready")
    ctl.process_once()
    assert run_resources.reconcile(db, sf, dirty_run)["requested"] == [dirty_run]
    ctl.process_once()
    kept = run_isolation.reap_released_worktrees(db, sf)["retained"]
    assert any(item["run_id"] == dirty_run and "uncommitted" in item["reason"]
               for item in kept)
    assert dirty.is_dir()

    # An isolation record whose engine identity is unknown fails closed.
    unknown_rec = run_isolation.ensure_for_run(
        db, run_id="unknown-run", project_id="unknown", config_name="cycle_b")
    unknown = run_resources.reconcile(db, sf, "unknown-run")
    assert unknown["retained"][0]["run_id"] == "unknown-run"
    assert Path(unknown_rec["worktree_path"]).is_dir()
    assert report_b.is_file() and sha(report_b) == report_b_sha
    assert any("--drop" in line for line in zg_log.read_text().splitlines())
