"""The write gate must report WHY it denied, not just that it did.

`request_can_write` returned a bare boolean, so `write_gate` could only emit one
hardcoded sentence for three different situations — not signed in, signed in
without write rights, and a bad admin token all read the same. The reason now
comes back as a stable code in the 403 body, which the SPA maps to localized
text (web/src/lib/api.ts:errorMessageKey).

Guarded here: the three codes, that WHO may write is unchanged, and that the
body never echoes the admin token, the writer allowlist or a JWT claim.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import authz
from api.main import app
from core import cf_access

WRITER = "writer@example.com"
STRANGER = "stranger@example.com"


class _Req:
    """Minimal stand-in for the pieces of Request that authz touches."""

    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


@pytest.fixture
def gate_on(monkeypatch):
    """Cloudflare verification configured → the gate is active."""
    monkeypatch.setattr(cf_access, "_TEAM_DOMAIN", "team.cloudflareaccess.com")
    monkeypatch.setattr(cf_access, "_AUD", "test-aud")
    monkeypatch.setattr(authz, "WRITERS", {WRITER})
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "s3cret-admin-token")
    # Any token spelled "jwt:<email>" verifies to that identity; anything else
    # is rejected, exactly as a bad signature would be.
    monkeypatch.setattr(
        cf_access, "verify",
        lambda tok: {"email": tok[4:]} if tok.startswith("jwt:") else None,
    )


class TestDenialReason:
    def test_no_credential_at_all(self, gate_on):
        assert authz.write_denial_reason(_Req()) == authz.WRITE_DENIED_NOT_AUTHENTICATED

    def test_valid_identity_outside_the_allowlist(self, gate_on):
        req = _Req({"Cf-Ray": "abc", "Cf-Access-Jwt-Assertion": f"jwt:{STRANGER}"})
        assert authz.write_denial_reason(req) == authz.WRITE_DENIED_NOT_A_WRITER

    def test_unverifiable_jwt_is_no_credential(self, gate_on):
        req = _Req({"Cf-Ray": "abc", "Cf-Access-Jwt-Assertion": "garbage"})
        assert authz.write_denial_reason(req) == authz.WRITE_DENIED_NOT_AUTHENTICATED

    def test_wrong_admin_token(self, gate_on):
        req = _Req({"X-AItelier-Admin-Token": "wrong"})
        assert authz.write_denial_reason(req) == authz.WRITE_DENIED_BAD_ADMIN_TOKEN

    def test_admin_token_offered_through_the_tunnel_is_not_honored(self, gate_on):
        """The token is only honored off-tunnel — a replay through the edge is
        still a denial, and reads as a bad token rather than silently working."""
        req = _Req({"Cf-Ray": "abc", "X-AItelier-Admin-Token": "s3cret-admin-token"})
        assert authz.write_denial_reason(req) == authz.WRITE_DENIED_BAD_ADMIN_TOKEN


class TestWhoMayWriteIsUnchanged:
    def test_allowlisted_identity_passes(self, gate_on):
        req = _Req({"Cf-Ray": "abc", "Cf-Access-Jwt-Assertion": f"jwt:{WRITER}"})
        assert authz.write_denial_reason(req) is None
        assert authz.request_can_write(req) is True

    def test_admin_token_off_tunnel_passes(self, gate_on):
        req = _Req({"X-AItelier-Admin-Token": "s3cret-admin-token"})
        assert authz.write_denial_reason(req) is None
        assert authz.request_can_write(req) is True

    def test_gate_off_lets_everyone_through(self, monkeypatch):
        monkeypatch.setattr(cf_access, "_AUD", "")
        assert authz.gate_enabled() is False
        assert authz.write_denial_reason(_Req()) is None
        assert authz.request_can_write(_Req()) is True

    @pytest.mark.parametrize("headers", [
        {},
        {"X-AItelier-Admin-Token": "wrong"},
        {"Cf-Ray": "abc", "Cf-Access-Jwt-Assertion": f"jwt:{STRANGER}"},
    ])
    def test_verdict_still_agrees_with_the_reason(self, gate_on, headers):
        req = _Req(headers)
        assert authz.request_can_write(req) is False


class TestDenialBody:
    def test_every_code_has_a_message(self):
        for code in (authz.WRITE_DENIED_NOT_AUTHENTICATED,
                     authz.WRITE_DENIED_NOT_A_WRITER,
                     authz.WRITE_DENIED_BAD_ADMIN_TOKEN):
            body = authz.denial_body(code)
            assert body["code"] == code
            assert isinstance(body["detail"], str) and body["detail"]


class TestWriteGateMiddleware:
    """End to end through the real app, with the gate actually armed."""

    @pytest.fixture
    def gated_client(self, gate_on, monkeypatch):
        import api.main as main
        # The TestClient's peer is "testclient", never 127.0.0.1 — same as the
        # containerized deployment, which runs with AITELIER_ALLOW_EXTERNAL=1.
        monkeypatch.setattr(main, "_ALLOW_EXTERNAL", True)
        with TestClient(app) as c:
            # Set AFTER startup so the lifespan still takes the test-mode path
            # (no instance lock, no claim recovery, no scheduler).
            monkeypatch.setattr(app.state, "_test_mode", False, raising=False)
            yield c

    def test_reader_gets_403_with_a_reason(self, gated_client):
        r = gated_client.post("/api/projects", json={"name": "x"})
        assert r.status_code == 403
        assert r.json() == {
            "detail": authz.DENIAL_MESSAGES[authz.WRITE_DENIED_NOT_AUTHENTICATED],
            "code": authz.WRITE_DENIED_NOT_AUTHENTICATED,
        }

    def test_non_writer_identity_gets_its_own_code(self, gated_client):
        r = gated_client.post(
            "/api/projects", json={"name": "x"},
            headers={"Cf-Ray": "abc", "Cf-Access-Jwt-Assertion": f"jwt:{STRANGER}"},
        )
        assert r.status_code == 403
        assert r.json()["code"] == authz.WRITE_DENIED_NOT_A_WRITER

    def test_bad_admin_token_gets_its_own_code(self, gated_client):
        r = gated_client.post(
            "/api/projects", json={"name": "x"},
            headers={"X-AItelier-Admin-Token": "wrong"},
        )
        assert r.status_code == 403
        assert r.json()["code"] == authz.WRITE_DENIED_BAD_ADMIN_TOKEN

    def test_denial_leaks_nothing(self, gated_client):
        r = gated_client.post(
            "/api/projects", json={"name": "x"},
            headers={"X-AItelier-Admin-Token": "wrong",
                     "Cf-Access-Jwt-Assertion": f"jwt:{STRANGER}"},
        )
        blob = r.text
        assert "s3cret-admin-token" not in blob
        assert "wrong" not in blob
        assert WRITER not in blob
        assert STRANGER not in blob

    def test_reads_stay_open(self, gated_client):
        assert gated_client.get("/health").status_code == 200
