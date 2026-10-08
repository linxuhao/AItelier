"""Pre-claim source-identity admission: the modern-vs-legacy line, visibly.

Two measured defects live here. First, the host ingress read "no isolation
record" as "legacy, admit" for EVERY record-less run, so a run this deployment
created after isolation began and whose record was gone was silently handed the
project-keyed answer instead of being refused. The resolver already draws that
line in `run_isolation._no_record`; the host must use it rather than a blanket
`has_record` check.

Second, the ingress wrapped the ledger lookup in `except Exception: return`, so
a genuinely failed SQLite query or an unrelated programming error was read as
"legacy, admit". `cannot tell` must stay distinct and visible.

No model/engine/provider is invoked. Independent execution of this file remains
required; authoring or syntax parsing is not a passing CPU receipt.
"""
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import run_isolation as ri
from core.db_manager import DBManager
from core.skillflow_host import AItelierSkillFlow
from skillflow.exceptions import IsolationUnavailable


# ── helpers ──────────────────────────────────────────────────────────

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
def db(tmp_path):
    return DBManager(str(tmp_path / "aitelier.db"))


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    return h


class _Engine:
    """The deployment engine, in the one shape `_no_record` asks of it."""

    def __init__(self, runs=None):
        self._runs = runs or {}

    def get_run(self, run_id):
        return self._runs.get(run_id)


def _host(monkeypatch, db, engine):
    """A real `AItelierSkillFlow` ingress driven against this test's ledger.

    The class is not constructed (that would build a real engine): the ingress
    is the unit under test, and it only touches `_workspace`, the DB, this host's
    own `get_run` (for the run's `created_at`) and the deployment engine
    `get_skillflow`. Those are bound here exactly as production binds them,
    through `api.dependencies`.
    """
    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_db_manager", lambda: db)
    monkeypatch.setattr(deps, "get_skillflow", lambda: engine)
    host = object.__new__(AItelierSkillFlow)
    host._workspace = object()
    # The ingress reads the run's `created_at` off this host's own `get_run`; bind
    # the test engine's run table onto the instance so it is driven by this
    # ledger, exactly as production binds it.
    host.get_run = engine.get_run
    return host


# ── record-less: the modern-vs-genuine-legacy line ────────────────────

def test_a_deployment_created_run_whose_record_is_gone_is_refused(
        db, home, monkeypatch):
    engine = _Engine({"run-modern": {"created_at": "2999-01-01 00:00:00"}})
    host = _host(monkeypatch, db, engine)
    with pytest.raises(IsolationUnavailable, match="no\\s+isolation record"):
        host._admit_source_identity("run-modern")


def test_a_run_this_deployment_never_created_keeps_its_old_answer(
        db, home, monkeypatch):
    host = _host(monkeypatch, db, _Engine({}))
    assert host._admit_source_identity("external-harness-run") is None


def test_a_genuine_pre_isolation_legacy_run_keeps_its_old_answer(
        db, home, monkeypatch):
    engine = _Engine({"run-legacy": {"created_at": "2000-01-01 00:00:00"}})
    host = _host(monkeypatch, db, engine)
    assert host._admit_source_identity("run-legacy") is None


def test_a_record_deleted_after_creation_is_not_read_as_legacy(
        db, home, tmp_path, monkeypatch):
    """The exact defect: a real modern record whose row is then deleted."""
    src = tmp_path / "src"
    _init_repo(src)
    db.ensure_project("p", name="p", repo_type="existing", repo_path=str(src))
    ri.ensure_for_run(db, run_id="run-doomed", project_id="p",
                      config_name="dpe_default_v2", repo_mode="code")
    engine = _Engine({"run-doomed": {"created_at": "2999-01-01 00:00:00"}})
    with db.get_connection() as conn:
        conn.execute("DELETE FROM run_isolation WHERE run_id=?", ("run-doomed",))
        conn.commit()
    host = _host(monkeypatch, db, engine)
    with pytest.raises(IsolationUnavailable):
        host._admit_source_identity("run-doomed")


