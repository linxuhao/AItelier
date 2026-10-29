"""Per-run isolation, host side: which tree a RUN owns, and who may touch it.

The framework half (skillflow) can only ask "which root does this run use?" —
the answer is here: the record that says what was decided for a run, the git
worktree that decision created, the lease that makes direct mode exclusive, and
the refusal that happens when any of it cannot be honoured.

Automatic reaping is deliberately conservative: released run-owned worktrees are
removed only when terminal + quiet + clean and locally proven merged into the
source checkout's current branch, or after an explicit discard disposition. A
push or open PR alone never qualifies.
"""

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import datadir, run_isolation as ri
from core.db_manager import DBManager
from core.run_isolation import CheckoutLeased
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
    """Worktrees land under the isolated data root, never a production one."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(h))
    return h



@pytest.fixture(autouse=True)
def _default_zvec_lifecycle(monkeypatch):
    """Pin the deployment's DEFAULT native-index lifecycle for this module.

    `run_resources.enabled()` is opt-in (`AITELIER_ZVEC_LIFECYCLE=1`, default
    "0"). These tests are about the isolation/worktree policy, not the native
    index; when the surrounding host exports the lifecycle flag the reaper's
    unrelated "native index release is not settled" guard would retain every
    tree and make the worktree-policy assertions environment-dependent. The
    default is restored explicitly, and the zvec guard has its own tests.
    """
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "0")


def _project(db, pid, repo_path, repo_type="existing"):
    db.ensure_project(pid, name=pid, repo_type=repo_type,
                      repo_path=str(repo_path) if repo_path else None)


# ── mode decision ────────────────────────────────────────────────────

def test_a_new_code_producing_run_is_isolated_by_default(db, home, tmp_path):
    src = tmp_path / "src"
    base = _init_repo(src)
    _project(db, "p1", src)

    rec = ri.ensure_for_run(db, run_id="run-aaa", project_id="p1",
                            config_name="dpe_default_v2", repo_mode="code")

    assert rec["mode"] == ri.MODE_WORKTREE
    wt = Path(rec["worktree_path"])
    assert wt.is_dir() and (wt / "seed.txt").exists()
    assert wt.parent == datadir.worktrees_dir()
    assert rec["branch"] == "codex/run/run-aaa", (
        "the branch must carry the full run id, not a prefix of it")
    assert rec["base_sha"] == base
    assert _git(wt, "rev-parse", "HEAD") == base
    assert _git(src, "rev-parse", "HEAD") == base
    assert _git(src, "status", "--porcelain") == ""


def test_a_repoless_run_stays_repoless(db, home, tmp_path):
    _project(db, "p2", None, repo_type="none")
    rec = ri.ensure_for_run(db, run_id="run-bbb", project_id="p2",
                            config_name="pipeline_forge", repo_mode="none")
    assert rec["mode"] == ri.MODE_NONE
    assert not rec["worktree_path"]
    assert ri.resolve_for_resolver(db, "run-bbb") is False
    assert not datadir.worktrees_dir().exists() or \
        not any(datadir.worktrees_dir().iterdir())


def test_an_explicit_direct_run_keeps_the_source_checkout(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "p3", src)
    rec = ri.ensure_for_run(db, run_id="run-ccc", project_id="p3",
                            config_name="coding_impl", repo_mode="code",
                            requested_mode=ri.MODE_DIRECT)
    assert rec["mode"] == ri.MODE_DIRECT
    # "No opinion": the project-keyed answer still applies, unchanged.
    assert ri.resolve_for_resolver(db, "run-ccc") is None


# ── resolution is per run, and fails closed ──────────────────────────

def test_two_runs_on_one_repo_resolve_to_different_trees(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "pA", src)
    _project(db, "pB", src)
    a = ri.ensure_for_run(db, run_id="run-A", project_id="pA",
                          config_name="dpe_default_v2", repo_mode="code")
    b = ri.ensure_for_run(db, run_id="run-B", project_id="pB",
                          config_name="dpe_default_v2", repo_mode="code")
    assert a["worktree_path"] != b["worktree_path"]
    assert ri.resolve_for_resolver(db, "run-A") == a["worktree_path"]
    assert ri.resolve_for_resolver(db, "run-B") == b["worktree_path"]


def test_a_deleted_worktree_fails_closed(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "p4", src)
    rec = ri.ensure_for_run(db, run_id="run-ddd", project_id="p4",
                            config_name="dpe_default_v2", repo_mode="code")
    import shutil
    shutil.rmtree(rec["worktree_path"])
    with pytest.raises(IsolationUnavailable) as e:
        ri.resolve_for_resolver(db, "run-ddd")
    assert "run-ddd" in str(e.value)
    assert str(src) not in str(e.value.__dict__.get("fallback", ""))


def test_a_path_that_is_not_our_worktree_fails_closed(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "p5", src)
    rec = ri.ensure_for_run(db, run_id="run-eee", project_id="p5",
                            config_name="dpe_default_v2", repo_mode="code")
    import shutil
    shutil.rmtree(rec["worktree_path"])
    # A plain directory at the recorded path is NOT the run's tree.
    Path(rec["worktree_path"]).mkdir(parents=True)
    with pytest.raises(IsolationUnavailable):
        ri.resolve_for_resolver(db, "run-eee")


def test_the_mode_comes_from_the_record_not_from_a_directory(db, home, tmp_path):
    """A directory appearing at the conventional path must change nothing."""
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "p6", src)
    ri.ensure_for_run(db, run_id="run-fff", project_id="p6",
                      config_name="coding_impl", repo_mode="code",
                      requested_mode=ri.MODE_DIRECT)
    (datadir.worktrees_dir() / "run-fff").mkdir(parents=True)
    assert ri.resolve_for_resolver(db, "run-fff") is None


def test_a_run_created_after_the_marker_with_no_record_fails_closed(db, home):
    since = ri.isolation_since(db)
    assert since, "the migration marker is what makes a missing record legible"
    with pytest.raises(IsolationUnavailable):
        ri.resolve_for_resolver(db, "run-ghost", run_created_at="9999-01-01 00:00:00")


def test_a_run_created_before_the_marker_keeps_the_old_answer(db, home):
    assert ri.resolve_for_resolver(
        db, "run-legacy", run_created_at="2000-01-01 00:00:00") is None


def test_resume_keeps_the_same_worktree(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "p7", src)
    first = ri.ensure_for_run(db, run_id="run-ggg", project_id="p7",
                              config_name="dpe_default_v2", repo_mode="code")
    (Path(first["worktree_path"]) / "work.txt").write_text("in progress\n")
    again = ri.ensure_for_run(db, run_id="run-ggg", project_id="p7",
                              config_name="dpe_default_v2", repo_mode="code")
    assert again["worktree_path"] == first["worktree_path"]
    assert again["base_sha"] == first["base_sha"]
    assert (Path(first["worktree_path"]) / "work.txt").exists(), \
        "resume re-provisioned and dropped work in flight"
    assert len([l for l in _git(src, "worktree", "list").splitlines()]) == 2


# ── on-repo review reads an immutable tree ───────────────────────────

def test_on_repo_review_reads_a_snapshot_that_the_source_cannot_move(
        db, home, tmp_path):
    """A pinned SHA in a record is not a pinned read: the checkout keeps moving.

    So the review gets a real detached worktree at the SHA it was launched
    against, and a commit landing in the source afterwards is invisible to it.
    """
    src = tmp_path / "src"
    base = _init_repo(src)
    _project(db, "target", src)
    _project(db, "review-run", src, repo_type="none")

    rec = ri.ensure_for_run(db, run_id="run-rev", project_id="review-run",
                            config_name="code_review", repo_mode="none")
    assert rec["mode"] == ri.MODE_READ_SNAPSHOT
    root = Path(ri.resolve_for_resolver(db, "run-rev"))
    assert (root / "seed.txt").read_text() == "seed\n"

    (src / "seed.txt").write_text("CHANGED AFTER LAUNCH\n")
    (src / "new.txt").write_text("added after launch\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "after review launch"], cwd=src,
                   check=True)

    assert (root / "seed.txt").read_text() == "seed\n", \
        "the review read a moving target"
    assert not (root / "new.txt").exists()
    assert rec["base_sha"] == base
    assert _git(src, "rev-parse", "HEAD") != base


def test_a_repoless_run_without_a_target_gets_no_snapshot(db, home, tmp_path):
    _project(db, "plain-review", None, repo_type="none")
    rec = ri.ensure_for_run(db, run_id="run-plain", project_id="plain-review",
                            config_name="code_review", repo_mode="none")
    assert rec["mode"] == ri.MODE_NONE
    assert ri.resolve_for_resolver(db, "run-plain") is False


# ── the lease: direct mode is exclusive for the WHOLE run ────────────

def test_a_second_direct_run_on_one_checkout_is_refused(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "d1", src)
    _project(db, "d2", src)
    ri.ensure_for_run(db, run_id="run-d1", project_id="d1",
                      config_name="coding_impl", repo_mode="code",
                      requested_mode=ri.MODE_DIRECT)
    with pytest.raises(CheckoutLeased) as e:
        ri.ensure_for_run(db, run_id="run-d2", project_id="d2",
                          config_name="coding_impl", repo_mode="code",
                          requested_mode=ri.MODE_DIRECT)
    assert "run-d1" in str(e.value), "the refusal must name the holder"


def test_an_isolated_run_does_not_take_the_lease(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "w1", src)
    _project(db, "d3", src)
    ri.ensure_for_run(db, run_id="run-w1", project_id="w1",
                      config_name="dpe_default_v2", repo_mode="code")
    rec = ri.ensure_for_run(db, run_id="run-d3", project_id="d3",
                            config_name="coding_impl", repo_mode="code",
                            requested_mode=ri.MODE_DIRECT)
    assert rec["mode"] == ri.MODE_DIRECT


def test_path_aliases_are_the_same_checkout(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    alias = tmp_path / "alias"
    alias.symlink_to(src)
    _project(db, "a1", src)
    _project(db, "a2", alias)
    ri.ensure_for_run(db, run_id="run-a1", project_id="a1",
                      config_name="coding_impl", repo_mode="code",
                      requested_mode=ri.MODE_DIRECT)
    with pytest.raises(CheckoutLeased):
        ri.ensure_for_run(db, run_id="run-a2", project_id="a2",
                          config_name="coding_impl", repo_mode="code",
                          requested_mode=ri.MODE_DIRECT)
    assert ri.canonical_checkout(alias) == ri.canonical_checkout(src)


def test_a_released_lease_lets_the_next_run_in(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "r1", src)
    _project(db, "r2", src)
    ri.ensure_for_run(db, run_id="run-r1", project_id="r1",
                      config_name="coding_impl", repo_mode="code",
                      requested_mode=ri.MODE_DIRECT)
    ri.release(db, "run-r1", disposition="kept")
    rec = ri.ensure_for_run(db, run_id="run-r2", project_id="r2",
                            config_name="coding_impl", repo_mode="code",
                            requested_mode=ri.MODE_DIRECT)
    assert rec["mode"] == ri.MODE_DIRECT


def test_host_repo_mutations_refuse_while_another_run_holds_the_lease(
        db, home, tmp_path, monkeypatch):
    """The lease is not a repo_apply lock. An operator committing through the
    supported host API during a leased run is the same collision."""
    from core.workspace_manager import WorkspaceManager
    import api.dependencies as deps

    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "op1", src)
    monkeypatch.setattr(deps, "db_instance", db)
    ri.ensure_for_run(db, run_id="run-op", project_id="op1",
                      config_name="coding_impl", repo_mode="code",
                      requested_mode=ri.MODE_DIRECT)

    ws = WorkspaceManager(str(tmp_path / "ws"), projects_base=str(tmp_path / "pb"))
    (src / "operator.txt").write_text("by hand\n")
    with pytest.raises(CheckoutLeased):
        ws.repo_commit("op1", "operator commit during a leased run")
    assert _git(src, "status", "--porcelain") != "", \
        "the refusal must not have committed anything"


# ── retention: deletion exists only behind the conservative reaper ─────

def test_no_broad_or_unguarded_cleanup_api_exists(db, home):
    for name in ("delete", "prune", "cleanup", "remove_worktree"):
        assert not hasattr(ri, name), f"unguarded cleanup API appeared: {name}"
    assert hasattr(ri, "reap_released_worktrees")


def test_disposal_is_refused_while_the_run_is_not_terminal(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "x1", src)
    ri.ensure_for_run(db, run_id="run-x1", project_id="x1",
                      config_name="dpe_default_v2", repo_mode="code")
    ok, why = ri.is_disposable(db, "run-x1", run_status="running",
                               admitted_ops=0)
    assert ok is False and "terminal" in why
    ok, why = ri.is_disposable(db, "run-x1", run_status="paused",
                               admitted_ops=0)
    assert ok is False and "terminal" in why, \
        "paused is not terminal — a checkpoint is not an ending"


def test_disposal_is_refused_while_an_operation_is_admitted(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "x2", src)
    ri.ensure_for_run(db, run_id="run-x2", project_id="x2",
                      config_name="dpe_default_v2", repo_mode="code")
    ok, why = ri.is_disposable(db, "run-x2", run_status="failed",
                               admitted_ops=1)
    assert ok is False and "admitted" in why


def test_disposal_is_refused_while_the_tree_holds_uncommitted_work(
        db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "x3", src)
    rec = ri.ensure_for_run(db, run_id="run-x3", project_id="x3",
                            config_name="dpe_default_v2", repo_mode="code")
    (Path(rec["worktree_path"]) / "unsaved.txt").write_text("work\n")
    ok, why = ri.is_disposable(db, "run-x3", run_status="completed",
                               admitted_ops=0)
    assert ok is False and "uncommitted" in why


def test_disposal_is_refused_while_the_branch_is_unmerged(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "x4", src)
    rec = ri.ensure_for_run(db, run_id="run-x4", project_id="x4",
                            config_name="dpe_default_v2", repo_mode="code")
    wt = Path(rec["worktree_path"])
    (wt / "delivered.txt").write_text("work\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "delivered"], cwd=wt, check=True)
    ok, why = ri.is_disposable(db, "run-x4", run_status="completed",
                               admitted_ops=0, integration_ref="main")
    assert ok is False and "unmerged" in why
    # …and the integration target is supplied by the caller, never assumed.
    with pytest.raises(TypeError):
        ri.is_disposable(db, "run-x4", run_status="completed", admitted_ops=0,
                         integration_ref=None, require_merge=True)


def test_retained_reports_every_worktree_it_made(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "y1", src)
    _project(db, "y2", src)
    ri.ensure_for_run(db, run_id="run-y1", project_id="y1",
                      config_name="dpe_default_v2", repo_mode="code")
    ri.ensure_for_run(db, run_id="run-y2", project_id="y2",
                      config_name="dpe_default_v2", repo_mode="code")
    rows = ri.retained(db)
    assert {r["run_id"] for r in rows} == {"run-y1", "run-y2"}
    assert all(r["worktree_path"] and r["base_sha"] for r in rows)

# ── conservative automatic reaping ───────────────────────────────────

class _ReapSF:
    def __init__(self, rows, audits=None):
        self.rows = rows
        self.audits = audits or {}

    def get_run(self, run_id):
        return self.rows.get(run_id)

    def audit_operation_owners(self, run_id):
        return self.audits.get(run_id, {"lost": [], "unknown": [], "alive": 0})


def _release_for_reap(db, run_id, disposition="auto_released_terminal_quiet"):
    ri.release(db, run_id, disposition=disposition)
    rec = ri.record(db, run_id)
    assert rec["released_at"]
    return rec


def test_reaper_keeps_a_clean_pushed_but_unmerged_run(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "reap-push", src)
    rec = ri.ensure_for_run(db, run_id="run-reap-push", project_id="reap-push",
                            config_name="coding_impl", repo_mode="code")
    wt = Path(rec["worktree_path"])
    (wt / "delivered.txt").write_text("work\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "delivered"], cwd=wt, check=True)
    _release_for_reap(db, "run-reap-push")

    report = ri.reap_released_worktrees(
        db, _ReapSF({"run-reap-push": {"status": "completed"}}), min_idle_seconds=0)

    assert wt.exists(), "an unmerged/open-PR-shaped delivery was reaped"
    assert report["removed"] == []
    assert "unmerged" in report["retained"][0]["reason"]


def test_reaper_removes_after_local_main_contains_the_run_branch(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "reap-merged", src)
    rec = ri.ensure_for_run(db, run_id="run-reap-merged", project_id="reap-merged",
                            config_name="coding_impl", repo_mode="code")
    wt = Path(rec["worktree_path"])
    (wt / "delivered.txt").write_text("work\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "delivered"], cwd=wt, check=True)
    branch = rec["branch"]
    _release_for_reap(db, "run-reap-merged")

    # Simulate an accepted PR/integration becoming visible in the local source.
    subprocess.run(["git", "merge", "--ff-only", branch], cwd=src, check=True,
                   capture_output=True, text=True)
    report = ri.reap_released_worktrees(
        db, _ReapSF({"run-reap-merged": {"status": "completed"}}), min_idle_seconds=0)
    assert not wt.exists()
    assert [x["run_id"] for x in report["removed"]] == ["run-reap-merged"]
    final = ri.record(db, "run-reap-merged")
    assert final["disposition"] == "reaped_merged_into:main"
    assert _git(src, "show-ref", "--verify", f"refs/heads/{branch}"), \
        "reaping a checkout must preserve its branch/commit"


def test_reaper_keeps_paused_and_dirty_work(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    for rid, pid in (("run-paused", "paused"), ("run-dirty", "dirty")):
        _project(db, pid, src)
        rec = ri.ensure_for_run(db, run_id=rid, project_id=pid,
                                config_name="coding_impl", repo_mode="code")
        _release_for_reap(db, rid)
        if rid == "run-dirty":
            (Path(rec["worktree_path"]) / "unsaved.txt").write_text("do not lose\n")

    sf = _ReapSF({"run-paused": {"status": "paused"},
                  "run-dirty": {"status": "completed"}})
    report = ri.reap_released_worktrees(db, sf, min_idle_seconds=0)

    assert Path(ri.record(db, "run-paused")["worktree_path"]).exists()
    assert Path(ri.record(db, "run-dirty")["worktree_path"]).exists()
    reasons = {x["run_id"]: x["reason"] for x in report["retained"]}
    assert "not terminal" in reasons["run-paused"]
    assert "uncommitted" in reasons["run-dirty"]


def test_reaper_allows_explicit_discard_only_after_terminal_quiet_clean(db, home, tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "discard", src)
    rec = ri.ensure_for_run(db, run_id="run-discard", project_id="discard",
                            config_name="coding_impl", repo_mode="code")
    wt = Path(rec["worktree_path"])
    (wt / "committed-but-unwanted.txt").write_text("throw away\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "unwanted"], cwd=wt, check=True)
    _release_for_reap(db, "run-discard", disposition="discarded")

    report = ri.reap_released_worktrees(
        db, _ReapSF({"run-discard": {"status": "failed"}}), min_idle_seconds=0)

    assert not wt.exists()
    assert [x["run_id"] for x in report["removed"]] == ["run-discard"]
    assert ri.record(db, "run-discard")["disposition"] == "reaped_discarded"


# ── unmerged, undiscarded trees: directory reclaimed, commit ref-held ─

_PAST_UNMERGED = 259200 + 3600


def _unmerged_run(db, tmp_path, rid, *, snapshot=False):
    """A released, clean tree whose HEAD is NOT in the source's current branch."""
    src = tmp_path / "src"
    if not src.exists():
        _init_repo(src)
    _project(db, rid, src)
    if snapshot:
        # Pin a snapshot to a commit the source then drops from every branch.
        (src / "dropped.txt").write_text("only in the snapshot\n")
        subprocess.run(["git", "add", "-A"], cwd=src, check=True)
        subprocess.run(["git", "commit", "-qm", "dropped"], cwd=src, check=True)
        rec = ri.ensure_for_run(db, run_id=rid, project_id=rid,
                                config_name="review", repo_mode="none")
        subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=src, check=True)
    else:
        rec = ri.ensure_for_run(db, run_id=rid, project_id=rid,
                                config_name="coding_impl", repo_mode="code")
        wt = Path(rec["worktree_path"])
        (wt / "delivered.txt").write_text("work\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "delivered"], cwd=wt, check=True)
    head = _git(rec["worktree_path"], "rev-parse", "HEAD")
    assert subprocess.run(["git", "merge-base", "--is-ancestor", head, "main"],
                          cwd=src).returncode == 1
    _release_for_reap(db, rid)
    return src, Path(rec["worktree_path"]), head


def _reap_unmerged(db, rids, *, status="completed", **kw):
    kw.setdefault("now", time.time() + _PAST_UNMERGED)
    row = {"status": status, "updated_at": datetime.now(UTC).isoformat()}
    rids = [rids] if isinstance(rids, str) else rids
    return ri.reap_released_worktrees(db, _ReapSF({rid: row for rid in rids}),
                                      min_idle_seconds=0, **kw)


@pytest.fixture
def unmerged_default(monkeypatch):
    monkeypatch.delenv(ri.UNMERGED_REAP_ENV, raising=False)
    monkeypatch.delenv(ri.UNMERGED_REAP_MAX_ENV, raising=False)
    monkeypatch.setattr(ri, "_UNCLEAN_TREES", {})


def test_unmerged_tree_is_kept_until_the_idle_threshold(db, home, tmp_path, unmerged_default):
    src, wt, _ = _unmerged_run(db, tmp_path, "run-um-young")
    report = _reap_unmerged(db, "run-um-young", now=None)
    assert wt.exists() and report["removed"] == []
    reason = report["retained"][0]["reason"]
    assert "unmerged" in reason and "waits for 259200s idle" in reason
    assert ri.record(db, "run-um-young")["disposition"] == "auto_released_terminal_quiet"


def test_unmerged_tree_on_its_branch_gets_a_reclaimed_ref_and_keeps_the_branch(
        db, home, tmp_path, unmerged_default):
    src, wt, head = _unmerged_run(db, tmp_path, "run-um-branch")
    branch = ri.record(db, "run-um-branch")["branch"]
    ref = ri.RECLAIMED_REF_PREFIX + "run-um-branch"
    report = _reap_unmerged(db, "run-um-branch")
    assert not wt.exists()
    assert [x["run_id"] for x in report["removed"]] == ["run-um-branch"]
    final = ri.record(db, "run-um-branch")
    # The run branch alone is unprotected once its worktree is gone.
    assert final["disposition"] == f"reaped_ref_preserved:{ref}"
    assert head in final["note"]
    assert _git(src, "rev-parse", ref) == head
    assert _git(src, "rev-parse", f"refs/heads/{branch}") == head, "branch must be untouched"
    # Reaped is terminal: a later sweep neither revisits nor reports it.
    assert _reap_unmerged(db, "run-um-branch") == {"removed": [], "retained": []}


def test_failed_unmerged_run_is_kept_because_it_may_be_rescued(
        db, home, tmp_path, unmerged_default):
    src, wt, _ = _unmerged_run(db, tmp_path, "run-um-failed")
    report = _reap_unmerged(db, "run-um-failed", status="failed")
    assert wt.exists() and report["removed"] == []
    assert "may be rescued" in report["retained"][0]["reason"]
    assert not _git(src, "for-each-ref", ri.RECLAIMED_REF_PREFIX)
    # The resolver a rescue resumes through still finds the tree.
    assert ri.resolve_for_resolver(db, "run-um-failed") == str(wt)


def test_unmerged_tree_of_a_detached_source_is_ref_preserved(
        db, home, tmp_path, unmerged_default):
    src, wt, head = _unmerged_run(db, tmp_path, "run-um-detached")
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=src, check=True)
    report = _reap_unmerged(db, "run-um-detached")
    ref = ri.RECLAIMED_REF_PREFIX + "run-um-detached"
    assert not wt.exists() and report["removed"]
    assert _git(src, "rev-parse", ref) == head
    assert ri.record(db, "run-um-detached")["disposition"] == f"reaped_ref_preserved:{ref}"


