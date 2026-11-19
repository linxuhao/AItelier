"""The backend event loop never waits for a SQLite write or a WAL close.

THE MEASUREMENT (`iss-ecea53516eb543c4`): ten captured stacks held the backend
event-loop thread in uninterruptible disk sleep at three sites. Live rate before
the fix was 207 stalls >= 0.50 s per hour (an uncensored 0.2 s HTTP probe -
the product's own monitor logs only 48/h because its 30 s log cooldown censors
the rest), median 1.92 s, max 9.65 s.

  1. `api/dsh_driver_routers.completion` called `DshDrivers.record_model_call`
     ON the loop, and that function's explicit `COMMIT` fsyncs the WAL (wchan
     `jbd2_log_wait_commit`). 8 of 10 stacks, 64 of 70 sampled episodes.
  2. `core/db_manager.DBManager` opens a NEW connection per operation, so
     closing it ran `sqlite3WalClose -> sqlite3WalCheckpoint -> fdatasync` on
     the loop (wchan `submit_bio_wait`). 2 of 10 stacks.
  3. `DriverRegistry.lookup_token` (SELECT) plus its throttled `last_seen_at`
     UPDATE + COMMIT, reached from `api.authz` through the `write_gate`
     middleware for EVERY mutating request. One sampled episode was 2.81 s.

WHAT IS WITNESSED, and what is not. The subject is the loop, so the assertion is
about a request that is NOT the writer: a trivial `GET /health` must be answered
while the slow database operation is still IN FLIGHT, and it must not cost the
machine's own speed several times over (see "the read allowance" below). The
slowness is put INSIDE the database operation by a connection proxy - this file
never sleeps in the test coroutine, so the loop is only ever held by the code
under test. The slow operation's own clock (`gate.began_at`) is the reference
point for the latency, because on the base tree the loop is blocked from that
instant: measuring from a timestamp taken in the test coroutine would let the
stall hide behind the coroutine's own resumption and the witness would be green
on a tree that stalls for seconds.

ALL THREE ARE RED ON `7b9baacdfb`: there the loop thread IS the thread that runs
the operation, so `/health` is answered only after it ends. `_STALL` is 2 s
against a control sample of a few milliseconds - the base failures are ~2.0 s,
not a flake margin.

Durability and ordering are pinned too, not traded away: witness 1 never
releases the slow write, so the completion response can only arrive after the
write's `COMMIT` has finished and its row is on disk. A fire-and-forget write
would answer at ~10 ms with no row, and fail there. The last test keeps two
concurrent State writers serialized - one commits, the other gets the typed
`state_write_busy` 503 - while a concurrent read is answered in between.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

# What "the database layer is slow" means here: longer than the allowance a
# second request gets, and long enough that no plausible scheduler hiccup can
# turn the base tree's ~2.0 s stall into a pass.
_STALL = 2.0
# ── the read allowance: a CONTROL SAMPLE, not a host constant ────────────────
# These witnesses used to assert that the second request finished within 0.5 s.
# That is a property of the HOST, not of the product: on a loaded machine the
# same trivial read takes seconds while the invariant holds - measured 2.44 s in
# a whole-suite arm whose row passes alone on the same tree - so the constant
# accused the product of a defect it did not have. What is decided now is
# RELATIVE: the contended read is compared with the SAME read taken in this same
# process while NO writer is in flight. `_DEGRADATION` is how many times the
# machine's own speed a read may cost before the delay is attributed to the
# writer rather than to the machine.
_DEGRADATION = 4.0
# A healthy read is a few milliseconds, so a control sample can itself be noise;
# the allowance has a floor instead of scaling a near-zero sample.
_ALLOWANCE_FLOOR = 0.25
# How many samples a control takes. The WORST is used: one lucky fast sample on
# a host that is stalling reads must not set the allowance.
_CONTROL_SAMPLES = 3
# The ABSOLUTE CEILING, kept as the backstop the old constant used to be: a read
# that takes longer than two whole injected stalls is not "a slow machine" by
# any reading, it is a reader that never came back. It sits far above the 0.5 s
# the rows used to demand, because that bound itself was the flake.
_CEILING = 2 * _STALL
# Everything the proxies may wait on is bounded, so a regression can never hang
# the suite: the stall gives up on its own, the handshake gives up too, and every
# control sample is an ordinary request the client already bounds.
_HANDSHAKE_TIMEOUT = 10.0

ROOT_SESSION = "sess-root"
# The route the root session asks for (the row's model) and the concrete
# endpoint it resolves to. They must differ: the store refuses to record a value
# that IS the route alias, because "worker answered" is not a model.
ROOT_ROUTE = "worker"
ROOT_ENDPOINT = "opencodego/deepseek-v4.1-flash"


# ── the database layer, made slow (never the test coroutine) ──────────────────

class _Gate:
    """One slow operation's timeline, on whichever thread runs it.

    `began` / `began_at` say WHEN the operation started and `ended_at` when it
    finished, both on `time.monotonic` - the same clock the test reads. `stalls`
    counts how many operations were actually made slow, so a witness can refuse
    to be vacuous (a proxy that never matched anything would leave the second
    request fast for the wrong reason).
    """

    def __init__(self, stall: float = _STALL):
        self.stall_seconds = stall
        self.began = threading.Event()
        self.release = threading.Event()
        self.began_at: float | None = None
        self.ended_at: float | None = None
        self.stalls = 0

    def stall(self) -> None:
        self.stalls += 1
        if self.began_at is None:
            self.began_at = time.monotonic()
        self.began.set()
        # Bounded: a test that fails before releasing must not block forever.
        self.release.wait(self.stall_seconds)
        if self.ended_at is None:
            self.ended_at = time.monotonic()


class _StalledConnection:
    """A real DBManager connection whose ONE matched operation takes `gate`.

    The proxy is transparent (`__getattr__` / `__setattr__` delegate), so
    row_factory, isolation_level, in_transaction and every unmatching statement
    reach the real connection untouched. Only the matched operation - the
    completion write's `COMMIT`, a per-operation `close`, or the last-seen
    UPDATE - is slow, which is what makes each witness name ONE of the three
    measured sites.
    """

    def __init__(self, conn, gate: _Gate, *, statement=None, close: bool = False):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_gate", gate)
        object.__setattr__(self, "_statement", statement)
        object.__setattr__(self, "_slow_close", close)

    def execute(self, sql, *params):
        match = self._statement
        if match is not None and match(sql):
            self._gate.stall()
        return self._conn.execute(sql, *params)

    def close(self):
        if self._slow_close:
            self._gate.stall()
        self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)


def _every_connect(monkeypatch, module, wrap) -> None:
    """Route every `sqlite3.connect` of `module` through `wrap`.

    The proxy is installed at the SQLITE boundary, not on a method of the class
    under test, for two reasons: it reaches the connection the code actually
    opens whichever THREAD that turns out to be (the whole question here), and
    it instruments the base tree the same way it instruments the fixed one - so
    a red there is the latency assertion, not a stand-in for it. Stubbing the
    operation out (a fake `record_model_call`, say) would delete the thing being
    measured, so nothing above the connection is replaced.
    """
    real_connect = sqlite3.connect
    monkeypatch.setattr(module, "sqlite3", SimpleNamespace(
        connect=lambda *args, **kwargs: wrap(real_connect(*args, **kwargs)),
        Row=sqlite3.Row, Error=sqlite3.Error,
        OperationalError=sqlite3.OperationalError,
        IntegrityError=sqlite3.IntegrityError,
        DatabaseError=sqlite3.DatabaseError,
        ProgrammingError=sqlite3.ProgrammingError))


def _slow_down(monkeypatch, gate: _Gate, *, statement=None, close: bool = False) -> None:
    """Make ONE database operation of the host database take `_STALL`."""
    from core import db_manager

    _every_connect(monkeypatch, db_manager, lambda conn: _StalledConnection(
        conn, gate, statement=statement, close=close))


# ── the real dsh-driver router, over a real host database ─────────────────────

def _answer(gateway, kwargs, **unused):
    """The one stubbed thing: the LLM transport. No provider is called."""
    import litellm

    return litellm.ModelResponse(
        choices=[{"message": {"role": "assistant", "content": "ok"},
                  "finish_reason": "stop"}],
        usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12})


@pytest.fixture
def driver_world(tmp_path, monkeypatch):
    """The REAL `api.main.app`, a real DBManager, a real DriverRegistry.

    Nothing about authorization is replaced: the gate is ON because the
    DEPLOYMENT is configured (`cf_access` is told it has a team domain and an
    AUD, which is the real switch), and the driver presents its own registered
    token, so a 403 or a 404 measured here is the app's decision. Only two
    things are the test's: the registry lookup is pointed at a tmp database
    (resolving it lazily would open the production one) and the LLM transport
    returns a canned answer.
    """
    from api import authz
    from api.main import app
    from core import cf_access, drivers
    from core.db_manager import DBManager
    from core.drivers import DriverRegistry
    from core.dsh_driver import DshDrivers
    import core.ai_router as ai

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = DBManager(str(tmp_path / "host.sqlite"))
    registry = DriverRegistry(db, "loop-fix-witness-pepper-" * 2)
    registered = registry.register("dsh-fixture", "DSH fixture", actor="test")
    token = registered["token"]
    runtime = DshDrivers(db, registry, tmp_path / "drivers")
    # The managed driver row a started process would have. A live CHILD process
    # is not what these witnesses measure; the turn is stubbed at the transport
    # and what is under test is the thread the resulting SQLite write runs on.
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO dsh_drivers(project_id,driver_id,enabled,phase,provider,model,"
            "session_id,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("alpha", "dsh-fixture", 1, "running", "aitelier", ROOT_ROUTE,
             ROOT_SESSION, "2026-11-16T00:00:00+00:00"))
        conn.commit()
    monkeypatch.setattr(DshDrivers, "running", lambda self, project_id: True)
    # The route table the gateway resolves ROOT_ROUTE (`worker`) through, and the
    # one stubbed thing in the whole fixture: the LLM transport. Both tables are
    # provider data, not behaviour.
    providers = tmp_path / "llm_providers.json"
    routes = tmp_path / "model_routes.json"
    providers.write_text(json.dumps({ROOT_ENDPOINT.split("/", 1)[0]: {
        "base_url": "https://example.invalid/v1", "api_key_env": "FIXTURE"}}))
    routes.write_text(json.dumps({ROOT_ROUTE: [ROOT_ENDPOINT]}))
    gateway_class = ai.AIGateway
    monkeypatch.setattr(gateway_class, "_call_llm", _answer)
    monkeypatch.setattr(ai, "_read_secret", lambda name: "fixture-key")
    monkeypatch.setattr(ai, "_ENDPOINT_COOLDOWN", {})
    monkeypatch.setattr(ai, "AIGateway", lambda model, **kw: gateway_class(
        model, config_path=str(providers), routes_path=str(routes), **kw))
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "fixture.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "fixture-aud")
    monkeypatch.setattr(authz, "driver_registry", lambda: registry)
    # The last-seen stamp is throttled module-wide, and these witnesses need the
    # write to actually run: a fresh table means a fresh first sighting.
    monkeypatch.setattr(drivers, "_LAST_SEEN", {})
    # The real app's DRIVER RUNTIME cache, which `manager()` reads first; the
    # real gate, the real router and the real /health stay as they are.
    previous = getattr(app.state, "dsh_drivers", None)
    previous_test_mode = getattr(app.state, "_test_mode", False)
    app.state.dsh_drivers = runtime
    app.state._test_mode = False
    try:
        yield SimpleNamespace(app=app, db=db, registry=registry, runtime=runtime,
                              token=token,
                              headers={"X-AItelier-Driver-Token": token},
                              completion={"session_id": ROOT_SESSION,
                                          "messages": [{"role": "user", "content": "work"}]})
    finally:
        app.state.dsh_drivers = previous
        app.state._test_mode = previous_test_mode


def _client(app) -> httpx.AsyncClient:
    """ONE event loop, no portal thread: the app really runs on this loop."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