# ── cannot tell stays visible, and distinct from the typed refusal ───

def test_an_unreadable_ledger_is_not_read_as_admitted(db, home, monkeypatch):
    def boom(_db, _run_id):
        raise RuntimeError("accessor is broken")
    monkeypatch.setattr(ri, "record", boom)
    host = _host(monkeypatch, db, _Engine({"run-x": {"created_at": "2999-01-01 00:00:00"}}))
    with pytest.raises(RuntimeError, match="accessor is broken"):
        host._admit_source_identity("run-x")


def test_an_unrelated_programming_error_is_not_swallowed(db, home, monkeypatch):
    class Weird(TypeError):
        pass

    def boom(_db, _run_id):
        raise Weird("a different kind of failure")
    monkeypatch.setattr(ri, "record", boom)
    host = _host(monkeypatch, db, _Engine({"run-x": {"created_at": "2999-01-01 00:00:00"}}))
    with pytest.raises(Weird):
        host._admit_source_identity("run-x")


def test_an_unavailable_isolation_table_surfaces_before_a_claim(
        db, home, tmp_path, monkeypatch):
    """A real SQLite table that is truly absent — not a stub or a mock."""
    broken = DBManager(str(tmp_path / "broken.db"))
    with broken.get_connection() as conn:
        conn.execute("DROP TABLE run_isolation")
        conn.commit()
    host = _host(monkeypatch, broken, _Engine({"run-x": {"created_at": "2999-01-01 00:00:00"}}))
    with pytest.raises(sqlite3.OperationalError, match="run_isolation"):
        host._admit_source_identity("run-x")


def test_the_host_ingress_refuses_a_dirty_snapshot_before_a_claim(
        db, home, tmp_path, monkeypatch):
    src = tmp_path / "src"
    _init_repo(src)
    db.ensure_project("review-run", name="review-run", repo_type="none",
                      repo_path=str(src))
    rec = ri.ensure_for_run(db, run_id="run-snap", project_id="review-run",
                            config_name="code_review", repo_mode="none")
    (Path(rec["worktree_path"]) / "seed.txt").write_text("preview\n")
    host = _host(monkeypatch, db, _Engine({"run-snap": {"created_at": "2999-01-01 00:00:00"}}))
    with pytest.raises(IsolationUnavailable):
        host._admit_source_identity("run-snap")


# ── the snapshot binding: identity AND bytes ──────────────────────────

@pytest.fixture
def snapshot(db, home, tmp_path):
    src = tmp_path / "src"
    base = _init_repo(src)
    db.ensure_project("review-run", name="review-run", repo_type="none",
                      repo_path=str(src))
    rec = ri.ensure_for_run(db, run_id="run-snap", project_id="review-run",
                            config_name="code_review", repo_mode="none")
    assert rec["mode"] == ri.MODE_READ_SNAPSHOT and rec["base_sha"] == base
    return SimpleNamespace(db=db, src=src, base=base, rec=rec)


def test_a_clean_snapshot_at_the_recorded_candidate_is_served(snapshot):
    assert ri.resolve_for_resolver(
        snapshot.db, "run-snap") == snapshot.rec["worktree_path"]


def test_a_dirty_tracked_preview_is_refused(snapshot):
    path = snapshot.rec["worktree_path"]
    (Path(path) / "seed.txt").write_text("preview\n")
    assert _git(path, "rev-parse", "HEAD") == snapshot.base
    with pytest.raises(IsolationUnavailable, match="tracked"):
        ri.resolve_for_resolver(snapshot.db, "run-snap")