def test_unmerged_tree_of_an_unavailable_source_is_kept(db, home, tmp_path, unmerged_default):
    src, wt, _ = _unmerged_run(db, tmp_path, "run-um-nosrc")
    src.rename(tmp_path / "moved-away")
    report = _reap_unmerged(db, "run-um-nosrc")
    assert wt.exists() and report["removed"] == [] and report["retained"]
    assert ri.record(db, "run-um-nosrc")["disposition"] == "auto_released_terminal_quiet"


def test_recent_git_activity_blocks_an_unmerged_reap(db, home, tmp_path, unmerged_default):
    src, wt, _ = _unmerged_run(db, tmp_path, "run-um-active")
    now = time.time() + _PAST_UNMERGED
    index = Path(_git(wt, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    os.utime(index, (now - 100, now - 100))
    report = _reap_unmerged(db, "run-um-active", now=now)
    assert wt.exists() and report["removed"] == []
    assert "waits for 259200s idle" in report["retained"][0]["reason"]


def test_unreachable_snapshot_commit_gets_a_reclaimed_ref_before_removal(
        db, home, tmp_path, unmerged_default):
    src, wt, head = _unmerged_run(db, tmp_path, "run-um-snap", snapshot=True)
    ref = ri.RECLAIMED_REF_PREFIX + "run-um-snap"
    report = _reap_unmerged(db, "run-um-snap")
    assert not wt.exists() and report["removed"]
    assert _git(src, "rev-parse", ref) == head
    assert ri.record(db, "run-um-snap")["disposition"] == f"reaped_ref_preserved:{ref}"


@pytest.mark.parametrize("dirt", ["untracked", "tracked"])
def test_unmerged_tree_with_uncommitted_work_is_kept(db, home, tmp_path, unmerged_default, dirt):
    src, wt, _ = _unmerged_run(db, tmp_path, f"run-um-{dirt}")
    (wt / ("new.txt" if dirt == "untracked" else "delivered.txt")).write_text("unsaved\n")
    report = _reap_unmerged(db, f"run-um-{dirt}")
    assert wt.exists() and report["removed"] == []
    assert "uncommitted" in report["retained"][0]["reason"]
    assert not _git(src, "for-each-ref", ri.RECLAIMED_REF_PREFIX)
    assert ri.record(db, f"run-um-{dirt}")["disposition"] == "auto_released_terminal_quiet"


def test_unmerged_tree_is_kept_when_its_ref_cannot_be_created(
        db, home, tmp_path, unmerged_default):
    src, wt, head = _unmerged_run(db, tmp_path, "run-um-noref", snapshot=True)
    ref = ri.RECLAIMED_REF_PREFIX + "run-um-noref"
    other = _git(src, "rev-parse", "main")
    subprocess.run(["git", "update-ref", ref, other], cwd=src, check=True)
    report = _reap_unmerged(db, "run-um-noref")
    assert wt.exists() and report["removed"] == []
    assert "could not create" in report["retained"][0]["reason"]
    assert _git(src, "rev-parse", ref) == other, "an existing ref is never moved"
    assert ri.record(db, "run-um-noref")["disposition"] == "auto_released_terminal_quiet"


@pytest.mark.parametrize("setting", ["0", "nonsense"])
def test_unmerged_reap_disabled_or_misconfigured_keeps_old_behaviour(
        db, home, tmp_path, monkeypatch, setting):
    monkeypatch.setenv(ri.UNMERGED_REAP_ENV, setting)
    src, wt, _ = _unmerged_run(db, tmp_path, "run-um-off")
    branch = ri.record(db, "run-um-off")["branch"]
    report = _reap_unmerged(db, "run-um-off")
    assert wt.exists() and report["removed"] == []
    reason = report["retained"][0]["reason"]
    if setting == "0":
        assert reason == f"branch {branch} is unmerged into main"
    else:
        assert reason.startswith(f"branch {branch} is unmerged into main; ")
        assert "misconfigured" in reason
    assert not _git(src, "for-each-ref", ri.RECLAIMED_REF_PREFIX)


def test_merged_run_still_reaps_as_merged_with_unmerged_reap_enabled(
        db, home, tmp_path, unmerged_default):
    src, wt, head = _unmerged_run(db, tmp_path, "run-um-merged")
    subprocess.run(["git", "merge", "-q", "--ff-only", head], cwd=src, check=True)
    _reap_unmerged(db, "run-um-merged")
    assert not wt.exists()
    assert ri.record(db, "run-um-merged")["disposition"] == "reaped_merged_into:main"


@pytest.mark.parametrize("window", ["before_remove", "after_remove"])
def test_ref_preserving_reap_recovers_each_crash_window(
        db, home, tmp_path, monkeypatch, unmerged_default, window):
    src, wt, head = _unmerged_run(db, tmp_path, "run-um-crash", snapshot=True)
    ref = ri.RECLAIMED_REF_PREFIX + "run-um-crash"
    real_remove = ri._remove_run_worktree

    def crash(rec, **kw):
        if window == "after_remove":
            assert real_remove(rec, **kw)[0]
        raise RuntimeError("process died")

    monkeypatch.setattr(ri, "_remove_run_worktree", crash)
    report = _reap_unmerged(db, "run-um-crash")
    assert report["removed"] == []
    assert ri.record(db, "run-um-crash")["disposition"] == \
        f"reaping_ref_preserved:{head}:{ref}"
    assert _git(src, "rev-parse", ref) == head
    assert wt.exists() is (window == "before_remove")
    monkeypatch.setattr(ri, "_remove_run_worktree", real_remove)

    report = _reap_unmerged(db, "run-um-crash")
    assert [x["run_id"] for x in report["removed"]] == ["run-um-crash"]
    assert not wt.exists()
    assert ri.record(db, "run-um-crash")["disposition"] == f"reaped_ref_preserved:{ref}"
    assert _git(src, "rev-parse", ref) == head


@pytest.mark.parametrize("mode", ["snapshot", "worktree"])
@pytest.mark.parametrize("change", ["deleted", "moved"])
def test_pending_ref_preserving_reap_is_not_finalised_without_its_ref(
        db, home, tmp_path, monkeypatch, unmerged_default, mode, change):
    rid = f"run-um-lost-{mode}-{change}"
    src, wt, head = _unmerged_run(db, tmp_path, rid, snapshot=mode == "snapshot")
    ref = ri.RECLAIMED_REF_PREFIX + rid
    real_remove = ri._remove_run_worktree

    def crash(rec, **kw):
        assert real_remove(rec, **kw)[0]
        raise RuntimeError("process died after removal")

    monkeypatch.setattr(ri, "_remove_run_worktree", crash)
    _reap_unmerged(db, rid)
    assert not wt.exists()
    if change == "deleted":
        subprocess.run(["git", "update-ref", "-d", ref], cwd=src, check=True)
    else:
        seed = _git(src, "rev-list", "--max-parents=0", "main")
        subprocess.run(["git", "update-ref", ref, seed], cwd=src, check=True)
    report = _reap_unmerged(db, rid)
    assert report["removed"] == [] and "no longer holds" in report["retained"][0]["reason"]
    assert ri.record(db, rid)["disposition"] == f"reaping_ref_preserved:{head}:{ref}"


def test_native_removal_hazard_is_refused_before_any_ref_or_marker(
        db, home, tmp_path, unmerged_default):
    src, wt, _ = _unmerged_run(db, tmp_path, "run-um-hazard")
    (wt / ".gitmodules").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "gitmodules"], cwd=wt, check=True)
    report = _reap_unmerged(db, "run-um-hazard")
    assert wt.exists() and report["removed"] == []
    assert ".gitmodules is present" in report["retained"][0]["reason"]
    assert not _git(src, "for-each-ref", ri.RECLAIMED_REF_PREFIX)
    assert ri.record(db, "run-um-hazard")["disposition"] == "auto_released_terminal_quiet"


