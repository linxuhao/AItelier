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

import subprocess
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
        db, _ReapSF({"run-reap-push": {"status": "completed"}}))

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
        db, _ReapSF({"run-reap-merged": {"status": "completed"}}))

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
    report = ri.reap_released_worktrees(db, sf)

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
        db, _ReapSF({"run-discard": {"status": "failed"}}))

    assert not wt.exists()
    assert [x["run_id"] for x in report["removed"]] == ["run-discard"]
    assert ri.record(db, "run-discard")["disposition"] == "reaped_discarded"


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
