"""Private-body boundary on the REST task read surfaces (authored UNRUN).

These tests pin ``api.authz.execution_progress``: an identity that may not read
private records must never receive ``Task.prompt``, ``Task.last_error`` or
``owner_email`` through the task/list read projection, while public
status/progress fields stay available; a trusted writer/admin keeps the
complete row.

They drive a REAL Starlette/FastAPI ``Request`` (app state + raw headers, with
``Request.cookies`` derived from those headers) through the REAL
``execution_progress`` -> ``may_read_private`` -> ``write_denial_reason`` chain.
Nothing in that chain is mocked. Only the active-gate configuration is scoped
per test with ``monkeypatch`` (team domain + audience), and the admin token is
an explicit synthetic value. ``_test_mode`` is False in every case, so the
authorization boundary is exercised rather than bypassed.

The prior fixture used a fake ``_Req`` that lacked ``cookies`` (AttributeError
at api/authz.py:111), permanently overwrote ``cf_access.is_configured`` and
leaked active-gate state into later tests, and used the ``_test_mode`` bypass as
privilege instead of a real admin/writer identity. This file replaces all of
that without touching production code or criteria.

Authored for the source phase and deliberately left UNRUN: no execution or
passing claim is made here.
"""

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api import authz
from core import cf_access


# A representative full task row: sensitive source/execution fields plus the
# public status/progress fields that must survive redaction.
FULL_ROW = {
    "id": 7,
    "project_id": "demo",
    "prompt": "SECRET EXECUTION PROMPT BODY",
    "last_error": "SECRET TRACE ERROR",
    "owner_email": "someone@example.com",
    "status": "running",
    "name": "demo",
    "created_at": "2026-10-07T00:00:00Z",
    "current_step": "1_2",
}

SENSITIVE_FIELDS = ("prompt", "last_error", "owner_email")

# Synthetic, explicit credential values for this harness only.
ADMIN_TOKEN = "synthetic-off-tunnel-admin-token"
TEAM_DOMAIN = "team.cloudflareaccess.com"
AUD = "test-aud"


@pytest.fixture
def gate_on(monkeypatch):
    """Arm the Cloudflare gate for ONE test, and only for that test.

    ``cf_access.is_configured()`` reads the module-level ``_TEAM_DOMAIN`` and
    ``_AUD``; setting both makes the gate active. ``monkeypatch`` restores the
    originals afterwards, so an active-gate assertion here can never contaminate
    a later test (the earlier version assigned ``cf_access.is_configured``
    globally and did exactly that).
    """
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", TEAM_DOMAIN)
    monkeypatch.setattr(cf_access, "_AUD", AUD)
    monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN_TOKEN)
    # Precondition: the boundary is genuinely active for this test.
    assert authz.gate_enabled() is True


def _request(headers=None):
    """A real request carrying app state, raw headers and derived cookies.

    ``app.state._test_mode`` is False: this is the production authorization
    path, not the test-mode bypass. ``Request.cookies`` is parsed from the raw
    headers, so ``cf_access.email_from_request_headers`` can consult it without
    raising.
    """
    app = SimpleNamespace(state=SimpleNamespace(_test_mode=False))
    raw_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/tasks",
        "raw_path": b"/api/tasks",
        "query_string": b"",
        "root_path": "",
        "headers": raw_headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "app": app,
    })


def test_anonymous_cf_ray_denies_sensitive_fields_but_keeps_status(gate_on):
    """Anonymous tunnelled request: sensitive body/trace/owner fields are
    stripped while public status and progress fields survive."""
    request = _request({"Cf-Ray": "anonymous-throughput-ray"})
    assert getattr(request.app.state, "_test_mode", False) is False

    out = authz.execution_progress(request, dict(FULL_ROW))

    for field in SENSITIVE_FIELDS:
        assert field not in out
    assert out["id"] == FULL_ROW["id"]
    assert out["status"] == FULL_ROW["status"]
    assert out["name"] == FULL_ROW["name"]


def test_off_tunnel_admin_keeps_the_complete_row(gate_on):
    """An off-tunnel admin token is a real privileged identity (no test-mode
    bypass) and receives the full row unchanged."""
    request = _request({"X-AItelier-Admin-Token": ADMIN_TOKEN})
    assert getattr(request.app.state, "_test_mode", False) is False

    assert authz.may_read_private(request) is True
    out = authz.execution_progress(request, dict(FULL_ROW))
    assert out == FULL_ROW


def test_admin_token_replayed_through_cf_ray_cannot_read_private_fields(gate_on):
    """The same admin token arriving through the Cloudflare tunnel is NOT
    honored; the identity is treated as unprivileged and the private fields are
    withheld."""
    request = _request({"Cf-Ray": "replay-ray",
                        "X-AItelier-Admin-Token": ADMIN_TOKEN})
    assert getattr(request.app.state, "_test_mode", False) is False

    assert authz.write_denial_reason(request) == authz.WRITE_DENIED_BAD_ADMIN_TOKEN
    assert authz.may_read_private(request) is False

    out = authz.execution_progress(request, dict(FULL_ROW))
    for field in SENSITIVE_FIELDS:
        assert field not in out
    assert out["id"] == FULL_ROW["id"]
    assert out["status"] == FULL_ROW["status"]

