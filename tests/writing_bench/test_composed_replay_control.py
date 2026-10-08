"""Normal ledger_ready dispatch preserves transient recovery and cancellation."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from aitelier.writing_bench.bench import Bench
from aitelier.writing_bench.storage import git
from test_bench import bench, request
from test_early_semantic_refusal import _accept, _value, _traveler
from test_workflow import Session


def test_complete_candidate_retries_transient_io_then_releases_reviews(tmp_path, monkeypatch, bench):
    original = Bench._reset_replay
    calls = []

    def transient(self, wt, genesis_files):
        calls.append(str(wt))
        if len(calls) == 1:
            raise OSError("full replay refused: synthetic transient I/O outage")
        return original(self, wt, genesis_files)

    monkeypatch.setattr(Bench, "_reset_replay", transient)
    session = Session(tmp_path, monkeypatch, bench)
    base = git(bench.policy.repo, "rev-parse", "HEAD")
    run = session.start(request(bench))
    try:
        with pytest.raises(OSError, match="synthetic transient I/O outage"):
            session.drive(run)
        assert session.sf.get_run(run)["status"] == "running"
        assert session.agent_calls == []
        assert not (bench.work(run) / "candidate_replay.json").exists()
        assert not list((bench.root / "scratch").glob("candidate-*"))
        assert session.drive(run) == "paused"
        assert session.agent_calls == ["literary_review", "ledger_audit"]
        assert (bench.work(run) / "candidate_replay.json").is_file()
        assert (bench.work(run) / "stage.json").is_file()
        assert git(bench.policy.repo, "rev-parse", "HEAD") == base
        assert not session.backup_calls
        with session.sf._ro() as conn:
            assert conn.execute("SELECT COUNT(*) FROM skillflow_active_ops WHERE run_id=?", (run,)).fetchone()[0] == 0
    finally:
        session.sf._conn.close()


def test_complete_candidate_refusal_after_stop_keeps_stop_reason_and_cleans_checkout(tmp_path, monkeypatch, bench):
    _accept(bench, 1, _value(1, [_traveler({"status": "dead"})]))
    original = Bench._reset_replay
    entered, release = Event(), Event()
    calls = []

    def held_candidate(self, wt, genesis_files):
        calls.append(str(wt))
        # First reset checks the valid accepted baseline; second checks the
        # actual invalid candidate after the proposed chapter has been written.
        if len(calls) == 2:
            entered.set()
            assert release.wait(10), "owned candidate controller did not release"
        return original(self, wt, genesis_files)

    monkeypatch.setattr(Bench, "_reset_replay", held_candidate)
    session = Session(tmp_path, monkeypatch, bench)
    req = request(bench, n=2, value=_value(2, [_traveler({"位置": "桥边"})]))
    base = git(bench.policy.repo, "rev-parse", "HEAD")
    run = session.start(req)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            worker = executor.submit(session.drive, run)
            try:
                assert entered.wait(10)
                assert session.sf.stop_run(run, "owned exact candidate stop reason")["outcome"] == "draining"
                assert session.sf.get_run(run)["status"] == "running"
            finally:
                release.set()
            assert worker.result(timeout=10) == "failed"
        assert session.sf.get_run(run)["error_reason"] == "owned exact candidate stop reason"
        ready = next(row for row in session.sf.get_steps(run, include_payloads=True)
                     if row["step_id"] == "ledger_ready")
        assert ready["status"] == "failed" and "is dead but received changes" in ready["last_error"]
        for _ in range(3):
            session.sf.advance_run(run)
        assert len(calls) == 2 and session.agent_calls == []
        assert not (bench.work(run) / "candidate_replay.json").exists()
        assert not (bench.work(run) / "stage.json").exists()
        assert not list((bench.root / "scratch").glob("candidate-*"))
        assert "candidate-" not in git(bench.policy.repo, "worktree", "list", "--porcelain")
        assert git(bench.policy.repo, "rev-parse", "HEAD") == base
        assert git(bench.policy.repo, "status", "--porcelain") == ""
        with session.sf._ro() as conn:
            assert conn.execute("SELECT COUNT(*) FROM skillflow_active_ops WHERE run_id=?", (run,)).fetchone()[0] == 0
    finally:
        session.sf._conn.close()
