# tests/integration/test_cli_middleware.py
# Tests for the CLI API localhost-only middleware.

import pytest
from fastapi import FastAPI, Request, HTTPException
from fastapi.testclient import TestClient
from api.main import app
from api.dependencies import get_db_manager, get_workspace_manager
from core.db_manager import DBManager
from core.workspace_manager import WorkspaceManager


def test_localhost_allowed_by_default(client: TestClient):
    """Standard conftest client (test_mode=True) can reach all endpoints."""
    resp = client.get("/health")
    assert resp.status_code == 200


def test_middleware_rejects_non_localhost(monkeypatch):
    """Middleware should raise 403 for non-localhost client host."""
    import api.main
    from api.main import localhost_only

    # AITELIER_ALLOW_EXTERNAL=1 (set in the Docker backend) freezes
    # api.main._ALLOW_EXTERNAL=True at import, which bypasses the guard. Pin it
    # off so this test exercises the reject path regardless of ambient env.
    monkeypatch.setattr(api.main, "_ALLOW_EXTERNAL", False)

    class FakeRequest:
        class Client:
            host = "203.0.113.1"
        client = Client()
        app = app
        state = app.state

    import asyncio
    async def _test():
        with pytest.raises(HTTPException) as exc_info:
            # call_next is irrelevant — middleware raises before calling it
            await localhost_only(FakeRequest(), lambda r: None)
        assert exc_info.value.status_code == 403

    asyncio.run(_test())


def test_middleware_allows_localhost_ip():
    """Middleware should allow 127.0.0.1."""
    from api.main import localhost_only
    from starlette.responses import PlainTextResponse
    import asyncio

    class FakeRequest:
        class Client:
            host = "127.0.0.1"
        client = Client()
        app = app
        state = app.state

    async def _test():
        async def fake_call_next(request):
            return PlainTextResponse("ok")
        response = await localhost_only(FakeRequest(), fake_call_next)
        assert response.status_code == 200

    asyncio.run(_test())


def test_middleware_allows_ipv6_localhost():
    """Middleware should allow ::1."""
    from api.main import localhost_only
    from starlette.responses import PlainTextResponse
    import asyncio

    class FakeRequest:
        class Client:
            host = "::1"
        client = Client()
        app = app
        state = app.state

    async def _test():
        async def fake_call_next(request):
            return PlainTextResponse("ok")
        response = await localhost_only(FakeRequest(), fake_call_next)
        assert response.status_code == 200

    asyncio.run(_test())


def test_test_mode_bypasses_middleware(client: TestClient):
    """When _test_mode is True, middleware skips the check entirely."""
    # TestClient uses 'testclient' as host — NOT in allowed list
    # But it works because _test_mode bypasses the check
    resp = client.get("/health")
    assert resp.status_code == 200
