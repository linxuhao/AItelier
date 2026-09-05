"""Two runs, one repository, the whole host stack — the acceptance case.

The framework suite proves the routing; this proves the thing the routing was
for, through the pieces that actually ship: the host's own code-path resolver
(`api.dependencies._existing_repo_code_path`), the isolation records the host
writes at launch, real `git worktree`s, the real `repo_apply` on both sides, and
a barrier fired inside run A's `git add -A` — the only window in which the
cross-staging defect exists.

`test_shared_root...` is the control. It is the same two runs with isolation
removed, and it must keep passing: without it the positive test proves only that
two runs writing to two directories do not collide, which was never in doubt.
"""

import subprocess
from pathlib import Path

import pytest
import skillflow as _skillflow_pkg
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager as SFWorkspace

from core import run_isolation as ri
from core.db_manager import DBManager

_REAL_TOOLS = Path(_skillflow_pkg.__file__).parent / "tools"


def _git(repo, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                          text=True).stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=path, check=True)
    return _git(path, "rev-parse", "HEAD")


class _AddBarrier:
    """Run B delivers inside run A's `git add -A`, deterministically."""

    def __init__(self, real_run, callback):
        self._real, self._cb, self.fired = real_run, callback, False

    def __call__(self, args, **kwargs):
        if (not self.fired and isinstance(args, list)
                and args[:3] == ["git", "add", "-A"]):
            self.fired = True
            self._cb()
        return self._real(args, **kwargs)


def _graph():
    return PipelineGraph(
        name="apply", begin="s1",
        steps=[StepNode(
            id="s1", step_type="agent", output_mode="content",
            output_fixed={"out": "out.txt"},
            lifecycle={"on_deliver": {"tool": "repo_apply",
                                      "params": {"source_dir": "$STEP_DIR"}}},
            transitions=[Transition(to=None)])])


@pytest.fixture
def stack(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    db = DBManager(str(tmp_path / "aitelier.db"))
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)

    src = tmp_path / "src"
    base = _init_repo(src)
    for pid in ("pA", "pB"):
        db.ensure_project(pid, name=pid, repo_type="existing",
                          repo_path=str(src))

    sf = SkillFlow(":memory:")
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._workspace = SFWorkspace(
        str(tmp_path / "ws"), projects_base=str(tmp_path / "projects"),
        code_path_resolver=deps._existing_repo_code_path)
    sf.register_graph(_graph())
    return {"db": db, "sf": sf, "src": src, "base": base, "deps": deps}


def _stage(sf, pid, filename, content):
    rid = sf.create_run("apply", {"project_id": pid}, project_id=pid)
    sf.start_run(rid)
    sf.advance_run(rid)
    claimed = sf.claim_next_step(rid)
    (sf._workspace.get_step_tmp_dir(pid, "apply", "s1") / filename
     ).write_text(content)
    return rid, claimed.token


def test_two_isolated_runs_share_a_repository_and_nothing_else(stack, monkeypatch):
    sf, db, src, base = stack["sf"], stack["db"], stack["src"], stack["base"]

    rid_a, tok_a = _stage(sf, "pA", "a.txt", "A\n")
    rec_a = ri.ensure_for_run(db, run_id=rid_a, project_id="pA",
                              config_name="apply", repo_mode="code")
    rid_b, tok_b = _stage(sf, "pB", "b.txt", "B\n")
    rec_b = ri.ensure_for_run(db, run_id=rid_b, project_id="pB",
                              config_name="apply", repo_mode="code")

    assert rec_a["branch"] == f"codex/run/{rid_a}"
    assert rec_b["branch"] == f"codex/run/{rid_b}"
    assert rec_a["base_sha"] == rec_b["base_sha"] == base

    import skillflow.tools.repo_apply.impl as apply_impl
    barrier = _AddBarrier(
        subprocess.run,
        lambda: sf.confirm_step(tok_b, StepResult(outputs={}, flags={})))
    monkeypatch.setattr(apply_impl.subprocess, "run", barrier)
    sf.confirm_step(tok_a, StepResult(outputs={}, flags={}))

    assert barrier.fired, "the two deliveries did not actually interleave"

    for rec, own, other in ((rec_a, "a.txt", "b.txt"), (rec_b, "b.txt", "a.txt")):
        wt = Path(rec["worktree_path"])
        tip = _git(wt, "rev-parse", "HEAD")
        assert tip != base, f"{rec['run_id']} committed nothing"
        assert _git(wt, "show", "--name-only", "--format=", tip).split() == [own]
        assert (wt / own).exists() and not (wt / other).exists()

    assert _git(src, "rev-parse", "HEAD") == base, "the source checkout moved"
    assert _git(src, "status", "--porcelain") == "", "the source checkout is dirty"
    assert not (src / "a.txt").exists() and not (src / "b.txt").exists()


def test_the_same_two_runs_cross_stage_without_isolation(stack, monkeypatch):
    """The control. Isolation removed, everything else identical."""
    sf, src, base = stack["sf"], stack["src"], stack["base"]
    monkeypatch.setattr(sf._workspace, "_code_path_resolver",
                        lambda pid, run_id=None: str(src))
    sf._workspace._resolver_arity = None

    rid_a, tok_a = _stage(sf, "pA", "a.txt", "A\n")
    rid_b, tok_b = _stage(sf, "pB", "b.txt", "B\n")

    import skillflow.tools.repo_apply.impl as apply_impl
    barrier = _AddBarrier(
        subprocess.run,
        lambda: sf.confirm_step(tok_b, StepResult(outputs={}, flags={})))
    monkeypatch.setattr(apply_impl.subprocess, "run", barrier)
    sf.confirm_step(tok_a, StepResult(outputs={}, flags={}))

    first = _git(src, "log", "--format=%H %s", "--reverse").splitlines()[1]
    touched = _git(src, "show", "--name-only", "--format=", first.split()[0]).split()
    assert set(touched) == {"a.txt", "b.txt"}, (
        f"the control did not reproduce cross-staging: {touched}")
    assert "(1 file(s))" in first
