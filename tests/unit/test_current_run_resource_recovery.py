"""Current public isolation path, real temporary Git and durable SQLite controls.

Index teardown uses the already accepted controlled executor; no daemon/live
activation is asserted. All crash tests actually remove the temporary checkout.
"""
from pathlib import Path
import pytest

from core import run_isolation as ri, run_resources as resources
from tests.unit.test_run_index_resources import rig as rig, finish, git


def pending_removed(rig):
    rec = rig.run()
    finish(rig, rec)
    root = Path(rec["worktree_path"])
    # Persist the exact decision before real Git removal, as the reaper does.
    with rig.db.get_connection() as conn:
        conn.execute("UPDATE run_isolation SET disposition=? WHERE run_id=?",
                     ("reaping_merged_into:main", rec["run_id"]))
        conn.commit()
    ok, why = ri._remove_run_worktree(rec)
    assert ok, why
    assert not root.exists()
    return rec


@pytest.mark.parametrize("window", ["before_remove", "after_remove", "after_forget"])
def test_durable_reap_recovers_each_crash_window_and_preserves_artifacts(rig, monkeypatch, window):
    rec = rig.run()
    finish(rig, rec)
    artifact = rig.home / "workspaces" / rec["run_id"] / "delivery.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("retained delivery and evidence")
    original_remove = ri._remove_run_worktree
    original_forget = rig.ctl.forget
    monkeypatch.setattr(resources, "control", lambda: rig.ctl)
    if window in ("before_remove", "after_remove"):
        def crash(value):
            if window == "after_remove":
                ok, why = original_remove(value)
                assert ok, why
            raise SystemExit("controlled crash " + window)
        monkeypatch.setattr(ri, "_remove_run_worktree", crash)
    else:
        def crash(rid):
            original_forget(rid)
            raise SystemExit("controlled crash after_forget")
        monkeypatch.setattr(rig.ctl, "forget", crash)
    with pytest.raises(SystemExit, match="controlled crash"):
        ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert ri.record(rig.db, rec["run_id"])["disposition"] == "reaping_merged_into:main"
    monkeypatch.setattr(ri, "_remove_run_worktree", original_remove)
    monkeypatch.setattr(rig.ctl, "forget", original_forget)
    result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert [x["run_id"] for x in result["removed"]] == [rec["run_id"]]
    assert not Path(rec["worktree_path"]).exists()
    assert rig.ctl.get(rec["run_id"]) is None
    assert git(rig.source, "show-ref", "--verify", "refs/heads/" + rec["branch"])
    assert artifact.read_text() == "retained delivery and evidence"
    assert ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0) == {"removed": [], "retained": []}


@pytest.mark.parametrize("fault", ["no_intent", "wrong_path", "foreign_source", "lost_branch", "unreleased", "paused", "unknown", "inflight"])
def test_absence_never_substitutes_for_ownership_and_quiescence(rig, monkeypatch, fault):
    rec = pending_removed(rig)
    rid = rec["run_id"]
    if fault in ("no_intent", "wrong_path", "foreign_source"):
        field, value = {
            "no_intent": ("disposition", "auto_released_terminal_quiet"),
            "wrong_path": ("worktree_path", str(rig.home / "worktrees" / "foreign-root")),
            "foreign_source": ("source_repo", str(rig.home / "not-a-source")),
        }[fault]
        with rig.db.get_connection() as conn:
            conn.execute("UPDATE run_isolation SET " + field + "=? WHERE run_id=?", (value, rid))
            conn.commit()
    elif fault == "lost_branch": git(rig.source, "branch", "-D", rec["branch"])
    elif fault == "unreleased":
        with rig.ctl.connection() as conn:
            conn.execute("UPDATE indexes SET desired='ready' WHERE run_id=?", (rid,))
            conn.commit()
    elif fault in ("paused", "unknown"):
        original = rig.sf.get_run
        monkeypatch.setattr(rig.sf, "get_run", lambda run: None if fault == "unknown" else {**original(run), "status": "paused"})
    else:
        # The public engine observation is fail-closed even when its owner audit
        # reports an operation whose actual process has gone away.
        monkeypatch.setattr(rig.sf, "audit_operation_owners", lambda _: {"alive": 0, "lost": [], "unknown": ["unsettled-operation"]})
    before = dict(rig.ctl.get(rid))
    result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert not result["removed"] and result["retained"]
    assert rig.ctl.get(rid) == before
    assert not ri.record(rig.db, rid)["disposition"].startswith("reaped_")


def test_accepted_recovery_positive_then_disabling_intent_is_a_real_refusal(rig, monkeypatch):
    first = pending_removed(rig)
    result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert result["removed"] and rig.ctl.get(first["run_id"]) is None
    second = rig.run("second")
    finish(rig, second)
    with rig.db.get_connection() as conn:
        conn.execute("UPDATE run_isolation SET disposition='reaping_merged_into:main' WHERE run_id=?", (second["run_id"],))
        conn.commit()
    ok, why = ri._remove_run_worktree(second)
    assert ok, why
    monkeypatch.setattr(ri, "_reap_intent", lambda _: None)
    result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert not result["removed"] and result["retained"]
    assert rig.ctl.get(second["run_id"]) is not None


def test_current_host_write_admission_fences_reap_through_supported_api(rig):
    rec = rig.run()
    finish(rig, rec)
    with ri.write_admission(rig.db, rec["worktree_path"], kind="fixture", detail="owned temporary write"):
        result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
        assert not result["removed"] and "host write admissions" in result["retained"][0]["reason"]
        assert Path(rec["worktree_path"]).exists()
        assert rig.ctl.get(rec["run_id"]) is not None
    result = ri.reap_released_worktrees(rig.db, rig.sf, min_idle_seconds=0)
    assert result["removed"] and not Path(rec["worktree_path"]).exists()


def test_index_control_failure_does_not_weaken_current_isolation(rig, monkeypatch, caplog):
    def unavailable(): raise OSError("controlled unavailable index storage")
    monkeypatch.setattr(resources, "control", unavailable)
    rec = rig.run()
    assert Path(rec["worktree_path"]).is_dir()
    assert rig.sf.get_run(rec["run_id"])["status"] == "running"
    assert "semantic_index_prepare_degraded" in caplog.text
    assert ri.record(rig.db, rec["run_id"])["base_sha"] == rec["base_sha"]
