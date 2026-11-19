"""A busy database refuses TYPED and retryable, on all three driver paths.

THE INCIDENT (`iss-89b4c2a1e8c048b3`, measured on the live backend 2026-11-19
20:17Z, under a stream of small concurrent writers): one waiter starved out its
whole budget and

  * `sqlite3.OperationalError: database is locked` escaped
    `core/dsh_driver.py::DshDrivers.update()` as an untyped HTTP 500 on
    `POST /api/dsh-drivers/<project>/activity` (`api/dsh_driver_routers.py:242`
    -> `asyncio.to_thread` -> `core/dsh_driver.py:489 conn.execute`);
  * the SAME busy condition was reported as **409 Conflict** twice on
    `POST /api/state/query/wait_for_state_change`, because a wait that cannot
    record its presence row raised `ClaimError('wait_not_registered')` - and
    `ClaimError` is a `StateConflict`, which every State read route answers 409.

One condition, three answers. The repository already states the contract this
violates (`core/state_graph.py:48-77`, "refuse with a typed, retryable error
instead of letting `sqlite3.OperationalError('database is locked')` escape the
command path as a 500 the caller cannot act on") and already HAS the mechanism
(`api/state_http.py:31-62`: `state_write_busy_response` + `apply_state_write_busy`
= 503 plus `Retry-After`). What was missing is that the driver's own refusals
were not part of that family.

What is pinned here, with a REAL foreign `BEGIN IMMEDIATE` write lock on the
product's own database file (never a simulated one), and the stamp's real
`STAMP_LOCK_ATTEMPTS`/`STAMP_LOCK_PAUSE` budget:

Arm A - `DshDrivers.update()` still raises an instance of
         `sqlite3.OperationalError` whose message names the locked database -
         the contract arm (c) of `tests/unit/test_dsh_driver_activity_stamp.py`
         keeps passing - but it is ALSO the typed busy refusal, carrying
         `state_write_busy` and a `Retry-After` wait, and it wrote nothing.
Arm B - the real activity route answers 503 + `Retry-After` + the retryable code
         in the body, NOT the untyped 500.
Arm C - a wait that cannot register its presence row answers 503 + `Retry-After`
         with `wait_not_registered`, never 409, and its body does not read as a
         conflict. Nothing pretended the driver was waiting.
Arm D - a CONTROL-PLANE stamp (`enabled`: a start, a stop) whose write was
         refused never answers 200 - a start/stop that was not recorded cannot
         look successful - and it stamps no "failed start" it never attempted.

Every inactive arm is timed against the copy that really holds the lock: each
number printed below is a measured wall time, and the arm's release is its own
request finishing, never a sleep the row placed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz, dsh_driver_routers, state_http
from api.state_http import create_state_router
from core import driver_presence, dsh_driver
from core.drivers import DriverRegistry, Identity
from core.dsh_driver import DshDrivers
from core.state_database import StateDatabase
from core.state_graph import STATE_WRITE_BUSY_RETRY_AFTER, StateWriteBusy
from core.state_service import StateService

PROJECT = "alpha"
DRIVER = "dsh-alpha-witness"
START_PROJECT = "beta"
START_DRIVER = "dsh-beta-witness"
ACTIVITY = f"/api/dsh-drivers/{PROJECT}/activity"
WAIT = "/api/state/query/wait_for_state_change"

# The stamp's per-attempt patience, SHRUNK for this witness. The product budget
# is `STAMP_LOCK_ATTEMPTS` x `STAMP_BUSY_TIMEOUT_MS` (~15.4 s) and really waiting
# it out would make the suite slower without making the measurement truer: the
# lock, the refusal and the give-up are the product's, only the clock is shorter.
# `tests/unit/test_dsh_driver_activity_stamp.py` measures the real budget.
IMPATIENT_STAMP_MS = 150
# The bound on a wait whose registration cannot be written, cut the same way (the
# product value is `driver_presence.WAIT_REGISTER_SECONDS` = 10 s).
IMPATIENT_REGISTER_SECONDS = 0.5
# A CEILING, not the answer: a bounded refusal must not become an unbounded one.
CEILING_SECONDS = 5.0


class _ForeignWriteLock:
    """Another connection really holds SQLite's write lock, until `release()`.

    `BEGIN IMMEDIATE` is what takes the write lock in WAL mode; it is issued on a
    second connection (never the store's) and rolled back, so the holder changes
    NO data - every row this file asserts on afterwards is untouched by it.

    The holder ends when the arm's request does, not after a fixed sleep: the
    duration under measurement is therefore the PRODUCT's bounded wait against
    real contention, not a number the test chose. `ceiling` is only a safety net
    so a product that never returns cannot strand the thread.
    """

    def __init__(self, db_path, ceiling=60.0):
        self.db_path, self.ceiling = db_path, ceiling
        self.held, self.released = threading.Event(), threading.Event()
        self.thread = None

    def __enter__(self):
        def hold():
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.held.set()
                self.released.wait(self.ceiling)
                conn.rollback()
            finally:
                conn.close()

        self.thread = threading.Thread(target=hold, name="witness-write-lock", daemon=True)
        self.thread.start()
        assert self.held.wait(10), "the foreign connection never took the write lock"
        return self

    def release(self):
        self.released.set()
        if self.thread is not None:
            self.thread.join(30)

    def __exit__(self, *exc_info):
        self.release()
        return False


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Real State storage, the product's `DshDrivers`, and two driver rows.

    `alpha` is a live driver (`enabled=1`, phase `running`) - what the activity
    route and `stop` act on; `beta` is stopped (`enabled=0`), so a `start` has a
    control-plane stamp to write. A real `DriverRegistry` is composed because
    `_provision` refuses to run at all without one; every row this file drives
    already exists, so no provisioning (and no token minting) happens.
    """
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project(PROJECT, "Alpha")
    seed.create_project(START_PROJECT, "Beta")
    registry = DriverRegistry(db, "stamp-busy-witness-pepper-not-a-secret")
    root = tmp_path / "drivers"
    runtime = DshDrivers(db, registry, root)
    with db.get_connection() as conn:
        for project_id, driver_id, enabled, phase in (
                (PROJECT, DRIVER, 1, "running"), (START_PROJECT, START_DRIVER, 0, "stopped")):
            conn.execute("INSERT INTO dsh_drivers(project_id,driver_id,provider,model,session_id,"
                         "enabled,phase,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                         (project_id, driver_id, "fixture", "driver", "aitelier-" + driver_id,
                          enabled, phase, "2020-01-01T00:00:00+00:00"))
        conn.commit()
    # `start` builds the runtime's environment only after reading the driver's own
    # token (`read_token_file`), which refuses a file that is not mode 0600.
    for driver_id in (DRIVER, START_DRIVER):
        directory = root / driver_id
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        token = directory / "token"
        token.write_text("witness-driver-token\n")
        os.chmod(token, 0o600)
    return SimpleNamespace(db=db, runtime=runtime, root=root)


