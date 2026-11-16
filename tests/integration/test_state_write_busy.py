"""A State write that cannot take SQLite's write lock refuses; it never 500s.

THE INCIDENT (`iss-434bc51c246d4a5a`, 2026-11-15): one of the owner's dashboard
answers was lost. `POST /api/state/commands/send_user_conversation_message` lost
the race for SQLite's write lock, `BEGIN IMMEDIATE` ran out its busy timeout and
`sqlite3.OperationalError('database is locked')` escaped `core/state_graph.py`'s
`transaction()`, where `api/state_http.py` turned it into an opaque HTTP 500 -
an answer the caller could neither act on nor safely repeat.

What is pinned here, all through the REAL command endpoint
(`create_state_router` -> `POST /api/state/commands/{action}` -> `execute` ->
`StateGraphStore.transaction`), with a real SQLite database in `tmp_path`:

Arm A - a foreign sqlite3 connection holds the write lock past the budget: the
        response is 503 with `Retry-After` and the machine-readable busy code,
        NOT 500, and no `sqlite3.OperationalError` escapes the transaction.
        Nothing was written.
Arm B - no foreign lock: 200 and exactly one row for that request_key, and
        replaying the identical request_key still leaves exactly one row. A lock
        RELEASED inside the budget is waited out (the acquisition loop really
        retries `BEGIN IMMEDIATE`) and the same write succeeds.
Body   - a failure raised AFTER `BEGIN` succeeds is not retried: it fires
        exactly once and its original error propagates untouched.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.state_http import create_state_router
from core import state_graph
from core.driver_inbox import DriverInbox
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_graph import StateGraphError, StateGraphStore, StateWriteBusy
from core.state_service import StateService

COMMAND = "/api/state/commands/send_user_conversation_message"
OWNER = "owner:user@example.test"
BODY = "node a first"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A real State-only database: one project with one member driver."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, "state-write-busy-pepper-not-a-secret")
    registry.register("ada", "Ada", actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    return SimpleNamespace(db=db,
                           service=StateService(db, actor=OWNER, project_read_trusted=True))


def client(world):
    """The production router, exactly as `api/state_graph_routers` builds it."""
    app = FastAPI()
    app.include_router(create_state_router(lambda: world.service, lambda: None))
    return TestClient(app)


def body(request_key, **overrides):
    return {"request_key": request_key, "project_id": "alpha", "body": BODY, **overrides}


def stored(world, request_key):
    """(rows in the idempotency table, message ids that request wrote)."""
    with world.db.get_connection() as conn:
        requests = conn.execute(
            "SELECT COUNT(*) FROM driver_inbox_requests WHERE actor=? AND "
            "operation='send_user_conversation' AND request_key=?", (OWNER, request_key)).fetchone()[0]
        messages = [row["message_id"] for row in conn.execute(
            "SELECT message_id FROM driver_inbox_messages WHERE project_id='alpha' AND body=? "
            "ORDER BY message_id", (BODY,))]
    return requests, messages


class _ObservedConnection:
    """The acquisition loop's view of one connection: records what it ASKED for."""

    def __init__(self, conn, record):
        self._conn, self._record = conn, record

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA busy_timeout="):
            self._record["timeouts"].append(int(sql.split("=", 1)[1]))
        elif sql.startswith("BEGIN IMMEDIATE"):
            self._record["begins"] += 1
        return self._conn.execute(sql, *args)


def observe_acquisition(monkeypatch):
    """Record every `PRAGMA busy_timeout` and `BEGIN IMMEDIATE` the loop issues.

    One record per `_begin_write` call, i.e. per write transaction, so a body
    that ran twice or a `BEGIN` that was retried is visible in the counts.
    """
    real = state_graph._begin_write
    calls = []

    def observed(conn):
        record = {"timeouts": [], "begins": 0}
        calls.append(record)
        return real(_ObservedConnection(conn, record))

    monkeypatch.setattr(state_graph, "_begin_write", observed)
    return calls


