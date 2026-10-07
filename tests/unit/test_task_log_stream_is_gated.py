"""GET /api/tasks/{task_id}/stream streams a PRIVATE execution body.

The SSE endpoint carried the SAME raw bytes as `GET /api/tasks/{task_id}`'s
`last_error` -- the sandbox command log -- but with no reader verdict at all, so
an anonymous request received each private body as 200 text/event-stream. The
per-task channel key is caller-supplied, so `GET /api/tasks/__global__/stream`
aliased the cross-project `__global__` fan-out and reached its raw, unprojected
event body the same way.

The property these tests pin is the one that was missing: the task-log stream is
a private execution READ and is refused to every identity that may not read a
private record -- while the public `GET /api/events/stream` progress surface and
an authorized writer's own stream stay open.

The gate is ARMED and test mode is OFF, so the ACTUAL authorization functions run,
not a test-only bypass. The stream fixture is FINITE on purpose: the body and the
`__END__` sentinel are buffered before the client connects, so the generator
replays them and closes instead of parking on its 15s heartbeat.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import authz
from api.routers import router as tasks_router, stream_task_logs
from api.sse_manager import stream_manager
from core import cf_access

WRITER = "writer@example.com"
ADMIN_TOKEN = "s3cret-admin-token"
PRIVATE_BODY = "PRIVATE-EXECUTION-BODY-Q7"


def _stream_route():
    """The task-log stream route, off the ROUTER (the object carrying the dep)."""
    return next(r for r in tasks_router.routes if r.path.endswith("/stream"))


def test_the_stream_route_carries_require_reader():
    """Structural: the dependency is declared on the route, so a later edit that
    drops it fails here rather than serving private bodies anonymously."""
    route = _stream_route()
    calls = [d.call for d in route.dependant.dependencies]
    assert authz.require_reader in calls, f"{route.path} is an unguarded private read"


def test_the_guard_is_the_shared_private_read_verdict():
    """The stream must reuse the SAME verdict as every other private execution
    read, not a bespoke check that can drift from it."""
    assert _stream_route().dependant.dependencies
    assert stream_task_logs.__module__ == "api.routers"


@pytest.fixture
def gated_client(monkeypatch):
    """A minimal app carrying the tasks router, gate ARMED, test mode OFF."""
    app = FastAPI()
    app.include_router(tasks_router)
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {WRITER})
    monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(
        cf_access, "verify",
        lambda tok: {"email": tok[4:]} if tok.startswith("jwt:") else None,
    )
    with TestClient(app) as c:
        monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
        yield c


ANONYMOUS_CONTROLS = [
    {},                                                       # no credential
    {"X-AItelier-Admin-Token": "invalid"},                    # rejected admin token
    {"Cf-Access-Jwt-Assertion": "invalid"},                   # rejected JWT
    {"Cf-Access-Jwt-Assertion": "jwt:reader@example.com"},    # signed in, not a writer
]


class TestThePrivateBodyIsRefused:
    @pytest.mark.parametrize("headers", ANONYMOUS_CONTROLS)
    @pytest.mark.parametrize("channel", ["1", "__global__"])
    def test_anonymous_or_invalid_is_refused(self, gated_client, headers, channel):
        """Both the numeric task channel and the `__global__` alias are refused,
        and no private body comes back on the refusal."""
        resp = gated_client.get(f"/api/tasks/{channel}/stream", headers=headers)
        assert resp.status_code == 403, (channel, headers, resp.status_code)
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        assert PRIVATE_BODY not in resp.text

    def test_the_alias_is_not_a_second_public_progress_door(self, gated_client):
        """`/api/tasks/__global__/stream` must not hand an anonymous caller the
        raw `__global__` fan-out that `GET /api/events/stream` projects."""
        assert gated_client.get("/api/tasks/__global__/stream").status_code == 403

    def test_the_public_progress_surface_stays_open(self, gated_client):
        """A gate that refuses everyone is an outage: the public progress stream
        is a different route and stays reachable."""
        from api.main import health_check
        assert health_check() == {"status": "ok", "engine": "DPE SOTA v3.0"}


class TestTheAuthorizedStreamWorks:
    def _seed_and_read(self, client, channel, headers):
        async def seed():
            await stream_manager.push_log(channel, PRIVATE_BODY)
            await stream_manager.push_log(channel, "__END__")
        asyncio.run(seed())
        body = []
        with client.stream("GET", f"/api/tasks/{channel}/stream",
                            headers=headers) as resp:
            assert resp.status_code == 200, resp.status_code
            assert resp.headers["content-type"] == "text/event-stream; charset=utf-8"
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    body.append(json.loads(line[6:])["log"])
        return body

    def test_a_writer_gets_the_real_log_bytes(self, gated_client):
        """The guard must not blank the stream for the identity that may read it:
        an authorized writer receives the actual body, unchanged."""
        headers = {"Cf-Ray": "abc",
                   "Cf-Access-Jwt-Assertion": f"jwt:{WRITER}"}
        assert self._seed_and_read(gated_client, "stream-fixture-writer",
                                   headers) == [PRIVATE_BODY]

    def test_the_admin_token_gets_the_real_log_bytes(self, gated_client):
        """The off-tunnel admin token is the other writer identity; it must reach
        the same real body through the real auth functions."""
        headers = {"X-AItelier-Admin-Token": ADMIN_TOKEN}
        assert self._seed_and_read(gated_client, "stream-fixture-admin",
                                   headers) == [PRIVATE_BODY]