def test_refused_removal_after_the_marker_restores_the_disposition(
        db, home, tmp_path, monkeypatch, unmerged_default):
    src, wt, head = _unmerged_run(db, tmp_path, "run-um-refused")
    monkeypatch.setattr(ri.closed_git, "native_remove",
                        lambda *a, **kw: (False, "git worktree remove refused: fixture"))
    report = _reap_unmerged(db, "run-um-refused")
    assert wt.exists() and report["removed"] == []
    assert "fixture" in report["retained"][0]["reason"]
    assert ri.record(db, "run-um-refused")["disposition"] == "auto_released_terminal_quiet"


def test_a_commit_landing_after_preservation_retains_the_snapshot(
        db, home, tmp_path, monkeypatch, unmerged_default):
    src, wt, head = _unmerged_run(db, tmp_path, "run-um-late", snapshot=True)
    real_preserve = ri._preserve_run_head

    def preserve_then_commit(rec, expected):
        held = real_preserve(rec, expected)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit",
                        "-q", "--allow-empty", "-m", "late"], cwd=wt, check=True)
        return held

    monkeypatch.setattr(ri, "_preserve_run_head", preserve_then_commit)
    report = _reap_unmerged(db, "run-um-late")
    assert wt.exists() and report["removed"] == []
    assert f"HEAD is no longer {head}" in report["retained"][0]["reason"]
    assert _git(wt, "rev-parse", "HEAD") != head
    assert ri.record(db, "run-um-late")["disposition"] == "auto_released_terminal_quiet"