def observe_escapes(monkeypatch):
    """Every exception that leaves `StateGraphStore.transaction`, in order."""
    real = StateGraphStore.transaction
    escapes = []

    @contextmanager
    def observed(self, **kwargs):
        try:
            with real(self, **kwargs) as conn:
                yield conn
        except BaseException as error:
            escapes.append(error)
            raise

    monkeypatch.setattr(StateGraphStore, "transaction", observed)
    return escapes


def hold_write_lock(world):
    """A FOREIGN sqlite3 connection (not the store's) holding the write lock."""
    holder = sqlite3.connect(world.db.db_path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    return holder


def test_a_foreign_lock_past_the_budget_answers_retryable_503(world, monkeypatch):
    """Arm A: the lock is never ours; the caller gets a retryable answer, not a 500."""
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "0.4")
    calls = observe_acquisition(monkeypatch)
    escapes = observe_escapes(monkeypatch)
    holder = hold_write_lock(world)
    try:
        with client(world) as http:
            started = time.monotonic()
            response = http.post(COMMAND, json=body("arm-a"))
            elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    # 503, not 500, and not a validation refusal: nothing about the request was
    # malformed, the lock simply was not available.
    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == str(state_graph.STATE_WRITE_BUSY_RETRY_AFTER)
    payload = response.json()
    assert payload["code"] == state_graph.STATE_WRITE_BUSY_CODE == "state_write_busy"
    assert payload["retry_after"] == state_graph.STATE_WRITE_BUSY_RETRY_AFTER
    assert "state_write_busy" in payload["detail"]

    # The typed error is what left the transaction; no sqlite3.OperationalError
    # escaped it for this request.
    assert escapes, "no exception left the write transaction"
    assert all(isinstance(error, StateWriteBusy) for error in escapes), escapes
    assert not any(isinstance(error, sqlite3.OperationalError) for error in escapes)

    # The per-attempt patience followed the shrunken budget (never the fixed
    # 5000 ms), and the whole wait stayed inside the bound.
    assert len(calls) == 1 and calls[0]["begins"] >= 1, calls
    assert all(1 <= ms <= 400 for ms in calls[0]["timeouts"]), calls[0]["timeouts"]
    assert elapsed < 5, f"the bounded wait took {elapsed:.3f}s"

    # A busy write wrote nothing: not the message, not the idempotency row.
    assert stored(world, "arm-a") == (0, [])


def test_no_contention_stores_exactly_one_row_per_request_key(world, monkeypatch):
    """Arm B: with the lock free the same write still lands, once, replay included."""
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "3")
    with client(world) as http:
        first = http.post(COMMAND, json=body("arm-b"))
        replay = http.post(COMMAND, json=body("arm-b"))

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["message_id"] == replay.json()["message_id"]
    assert first.json()["deliveries"] == replay.json()["deliveries"]

    requests, messages = stored(world, "arm-b")
    assert requests == 1, "the request_key was recorded more than once"
    assert messages == [first.json()["message_id"]], messages


def test_a_lock_released_inside_the_budget_is_waited_out(world, monkeypatch):
    """Arm B, contended: the acquisition loop retries BEGIN and the write lands.

    The per-attempt patience is shrunk so the OUTER loop - not SQLite's internal
    busy handler - is what waits the lock out: with `STATE_WRITE_LOCK_ATTEMPT_MS`
    at 100 the first attempt gives up long before the release, so a second
    `BEGIN IMMEDIATE` (and a third) is the only way this request can succeed.
    """
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "3")
    monkeypatch.setattr(state_graph, "STATE_WRITE_LOCK_ATTEMPT_MS", 100)
    calls = observe_acquisition(monkeypatch)
    holder = hold_write_lock(world)
    release = threading.Timer(0.35, holder.rollback)
    release.start()
    try:
        with client(world) as http:
            started = time.monotonic()
            response = http.post(COMMAND, json=body("arm-b-late"))
            elapsed = time.monotonic() - started
    finally:
        release.cancel()
        release.join()
        holder.rollback()
        holder.close()

    assert response.status_code == 200, response.text
    assert elapsed > 0.2, f"the request did not wait for the lock ({elapsed:.3f}s)"
    assert len(calls) == 1, calls
    assert calls[0]["begins"] >= 2, f"BEGIN IMMEDIATE was not retried: {calls[0]}"
    assert all(1 <= ms <= 100 for ms in calls[0]["timeouts"]), calls[0]["timeouts"]

    requests, messages = stored(world, "arm-b-late")
    assert requests == 1 and messages == [response.json()["message_id"]]