async def _worst_read(client, path: str) -> float:
    """Time the SAME trivial read with NO writer holding the lock: the CONTROL.

    This is what the latency decision is made against: the machine's own speed
    for exactly this request, in exactly this process, measured while nothing
    contends. It is taken before the slow operation starts and again once it has
    settled (callers pass the worse of the two), so a host that is stalling
    reads - and would stall this one too - sets the allowance instead of being
    reported as the writer's fault.
    """
    worst = 0.0
    for _ in range(_CONTROL_SAMPLES):
        started = time.monotonic()
        response = await client.get(path)
        worst = max(worst, time.monotonic() - started)
        assert response.status_code == 200, response.text
    return worst


def _assert_read_not_queued(contended: float, control: float, what: str) -> None:
    """Decide a read's latency against its own control, never against the host.

    The failure message quotes BOTH numbers, so a reader can tell "the reader
    queued behind the writer" (control fast, contended slow) from "the whole
    machine was slow" (both slow) - the distinction the single host constant
    could not draw, and the reason this row used to accuse a healthy product on
    a loaded machine.
    """
    allowance = max(_ALLOWANCE_FLOOR, _DEGRADATION * control)
    assert contended < allowance, (
        f"{what} took {contended:.3f}s while the writer was in flight, against a "
        f"control of {control:.3f}s for the same read with no writer in flight: "
        f"{contended / max(control, 1e-9):.1f}x the machine's own speed (allowance "
        f"{allowance:.3f}s = {_DEGRADATION:g}x the control, floor "
        f"{_ALLOWANCE_FLOOR:.2f}s) - the reader waited for the writer")
    assert contended < _CEILING, (
        f"{what} took {contended:.3f}s while the writer was in flight (control "
        f"{control:.3f}s): past the {_CEILING:.2f}s absolute ceiling - the reader "
        f"did not come back")