def test_unmerged_reaps_are_capped_per_sweep(db, home, tmp_path, unmerged_default):
    rids = [f"run-um-cap{i}" for i in range(3)]
    trees = [_unmerged_run(db, tmp_path, rid)[1] for rid in rids]
    report = _reap_unmerged(db, rids, unmerged_reap_max_per_sweep=2)
    assert len(report["removed"]) == 2
    assert [x["reason"].endswith("ref-preserved reap deferred (2 per sweep)")
            for x in report["retained"]] == [True]
    assert sum(t.exists() for t in trees) == 1
    report = _reap_unmerged(db, rids, unmerged_reap_max_per_sweep=2)
    assert len(report["removed"]) == 1 and not any(t.exists() for t in trees)


def test_a_dirty_unmerged_tree_is_not_rehashed_until_it_changes(
        db, home, tmp_path, monkeypatch, unmerged_default):
    src, wt, _ = _unmerged_run(db, tmp_path, "run-um-cache")
    (wt / "new.txt").write_text("unsaved\n")
    proofs = []
    real = ri.is_disposable

    def counting(*a, **kw):
        if kw.get("integration_ref") is None:
            proofs.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(ri, "is_disposable", counting)
    first = _reap_unmerged(db, "run-um-cache")["retained"][0]["reason"]
    second = _reap_unmerged(db, "run-um-cache")["retained"][0]["reason"]
    assert "uncommitted" in first and second == first and len(proofs) == 1
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "saved"], cwd=wt, check=True)
    report = _reap_unmerged(db, "run-um-cache")
    assert len(proofs) == 2 and report["removed"] and not wt.exists()