def identify(monkeypatch, driver_id):
    """The presented credential is this project's driver (the routes' auth seam)."""
    monkeypatch.setattr(authz, "request_identity",
                        lambda request: Identity("driver", "driver:" + driver_id,
                                                 driver_id=driver_id))


class _AlwaysRunning:
    """The REAL `DshDrivers`, with only `running` answered True.

    `get` and the stamp write are the product's own (`DshDrivers.get` /
    `DshDrivers.update`). `running` is replaced because in the non-host form it
    reports whether THIS process spawned the child's process - a witness has no
    child, and this arm is about the stamp, not about supervision.
    """

    def __init__(self, runtime):
        self.runtime = runtime

    def get(self, project_id):
        return self.runtime.get(project_id)

    def running(self, project_id):
        return True

    def update(self, project_id, **fields):
        return self.runtime.update(project_id, **fields)


def driver_app(runtime, monkeypatch, driver_id):
    """The REAL driver router, with only its auth inputs replaced.

    The app-wide busy default is registered by the SAME call `api/main.py:516`
    makes, so the answer under test is the product's, not one this file invented
    for the activity handler.
    """
    app = FastAPI()
    app.include_router(dsh_driver_routers.router)
    app.dependency_overrides[dsh_driver_routers.manager] = lambda: runtime
    app.dependency_overrides[authz.require_writer] = lambda: None
    state_http.apply_state_write_busy(app)
    identify(monkeypatch, driver_id)
    return app


def row(world, project_id):
    return world.runtime.get(project_id)