async def _health_while_in_flight(client, gate):
    """Time a trivial GET from the moment the slow database operation STARTED.

    The reference is `gate.began_at`, recorded by the database proxy on the
    thread that runs the operation - NOT a timestamp taken in this coroutine,
    because on the base tree this coroutine cannot resume until the stall is
    over, and measuring from after it would hide the very wait under test.
    """
    health = await client.get("/health")
    return health, time.monotonic() - gate.began_at


async def _begin_slow_request(client, gate, slow_request):
    """Start `slow_request` and return its task once the slow operation began.

    Waiting for the operation to start matters: without it the second request
    could be answered BEFORE the write, which would prove nothing. On the fixed
    tree the loop is free, so this returns within microseconds; on the base tree
    the loop is stuck INSIDE the operation, so it can only resume once it ends.
    """
    in_flight = asyncio.create_task(slow_request())
    began = await asyncio.to_thread(gate.began.wait, _HANDSHAKE_TIMEOUT)
    assert began, ("the database operation under test never became slow: the "
                   "witness would have been vacuous")
    return in_flight


async def _settle(task, gate):
    """Stop waiting on the injected stall and let the request finish."""
    gate.release.set()
    return await _finish_quietly(task)


async def _finish_quietly(task):
    """Await the in-flight request without touching the injected stall."""
    try:
        return await asyncio.wait_for(task, _HANDSHAKE_TIMEOUT)
    except asyncio.TimeoutError:
        return None


