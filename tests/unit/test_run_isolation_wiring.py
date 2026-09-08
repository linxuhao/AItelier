"""The host's own routes to a repository, and the run they belong to.

The framework asks its resolver; the host has FOUR more answers of its own —
`_existing_repo_code_path` (the resolver itself), `WorkspaceManager.get_code_path`
(dashboard, butler, prompt), `PipelineEngine._get_code_path` (which overrides the
engine on the agent-tool path by passing project_root explicitly) and
`git_push_post`, which re-resolves for itself because `$PROJECT_ROOT` used to be
wrong for existing-repo projects. All four have to agree about a run or the
isolation is decorative.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from core import datadir, run_isolation as ri
from core.db_manager import DBManager
from skillflow.exceptions import IsolationUnavailable

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


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


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A DB, a source repo, an isolated run — and the host singletons pointed
    at them."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    db = DBManager(str(tmp_path / "aitelier.db"))
    src = tmp_path / "src"
    base = _init_repo(src)
    db.ensure_project("p1", name="p1", repo_type="existing", repo_path=str(src))
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    rec = ri.ensure_for_run(db, run_id="run-1", project_id="p1",
                            config_name="dpe_default_v2", repo_mode="code")
    return {"db": db, "src": src, "base": base, "rec": rec, "tmp": tmp_path}


def test_the_resolver_answers_per_run(wired):
    from api.dependencies import _existing_repo_code_path
    assert _existing_repo_code_path("p1", "run-1") == wired["rec"]["worktree_path"]
    # No run asked about → the project-keyed answer, unchanged.
    assert _existing_repo_code_path("p1") == str(wired["src"])


def test_the_resolver_fails_closed_and_never_answers_the_source(wired):
    import shutil
    from api.dependencies import _existing_repo_code_path
    shutil.rmtree(wired["rec"]["worktree_path"])
    with pytest.raises(IsolationUnavailable):
        _existing_repo_code_path("p1", "run-1")


def test_workspace_manager_agrees_with_the_resolver(wired, tmp_path):
    from core.workspace_manager import WorkspaceManager
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    assert str(ws.get_code_path("p1", run_id="run-1")) == \
        wired["rec"]["worktree_path"]
    assert str(ws.get_code_path("p1")) == str(wired["src"])


def test_the_pipeline_engine_asks_about_its_own_run(wired, tmp_path):
    """`_code_path` is what the engine passes to execute_tool as project_root,
    where it OUTRANKS whatever the framework would have resolved."""
    from core.dpe_pipeline import PipelineEngine
    from core.workspace_manager import WorkspaceManager
    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    eng = PipelineEngine()
    eng._run_id = "run-1"
    assert str(eng._get_code_path(ws, "p1")) == wired["rec"]["worktree_path"]


def test_git_push_post_pushes_the_run_tree_not_the_source(wired):
    from aitelier.tools.git_push_post.impl import _code_path
    assert str(_code_path("p1", str(wired["src"]), run_id="run-1")) == \
        wired["rec"]["worktree_path"]


def test_the_dpe_config_states_its_sync_policy(wired):
    """Routing git_sync_pre at the real repository must not silently acquire a
    fetch/pull of it. The policy is written down, in the config, by name."""
    cfg = yaml.safe_load((_REPO_ROOT / "configs" / "dpe_default.yaml")
                         .read_text(encoding="utf-8"))
    node = next(s for s in cfg["steps"] if s["id"] == "git_sync_pre")
    assert "policy" in node["tool_params"], (
        "git_sync_pre now defaults to no network; a config that wants a sync "
        "has to say so, and one that does not should say that too")
    assert node["tool_params"]["policy"] in ("skip", "pull")


def test_a_run_that_cannot_be_isolated_is_not_started(wired, monkeypatch):
    """Fail closed at the scheduler boundary: no run, no tick, a named reason —
    never a run advancing against the shared checkout."""
    import core.scheduler as sched

    logged = {}
    monkeypatch.setattr(sched, "tick_log",
                        lambda pid, outcome, **d: logged.update(
                            {"pid": pid, "outcome": outcome, **d}))
    monkeypatch.setattr(sched, "db", wired["db"])
    # The two gates that stand between a picked project and a created run, and
    # that this test is not about: a missing cross-config input and an
    # unpublished seed. Both already have their own tests and both return
    # BEFORE the run exists, so leaving them in place would have this test pass
    # on the wrong refusal.
    monkeypatch.setattr("core.run_launcher.missing_cross_config_inputs",
                        lambda *a, **k: [])
    monkeypatch.setattr("core.seed_publication.seed_is_published",
                        lambda *a, **k: (True, ""))
    monkeypatch.setattr(sched.run_isolation, "ensure_for_run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            IsolationUnavailable("worktree add failed: disk full")))
    wired["db"].update_project("p1", config_name="dpe_default_v2")
    assert sched._get_or_create_skillflow_run("p1") is None
    assert logged.get("outcome") == "isolation_failed"
    assert "disk full" in str(logged.get("reason", ""))


def test_direct_launcher_isolates_before_starting_the_run(wired, monkeypatch):
    """A direct State launch cannot expose a running record-less run."""
    import core.run_launcher as launcher
    import core.scheduler as scheduler
    import api.dependencies as deps
    from types import SimpleNamespace

    source = wired["tmp"] / "direct-src"
    _init_repo(source)
    wired["db"].ensure_project("direct", name="direct", repo_type="existing",
                               repo_path=str(source), config_name="coding_impl")
    manifest = SimpleNamespace(config_name="coding_impl", seed_file="plan.md",
                               scheduler_owned=True, repo_mode="code")
    registry = SimpleNamespace(get=lambda name: manifest)
    monkeypatch.setattr(deps, "get_config_registry", lambda: registry)
    monkeypatch.setattr(launcher, "missing_cross_config_inputs", lambda *a: [])
    monkeypatch.setattr(scheduler, "wake_scheduler", lambda *a, **k: None)

    sf = deps.get_skillflow()
    observed = []
    real_ensure = ri.ensure_for_run

    def ensure(*args, **kwargs):
        observed.append(sf.get_run(kwargs["run_id"])["status"])
        return real_ensure(*args, **kwargs)

    monkeypatch.setattr(ri, "ensure_for_run", ensure)

    result = launcher.start_config_run(
        wired["db"], deps.get_workspace_manager(), "coding_impl", "direct",
        seed_text="approved State scope", repo_type="existing",
        repo_path=str(source))

    assert result["status"] == "started"
    assert observed == ["pending"]
    assert sf.get_run(result["run_id"])["status"] == "running"
    assert ri.record(wired["db"], result["run_id"]) is not None