# ── requested base (relay) ───────────────────────────────────────────

def test_a_requested_base_replaces_head_for_the_next_worktree(db, home, tmp_path):
    """A relayed State attempt inherits the failed run's branch head, not HEAD."""
    src = tmp_path / "src"
    head = _init_repo(src)
    _project(db, "p-failed", src)
    failed = ri.ensure_for_run(db, run_id="run-failed", project_id="p-failed",
                               config_name="coding_impl", repo_mode="code")
    wt = Path(failed["worktree_path"])
    (wt / "draft.txt").write_text("half done\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "half done"], cwd=wt, check=True)
    relay_base = _git(wt, "rev-parse", "HEAD")
    assert relay_base != head

    _project(db, "p-relay", src)
    ri.request_base(db, "p-relay", relay_base, note="relay of attempt-x")
    rec = ri.ensure_for_run(db, run_id="run-relay", project_id="p-relay",
                            config_name="coding_impl", repo_mode="code")

    assert rec["base_sha"] == relay_base
    assert rec["note"] == "relay of attempt-x"
    assert (Path(rec["worktree_path"]) / "draft.txt").read_text() == "half done\n"
    # The source checkout itself never moved.
    assert _git(src, "rev-parse", "HEAD") == head


def test_a_requested_base_that_is_not_a_commit_fails_closed(db, home, tmp_path):
    """Provisioning from HEAD instead would be the silent restart a relay exists to stop."""
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "p-relay", src)
    ri.request_base(db, "p-relay", "f" * 40)
    with pytest.raises(IsolationUnavailable, match="not a commit"):
        ri.ensure_for_run(db, run_id="run-relay", project_id="p-relay",
                          config_name="coding_impl", repo_mode="code")
    assert ri.record(db, "run-relay") is None


