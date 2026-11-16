"""A driver heartbeat that loses the State write-lock race answers retryable 503.

THE INCIDENT (`iss-1b65b948336342cb`, production, 2026-11-16): the live backend
answered `POST /api/dsh-drivers/wuxia-myth/heartbeat` with an opaque HTTP 500
whose escaped exception was `core.state_graph.StateWriteBusy: state_write_busy:
another writer held the State write lock for the whole 10 s budget; nothing was
written, so retry the same request after 1 s`.

Nothing had failed: a State write that cannot take SQLite's write lock writes
NOTHING and is worth repeating a second later. The retryable answer (503, a
machine-readable `state_write_busy` code and `Retry-After`) existed only inside
`api/state_http.py`'s `_call`, i.e. only for the `/api/state` routers.
`api/dsh_driver_routers.heartbeat` calls `execute(..., allow_write=True)`
directly, so its refusal escaped the route and became a 500.

What is pinned here, through the REAL driver route (`api.dsh_driver_routers`:
router -> `heartbeat` handler -> `owned` -> `execute` ->
`StateGraphStore.transaction`), with a real State database in `tmp_path`:

Arm A - a FOREIGN sqlite3 connection holds the write lock past the budget: the
        response is 503 with `Retry-After` and the machine-readable busy code,
        NOT 500, and no `sqlite3.OperationalError` escapes the transaction.
        Nothing was written. This is the arm that answers 500 on the base tree.
Arm B - the lock is free: the same route answers 200 and renews the claim, so
        the app-wide default is not turning healthy calls into 503.
Arm C - the default is on the APP, not in the heartbeat handler: a route this
        change never touched raises the same typed refusal and answers the same
        503. `api/dsh_driver_routers.py` is deliberately untouched - a fix that
        added one more per-route `except StateWriteBusy` would pass Arm A and
        leave the next route that calls `execute` broken again.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz, dsh_driver_routers, state_http
from api.dsh_driver_routers import router as driver_router
from api.state_graph_routers import get_service
from core import state_graph
from core.drivers import DriverRegistry, Identity
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphStore, StateWriteBusy
from core.state_service import StateService

PROJECT = "alpha"
DRIVER = "ada"
HEARTBEAT = f"/api/dsh-drivers/{PROJECT}/heartbeat"


@pytest.fixture
def world(tmp_path):
    """A real State database: one project, one registered member driver, one claim."""
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project(PROJECT, "Alpha")
    registry = DriverRegistry(db, "heartbeat-busy-pepper-not-a-secret")
    registry.register(DRIVER, "Ada", actor="test")
    registry.set_membership(PROJECT, DRIVER, "member", 0, "fixture", actor="test")
    service = StateService(db, actor=f"driver:{DRIVER}", driver_id=DRIVER,
                           project_read_trusted=True)
    execute(service, "add_nodes", {"project_id": PROJECT, "nodes": [
        {"key": "beat", "facet": "content", "goal": "a node this driver holds",
         "acceptance": [{"id": "held", "kind": "test",
                         "description": "the heartbeat renews the claim"}]}]},
            allow_write=True)
    claim = execute(service, "claim_node", {
        "project_id": PROJECT, "node_key": "beat", "purpose": "implement",
        "expected_revision": 1, "request_key": "beat-claim"}, allow_write=True)
    return SimpleNamespace(db=db, service=service, claim=claim)


def apply_app_defaults(app):
    """Register the app-wide defaults exactly where `api/main.py` registers them.

    The base tree has no `apply_state_write_busy` at all - there the product app
    registers only `apply_project_privacy`, which is why the busy refusal becomes
    an opaque 500. Reading the applier with `getattr` keeps this file an honest
    witness on BOTH trees: it is runnable against the base revision (where it
    must report 500) instead of dying at import with an AttributeError that would
    prove nothing about the response. If the applier is ever removed from a
    delivered tree, Arm A fails as the 500 it is, not as a skip.
    """
    applier = getattr(state_http, "apply_state_write_busy", None)
    if applier is not None:
        applier(app)


def application(world, monkeypatch, add_routes=None):
    """The REAL driver router on a minimal app, with only its inputs replaced.

    What is real: `dsh_driver_routers.router`, the `heartbeat` handler, `owned`,
    the claim listing, the attempt read and the `execute(..., allow_write=True)`
    call that takes the lock. What is replaced, and nothing else: the `manager`
    dependency (so no driver process and no production database is needed), the
    `get_service` dependency, `authz.require_writer` (who may write is its own
    test; this one is about lock contention), and `authz.request_identity`,
    which `owned` calls DIRECTLY rather than as a dependency, so FastAPI's
    `dependency_overrides` cannot reach it - the same seam the activity-stamp
    test (`tests/unit/test_dsh_driver_activity_stamp.py`) uses.
    """
    runtime = SimpleNamespace(
        get=lambda project_id: {"project_id": project_id, "driver_id": DRIVER, "enabled": 1},
        running=lambda project_id: True)
    app = FastAPI()
    app.include_router(driver_router)
    if add_routes is not None:
        add_routes(app, world)
    apply_app_defaults(app)
    app.dependency_overrides[dsh_driver_routers.manager] = lambda: runtime
    app.dependency_overrides[get_service] = lambda: world.service
    app.dependency_overrides[authz.require_writer] = lambda: None
    monkeypatch.setattr(authz, "request_identity",
                        lambda request: Identity("driver", f"driver:{DRIVER}", driver_id=DRIVER))
    return app


def client(app, *, answer_server_errors=True):
    """A TestClient; `answer_server_errors=False` asks for the RESPONSE, not a raise.

    Starlette's default (`True`) re-raises whatever the app did not handle, so on
    a tree WITHOUT the app-wide default the busy refusal would surface here as
    `StateWriteBusy` rather than as the 500 the production server really
    answered. The contended arm therefore turns it off: the assertion is about
    the status/body/headers an HTTP caller receives, and the body `code` asserted
    below is what proves the APP's handler - not the mere absence of an
    exception - produced that answer.
    """
    return TestClient(app, raise_server_exceptions=answer_server_errors)


def hold_write_lock(world):
    """A FOREIGN sqlite3 connection (not the store's) holding the write lock."""
    holder = sqlite3.connect(world.db.db_path, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    return holder


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


def lease(world, claim_id):
    """(lease_expires_at, last_heartbeat_at) - the two columns a heartbeat writes."""
    with world.db.get_connection() as conn:
        row = conn.execute("SELECT lease_expires_at, last_heartbeat_at FROM state_node_claims "
                           "WHERE claim_id=?", (claim_id,)).fetchone()
    return tuple(row)


def test_a_busy_heartbeat_answers_retryable_503_not_an_opaque_500(world, monkeypatch):
    """Arm A: the lock is never ours; the caller gets a retryable answer, not a 500."""
    monkeypatch.setenv(state_graph.STATE_WRITE_LOCK_SECONDS_ENV, "0.4")
    escapes = observe_escapes(monkeypatch)
    before = lease(world, world.claim["claim_id"])
    app = application(world, monkeypatch)
    holder = hold_write_lock(world)
    try:
        with client(app, answer_server_errors=False) as http:
            started = time.monotonic()
            response = http.post(HEARTBEAT)
            elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()

    # 503, not 500, and not a validation refusal: nothing about the request was
    # malformed - the lock simply was not available.
    assert response.status_code == 503, response.text
    assert response.headers["Retry-After"] == str(state_graph.STATE_WRITE_BUSY_RETRY_AFTER)
    payload = response.json()
    # The body code is what proves the app-wide handler answered: without it the
    # same tree would have produced Starlette's plain 500 page instead.
    assert payload["code"] == state_graph.STATE_WRITE_BUSY_CODE == "state_write_busy"
    assert payload["retry_after"] == state_graph.STATE_WRITE_BUSY_RETRY_AFTER
    assert "state_write_busy" in payload["detail"]

    # The typed error is what left the write transaction; no
    # sqlite3.OperationalError escaped it for this request.
    assert len(escapes) == 1, escapes
    assert all(isinstance(error, StateWriteBusy) for error in escapes), escapes
    assert not any(isinstance(error, sqlite3.OperationalError) for error in escapes), escapes

    # A busy write wrote nothing: the lease columns are exactly as they were.
    assert lease(world, world.claim["claim_id"]) == before
    # And the bounded wait stayed inside its bound instead of hanging the route.
    assert elapsed < 5, f"the bounded wait took {elapsed:.3f}s"


def test_the_lock_free_heartbeat_still_answers_200(world, monkeypatch):
    """Arm B: the app-wide default must not turn a healthy call into a 503.

    Deliberately cheap: no foreign connection and no shortened budget, so the
    lock is free and this only asserts the successful arm is left alone. The
    default client (raise_server_exceptions=True) is used here on purpose - a
    healthy request must not raise anything at all.
    """
    app = application(world, monkeypatch)
    with client(app) as http:
        response = http.post(HEARTBEAT)

    assert response.status_code == 200, response.text
    renewed = response.json()["results"][0]["renewed"]
    assert [item["id"] for item in renewed] == [world.claim["claim_id"]], renewed


def test_the_default_is_on_the_app_not_in_the_heartbeat_handler(world, monkeypatch):
    """Arm C: a route this change never touched gets the same answer.

    `api/dsh_driver_routers.py` is byte-identical to the base revision: the fix
    is a default for routes nobody thought of, not one more per-route clause. A
    hand-written route on the same app - one that knows nothing about the
    heartbeat - raises the same typed refusal and must answer the identical 503.
    """

    def forgotten(app, world):
        @app.get("/never-thought-of")
        def never_thought_of():
            raise StateWriteBusy("state_write_busy: nothing was written, retry")

    app = application(world, monkeypatch, add_routes=forgotten)
    with client(app) as http:
        response = http.get("/never-thought-of")

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "state_write_busy"
    assert response.headers["Retry-After"] == str(state_graph.STATE_WRITE_BUSY_RETRY_AFTER)


def test_both_assembly_points_apply_the_default(tmp_path):
    """The WIRING, not only the applier: both places that assemble an app call it.

    A handler that is defined but never applied is precisely the failure this
    change removes, so the two assembly points are checked rather than assumed
    from the definition sitting in `api/state_http.py`:

    * `api/main.py` — the product app, the one whose heartbeat answered 500;
    * `api/state_only.py` — the second assembly point, assembled without the
      workflow runtime.

    On the base tree `apply_state_write_busy` does not exist and this arm fails
    here too, which is the honest red rather than a silent skip.
    """
    from api import main as product
    from api.state_only import create_app

    assert StateWriteBusy in product.app.exception_handlers
    state_only = create_app(str(tmp_path / "state-only.sqlite"), "s" * 40, with_mcp=False)
    assert StateWriteBusy in state_only.exception_handlers