def test_a_staged_index_change_is_refused(snapshot):
    path = Path(snapshot.rec["worktree_path"])
    (path / "seed.txt").write_text("staged\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=path, check=True)
    with pytest.raises(IsolationUnavailable):
        ri.resolve_for_resolver(snapshot.db, "run-snap")


def test_a_tracked_file_mode_change_is_refused(snapshot):
    path = Path(snapshot.rec["worktree_path"])
    (path / "seed.txt").chmod(0o755)
    with pytest.raises(IsolationUnavailable):
        ri.resolve_for_resolver(snapshot.db, "run-snap")


def test_an_untracked_file_is_not_a_dirty_preview(snapshot):
    path = Path(snapshot.rec["worktree_path"])
    (path / "scratch.txt").write_text("untracked\n")
    assert ri.resolve_for_resolver(
        snapshot.db, "run-snap") == snapshot.rec["worktree_path"]


def test_the_check_writes_nothing_to_the_index(snapshot):
    path = Path(snapshot.rec["worktree_path"])
    index = Path(_git(path, "rev-parse", "--git-path", "index"))
    if not index.is_absolute():
        index = path / index
    before = index.read_bytes()
    assert ri.resolve_for_resolver(
        snapshot.db, "run-snap") == snapshot.rec["worktree_path"]
    assert index.read_bytes() == before, "the binding check mutated the index"


def test_a_snapshot_whose_head_is_not_the_recorded_candidate_is_refused(snapshot):
    src = snapshot.src
    (src / "seed.txt").write_text("moved\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "moved"], cwd=src, check=True)
    moved = _git(src, "rev-parse", "HEAD")
    path = snapshot.rec["worktree_path"]
    subprocess.run(["git", "worktree", "remove", str(path)], cwd=src, check=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(path), moved],
                   cwd=src, check=True)
    with pytest.raises(IsolationUnavailable, match="candidate"):
        ri.resolve_for_resolver(snapshot.db, "run-snap")


def test_a_missing_snapshot_tree_is_refused(snapshot):
    shutil.rmtree(snapshot.rec["worktree_path"])
    with pytest.raises(IsolationUnavailable):
        ri.resolve_for_resolver(snapshot.db, "run-snap")


def test_a_plain_directory_at_the_snapshot_path_is_refused(snapshot):
    shutil.rmtree(snapshot.rec["worktree_path"])
    Path(snapshot.rec["worktree_path"]).mkdir()
    with pytest.raises(IsolationUnavailable):
        ri.resolve_for_resolver(snapshot.db, "run-snap")


def test_a_failed_git_inspection_is_refused_not_admitted(snapshot, monkeypatch):
    real = ri._git

    def only_head(cwd, *args, **kwargs):
        if args and args[0] == "diff-index":
            return subprocess.CompletedProcess(
                ["git", *args], 128, "", "fatal: unable to read tree")
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(ri, "_git", only_head)
    with pytest.raises(IsolationUnavailable, match="cannot be verified"):
        ri.resolve_for_resolver(snapshot.db, "run-snap")


def test_an_unknown_isolation_mode_is_refused(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    db.ensure_project("p", name="p", repo_type="existing", repo_path=str(src))
    ri.ensure_for_run(db, run_id="run-weird", project_id="p",
                      config_name="dpe_default_v2", repo_mode="code")
    with db.get_connection() as conn:
        conn.execute("UPDATE run_isolation SET mode='quantum' WHERE run_id=?",
                     ("run-weird",))
        conn.commit()
    with pytest.raises(IsolationUnavailable, match="does not decide"):
        ri.resolve_for_resolver(db, "run-weird")


# ── direct / repo-less / pre-isolation keep their old answers ─────────

def test_a_repoless_record_answers_false_without_a_refusal(db, home):
    db.ensure_project("plain", name="plain", repo_type="none")
    ri.ensure_for_run(db, run_id="run-none", project_id="plain",
                      config_name="pipeline_forge", repo_mode="none")
    assert ri.require_recorded_identity(db, "run-none") is False


def test_a_direct_record_keeps_no_opinion(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    db.ensure_project("d", name="d", repo_type="existing", repo_path=str(src))
    ri.ensure_for_run(db, run_id="run-direct", project_id="d",
                      config_name="coding_impl", repo_mode="code",
                      requested_mode=ri.MODE_DIRECT)
    assert ri.require_recorded_identity(db, "run-direct") is None


def test_a_never_provisioned_run_has_no_opinion(db):
    assert ri.require_recorded_identity(db, "never-provisioned") is None