# ── witness 1: the completion write's COMMIT ─────────────────────────────────

async def test_a_completion_write_does_not_stall_the_loop(driver_world, monkeypatch):
    """Site 1: the completion handler's `record_model_call` COMMIT.

    The write is forced to take 2 s. On the base tree that is 2 s of the WHOLE
    backend, because `record_model_call` runs on the loop thread; here a request
    that has nothing to do with it must still be answered while the write is in
    flight, at no more than a few times what the same read costs with no writer
    in flight, and the completion response itself must still wait for the write
    (the row is recorded before the answer, or the answer carries the failure).
    """
    world = driver_world
    gate = _Gate()
    # Only the completion write's explicit `COMMIT` - `record_model_call` sets
    # isolation_level=None and commits with a statement, so this matches exactly
    # one operation in one request.
    _slow_down(monkeypatch, gate,
               statement=lambda sql: sql.strip().upper() == "COMMIT")
    async with _client(world.app) as client:
        control = await _worst_read(client, "/health")
        in_flight = await _begin_slow_request(
            client, gate, lambda: client.post(
                "/api/dsh-drivers/alpha/completion", json=world.completion,
                headers=world.headers))
        try:
            health, latency = await _health_while_in_flight(client, gate)
            assert gate.stalls >= 1, "the completion write was never made slow"
            assert health.status_code == 200, health.text
            assert gate.ended_at is None, (
                "the slow write had already finished when the second request was answered")
        finally:
            # Witness 1 deliberately does NOT release the stall: the write only
            # ends on its own bound, so `ended_at` being set the moment the
            # response arrives is what proves the answer waited for it. A
            # fire-and-forget write would answer at ~10 ms with its write still
            # stalled, and there would be no row on disk.
            response = await _finish_quietly(in_flight)
        control = max(control, await _worst_read(client, "/health"))
        assert response is not None and response.status_code == 200, response
        assert gate.ended_at is not None, (
            "the completion answered while its write was still in flight: the row "
            "is not guaranteed to be recorded when the driver gets the turn")
    _assert_read_not_queued(latency, control, "a trivial GET /health")
    with world.db.get_connection() as conn:
        rows = conn.execute("SELECT served_by FROM dsh_driver_session_models "
                            "WHERE project_id=? AND session_id=?",
                            ("alpha", ROOT_SESSION)).fetchall()
    assert [row["served_by"] for row in rows] == [ROOT_ENDPOINT], (
        "the answering model was not recorded - the write moved off the loop, "
        "it did not stop happening")


