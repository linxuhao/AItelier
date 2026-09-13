"""Resource lifecycle tests use only temporary Git roots and an isolated engine."""
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition

from core import datadir
from core import run_isolation as ri
from core import run_resources as resources
from core.db_manager import DBManager
from core.semantic_index_control import IndexControl, validate_checkout


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AITELIER_HOME", str(home))
    monkeypatch.setenv("AITELIER_ZVEC_LIFECYCLE", "1")
    monkeypatch.setenv("AITELIER_ZVEC_PREPARE_WAIT_SECONDS", "0")
    db = DBManager(str(tmp_path / "host.db"))
    sf = SkillFlow(str(tmp_path / "engine.db"))
    sf.register_graph(PipelineGraph(name="resource", begin="s1", steps=[
        StepNode(id="s1", step_type="agent", output_mode="content",
                 output_fixed={"out": "out.txt"}, transitions=[Transition(to=None)])]))
    import api.dependencies as deps
    monkeypatch.setattr(deps, "db_instance", db)
    monkeypatch.setattr(deps, "get_db_manager", lambda: db)
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q", "-b", "main")
    git(source, "config", "user.email", "fixture@example.invalid")
    git(source, "config", "user.name", "fixture")
    (source / "code.py").write_text("def base_function(): pass\n")
    git(source, "add", "code.py")
    git(source, "commit", "-qm", "initial")
    ctl = resources.control()
    calls = []

    def execute(command, *, timeout):
        calls.append(command)
        assert command[-2:] == ["--mode", "server"] or "--mode" in command
        root = Path(command[2])
        assert root.parent == datadir.worktrees_dir()
        cache = root / ".zvec-grep"
        if command[1] == "status":
            assert (cache / "manifest.json").exists()
        elif "--drop" in command:
            if cache.exists():
                shutil.rmtree(cache)  # temporary fixture standing in for server drop
        else:
            cache.mkdir(exist_ok=True)
            (cache / "manifest.json").write_text("fixture index")
            for name in ("files.zvec", "index.zvec"):
                (cache / name).mkdir(exist_ok=True)

    def run(pid="p1", mode=ri.MODE_WORKTREE):
        db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(source))
        rid = sf.create_run("resource", {"project_id": pid}, project_id=pid)
        rec = ri.ensure_for_run(db, run_id=rid, project_id=pid,
                               config_name="resource", requested_mode=mode,
                               repo_mode="none" if mode == ri.MODE_READ_SNAPSHOT else "code")
        sf.start_run(rid)
        return rec

    return SimpleNamespace(db=db, sf=sf, source=source, ctl=ctl, calls=calls,
                           execute=execute, run=run, home=home)


def test_preparation_is_exact_ready_and_does_not_index_inventory(rig):
    one, two = rig.run("one"), rig.run("two")
    history = rig.home / "worktrees" / "unregistered-history"
    git(rig.source, "worktree", "add", "-qb", "history", str(history))
    rig.ctl.process_once(execute=rig.execute)
    for rec in (one, two):
        assert rig.ctl.settled(rig.ctl.get(rec["run_id"]), "ready")
        assert git(rec["worktree_path"], "status", "--porcelain") == ""
    assert not (history / ".zvec-grep").exists()
    assert not (rig.source / ".zvec-grep").exists()
    assert len(rig.calls) == 4  # index and explicit readiness check for each root
    assert rig.ctl.process_once(execute=rig.execute) == []


def test_terminal_without_lease_reconciles_at_real_startup_and_releases_only_index(rig, monkeypatch):
    rec, other = rig.run("finished"), rig.run("live")
    rig.ctl.process_once(execute=rig.execute)
    worktree = Path(rec["worktree_path"])
    (worktree / "unfinished.txt").write_text("preserve unmerged dirty work\n")
    models = rig.home / "zvec-grep-home" / "models"
    models.mkdir(parents=True)
    (models / "keep").write_text("shared model")
    rig.sf.stop_run(rec["run_id"], "fixture terminal")
    from core import scheduler
    monkeypatch.setattr(scheduler, "db", rig.db)
    scheduler.recover_leases_on_startup()
    assert ri.record(rig.db, rec["run_id"])["released_at"]
    rig.ctl.process_once(execute=rig.execute)
    assert rig.ctl.settled(rig.ctl.get(rec["run_id"]), "released")
    assert not (worktree / ".zvec-grep").exists()
    assert (worktree / "unfinished.txt").read_text().startswith("preserve")
    assert (Path(other["worktree_path"]) / ".zvec-grep").exists()
    assert (models / "keep").exists()
    resources.reconcile(rig.db, rig.sf)
    assert rig.ctl.process_once(execute=rig.execute) == []


