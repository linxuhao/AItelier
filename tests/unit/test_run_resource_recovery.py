"""Recovery and review regressions; all state and Git roots are temporary."""
import logging
import os
import signal
import subprocess
import time
from pathlib import Path

from core import run_isolation as ri
from core import run_resources as resources
from tests.unit.test_run_index_resources import rig as _rig
from tests.unit.test_zvec_indexer import ENTRYPOINT, setup

rig = _rig  # Reuse the isolated fixture without importing a shadowed name.


def test_index_control_failure_does_not_block_run_isolation(rig, monkeypatch, caplog):
    def unavailable():
        raise OSError("fixture control storage is unavailable")
    monkeypatch.setattr(resources, "control", unavailable)
    with caplog.at_level(logging.WARNING):
        rec = rig.run()
    assert Path(rec["worktree_path"]).is_dir()
    assert rig.sf.get_run(rec["run_id"])["status"] == "running"
    assert "semantic_index_prepare_degraded" in caplog.text
    assert rec["run_id"] in caplog.text


def test_all_lease_reconciliation_exposes_index_refusals_without_a_checkout_lease(rig, monkeypatch):
    rec = rig.run()
    rig.sf.stop_run(rec["run_id"], "fixture terminal")
    monkeypatch.setattr(rig.sf, "audit_operation_owners", lambda _: {})
    result = ri.reconcile_all_leases(rig.db, rig.sf)
    assert result["index_resources"]["retained"][0]["run_id"] == rec["run_id"]
    assert "malformed" in result["index_resources"]["retained"][0]["reason"]
    assert rig.ctl.get(rec["run_id"])["desired"] == "ready"


def test_sidecar_launcher_execs_the_tracked_worker_pid(tmp_path):
    _, _, env = setup(tmp_path)
    script = tmp_path / "blocking_worker.py"
    pidfile = tmp_path / "worker.pid"
    script.write_text(
        "import os, signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, lambda *_: exit(0))\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.05)\n"
    )
    env["AITELIER_ZG_WORKER_SCRIPT"] = str(script)
    proc = subprocess.Popen(["sh", "-c", f'. "{ENTRYPOINT}"; process_index_requests'], env=env)
    worker_pid = None
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pidfile.exists(), "temporary worker failed to start"
        worker_pid = int(pidfile.read_text())
        assert worker_pid == proc.pid, "TERM would target an intermediate shell, not its worker"
        proc.terminate()
        assert proc.wait(timeout=5) == 0
    finally:
        if worker_pid and worker_pid != proc.pid:
            try:
                os.kill(worker_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)


def test_retention_reasons_are_visible_coalesced_and_removed_after_resolution(monkeypatch):
    from core import scheduler
    events = []
    monkeypatch.setattr(scheduler, "_resource_sweep_reported", {})
    monkeypatch.setattr(scheduler, "tick_log", lambda *args, **kwargs: events.append((args, kwargs)))
    item = {"run_id": "fixture-run", "reason": "idle 1s; minimum 86400s"}
    scheduler._log_resource_retention("worktree_retained", [item])
    scheduler._log_resource_retention("worktree_retained", [{**item, "reason": "idle 5s; minimum 86400s"}])
    assert len(events) == 1
    scheduler._log_resource_retention("worktree_retained", [{**item, "reason": "uncommitted work"}])
    assert len(events) == 2 and events[-1][1]["reason"] == "uncommitted work"
    scheduler._log_resource_retention("worktree_retained", [])
    assert scheduler._resource_sweep_reported == {}


def test_startup_logs_refused_semantic_release(rig, monkeypatch):
    from core import scheduler
    rec = rig.run()
    rig.sf.stop_run(rec["run_id"], "fixture terminal")
    monkeypatch.setattr(rig.sf, "audit_operation_owners", lambda _: {})
    monkeypatch.setattr(scheduler, "db", rig.db)
    monkeypatch.setattr(scheduler, "_resource_sweep_reported", {})
    events = []
    monkeypatch.setattr(scheduler, "tick_log", lambda *args, **kwargs: events.append((args, kwargs)))
    scheduler.recover_leases_on_startup()
    assert any(args[1] == "semantic_index_retained" and "malformed" in kw["reason"]
               for args, kw in events)


def test_unknown_index_leftovers_are_not_deleted_with_the_checkout(rig):
    from tests.unit.test_run_index_resources import finish
    rec = rig.run()
    finish(rig, rec)
    cache = Path(rec["worktree_path"]) / ".zvec-grep"
    cache.mkdir()
    precious = cache / "unknown-report.txt"
    precious.write_text("unclassified evidence")
    result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert not result["removed"]
    assert precious.read_text() == "unclassified evidence"


def test_another_worktrees_gitfile_is_not_valid_ownership(rig):
    import pytest

    from core.semantic_index_control import validate_checkout
    one, two = rig.run("one"), rig.run("two")
    first = Path(one["worktree_path"]) / ".git"
    first.write_text((Path(two["worktree_path"]) / ".git").read_text())
    with pytest.raises(ValueError, match="ownership|linked|backlink"):
        validate_checkout(one, resources.datadir.worktrees_dir())


def test_busy_resource_has_no_unleased_fallback_read(rig, monkeypatch):
    from aitelier.tools.semantic_search import impl
    rec = rig.run()
    calls = []
    monkeypatch.setattr(impl, "_call", lambda *a: calls.append("semantic"))
    monkeypatch.setattr(impl, "_lexical_fallback", lambda *a: calls.append("literal"))
    with rig.ctl.lock(rec["run_id"]):
        result = impl.semantic_search("source", project_root=rec["worktree_path"])
    assert result["mode"] == "unavailable" and result["degraded"]
    assert calls == []


def test_missing_index_returns_literal_results_and_schedules_repair(rig, monkeypatch):
    from aitelier.tools.semantic_search import impl
    rec = rig.run()
    rig.ctl.process_once(execute=rig.execute)
    before = rig.ctl.get(rec["run_id"])["revision"]
    monkeypatch.setattr(impl, "_call", lambda *_: {"error": {"message": "INDEX_MISSING"}})
    result = impl.semantic_search("base_function", project_root=rec["worktree_path"])
    assert result["mode"] == "literal_fallback" and "base_function" in result["content"]
    assert rig.ctl.get(rec["run_id"])["revision"] == before + 1
    assert not rig.ctl.settled(rig.ctl.get(rec["run_id"]), "ready")
    rig.ctl.process_once(execute=rig.execute)
    assert rig.ctl.settled(rig.ctl.get(rec["run_id"]), "ready")


def test_resource_lock_excludes_a_separate_sidecar_process(rig):
    import json
    import sys
    rec = rig.run()
    code = ("import json,sys; from core.semantic_index_control import IndexControl; "
            "ctl=IndexControl(sys.argv[1],sys.argv[2]); print(json.dumps(ctl.process_once()))")
    with rig.ctl.lock(rec["run_id"], exclusive=False):
        child = subprocess.run([sys.executable, "-c", code, str(rig.ctl.directory),
                                str(resources.datadir.worktrees_dir())],
                               capture_output=True, text=True, check=True, timeout=10)
    result = json.loads(child.stdout)
    assert result[0]["outcome"] == "busy"
    assert not (Path(rec["worktree_path"]) / ".zvec-grep").exists()