# ── witness 2: the per-operation connection close (WAL checkpoint) ───────────

async def test_a_per_operation_connection_close_does_not_stall_the_loop(driver_world, monkeypatch):
    """Site 2: `DBManager` opens a NEW connection per operation, and CLOSING it
    runs `sqlite3WalClose -> sqlite3WalCheckpoint -> fdatasync`.

    Every close is forced to take 2 s. The connection that write closes is the
    one the loop used to finish; the second request must not wait for it, and
    must not cost several times what the same read costs with no close in
    flight.
    """
    world = driver_world
    gate = _Gate()
    _slow_down(monkeypatch, gate, close=True)
    async with _client(world.app) as client:
        control = await _worst_read(client, "/health")
        in_flight = await _begin_slow_request(
            client, gate, lambda: client.post(
                "/api/dsh-drivers/alpha/completion", json=world.completion,
                headers=world.headers))
        try:
            health, latency = await _health_while_in_flight(client, gate)
            assert gate.stalls >= 1, "no connection close was made slow"
            assert health.status_code == 200, health.text
            assert gate.ended_at is None, (
                "the slow close had already finished when the second request was answered")
        finally:
            response = await _settle(in_flight, gate)
        control = max(control, await _worst_read(client, "/health"))
        assert response is not None and response.status_code == 200, response
    _assert_read_not_queued(latency, control, "a trivial GET /health")
    with world.db.get_connection() as conn:
        rows = conn.execute("SELECT calls FROM dsh_driver_session_models "
                            "WHERE project_id=? AND session_id=?",
                            ("alpha", ROOT_SESSION)).fetchall()
    assert [row["calls"] for row in rows] == [1], "the completion write did not land"


