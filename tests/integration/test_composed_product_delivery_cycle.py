"""One product-path delivery chain over disposable private fixtures.

This deliberately composes the state adjudicator, run-owned worktrees, native
search/patch, a real behavior check, resource quiescence, scope authority, and
an authorized successor base.  No production State or user repository is used.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from core import run_isolation, run_resources
from core.db_manager import DBManager
from core.state_attempts import StateAttempts
from core.state_database import StateDatabase
from core.state_external import ExternalAttempts
from core.state_graph import StateGraphStore
from core.write_scope import WriteScope, out_of_scope


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True,
                            capture_output=True, check=True)
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=fixture", "-c",
         "user.email=fixture@example.invalid", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _report_sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _adjudicate(state: StateDatabase, node: str, artifact: str, suffix: str) -> dict:
    store = StateGraphStore(state)
    attempts = StateAttempts(store)
    external = ExternalAttempts(attempts, "independent-director")
    attempt = external.register("delivery", node, 1, "fixture-verifier",
                                f"worker-{node}", f"request-{node}")
    candidate_report = _report_sha(f"candidate-{suffix}")
    attempt = external.observe(
        attempt["attempt_id"], f"candidate-{suffix}", 0,
        attempt["context_hash"], "candidate", f"reports/{suffix}.json",
        candidate_report, quiescent=True, artifact=artifact,
        artifact_kind="sha256", detail="candidate from isolated worktree")
    for criterion in ("test", "review"):
        attempts.record_evidence(
            attempt["attempt_id"], f"{suffix}-{criterion}", criterion, "pass",
            artifact, f"reports/{suffix}-{criterion}.json",
            _report_sha(f"independent-{suffix}-{criterion}"),
            "independent-verifier", "fresh evidence, separate from candidate")
    return attempts.verify("delivery", node, 1, attempt["attempt_id"],
                           "authorized-director")


def test_composed_product_delivery_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "aitelier-home"))
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "0")

    source = tmp_path / "product"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.name", "fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    (source / "app.py").write_text('VALUE = "old"\n')
    (source / "check.py").write_text(
        "from app import VALUE\nassert VALUE == 'new', f'old behavior: {VALUE}'\n"
    )
    base = _commit(source, "baseline")

    db = DBManager(str(tmp_path / "host.sqlite"))
    for project in ("delivery-a", "delivery-b"):
        db.ensure_project(project, name=project, repo_type="existing",
                          repo_path=str(source))
    a = run_isolation.ensure_for_run(
        db, run_id="run-a", project_id="delivery-a", config_name="code_cycle",
        repo_mode="code")
    a_tree = Path(a["worktree_path"])
    assert a["base_sha"] == base

    # Search, strict patch, and the behavior test all operate on A's selected
    # run worktree, never on the source checkout or a sibling workspace.
    from skillflow.read_tools import unified_search
    hits = unified_search(
        {"working_tree": [("code", str(a_tree))], "named": {}, "allowed": []},
        "VALUE", max_results=10)
    assert hits["matches"]
    from skillflow.tools.apply_patch.impl import apply_patch
    patched = apply_patch(
        "*** Begin Patch\n*** Update File: app.py\n@@\n-VALUE = \"old\"\n+VALUE = \"new\"\n*** End Patch\n",
        project_root=str(a_tree), output_dir=str(a_tree), output_target="code")
    assert patched.get("applied") is True, patched
    tested = subprocess.run([sys.executable, "check.py"], cwd=a_tree,
                            text=True, capture_output=True,
                            env={**__import__("os").environ,
                                 "PYTHONDONTWRITEBYTECODE": "1"})
    assert tested.returncode == 0, tested.stderr

    # Independent state adjudication authorizes A's candidate before integration.
    state = StateDatabase(str(tmp_path / "state.sqlite"))
    store = StateGraphStore(state)
    store.create_project("delivery", "fixture delivery")
    store.add_nodes("delivery", [
        {"key": "a", "goal": "deliver A", "acceptance": [
            {"id": "test", "kind": "test", "description": "behavior test"},
            {"id": "review", "kind": "review", "description": "independent review"},
        ]},
        {"key": "b", "goal": "deliver B", "dependencies": ["a"], "acceptance": [
            {"id": "test", "kind": "test", "description": "behavior test"},
            {"id": "review", "kind": "review", "description": "independent review"},
        ]},
    ])
    artifact_a = hashlib.sha256((a_tree / "app.py").read_bytes()).hexdigest()
    receipt_a = _adjudicate(state, "a", artifact_a, "a")
    assert receipt_a["reviewer"] == "authorized-director"
    integrated_a = _commit(a_tree, "delivery A candidate")
    _git(source, "merge", "--ff-only", integrated_a)
    assert _git(source, "rev-parse", "HEAD") == integrated_a

    # B is explicitly authorized from A's integrated SHA, then receives its own
    # linked worktree.  No ambient source HEAD or A worktree is consulted.
    run_isolation.request_base(db, "delivery-b", integrated_a,
                               note="authorized integration of A")
    b = run_isolation.ensure_for_run(
        db, run_id="run-b", project_id="delivery-b", config_name="code_cycle",
        repo_mode="code")
    b_tree = Path(b["worktree_path"])
    assert b["base_sha"] == integrated_a
    assert _git(b_tree, "rev-parse", "HEAD") == integrated_a
    assert (b_tree / "app.py").read_text() == 'VALUE = "new"\n'

    # The declared task-card scope rejects cross-task paths while preserving the
    # product's own allowed paths.
    scope = WriteScope.from_card({"owns": ["src/current/"]}, "current")
    assert scope is not None and scope.authorizes("src/current/app.py")
    assert out_of_scope(["src/other/app.py", "../escape"], scope) == [
        "src/other/app.py", "../escape"]

    (b_tree / "app.py").write_text('VALUE = "newer"\n')
    assert (a_tree / "app.py").read_text() == 'VALUE = "new"\n'
    assert (source / "app.py").read_text() == 'VALUE = "new"\n'
    assert _git(source, "status", "--porcelain") == ""
    assert _git(a_tree, "status", "--porcelain") == ""
    assert _git(b_tree, "status", "--porcelain")

    # The resource lifecycle implementation requires a complete terminal
    # observation and zero live/lost/unknown owners before release is eligible.
    class Engine:
        def get_run(self, run_id):
            return {"id": run_id, "status": "completed"}

        def audit_operation_owners(self, run_id):
            return {"alive": 0, "lost": [], "unknown": []}

    row, reason = run_resources.terminal_quiet(Engine(), "run-a")
    assert row and reason == "terminal and quiet"
    class BusyEngine(Engine):
        def audit_operation_owners(self, run_id):
            return {"alive": 1, "lost": [], "unknown": []}
    busy, busy_reason = run_resources.terminal_quiet(BusyEngine(), "run-a")
    assert busy is None and "not retired" in busy_reason

    receipt_b = _adjudicate(state, "b", hashlib.sha256((b_tree / "app.py").read_bytes()).hexdigest(), "b")
    assert receipt_b["reviewer"] == "authorized-director"
    assert _git(source, "rev-parse", "HEAD") == integrated_a

    # Exercise the real native budget trace/resume path on the same disposable
    # product chain.  The partial draft survives exhaustion and a resumed
    # execution consumes the retained trace without resetting the worktree.
    from unittest.mock import MagicMock, patch
    from core.dpe_pipeline import NativeTurnBudgetExhausted, PipelineEngine
    from tests.integration.test_native_parity import _WS, _setup, _run, _tc, _turn
    budget_root = tmp_path / "budget"
    _setup(budget_root)
    budget_ws = _WS(budget_root)
    with patch("core.agents.AgentFactory.__init__", return_value=None):
        engine = PipelineEngine()
    engine.factory = MagicMock()
    engine.factory.is_native.return_value = True
    engine.factory.get_fallback_to_json.return_value = False
    engine.factory.get_max_retries.return_value = 1
    engine.factory.get_max_tool_turns.return_value = 2
    native = engine.factory.get_native_agent.return_value
    native.gateway.litellm_model = "fixture"
    native.gateway.last_usage = {}
    events = []
    engine._trace = lambda category, event, payload: events.append((category, event, payload))
    draft = budget_root / "default" / "t_impl.tmp" / "partial.py"
    def write_partial(action):
        draft.write_text("partial = True\n")
        return {"written": "partial.py"}
    engine._exec_tool = MagicMock(side_effect=write_partial)
    native.turn.side_effect = [
        _turn(tool_calls=[_tc("write", {"file": "partial.py", "content": "partial = True"})]),
        _turn(tool_calls=[_tc("read_file")]),
    ]
    try:
        _run(engine, budget_ws)
    except NativeTurnBudgetExhausted:
        pass
    else:
        raise AssertionError("budget exhaustion was not observed")
    rebuilt = PipelineEngine._rebuild_from_deltas(
        [(event, payload) for _category, event, payload in events], 2)
    assert rebuilt["turns"] == 2 and rebuilt["written_files"] == ["partial.py"]
    engine._resume_from_trace = MagicMock(return_value=rebuilt)
    budget_ws._draft_dir = lambda *_args: draft.parent
    native.turn.reset_mock()
    try:
        _run(engine, budget_ws)
    except NativeTurnBudgetExhausted:
        pass
    assert native.turn.call_count == 0
    assert draft.read_text() == "partial = True\n"
    assert any(event == "resumed_from_trace" for _category, event, payload in events)