def test_a_failure_after_begin_succeeds_is_not_retried(world, monkeypatch):
    """The retry covers `BEGIN IMMEDIATE`, never the body of the transaction.

    `BEGIN IMMEDIATE` fails BEFORE any body statement runs, which is why it is
    the only statement that may be repeated. A failure raised after it succeeded
    already has side effects inside the transaction, so it must fire exactly
    once and propagate untouched - here both an error that LOOKS like contention
    and a real constraint violation.
    """
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "3")
    calls = observe_acquisition(monkeypatch)

    # (1) The body raises the very text a lock retry keys on. It is not retried
    # and is not relabelled as busy: the original error is what the caller sees.
    burns = []
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        with world.service.store.transaction(write=True) as conn:
            burns.append("body")
            raise sqlite3.OperationalError("database is locked")
    assert burns == ["body"], f"the body ran {len(burns)} times"
    assert len(calls) == 1 and calls[0]["begins"] == 1, calls

    # (2) Through the real command route: a constraint violation raised AFTER
    # the message INSERT fires once, rolls the whole transaction back, and is
    # never turned into a retryable busy answer.
    real_record = DriverInbox._record
    fired = []

    def refuse(self, conn, operation, request_key, payload, result):
        fired.append(request_key)
        real_record(self, conn, operation, request_key, payload, result)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: driver_inbox_requests.request_key")

    monkeypatch.setattr(DriverInbox, "_record", refuse)
    with client(world) as http:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
            http.post(COMMAND, json=body("body-side"))

    assert fired == ["body-side"], f"the write body ran {len(fired)} times"
    assert stored(world, "body-side") == (0, []), "a rolled-back write left a row behind"
    assert len(calls) == 2 and calls[1]["begins"] == 1, calls


def test_the_bound_is_one_constant_of_at_most_ten_seconds(monkeypatch):
    """The total wait is bounded by ONE named constant, and the override only shrinks."""
    assert state_graph.STATE_WRITE_LOCK_SECONDS <= 10.0
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "60")
    assert state_graph._write_lock_budget() == state_graph.STATE_WRITE_LOCK_SECONDS
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "0.25")
    assert state_graph._write_lock_budget() == 0.25
    for unusable in ("", "not-a-number", "0", "-1", "nan"):
        monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, unusable)
        assert state_graph._write_lock_budget() == state_graph.STATE_WRITE_LOCK_SECONDS, unusable
    monkeypatch.delenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV)
    assert state_graph._write_lock_budget() == state_graph.STATE_WRITE_LOCK_SECONDS


def test_the_typed_refusal_is_a_graph_error_that_names_the_code_and_the_wait():
    """A text-only transport (the MCP tool surface) has no status codes.

    `api/state_graph_tools.invoke` catches `StateGraphError` and shows its text,
    so the TYPE and the LEADING CODE are what make this same refusal legible
    there - and the retry hint rides in the message as well as in `retry_after`,
    which only an HTTP transport can turn into a header.
    """
    busy = StateWriteBusy(f"{state_graph.STATE_WRITE_BUSY_CODE}: held for the budget")
    assert isinstance(busy, StateGraphError)
    assert busy.code == "state_write_busy"
    assert busy.retry_after == state_graph.STATE_WRITE_BUSY_RETRY_AFTER >= 1
    assert str(busy).startswith("state_write_busy")
