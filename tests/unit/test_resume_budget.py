"""Only the selected current failure may regain its retry budget.

Historical failed rows and successful loop passes remain immutable on recovery.
"""
import sqlite3

import pytest

from core.run_driver import restore_retry_budget


class _FakeSF:
    """Just enough skillflow: the two tables the helper writes.

    `validation_retry_count` and `inputs_json` are here because leaving them
    out is what let a real bug through. skillflow spends ONE budget across both
    counters (core.py: `total_retries = retry_count + validation_retry_count`),
    and the helper originally zeroed only the first — so a step that failed
    through validation exhaustion came back with its budget still spent. The
    fake schema had no such column, so no test could see it. Keep this mirroring
    the columns the helper touches.
    """

    def __init__(self):
        import threading
        # The ENGINE lock. Host code that writes on skillflow's shared
        # connection must hold it — `_tx` spans BEGIN IMMEDIATE…commit on
        # that same connection, so a host write without it can commit a
        # transaction the engine meant to roll back.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            CREATE TABLE skillflow_steps (
                id INTEGER PRIMARY KEY, run_id TEXT, step_id TEXT,
                status TEXT, retry_count INT, validation_retry_count INT
                DEFAULT 0, max_retries INT, inputs_json TEXT,
                version INT DEFAULT 1, claimed_at TEXT, claimed_by TEXT,
                -- `release_claim` counts driver cancellations here. It is
                -- NOT charged against the retry budget, so a budget restore
                -- deliberately leaves it alone: it is the only record that a
                -- step was repeatedly abandoned.
                release_count INT DEFAULT 0,
                updated_at TEXT);
            CREATE TABLE skillflow_runs (
                id TEXT PRIMARY KEY, current_node TEXT, updated_at TEXT);
        """)

    def add_step(self, sid, run, step, status, retries=0, max_retries=3,
                 validation_retries=0, inputs_json=None):
        self._conn.execute(
            "INSERT INTO skillflow_steps (id, run_id, step_id, status, "
            "retry_count, validation_retry_count, max_retries, inputs_json, "
            "claimed_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, run, step, status, retries, validation_retries, max_retries,
             inputs_json, "pid:123"))
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
    f.add_run(RUN, node="5_review")        # the failed current node selected by reactivate_run
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
    """Budget restoration keeps the selected failed node."""
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
    got.pop("also_restored"); got.pop("was_validation_retry_count")
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


def test_unrelated_failed_step_is_not_resurrected(sf):
    sf.add_step(1500, RUN, "t_impl", "failed", retries=3)
    before = sf.step(1500)
    got = restore_retry_budget(sf, RUN)
    assert sf.step(1457)["status"] == "pending"
    assert sf.step(1500) == before
    assert got["also_restored"] == []


def test_superseded_failure_and_completed_current_are_untouched(sf):
    sf.add_step(1500, RUN, "5_review", "completed")
    before = [sf.step(i) for i in (1456, 1457, 1500)]
    assert restore_retry_budget(sf, RUN) is None
    assert [sf.step(i) for i in (1456, 1457, 1500)] == before


def test_validation_exhaustion_gets_its_budget_back(sf):
    """skillflow spends ONE budget across retry_count + validation_retry_count.

    A step that failed through validation carries retry_count=0 and
    validation_retry_count=max, so zeroing only the first leaves
    total_retries == max_allowed: the resumed step dies on its first validation
    failure and 'retry' silently did nothing all over again.
    """
    sf.add_step(1700, RUN, "5_review", "failed", retries=0, validation_retries=3)
    restore_retry_budget(sf, RUN)
    row = sf.step(1700)
    assert row["retry_count"] == 0
    assert row["validation_retry_count"] == 0, (
        "skillflow adds the two together; leaving this at the cap restores "
        "nothing")


def test_the_stale_validation_complaint_is_dropped(sf):
    """Otherwise the resumed attempt is re-prompted with the error that killed
    the previous one, and 'fix this' points at a complaint about old output."""
    import json

    sf.add_step(1800, RUN, "5_review", "failed", retries=3,
                inputs_json=json.dumps({"_validation_error": "missing foo.py",
                                        "keep": "me"}))
    restore_retry_budget(sf, RUN)
    inputs = json.loads(sf.step(1800)["inputs_json"])
    assert "_validation_error" not in inputs
    assert inputs["keep"] == "me", "unrelated inputs must survive"


def test_it_survives_an_engine_whose_schema_predates_release_count():
    """Raw SQL from the HOST against the ENGINE's table, and the two ship apart.

    AItelier is bind-mounted and goes live on the next container restart;
    skillflow arrives from PyPI whenever it is published. Naming `release_count`
    unconditionally broke resume outright against the deployed engine — every
    failed run, `OperationalError: no such column` — while the dev box, whose
    editable checkout has the column, stayed green. That asymmetry is the
    repo's documented cross-repo-skew class, so the guard belongs in a test
    that builds the OLD schema on purpose.
    """
    import sqlite3
    from core.run_driver import restore_retry_budget

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE skillflow_steps (
            id INTEGER PRIMARY KEY, run_id TEXT, step_id TEXT, status TEXT,
            retry_count INT, validation_retry_count INT DEFAULT 0,
            max_retries INT, inputs_json TEXT, version INT DEFAULT 1,
            claimed_at TEXT, claimed_by TEXT, updated_at TEXT);
        CREATE TABLE skillflow_runs (
            id TEXT PRIMARY KEY, current_node TEXT, updated_at TEXT);
        INSERT INTO skillflow_steps
            (id, run_id, step_id, status, retry_count, max_retries, inputs_json)
            VALUES (1, 'r1', 'a', 'failed', 3, 3, '{}');
        INSERT INTO skillflow_runs VALUES ('r1', 'a', '');
    """)
    conn.commit()

    import threading

    class _SF:
        pass
    sf = _SF()
    sf._lock = threading.RLock()
    sf._conn = conn

    out = restore_retry_budget(sf, "r1")

    assert out is not None and out["step"] == "a"
    row = conn.execute("SELECT status, retry_count FROM skillflow_steps "
                       "WHERE id = 1").fetchone()
    assert (row["status"], row["retry_count"]) == ("pending", 0)