def test_search_waits_for_its_root_and_same_call_falls_back(rig, monkeypatch):
    from aitelier.tools.semantic_search import impl
    first, second = rig.run("one"), rig.run("two")
    a, b = Path(first["worktree_path"]), Path(second["worktree_path"])
    (a / "code.py").write_text("apple_only_identifier\n")
    (b / "code.py").write_text("banana_only_identifier\n")
    called = []
    monkeypatch.setattr(impl, "_call", lambda args: called.append(args) or {
        "result": {"content": [{"type": "text", "text": (Path(args["root"])/"code.py").read_text()}]}})
    fallback = impl.semantic_search("apple_only_identifier", project_root=str(a))
    assert fallback["mode"] == "literal_fallback" and fallback["degraded"]
    assert "apple_only_identifier" in fallback["content"] and "banana" not in fallback["content"]
    assert called == [] and "error" not in fallback
    rig.ctl.process_once(execute=rig.execute)
    for path in (a, b):
        result = impl.semantic_search("identifier", project_root=str(path))
        assert result["mode"] == "semantic" and called[-1]["root"] == str(path)
        assert ("apple_only_identifier" if path == a else "banana_only_identifier") in result["content"]


def test_reader_blocks_drop_and_reaper_until_positive_acknowledgement(rig):
    rec = rig.run()
    rig.ctl.process_once(execute=rig.execute)
    with resources.search_lease(rec["worktree_path"]):
        rig.sf.stop_run(rec["run_id"], "finished during reader")
        resources.reconcile(rig.db, rig.sf)
        result = rig.ctl.process_once(execute=rig.execute)
        assert result[0]["outcome"] == "busy"
        assert ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)["removed"] == []
    rig.ctl.process_once(execute=rig.execute)
    result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert result["removed"] and rig.ctl.get(rec["run_id"]) is None
    assert git(rig.source, "show-ref", "--verify", "refs/heads/"+rec["branch"])


def test_stale_job_completion_cannot_acknowledge_a_new_request(rig):
    rec = rig.run()
    def changed(command, **kwargs):
        rig.execute(command, **kwargs)
        if command[1] == "index":
            rig.ctl.request(rec, "released")
    rig.ctl.process_once(execute=changed)
    assert not rig.ctl.settled(rig.ctl.get(rec["run_id"]), "released")
    rig.ctl.process_once(execute=rig.execute)
    assert rig.ctl.settled(rig.ctl.get(rec["run_id"]), "released")


@pytest.mark.parametrize("failure", [RuntimeError("sidecar unavailable"), subprocess.TimeoutExpired("zg", 1)])
def test_failed_release_is_not_a_receipt_and_restart_retries(rig, failure):
    rec = rig.run()
    rig.ctl.process_once(execute=rig.execute)
    rig.sf.stop_run(rec["run_id"], "terminal")
    resources.reconcile(rig.db, rig.sf)
    def fail(*args, **kwargs):
        raise failure
    rig.ctl.process_once(execute=fail, retry_seconds=0)
    assert not rig.ctl.settled(rig.ctl.get(rec["run_id"]), "released")
    assert ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)["removed"] == []
    fresh = IndexControl(rig.ctl.directory, datadir.worktrees_dir())
    fresh.process_once(execute=rig.execute)
    assert fresh.settled(fresh.get(rec["run_id"]), "released")


@pytest.mark.parametrize("audit", [None, {}, {"alive": "0", "lost": [], "unknown": []},
                                  {"alive": 0, "lost": [1], "unknown": []}])
def test_unknown_audit_never_releases(rig, monkeypatch, audit):
    rec = rig.run()
    rig.sf.stop_run(rec["run_id"], "terminal")
    monkeypatch.setattr(rig.sf, "audit_operation_owners", lambda rid: audit)
    result = resources.reconcile(rig.db, rig.sf)
    assert result["retained"] and rig.ctl.get(rec["run_id"])["desired"] == "ready"
    assert not ri.record(rig.db, rec["run_id"])["released_at"]


def finish(rig, rec):
    rig.ctl.process_once(execute=rig.execute)
    rig.sf.stop_run(rec["run_id"], "fixture terminal")
    resources.reconcile(rig.db, rig.sf)
    rig.ctl.process_once(execute=rig.execute)


def test_default_24_hour_idle_gate_then_preserves_branch_on_reap(rig):
    rec = rig.run()
    finish(rig, rec)
    result = ri.reap_released_worktrees(rig.db, rig.sf)
    assert not result["removed"] and "86400" in result["retained"][0]["reason"]
    result = ri.reap_released_worktrees(rig.db, rig.sf, now=time.time()+86402)
    assert result["removed"]
    assert git(rig.source, "show-ref", "--verify", "refs/heads/"+rec["branch"])
    assert ri.record(rig.db, rec["run_id"])["disposition"] != "discarded"


def test_activity_resets_the_idle_window_and_sweep_does_not(rig):
    rec = rig.run()
    finish(rig, rec)
    before = rig.ctl.get(rec["run_id"])["activity_at"]
    resources.reconcile(rig.db, rig.sf)
    assert rig.ctl.get(rec["run_id"])["activity_at"] == before
    future = time.time()+90000
    index = Path(git(rec["worktree_path"], "rev-parse", "--path-format=absolute", "--git-path", "index"))
    os.utime(index, (future-100, future-100))
    result = ri.reap_released_worktrees(rig.db, rig.sf, now=future)
    assert not result["removed"] and "idle 100s" in result["retained"][0]["reason"]