def test_a_base_request_is_refused_once_the_project_has_a_record(db, home, tmp_path):
    src = tmp_path / "src"
    head = _init_repo(src)
    _project(db, "p1", src)
    ri.ensure_for_run(db, run_id="run-aaa", project_id="p1",
                      config_name="coding_impl", repo_mode="code")
    with pytest.raises(IsolationUnavailable, match="would never apply"):
        ri.request_base(db, "p1", head)
    with pytest.raises(IsolationUnavailable, match="40-hex"):
        ri.request_base(db, "p-new", "main")


def test_a_read_snapshot_cannot_honour_a_requested_base(db, home, tmp_path):
    src = tmp_path / "src"
    head = _init_repo(src)
    _project(db, "p-review", src)
    ri.request_base(db, "p-review", head)
    with pytest.raises(IsolationUnavailable, match="read snapshot"):
        ri.ensure_for_run(db, run_id="run-review", project_id="p-review",
                          config_name="code_review", repo_mode="none")


# ── concurrent provisioning of ONE run (iss-6721ec6a2b774b6b) ──────
#
# The launch API and the poller both call `ensure_for_run` for the same run
# before starting it. Both used to read "no record", then both ran
# `git worktree add -b codex/run/<id>`; the loser died with
# `fatal: a branch named 'codex/run/<id>' already exists` and the run failed
# before any worker started. These regressions drive two REAL callers through
# REAL git and the REAL isolation DB, and require one tree, one durable
# record and no error.