def test_completed_card_history_survives_real_framework_and_host_recovery():
    from skillflow import SkillFlow
    from skillflow.graph import PipelineGraph, StepNode
    sf = SkillFlow(":memory:")
    sf.register_graph(PipelineGraph(name="recover", begin="3_review",
                                   steps=[StepNode(id="3_review", step_type="agent")]))
    run = sf.create_run("recover")
    sf.start_run(run)
    sf.advance_run(run)
    with sf._conn:
        sf._conn.execute("UPDATE skillflow_steps SET status='completed',outputs_json='{}',completion_seq=1 WHERE run_id=?", (run,))
        sf._conn.execute("INSERT INTO skillflow_steps (id,run_id,step_id,status) VALUES (4480,?,'t_impl','failed')",(run,))
        sf._conn.execute("INSERT INTO skillflow_steps (id,run_id,step_id,status,outputs_json) VALUES (4481,?,'t_impl','completed','{\"card\":\"keep\"}')",(run,))
        sf._conn.execute("INSERT INTO skillflow_steps (id,run_id,step_id,status,retry_count) VALUES (4491,?,'3_review','failed',3)",(run,))
        sf._conn.execute("UPDATE skillflow_runs SET status='failed',current_node='3_review',error_reason='Step 3_review: native turn budget exhausted' WHERE id=?",(run,))
    def history():
        return [dict(r) for r in sf._conn.execute("SELECT * FROM skillflow_steps WHERE run_id=? AND id<4491 ORDER BY id",(run,))]
    before = history()
    sf.reactivate_run(run)
    assert restore_retry_budget(sf, run) is None
    assert history() == before
    current = sf._conn.execute("SELECT status,retry_count FROM skillflow_steps WHERE id=4491").fetchone()
    assert tuple(current) == ("pending", 0)