async def test_a_connection_closed_on_the_loop_is_finished_off_it(tmp_path, monkeypatch):
    """The rule itself, where it lives: a close started on the loop leaves it.

    Witness 2 is only green for the right reason if the close really finishes on
    another thread, so this pins the mechanism `core/off_loop_sqlite` provides:
    a connection closed from the event-loop thread is closed by a DIFFERENT
    thread, and a connection that still holds an open transaction is closed
    INLINE instead (close() is what rolls it back, and a rollback must not be
    deferred behind other callers' work).
    """
    from core.db_manager import DBManager

    db = DBManager(str(tmp_path / "loop-close.sqlite"))
    closer_threads = []

    class _Recorder(_StalledConnection):
        def __init__(self, conn):
            super().__init__(conn, _Gate())      # matches nothing: records only

        def close(self):
            closer_threads.append(threading.current_thread().name)
            self._conn.close()

    from core import db_manager
    _every_connect(monkeypatch, db_manager, _Recorder)
    loop_thread = threading.current_thread().name
    with db.get_connection() as conn:              # committed / never opened
        conn.execute("SELECT 1").fetchone()
    with db.get_connection() as conn:              # an OPEN transaction
        conn.execute("CREATE TABLE IF NOT EXISTS witness_rollback(x)")
        conn.execute("INSERT INTO witness_rollback(x) VALUES (1)")

    for _ in range(200):
        if len(closer_threads) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(closer_threads) == 2, closer_threads
    assert closer_threads[0].startswith("sqlite-close"), (
        f"the close of a plain connection ran on {closer_threads[0]!r}, not the closer thread")
    assert closer_threads[0] != loop_thread
    assert closer_threads[1] == loop_thread, (
        "a connection with an open transaction was not closed inline; its ROLLBACK "
        "would have been deferred behind other callers' work")


# ── witness 3: the write gate's last-seen UPDATE + COMMIT ────────────────────

async def test_the_write_gate_does_not_stall_the_loop(driver_world, monkeypatch):
    """Site 3: `authz.write_denial_reason` -> `lookup_token` -> `_stamp_last_seen`.

    Every mutating request resolves the presented token (a SELECT) and refreshes
    `last_seen_at` (an UPDATE whose COMMIT fsyncs). The UPDATE is forced to take
    2 s; the request used to drive it is a POST to a path that does not exist,
    so the ONLY work it does is the gate's verdict - which makes the stall
    unambiguously the gate's, and lets the 404 below prove the verdict came back
    "authorized" rather than being cut short.
    """
    world = driver_world
    gate = _Gate()
    _slow_down(monkeypatch, gate,
               statement=lambda sql: sql.lstrip().upper().startswith(
                   "UPDATE DRIVERS SET LAST_SEEN_AT"))
    async with _client(world.app) as client:
        control = await _worst_read(client, "/health")
        in_flight = await _begin_slow_request(
            client, gate, lambda: client.post(
                "/api/not-a-route", json={}, headers=world.headers))
        try:
            health, latency = await _health_while_in_flight(client, gate)
            assert gate.stalls >= 1, "the last-seen UPDATE was never made slow"
            assert health.status_code == 200, health.text
            assert gate.ended_at is None, (
                "the slow last-seen write had already finished when the second "
                "request was answered")
        finally:
            response = await _settle(in_flight, gate)
        control = max(control, await _worst_read(client, "/health"))
    _assert_read_not_queued(latency, control, "a trivial GET /health")
    # The gate answered "this caller may write" - routing then rejected the
    # request itself (405 from the SPA mount, which serves no such path): the
    # verdict is unchanged by moving it off the loop, and it was not cut short.
    assert response is not None and response.status_code in (404, 405), response
    with world.db.get_connection() as conn:
        seen = conn.execute("SELECT last_seen_at FROM drivers WHERE driver_id=?",
                            ("dsh-fixture",)).fetchone()["last_seen_at"]
    assert seen, "the throttled last-seen stamp did not land"


# ── two concurrent State writers still serialize; a read is answered between ──

COMMAND = "/api/state/commands/send_user_conversation_message"
OWNER = "owner:user@example.test"


