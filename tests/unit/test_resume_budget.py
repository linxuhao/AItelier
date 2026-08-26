"""Resuming a failed run must give its blocker the retry budget back.

A step reaches status 'failed' only by exhausting max_retries, and
skillflow's reactivate_run resets the last COMPLETED step — so the one row
that blocks the resume is the one row the resume does not touch. Live on
2026-08-26: 5_review stuck at retry_count 3/3 after a provider quota outage;
the quota reopened, the run could not.
"""
import sqlite3

import pytest

from core.run_driver import restore_retry_budget


class _FakeSF:
    """Just enough skillflow: the two tables the helper writes."""

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            CREATE TABLE skillflow_steps (
                id INTEGER PRIMARY KEY, run_id TEXT, step_id TEXT,
                status TEXT, retry_count INT, max_retries INT,
                version INT DEFAULT 1, claimed_at TEXT, claimed_by TEXT,
                updated_at TEXT);
            CREATE TABLE skillflow_runs (
                id TEXT PRIMARY KEY, current_node TEXT, updated_at TEXT);
        """)

    def add_step(self, sid, run, step, status, retries=0, max_retries=3):
        self._conn.execute(
            "INSERT INTO skillflow_steps (id, run_id, step_id, status, "
            "retry_count, max_retries, claimed_by) VALUES (?,?,?,?,?,?,?)",
            (sid, run, step, status, retries, max_retries, "pid:123"))
        self._conn.commit()

    def add_run(self, run, node=None):
        self._conn.execute(
            "INSERT INTO skillflow_runs (id, current_node) VALUES (?,?)",
            (run, node))
        self._conn.commit()

    def step(self, sid):
        return dict(self._conn.execute(
            "SELECT * FROM skillflow_steps WHERE id = ?", (sid,)).fetchone())

    def run(self, rid):
        return dict(self._conn.execute(
            "SELECT * FROM skillflow_runs WHERE id = ?", (rid,)).fetchone())


RUN = "c6dce51c"


@pytest.fixture
def sf():
    f = _FakeSF()
    f.add_run(RUN, node="5_knowledge")     # where reactivate_run leaves it
    f.add_step(1456, RUN, "5_knowledge", "completed")
    f.add_step(1457, RUN, "5_review", "failed", retries=3, max_retries=3)
    return f


def test_exhausted_step_becomes_retryable_again(sf):
    """Without this the next attempt takes the 'retries exhausted' branch."""
    assert sf.step(1457)["retry_count"] == 3          # the blocker, as found
    restore_retry_budget(sf, RUN)
    row = sf.step(1457)
    assert row["status"] == "pending"
    assert row["retry_count"] == 0


def test_current_node_points_at_the_step_that_must_rerun(sf):
    """reactivate_run leaves it on the last completed step; the blocker wins."""
    restore_retry_budget(sf, RUN)
    assert sf.run(RUN)["current_node"] == "5_review"


def test_stale_claim_ownership_is_cleared(sf):
    """A row left owned by a dead process is not claimable by anyone else."""
    restore_retry_budget(sf, RUN)
    assert sf.step(1457)["claimed_by"] is None


def test_version_is_bumped_so_a_zombie_cannot_confirm(sf):
    before = sf.step(1457)["version"]
    restore_retry_budget(sf, RUN)
    assert sf.step(1457)["version"] == before + 1


def test_it_reports_what_it_reset(sf):
    got = restore_retry_budget(sf, RUN)
    assert got == {"step": "5_review", "instance": 1457,
                   "was_retry_count": 3, "max_retries": 3}


def test_completed_steps_are_left_alone(sf):
    restore_retry_budget(sf, RUN)
    assert sf.step(1456)["status"] == "completed"


def test_no_failed_step_is_not_an_error(sf):
    """A run that failed without a failed step row (routing dead end) resumes
    on reactivate_run alone — say so by returning None, don't invent a reset."""
    clean = _FakeSF()
    clean.add_run("r2", node="t_impl")
    clean.add_step(1, "r2", "t_impl", "completed")
    assert restore_retry_budget(clean, "r2") is None
    assert clean.run("r2")["current_node"] == "t_impl"


def test_only_this_runs_failure_is_touched(sf):
    """Two runs can be failed at once; a retry must not resurrect the neighbour."""
    sf.add_run("other")
    sf.add_step(2000, "other", "3_review", "failed", retries=3)
    restore_retry_budget(sf, RUN)
    assert sf.step(2000)["status"] == "failed"
    assert sf.step(2000)["retry_count"] == 3


def test_newest_failure_wins_when_a_step_failed_more_than_once(sf):
    """Loops re-open a step as a NEW instance; the old row stays failed."""
    sf.add_step(1600, RUN, "5_review", "failed", retries=3)
    got = restore_retry_budget(sf, RUN)
    assert got["instance"] == 1600
    assert sf.step(1457)["status"] == "failed"   # the older instance is history
