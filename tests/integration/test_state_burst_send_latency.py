"""A burst of State reads no longer starves a driver message (backend latency study, 2026-11-22).

Each DSH driver start read every todo plan's attempt at once. Every State request
took anyio's 40-token thread limiter about four times, and its `get_service` hop
rebuilt every store's schema, so a burst held every token while a
send_driver_message queued behind it for 30-120 s. Now:

* schema setup runs once per process per database file (core.state_schema_setup),
  so no worker is inside schema setup while endpoint work runs;
* get_service resolves the request identity once (one driver-token lookup);
* the State routes take the db/workspace getters on the event loop, so a request
  takes the limiter twice (reads) or three times (a command, with its verdict).

The app is the real `/api/state` router on a scratch database, driven in-process.
Measured on the base commit in the same container: the send took 6.3 s behind the
burst, 81 graph schema setups ran during it, and requests took the limiter 4/4/5
times with 3 token lookups each.
"""
import asyncio
import sqlite3
import statistics
import threading
import time

import httpx
import pytest
from fastapi import FastAPI

import anyio._backends._asyncio as anyio_backend
from api import authz
from api import state_graph_routers as routes
from api import state_http
from api.dependencies import get_db_manager, get_workspace_manager
from core import state_schema_setup
from core.db_manager import DBManager
from core.drivers import DriverRegistry
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager

READS = 80
# The base commit measured 6.3 s in a 2-CPU container; the fix measures under 2 s
# there on an overloaded host. The bound keeps a wide margin for host load.
SEND_BOUND_SECONDS = 5.0


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = DBManager(str(tmp_path / "state.db"))
    seed = StateService(db, None, actor="fixture", project_read_trusted=True)
    seed.create_project("p", "P")
    seed.store.add_nodes("p", [{"key": "n", "goal": "N",
                                "acceptance": [{"id": "a", "kind": "test", "description": "d"}]}])
    attempt_id = seed.start_external_attempt("p", "n", 1, "fixture", "x", "x")["attempt_id"]
    with db.get_connection() as conn:
        template = dict(conn.execute("SELECT * FROM state_attempts WHERE attempt_id=?", (attempt_id,)).fetchone())
        template.pop("seq")
        columns = list(template)
        conn.executemany(
            "INSERT INTO state_attempts(" + ",".join(columns) + ") VALUES(" + ",".join("?" * len(columns)) + ")",
            [tuple(dict(template, attempt_id=f"h-{i}", request_key=f"k-{i}", external_id=f"e-{i}",
                        status="candidate", context_json="x" * 2000)[c] for c in columns) for i in range(2000)])
        conn.executemany("INSERT INTO state_external_owners(attempt_id,project_id,node_key,harness,external_id,"
                         "status,admitted_at,updated_at) VALUES(?,'p','n','fixture',?,'settled','t','t')",
                         [(f"h-{i}", f"e-{i}") for i in range(2000)])
        conn.execute("CREATE TABLE tick(n INTEGER)")
        conn.commit()
    registry = DriverRegistry(db, "burst-test-pepper-not-a-production-secret")
    tokens = {}
    for did in ("ada", "bob"):
        tokens[did] = registry.register(did, did, actor="test")["token"]
        registry.set_membership("p", did, "member", 0, "fixture", actor="test")
    lookups = []
    lookup = registry.lookup_token
    monkeypatch.setattr(registry, "lookup_token", lambda token: lookups.append(1) or lookup(token))
    monkeypatch.setattr(authz, "driver_registry", lambda: registry)
    ws = WorkspaceManager(str(tmp_path / "ws"))
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db_manager] = lambda: db
    app.dependency_overrides[get_workspace_manager] = lambda: ws

    acquisitions = []
    acquire = anyio_backend.CapacityLimiter.acquire_on_behalf_of

    async def counted(self, borrower):
        acquisitions.append(1)
        return await acquire(self, borrower)
    monkeypatch.setattr(anyio_backend.CapacityLimiter, "acquire_on_behalf_of", counted)

    # Witness: schema setups that run, and endpoint work that starts while one runs.
    witness = {"setups": 0, "inside": 0, "overlaps": 0}
    lock = threading.Lock()
    once = state_schema_setup.once

    def watched_once(handle, name, setup, *args):
        def run(*a):
            with lock:
                witness["setups"] += 1
                witness["inside"] += 1
            try:
                return setup(*a)
            finally:
                with lock:
                    witness["inside"] -= 1
        return once(handle, name, run, *args)
    monkeypatch.setattr(state_schema_setup, "once", watched_once)
    execute = state_http.execute

    def watched_execute(*args, **kwargs):
        with lock:
            if witness["inside"]:
                witness["overlaps"] += 1
        return execute(*args, **kwargs)
    monkeypatch.setattr(state_http, "execute", watched_execute)
    return {"db": db, "app": app, "attempt_id": attempt_id, "headers": {"X-AItelier-Driver-Token": tokens["ada"]},
            "lookups": lookups, "acquisitions": acquisitions, "witness": witness}