def test_a_refused_stamp_is_still_the_locked_database_error_and_now_typed(world, monkeypatch):
    """Arm A: the type arm (c) depends on, plus the retryable half that was missing.

    `tests/unit/test_dsh_driver_activity_stamp.py` arm (c) requires that an
    outliving lock raises an instance of `sqlite3.OperationalError` whose message
    names the locked database and writes nothing. That is asserted here as well,
    against the same real lock, so the two cannot drift: the refusal is
    `sqlite3.OperationalError` AND the repository's typed busy refusal.
    """
    monkeypatch.setattr(dsh_driver, "STAMP_BUSY_TIMEOUT_MS", IMPATIENT_STAMP_MS)
    before = row(world, PROJECT)
    started = time.monotonic()
    with _ForeignWriteLock(world.db.db_path) as lock:
        try:
            with pytest.raises(StateWriteBusy) as raised:
                world.runtime.update(PROJECT, phase="busy")
        finally:
            lock.release()
    elapsed = time.monotonic() - started
    error = raised.value
    print(f"[arm A] refused after {elapsed:.2f}s against a held write lock: {error}")

    # The half arm (c) needs, unchanged: sqlite3.OperationalError, naming the
    # locked database, raised inside a bounded wait.
    assert isinstance(error, sqlite3.OperationalError), type(error)
    assert "locked" in str(error), str(error)
    assert world.db.db_path in str(error), str(error)
    assert elapsed < CEILING_SECONDS, f"the bounded wait took {elapsed:.2f}s"

    # The half this change adds: a typed, retryable refusal.
    assert error.code == "state_write_busy"
    assert error.retry_after == STATE_WRITE_BUSY_RETRY_AFTER >= 1
    assert f"after {STATE_WRITE_BUSY_RETRY_AFTER} s" in str(error)

    # A refused stamp wrote nothing at all: same row, same updated_at.
    assert row(world, PROJECT) == before


def test_the_activity_route_answers_503_with_retry_after_not_an_untyped_500(world, monkeypatch):
    """Arm B: the measured defect - the activity tick's 500 - answered retryably."""
    monkeypatch.setattr(dsh_driver, "STAMP_BUSY_TIMEOUT_MS", IMPATIENT_STAMP_MS)
    app = driver_app(_AlwaysRunning(world.runtime), monkeypatch, DRIVER)
    before = row(world, PROJECT)
    with _ForeignWriteLock(world.db.db_path) as lock:
        try:
            with TestClient(app, raise_server_exceptions=False) as http:
                started = time.monotonic()
                response = http.post(ACTIVITY, json={"phase": "running", "subagents": [],
                                                     "error": ""})
                elapsed = time.monotonic() - started
        finally:
            lock.release()
    print(f"[arm B] POST {ACTIVITY} -> {response.status_code} "
          f"(Retry-After {response.headers.get('Retry-After')}) after {elapsed:.2f}s")

    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == str(STATE_WRITE_BUSY_RETRY_AFTER)
    payload = response.json()
    assert payload["code"] == "state_write_busy"
    assert payload["retry_after"] == STATE_WRITE_BUSY_RETRY_AFTER
    # The marker in the body names the condition and the database, so a caller
    # that only ever sees the text can tell it apart from a bug.
    assert "locked" in payload["detail"] and world.db.db_path in payload["detail"]
    assert elapsed < CEILING_SECONDS, f"the bounded wait took {elapsed:.2f}s"
    assert row(world, PROJECT) == before, "a refused stamp still claimed a write"


def test_the_product_app_maps_the_stamp_refusal():
    """The refusal is answered by the PRODUCT app, not only by this file's app.

    `apply_state_write_busy` is registered on `api/main.py` for `StateWriteBusy`;
    the stamp's refusal is a subclass of it, so the deployed backend answers 503
    without a route knowing this class exists.
    """
    from api import main as product

    assert StateWriteBusy in product.app.exception_handlers
    assert issubclass(dsh_driver.DriverStampBusy, StateWriteBusy)
    assert issubclass(dsh_driver.DriverStampBusy, sqlite3.OperationalError)