@pytest.mark.parametrize("value", ["nan", "-1", "invalid", "inf"])
def test_bad_idle_setting_fails_closed(rig, monkeypatch, value):
    rec = rig.run()
    finish(rig, rec)
    monkeypatch.setenv("AITELIER_WORKTREE_IDLE_SECONDS", value)
    assert not ri.reap_released_worktrees(rig.db, rig.sf, now=time.time()+90000)["removed"]


def test_malformed_engine_time_and_future_activity_retain(rig, monkeypatch):
    rec = rig.run()
    finish(rig, rec)
    original = rig.sf.get_run
    monkeypatch.setattr(rig.sf, "get_run", lambda rid: {**original(rid), "updated_at": "not-a-time"})
    result = ri.reap_released_worktrees(rig.db, rig.sf, now=time.time()+90000)
    assert not result["removed"] and "inactivity" in result["retained"][0]["reason"]
    monkeypatch.setattr(rig.sf, "get_run", original)
    with rig.ctl.connection() as conn:
        conn.execute("UPDATE indexes SET activity_at=?", (time.time()+200000,))
    assert not ri.reap_released_worktrees(rig.db, rig.sf, now=time.time()+90000)["removed"]


@pytest.mark.parametrize("kind", ["dirty", "unmerged", "ignored-evidence", "changed-branch"])
def test_old_work_is_not_implicitly_discarded(rig, kind):
    rec = rig.run()
    root = Path(rec["worktree_path"])
    if kind in ("dirty", "unmerged"):
        (root/"delivery.txt").write_text("valuable work")
        if kind == "unmerged":
            git(root, "add", "delivery.txt")
            git(root, "commit", "-qm", "unmerged delivery")
    elif kind == "ignored-evidence":
        exclude = Path(git(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"))
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("evidence/\n")
        (root/"evidence").mkdir()
        (root/"evidence"/"report.txt").write_text("do not delete")
    else:
        git(root, "checkout", "-qb", "manual-branch")
    finish(rig, rec)
    result = ri.reap_released_worktrees(rig.db, rig.sf, now=time.time()+90000)
    assert not result["removed"] and root.exists()
    assert ri.record(rig.db, rec["run_id"])["disposition"] != "discarded"
    if kind == "ignored-evidence":
        assert "ignored" in result["retained"][0]["reason"]


def test_git_status_failure_is_not_a_clean_tree(rig, monkeypatch):
    rec = rig.run()
    finish(rig, rec)
    original = ri._git
    def fail(root, *args, **kwargs):
        if args[0] == "status":
            return subprocess.CompletedProcess(args, 128, "", "forced status failure")
        return original(root, *args, **kwargs)
    monkeypatch.setattr(ri, "_git", fail)
    result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert not result["removed"] and "status failed" in result["retained"][0]["reason"]


def test_out_of_scope_and_symlink_index_never_reach_drop(rig, tmp_path):
    rec = rig.run()
    with pytest.raises(ValueError, match="managed"):
        validate_checkout({**rec, "worktree_path": str(rig.source)}, datadir.worktrees_dir())
    outside = tmp_path/"unowned-cache"
    outside.mkdir()
    (outside/"precious").write_text("keep")
    (Path(rec["worktree_path"])/".zvec-grep").symlink_to(outside, target_is_directory=True)
    rig.sf.stop_run(rec["run_id"], "terminal")
    resources.reconcile(rig.db, rig.sf)
    result = rig.ctl.process_once(execute=rig.execute)
    assert result[0]["outcome"] == "error" and not rig.calls
    assert (outside/"precious").read_text() == "keep"


def test_paused_run_keeps_index_and_resume_reuses_worktree(rig, monkeypatch):
    rec = rig.run()
    rig.ctl.process_once(execute=rig.execute)
    original = rig.sf.get_run
    monkeypatch.setattr(rig.sf, "get_run", lambda rid: {**original(rid), "status": "paused"})
    resources.reconcile(rig.db, rig.sf)
    assert rig.ctl.get(rec["run_id"])["desired"] == "ready"
    assert not ri.record(rig.db, rec["run_id"])["released_at"]
    monkeypatch.setattr(rig.sf, "get_run", original)
    resumed = ri.ensure_for_run(rig.db, run_id=rec["run_id"], project_id=rec["project_id"], config_name="resource")
    assert resumed["worktree_path"] == rec["worktree_path"]
    assert rig.ctl.process_once(execute=rig.execute) == []


def test_read_snapshot_has_the_same_index_and_idle_lifecycle(rig):
    rec = rig.run(mode=ri.MODE_READ_SNAPSHOT)
    assert rec["mode"] == ri.MODE_READ_SNAPSHOT
    finish(rig, rec)
    result = ri.reap_released_worktrees(rig.db, rig.sf, now=time.time()+90000)
    assert result["removed"] and rig.source.exists()
