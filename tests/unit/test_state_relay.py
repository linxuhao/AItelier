"""A relayed State attempt inherits a failed attempt's commits AND staged draft.

Live audit 2026-09-10: 20 of 22 failed attempts died on the implement step's
turn/output budget with a draft retained in `implement.tmp/` that the next
attempt could not reach (fresh worktree from HEAD, seed without prior-attempt
context, read tools jailed to the new worktree). The one in-engine relay that
named the old path in its instruction re-grounded from scratch and failed again.
These tests bind the channel that now exists: request_base + parked draft +
seed_relay_draft, driven by `continue_from`.
"""
import subprocess
from pathlib import Path

import pytest
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode

from core import run_isolation as ri
from core.db_manager import DBManager
from core.state_attempts import StateAttempts
from core.state_graph import StateConflict, StateGraphStore
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"], ["git", "add", "-A"], ["git", "commit", "-qm", "seed"]):
        subprocess.run(cmd, cwd=path, check=True)
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "state.db"))
    store = StateGraphStore(db)
    store.create_project("game", "Long-running game")
    store.add_nodes("game", [{"key": "a", "goal": "Implement a", "dependencies": [], "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Behaviour validated"}]}])
    attempts = StateAttempts(store)
    sf = SkillFlow(str(tmp_path / "sf.db"))
    sf.register_graph(PipelineGraph(name="feature", begin="implementation", steps=[StepNode(id="implementation")]))
    ws = WorkspaceManager(str(tmp_path / "workspaces"))
    src = tmp_path / "src"
    head = _init_repo(src)
    yield {"db": db, "store": store, "attempts": attempts, "sf": sf, "ws": ws, "src": src, "head": head,
           "service": StateService(db, ws, sf, {})}
    sf._conn.close()


def _failed_attempt_with_draft(w):
    """A real failed run: one commit on its branch, one file left in staging."""
    attempts, sf, db, ws, src = w["attempts"], w["sf"], w["db"], w["ws"], w["src"]
    a = attempts.reserve("game", "a", 1, "feature", "first")
    pid = a["execution_project_id"]
    rid = sf.create_run("feature", project_id=pid)
    sf.start_run(rid)
    attempts.bind_run(a["attempt_id"], rid, sf)
    db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))
    rec = ri.ensure_for_run(db, run_id=rid, project_id=pid, config_name="feature", repo_mode="code")
    wt = Path(rec["worktree_path"])
    (wt / "done.py").write_text("DONE = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "first half"], cwd=wt, check=True)
    ws.write_draft(pid, "implementation", "half.py", "HALF = 1\n", graph_name="feature")
    sf.fail_run(rid, "Step implementation: native turn budget exhausted (32/32)")
    return attempts.reconcile(a["attempt_id"], sf), _git(wt, "rev-parse", "HEAD")


def test_failed_attempt_reports_what_it_left_behind(world):
    a, branch_head = _failed_attempt_with_draft(world)
    observed = world["service"].reconcile_attempt(a["attempt_id"])
    inv = observed["relay_inventory"]
    assert inv["branch"] == f"codex/run/{a['run_id']}"
    assert inv["head_sha"] == branch_head and inv["base_sha"] == world["head"]
    assert [c["subject"] for c in inv["commits"]] == ["first half"]
    assert inv["staged_files"] == {"implementation": ["half.py"]}
    assert inv["mainline_ahead_by"] == 0
    assert "turn budget" in inv["error"]


def test_relay_bases_the_new_worktree_on_the_failed_branch_and_seeds_its_draft(world):
    attempts, ws, db, sf, src = world["attempts"], world["ws"], world["db"], world["sf"], world["src"]
    a, branch_head = _failed_attempt_with_draft(world)
    b = attempts.reserve("game", "a", 1, "feature", "relay", "finish it", continue_from=a["attempt_id"])
    relay = world["service"]._prepare_relay(b, str(src))
    b = attempts.pin_relay(b["attempt_id"], relay)

    assert relay["base_sha"] == branch_head
    assert relay["staged_files"] == {"implementation": ["half.py"]}
    assert b["context"]["relay"] == relay
    assert ri.requested_base(db, b["execution_project_id"])["base_sha"] == branch_head
    # The failed attempt's own staging is untouched — it stays evidence.
    assert (ws._draft_dir(a["execution_project_id"], "implementation", "feature") / "half.py").exists()

    # The run that the launcher (or the poller) now provisions starts where the
    # failed one stopped, whichever of them provisions first.
    pid = b["execution_project_id"]
    db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(src))
    rec = ri.ensure_for_run(db, run_id="run-relay", project_id=pid, config_name="feature", repo_mode="code")
    assert rec["base_sha"] == branch_head
    assert (Path(rec["worktree_path"]) / "done.py").exists()
    assert _git(src, "rev-parse", "HEAD") == world["head"], "the source checkout never moves"

    # The engine seeds the draft into the fresh staging exactly once.
    assert ws.seed_relay_draft(pid, "implementation", "feature") == ["half.py"]
    assert (ws._draft_dir(pid, "implementation", "feature") / "half.py").read_text() == "HALF = 1\n"
    assert ws.seed_relay_draft(pid, "implementation", "feature") == [], "a loop-back must not re-seed"
    ws.clean_draft_dir(pid, "implementation", "feature")
    assert ws.seed_relay_draft(pid, "implementation", "feature") == []


def test_relay_refuses_when_the_failed_branch_is_gone(world):
    attempts, src = world["attempts"], world["src"]
    a, _ = _failed_attempt_with_draft(world)
    rec = ri.record(world["db"], a["run_id"])
    subprocess.run(["git", "worktree", "remove", "--force", rec["worktree_path"]], cwd=src, check=True)
    subprocess.run(["git", "branch", "-D", rec["branch"]], cwd=src, check=True)
    b = attempts.reserve("game", "a", 1, "feature", "relay", continue_from=a["attempt_id"])
    with pytest.raises(StateConflict, match="no run branch"):
        world["service"]._prepare_relay(b, str(src))
    assert ri.requested_base(world["db"], b["execution_project_id"]) is None


def test_start_attempt_exposes_continue_from_on_the_typed_surface():
    from core.state_commands import StartAttempt, describe
    assert "continue_from" in describe()["operations"]["start_attempt"]["arguments"]["properties"]
    assert StartAttempt(project_id="g", node_key="a", expected_revision=1, workflow="w",
                        request_key="r").continue_from is None