@pytest.fixture
def state_world(tmp_path, monkeypatch):
    """A real State-only database: one project, one registered member driver."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project(PROJECT, "Alpha")
    registry = DriverRegistry(db, "stamp-busy-witness-pepper-not-a-secret")
    registry.register(DRIVER, "Witness", actor="test")
    registry.set_membership(PROJECT, DRIVER, "member", 0, "fixture", actor="test")
    return SimpleNamespace(db=db, service=StateService(
        db, actor="driver:" + DRIVER, driver_id=DRIVER, project_read_trusted=True))


def wait_rows(db):
    """How many presence waits this database records (0 when the table is absent)."""
    with db.get_connection() as conn:
        exists = conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name='driver_presence_waits'").fetchone()
        if exists is None:
            return 0
        return conn.execute("SELECT COUNT(*) FROM driver_presence_waits").fetchone()[0]


def state_client(world):
    app = FastAPI()
    app.include_router(create_state_router(lambda: world.service, lambda: None))
    return TestClient(app, raise_server_exceptions=False)


def test_a_wait_that_cannot_register_answers_retryable_503_never_409(state_world, monkeypatch):
    """Arm C: the same busy database used to be reported as 409 Conflict here."""
    monkeypatch.setattr(driver_presence, "WAIT_REGISTER_SECONDS", IMPATIENT_REGISTER_SECONDS)
    with _ForeignWriteLock(state_world.db.db_path) as lock:
        try:
            with state_client(state_world) as http:
                started = time.monotonic()
                response = http.post(WAIT, json={"project_id": PROJECT, "timeout_seconds": 0})
                elapsed = time.monotonic() - started
        finally:
            lock.release()
    payload = response.json()
    print(f"[arm C] POST {WAIT} -> {response.status_code} "
          f"(Retry-After {response.headers.get('Retry-After')}) after {elapsed:.2f}s: "
          f"{payload.get('code')}")

    assert response.status_code == 503, response.text
    assert response.status_code != 409, "a busy write lock was reported as a conflict"
    assert response.headers["Retry-After"] == str(STATE_WRITE_BUSY_RETRY_AFTER)
    assert payload["code"] == "wait_not_registered"
    assert payload["retry_after"] == STATE_WRITE_BUSY_RETRY_AFTER
    # It must not read as a conflict: no 409 wording, and it points at a retry.
    assert "conflict" not in json.dumps(payload).lower(), payload
    assert "retry" in payload["detail"], payload
    assert elapsed < CEILING_SECONDS, f"the bounded wait took {elapsed:.2f}s"
    # Nothing pretended the driver was waiting.
    assert wait_rows(state_world.db) == 0


def test_the_lock_free_wait_still_answers_200_and_records_its_wait(state_world, monkeypatch):
    """Arm C, control: the retryable mapping must not turn a healthy wait into a 503."""
    monkeypatch.setattr(driver_presence, "WAIT_REGISTER_SECONDS", IMPATIENT_REGISTER_SECONDS)
    with state_client(state_world) as http:
        response = http.post(WAIT, json={"project_id": PROJECT, "timeout_seconds": 0})

    assert response.status_code == 200, response.text
    assert "timed_out" in response.json(), response.text
    assert wait_rows(state_world.db) == 1, "the wait was not recorded"


def test_a_start_whose_stamp_is_refused_never_answers_200(world, monkeypatch):
    """Arm D: refusal of `enabled=1` cannot look like a successful start."""
    monkeypatch.setattr(dsh_driver, "STAMP_BUSY_TIMEOUT_MS", IMPATIENT_STAMP_MS)
    stamps = []
    real_update = DshDrivers.update

    def counted(self, project_id, **fields):
        """THE product's update, recorded - a counting wrapper, not a substitute."""
        stamps.append({"project_id": project_id, **fields})
        return real_update(self, project_id, **fields)

    monkeypatch.setattr(DshDrivers, "update", counted)
    app = driver_app(world.runtime, monkeypatch, START_DRIVER)
    before = row(world, START_PROJECT)
    with _ForeignWriteLock(world.db.db_path) as lock:
        try:
            with TestClient(app, raise_server_exceptions=False) as http:
                started = time.monotonic()
                response = http.post(f"/api/dsh-drivers/{START_PROJECT}/start", json={})
                elapsed = time.monotonic() - started
        finally:
            lock.release()
    print(f"[arm D/start] POST start -> {response.status_code} after {elapsed:.2f}s; "
          f"stamps attempted: {[sorted(s) for s in stamps]}")

    assert response.status_code != 200, "a start that was not recorded answered 200"
    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == str(STATE_WRITE_BUSY_RETRY_AFTER)
    # ONE control-plane stamp was attempted, the `enabled=1` one. The recovery
    # path must not add a second one: `enabled=0, phase="failed"` would claim a
    # start that never ran, and relabel a retryable lock as a broken request.
    assert len(stamps) == 1, stamps
    assert stamps[0]["enabled"] == 1 and stamps[0]["phase"] == "starting"
    assert row(world, START_PROJECT) == before, "a refused start still stamped the row"


def test_a_stop_whose_stamp_is_refused_never_answers_200(world, monkeypatch):
    """Arm D: refusal of `enabled=0` cannot look like a successful stop."""
    monkeypatch.setattr(dsh_driver, "STAMP_BUSY_TIMEOUT_MS", IMPATIENT_STAMP_MS)
    app = driver_app(world.runtime, monkeypatch, DRIVER)
    before = row(world, PROJECT)
    with _ForeignWriteLock(world.db.db_path) as lock:
        try:
            with TestClient(app, raise_server_exceptions=False) as http:
                started = time.monotonic()
                response = http.post(f"/api/dsh-drivers/{PROJECT}/stop")
                elapsed = time.monotonic() - started
        finally:
            lock.release()
    print(f"[arm D/stop] POST stop -> {response.status_code} after {elapsed:.2f}s")

    assert response.status_code != 200, "a stop that was not recorded answered 200"
    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == str(STATE_WRITE_BUSY_RETRY_AFTER)
    assert row(world, PROJECT) == before
    assert row(world, PROJECT)["enabled"] == 1, "the refused stop disabled the driver anyway"