def test_two_concurrent_callers_provision_exactly_one_tree(db, home, tmp_path):
    import threading

    src = tmp_path / "src"
    base = _init_repo(src)
    _project(db, "pcc", src)

    barrier = threading.Barrier(2)
    results, errors = {}, {}

    def worker(name):
        try:
            barrier.wait(timeout=10)
            results[name] = ri.ensure_for_run(
                db, run_id="run-cc", project_id="pcc",
                config_name="dpe_default_v2", repo_mode="code")
        except BaseException as e:  # noqa: BLE001 - captured for the assertion
            errors[name] = e

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a concurrent caller failed: {errors!r}"
    assert set(results) == {"a", "b"}
    a, b = results["a"], results["b"]
    assert a["mode"] == ri.MODE_WORKTREE
    # Both callers resolve the SAME exact record — one tree, one branch, one
    # base — never two provisions racing to different answers.
    assert a["worktree_path"] == b["worktree_path"]
    assert a["branch"] == b["branch"] == "codex/run/run-cc"
    assert a["base_sha"] == b["base_sha"] == base
    # The durable record agrees, and there is exactly one of it.
    rec = ri.record(db, "run-cc")
    assert rec["worktree_path"] == a["worktree_path"]
    assert rec["base_sha"] == base
    # Exactly one linked worktree exists (plus the source checkout itself).
    assert len(_git(src, "worktree", "list").splitlines()) == 2
    assert Path(a["worktree_path"]).is_dir()
    assert ri.resolve_for_resolver(db, "run-cc") == a["worktree_path"]


def test_concurrent_repoless_provisioning_writes_one_consistent_record(
        db, home, tmp_path):
    import threading

    _project(db, "pnn", None, repo_type="none")
    barrier = threading.Barrier(2)
    results, errors = {}, {}

    def worker(name):
        try:
            barrier.wait(timeout=10)
            results[name] = ri.ensure_for_run(
                db, run_id="run-nn", project_id="pnn",
                config_name="pipeline_forge", repo_mode="none")
        except BaseException as e:  # noqa: BLE001
            errors[name] = e

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a concurrent caller failed: {errors!r}"
    assert results["a"]["mode"] == results["b"]["mode"] == ri.MODE_NONE
    assert ri.resolve_for_resolver(db, "run-nn") is False


def test_a_crashed_provisioner_releases_its_lock_without_stranding_the_run(
        db, home, tmp_path):
    """A provisioner that dies mid-provision must not strand the run.

    The lock is a KERNEL advisory lock, so the OS drops it when the holder
    exits — there is no provisional database row an operator would have to
    clear, and no bounded deadline that a healthy winner could outlive. The
    next caller simply takes the freed lock and provisions.
    """
    import sys

    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "pcrash", src)
    lock = ri._lock_path("run-crash")
    lock.parent.mkdir(parents=True, exist_ok=True)
    child = (
        "import fcntl, os, sys, time\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", child, str(lock)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
    finally:
        proc.kill()
        proc.wait(timeout=10)

    rec = ri.ensure_for_run(db, run_id="run-crash", project_id="pcrash",
                            config_name="dpe_default_v2", repo_mode="code")
    assert rec["mode"] == ri.MODE_WORKTREE
    assert ri.record(db, "run-crash")["worktree_path"] == rec["worktree_path"]


def test_a_delayed_winner_does_not_false_fail_the_blocked_caller(
        db, home, tmp_path, monkeypatch):
    """A slow (but healthy) provisioner must not turn the loser into a failure.

    The loser BLOCKS on the kernel lock and re-reads; there is no fixed wait
    after which the scheduler terminalises a run whose winner is merely slow.
    Both real callers end on the same exact record.
    """
    import threading

    src = tmp_path / "src"
    base = _init_repo(src)
    _project(db, "pdelay", src)
    real = ri._provision_tree
    import time as _time

    def delayed(*args, **kwargs):
        _time.sleep(0.5)                    # winner is slow, not dead
        return real(*args, **kwargs)

    monkeypatch.setattr(ri, "_provision_tree", delayed)

    barrier = threading.Barrier(2)
    results, errors = {}, {}

    def worker(name):
        try:
            barrier.wait(timeout=10)
            results[name] = ri.ensure_for_run(
                db, run_id="run-delay", project_id="pdelay",
                config_name="dpe_default_v2", repo_mode="code")
        except BaseException as e:  # noqa: BLE001
            errors[name] = e

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a caller false-failed a slow winner: {errors!r}"
    a, b = results["a"], results["b"]
    assert a["worktree_path"] == b["worktree_path"]
    assert a["base_sha"] == b["base_sha"] == base
    assert len(_git(src, "worktree", "list").splitlines()) == 2


def test_missing_kernel_lock_capability_fails_closed(db, home, tmp_path,
                                                     monkeypatch):
    """Without a kernel advisory lock provisioning must REFUSE, not proceed
    unprotected.

    Two callers both reading "no record" would otherwise create the same
    branch; the capability is therefore mandatory, and the refusal must land
    BEFORE any provisioning effect — no worktree directory, no branch, no
    durable record. The capability is removed at its source (the `fcntl`
    import) the way a non-POSIX host presents it.
    """
    import builtins

    real_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, *args, **kwargs)

    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "pnoflock", src)
    monkeypatch.setattr(builtins, "__import__", no_fcntl)

    with pytest.raises(IsolationUnavailable, match="advisory lock"):
        ri.ensure_for_run(db, run_id="run-noflock", project_id="pnoflock",
                          config_name="dpe_default_v2", repo_mode="code")

    # The refusal precedes every effect: no durable record, no worktree
    # directory, no run branch, and nothing added to the source checkout.
    assert ri.record(db, "run-noflock") is None
    assert not (datadir.worktrees_dir() / "run-noflock").exists()
    assert _git(src, "branch", "--list", "codex/run/run-noflock") == ""
    assert len(_git(src, "worktree", "list").splitlines()) == 1