@pytest.fixture
def state_world(tmp_path, monkeypatch):
    """A real State-only database with one project and one member driver."""
    from core.driver_inbox import DriverInbox
    from core.drivers import DriverRegistry
    from core.state_database import StateDatabase
    from core.state_service import StateService

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, "state-writer-pepper-not-a-secret-" * 2)
    registry.register("ada", "Ada", actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    return SimpleNamespace(db=db, inbox=DriverInbox,
                           service=StateService(db, actor=OWNER, project_read_trusted=True))


def _state_app(world):
    from fastapi import FastAPI
    from api.state_http import create_state_router

    app = FastAPI()
    app.include_router(create_state_router(lambda: world.service, lambda: None))
    return app


def _stored(world, request_key: str) -> tuple[int, list[str]]:
    with world.db.get_connection() as conn:
        requests = conn.execute(
            "SELECT COUNT(*) FROM driver_inbox_requests WHERE actor=? AND "
            "operation='send_user_conversation' AND request_key=?",
            (OWNER, request_key)).fetchone()[0]
        messages = [row["message_id"] for row in conn.execute(
            "SELECT message_id FROM driver_inbox_messages WHERE project_id='alpha' "
            "AND body=? ORDER BY message_id", ("hello",))]
    return requests, messages


async def test_two_state_writers_still_serialize_while_a_read_is_answered(
        state_world, monkeypatch):
    """Moving the loop off the database must not move the WRITE LOCK off it.

    Two writers race the same SQLite write lock: the one that gets in holds it
    inside its transaction (the injected slow step), commits, and answers 200;
    the other waits its budget and is refused with the typed `state_write_busy`
    503 that carries `Retry-After` - never a 500, never a silent success. While
    both are in flight a STATE READ is answered in between, because a WAL reader
    must not wait for a writer.

    The budget is shortened through the switch production already exposes
    (`AITELIER_STATE_WRITE_LOCK_SECONDS`); no threshold in the code is touched.
    """
    from core import state_graph

    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "0.6")
    world = state_world
    holding = threading.Event()
    release = threading.Event()
    real_replay = world.inbox._replay

    def slow_replay(self, conn, operation, request_key, payload):
        # INSIDE the writer's transaction, i.e. while it holds SQLite's write
        # lock. Only the writer that actually got in reaches this line: the
        # other one is still retrying `BEGIN IMMEDIATE`.
        if not holding.is_set():
            holding.set()
            # The holder keeps the write lock for LONGER than the other
            # writer's whole budget (`_STALL` 2 s against 0.6 s below), which is
            # what makes the refusal a fact rather than a race. Bounded, so a
            # regression cannot hang the suite.
            release.wait(_STALL)
        return real_replay(self, conn, operation, request_key, payload)

    monkeypatch.setattr(world.inbox, "_replay", slow_replay)

    def body(request_key: str) -> dict:
        return {"request_key": request_key, "project_id": "alpha", "body": "hello"}

    async with _client(_state_app(world)) as client:
        control = await _worst_read(client, "/api/state/projects")
        first = asyncio.create_task(client.post(COMMAND, json=body("writer-a")))
        assert await asyncio.to_thread(holding.wait, _HANDSHAKE_TIMEOUT), \
            "the first writer never got the write lock"
        began = time.monotonic()
        second = asyncio.create_task(client.post(COMMAND, json=body("writer-b")))
        read = await client.get("/api/state/projects")
        read_latency = time.monotonic() - began
        try:
            first_response, second_response = await asyncio.gather(first, second)
        finally:
            release.set()
        control = max(control, await _worst_read(client, "/api/state/projects"))

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 503, second_response.text
    assert second_response.headers["Retry-After"] == str(state_graph.STATE_WRITE_BUSY_RETRY_AFTER)
    payload = second_response.json()
    assert payload["code"] == state_graph.STATE_WRITE_BUSY_CODE == "state_write_busy"
    assert payload["retry_after"] == state_graph.STATE_WRITE_BUSY_RETRY_AFTER

    # The read was answered WHILE a writer held the lock and another waited, at
    # no more than a few times what the SAME read costs with no writer in flight:
    # a WAL reader must not wait for a writer, and a slow host is not a queue.
    assert read.status_code == 200, read.text
    _assert_read_not_queued(read_latency, control, "a State read")

    requests_a, messages = _stored(world, "writer-a")
    requests_b, _ = _stored(world, "writer-b")
    assert requests_a == 1, "the writer that committed did not record its request"
    assert requests_b == 0, "the refused writer stored something"
    assert messages, "the committed message is missing"
