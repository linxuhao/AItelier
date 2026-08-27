"""Restart recovery must not manufacture the wedge it is there to clear.

Three incidents on jinyong-hud, 2026-08-27, all from one restart path:

  * a checkpoint became unanswerable — `current_node` was nulled on a PAUSED
    run, and the host identifies the pending checkpoint from that pointer;
  * a task-loop card was silently dropped and then passed by a review over
    nothing — the reaped claim's row is keyed by step id, so the next claim
    stamped it with a different item;
  * a project stopped advancing for 66 minutes — an OLD step instance was
    revived alongside the live one, the engine claimed the zombie, the
    completion landed on the live row, and the zombie stayed 'claimed'. Its
    owner is the server itself, so the liveness probe called it in flight
    forever.
"""
import sqlite3
import threading

import pytest

from core import scheduler


_SCHEMA = """
CREATE TABLE skillflow_steps (
    id INTEGER PRIMARY KEY, run_id TEXT, step_id TEXT, status TEXT,
    version INTEGER DEFAULT 1, claimed_at TEXT, claimed_by TEXT,
    last_error TEXT, updated_at TEXT);
CREATE TABLE skillflow_runs (
    id TEXT PRIMARY KEY, status TEXT, current_node TEXT, updated_at TEXT);
"""


class _SF:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()

    def step(self, id, run, step, status, claimed_by=None, claimed_at=None):
        self._conn.execute(
            "INSERT INTO skillflow_steps (id, run_id, step_id, status, "
            "claimed_by, claimed_at) VALUES (?,?,?,?,?,?)",
            (id, run, step, status, claimed_by, claimed_at))
        self._conn.commit()

    def run(self, id, status, node):
        self._conn.execute(
            "INSERT INTO skillflow_runs (id, status, current_node) VALUES (?,?,?)",
            (id, status, node))
        self._conn.commit()

    def rows(self):
        return {r["id"]: dict(r) for r in
                self._conn.execute("SELECT * FROM skillflow_steps")}

    def run_row(self, id):
        return dict(self._conn.execute(
            "SELECT * FROM skillflow_runs WHERE id = ?", (id,)).fetchone())


@pytest.fixture
def sf(monkeypatch):
    s = _SF()
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: s)
    return s


# ── startup recovery ─────────────────────────────────────────────────────────

def test_only_the_newest_instance_of_a_step_is_reopened(sf):
    # The claim UPDATE is keyed by (run_id, step_id), so a revived OLD instance
    # is not history — it is a second claimable row for the same node.
    sf.run("r1", "running", "5_vision_human")
    sf.step(10, "r1", "5_vision_human", "claimed", "tool-inline host=x")
    sf.step(20, "r1", "5_vision_human", "claimed", "tool-inline host=x")

    scheduler.recover_claims_on_startup()

    rows = sf.rows()
    assert rows[20]["status"] == "pending", "the live instance must be reopened"
    assert rows[10]["status"] == "failed", "the superseded one must not be claimable"
    assert "superseded" in (rows[10]["last_error"] or "")


def test_a_single_claim_is_still_simply_reopened(sf):
    sf.run("r1", "running", "3")
    sf.step(10, "r1", "3", "claimed", "worker host=x")

    scheduler.recover_claims_on_startup()

    assert sf.rows()[10]["status"] == "pending"
    assert sf.rows()[10]["claimed_by"] is None


def test_different_steps_are_each_reopened(sf):
    sf.run("r1", "running", "3")
    sf.step(10, "r1", "3", "claimed", "worker host=x")
    sf.step(11, "r1", "t_impl", "claimed", "worker host=x")

    scheduler.recover_claims_on_startup()

    assert sf.rows()[10]["status"] == "pending"
    assert sf.rows()[11]["status"] == "pending"


def test_a_paused_run_keeps_its_current_node(sf):
    # The pointer is how the host finds the pending checkpoint. Nulling it made
    # the checkpoint unanswerable from every surface, with no way to resume.
    sf.run("r1", "paused", "5_vision_judged")
    sf.step(10, "r1", "5_vision_human", "claimed", "tool-inline host=x")

    scheduler.recover_claims_on_startup()

    assert sf.run_row("r1")["current_node"] == "5_vision_judged"


def test_a_running_run_still_loses_its_pointer(sf):
    # For a RUNNING run the reaped claim really does invalidate the pointer;
    # skillflow re-resolves from scratch. Unchanged on purpose.
    sf.run("r1", "running", "t_impl")
    sf.step(10, "r1", "t_impl", "claimed", "worker host=x")

    scheduler.recover_claims_on_startup()

    assert sf.run_row("r1")["current_node"] is None


# ── the immortal inline claim ────────────────────────────────────────────────

def _live_owner(monkeypatch, dead):
    monkeypatch.setattr(scheduler, "owner_is_dead", lambda o: dead)


def test_an_abandoned_inline_claim_does_not_block_forever(sf, monkeypatch):
    # `tool-inline` is claimed by the server itself, so the owner probe says
    # "alive" for as long as the server runs. 66 minutes of active_claim on a
    # project that had nothing running.
    _live_owner(monkeypatch, False)          # owner alive — the probe's answer
    sf.run("r1", "running", "3")
    sf.step(10, "r1", "5_vision_human", "claimed",
            "tool-inline host=x pid=7", "2020-01-01T00:00:00Z")

    assert scheduler._has_active_claim(sf, "r1") is False


def test_a_fresh_inline_claim_is_still_in_flight(sf, monkeypatch):
    import time
    _live_owner(monkeypatch, False)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sf.run("r1", "running", "5_vision_human")
    sf.step(10, "r1", "5_vision_human", "claimed", "tool-inline host=x", now)

    assert scheduler._has_active_claim(sf, "r1") is True


def test_a_long_agent_turn_is_still_in_flight(sf, monkeypatch):
    # The whole point of the identity probe: agent steps legitimately run past
    # any clock window (measured 1367 s). Only inline claims lose it.
    _live_owner(monkeypatch, False)
    sf.run("r1", "running", "3")
    sf.step(10, "r1", "3", "claimed", "worker host=x pid=7", "2020-01-01T00:00:00Z")

    assert scheduler._has_active_claim(sf, "r1") is True


def test_a_dead_worker_is_not_in_flight(sf, monkeypatch):
    _live_owner(monkeypatch, True)
    sf.run("r1", "running", "3")
    sf.step(10, "r1", "3", "claimed", "worker host=x pid=7", "2020-01-01T00:00:00Z")

    assert scheduler._has_active_claim(sf, "r1") is False