def test_a_foreign_caller_cannot_be_handed_another_projects_tree(
        db, home, tmp_path):
    """A record belongs to the run's own project/config; a foreign caller is
    refused rather than handed a tree that is not its own — while the owner's
    healthy idempotent resume still gets the same record."""
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "pf-a", src)
    _project(db, "pf-b", src)
    rec = ri.ensure_for_run(db, run_id="run-f", project_id="pf-a",
                            config_name="dpe_default_v2", repo_mode="code")

    with pytest.raises(IsolationUnavailable, match="belongs to the run's own project"):
        ri.ensure_for_run(db, run_id="run-f", project_id="pf-b",
                          config_name="dpe_default_v2", repo_mode="code")
    with pytest.raises(IsolationUnavailable):
        ri.ensure_for_run(db, run_id="run-f", project_id="pf-a",
                          config_name="code_review", repo_mode="code")

    again = ri.ensure_for_run(db, run_id="run-f", project_id="pf-a",
                              config_name="dpe_default_v2", repo_mode="code")
    assert again["worktree_path"] == rec["worktree_path"]



def test_a_failed_provision_leaves_no_placeholder_behind(db, home, tmp_path):
    """If provisioning raises, the claim is removed so the next caller sees an
    honest "no decision" and can retry — never a half-written record."""
    _project(db, "pfail", None, repo_type="none")
    with pytest.raises(IsolationUnavailable, match="has no"):
        ri.ensure_for_run(db, run_id="run-pfail", project_id="pfail",
                          config_name="dpe_default_v2", repo_mode="code")
    assert ri.record(db, "run-pfail") is None


def test_repeated_sequential_calls_do_not_reprovision(db, home, tmp_path):
    """Healthy repeated provisioning is a no-op returning the same record."""
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "pseq", src)
    first = ri.ensure_for_run(db, run_id="run-seq", project_id="pseq",
                              config_name="dpe_default_v2", repo_mode="code")
    for _ in range(3):
        again = ri.ensure_for_run(db, run_id="run-seq", project_id="pseq",
                                  config_name="dpe_default_v2", repo_mode="code")
        assert again["worktree_path"] == first["worktree_path"]
    assert len(_git(src, "worktree", "list").splitlines()) == 2



def test_a_placeholder_does_not_collide_with_a_foreign_directory(db, home, tmp_path):
    """Concurrency must not weaken fail-closed: a directory at the conventional
    path with no record is still refused rather than adopted."""
    src = tmp_path / "src"
    _init_repo(src)
    _project(db, "pfor", src)
    (datadir.worktrees_dir() / "run-for").mkdir(parents=True)
    with pytest.raises(IsolationUnavailable, match="refusing to adopt"):
        ri.ensure_for_run(db, run_id="run-for", project_id="pfor",
                          config_name="dpe_default_v2", repo_mode="code")
    assert ri.record(db, "run-for") is None


def test_concurrent_callers_share_one_requested_base(db, home, tmp_path):
    """A relayed attempt is provisioned once, at the requested base, by whichever
    of the concurrent callers wins — and the loser returns that same record."""
    import threading

    src = tmp_path / "src"
    head = _init_repo(src)
    _project(db, "p-failed-cc", src)
    failed = ri.ensure_for_run(db, run_id="run-failed-cc",
                               project_id="p-failed-cc",
                               config_name="coding_impl", repo_mode="code")
    wt = Path(failed["worktree_path"])
    (wt / "draft.txt").write_text("half done\n")
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "half done"], cwd=wt, check=True)
    relay_base = _git(wt, "rev-parse", "HEAD")
    assert relay_base != head

    _project(db, "p-relay-cc", src)
    ri.request_base(db, "p-relay-cc", relay_base, note="relay of attempt-cc")

    barrier = threading.Barrier(2)
    results, errors = {}, {}

    def worker(name):
        try:
            barrier.wait(timeout=10)
            results[name] = ri.ensure_for_run(
                db, run_id="run-relay-cc", project_id="p-relay-cc",
                config_name="coding_impl", repo_mode="code")
        except BaseException as e:  # noqa: BLE001
            errors[name] = e

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a concurrent relay caller failed: {errors!r}"
    a, b = results["a"], results["b"]
    assert a["base_sha"] == b["base_sha"] == relay_base
    assert a["worktree_path"] == b["worktree_path"]
    assert (Path(a["worktree_path"]) / "draft.txt").read_text() == "half done\n"
    assert _git(src, "rev-parse", "HEAD") == head
    # The relay run got exactly ONE worktree (the failed attempt's is separate).
    lines = _git(src, "worktree", "list").splitlines()
    assert len([l for l in lines if "run-relay-cc" in l]) == 1


def test_an_unknown_isolation_mode_refuses_rather_than_guessing(
        db, home, tmp_path):
    """A mode this deployment does not decide (e.g. a legacy provisional row)
    is refused — resolution never substitutes the shared checkout for it."""
    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_isolation "
            "(run_id, project_id, config_name, mode, created_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            ("run-inflight", "p-inflight", "dpe_default_v2", "provisioning"))
        conn.commit()
    with pytest.raises(IsolationUnavailable, match="does not decide"):
        ri.resolve_for_resolver(db, "run-inflight")
    disposable, why = ri.is_disposable(db, "run-inflight", run_status="failed",
                                      admitted_ops=0)
    assert disposable is False and "not a decision" in why