def _send_body(key):
    return {"request_key": key, "subject": "s", "body": "b", "target_driver_id": "bob"}


def test_burst_of_reads_does_not_starve_a_driver_message(world):
    attempt_id, headers = world["attempt_id"], world["headers"]
    result = {}

    async def main():
        transport = httpx.ASGITransport(app=world["app"])
        async with httpx.AsyncClient(transport=transport, base_url="http://state", timeout=120) as client:
            calls = {
                "get_attempt": lambda: client.get(f"/api/state/attempts/{attempt_id}", headers=headers),
                "query": lambda: client.post("/api/state/query/get_attempt",
                                             json={"attempt_id": attempt_id}, headers=headers),
                "command": lambda: client.post("/api/state/commands/send_driver_message",
                                               json=_send_body(f"one-{time.monotonic_ns()}"), headers=headers),
            }
            per_request = {}
            for name, call in calls.items():
                assert (await call()).status_code == 200
                world["acquisitions"].clear()
                world["lookups"].clear()
                response = await call()
                assert response.status_code == 200, response.text[:300]
                per_request[name] = (len(world["acquisitions"]), len(world["lookups"]))
            result["per_request"] = per_request

            stop = threading.Event()

            def writer():           # something commits every 100 ms, as on the live host
                conn = sqlite3.connect(world["db"].db_path, timeout=10)
                try:
                    while not stop.is_set():
                        conn.execute("INSERT INTO tick VALUES(1)")
                        conn.commit()
                        time.sleep(0.1)
                finally:
                    conn.close()
            thread = threading.Thread(target=writer)
            thread.start()
            world["witness"].update(setups=0, overlaps=0)
            started = time.perf_counter()

            async def read():
                response = await client.get(f"/api/state/attempts/{attempt_id}", headers=headers)
                assert response.status_code == 200
                return time.perf_counter() - started

            async def send():
                await asyncio.sleep(0)
                response = await client.post("/api/state/commands/send_driver_message",
                                             json=_send_body("during-the-burst"), headers=headers)
                assert response.status_code == 200, response.text[:300]
                return time.perf_counter() - started
            try:
                reads = [asyncio.create_task(read()) for _ in range(READS)]
                sent = asyncio.create_task(send())
                result["reads"] = await asyncio.gather(*reads)
                result["send"] = await sent
            finally:
                stop.set()
                thread.join()

    asyncio.run(main())
    witness = world["witness"]
    print(f"BURST send={result['send']:.3f}s reads median={statistics.median(result['reads']):.3f}s "
          f"max={max(result['reads']):.3f}s setups={witness['setups']} overlaps={witness['overlaps']} "
          f"per_request(limiter, token lookups)={result['per_request']}")
    assert witness["setups"] == 0, "a request ran schema setup on a file already set up"
    assert witness["overlaps"] == 0, "endpoint work ran while a worker was inside schema setup"
    assert result["send"] < SEND_BOUND_SECONDS, f"the send took {result['send']:.2f}s behind {READS} reads"
    # Limiter tokens per request: a read takes get_service + its endpoint; a command
    # also takes its writer verdict. One driver-token lookup per request in get_service.
    assert result["per_request"] == {"get_attempt": (2, 1), "query": (2, 1), "command": (3, 1)}
