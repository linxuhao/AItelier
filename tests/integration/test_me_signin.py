"""/api/me has to tell a reader WHY they are a reader, and where to go.

Both failures render identically in the UI otherwise — "no credential" and
"credential refused" are both a read-only session — and the second is the one
that actually bites: an Access application re-created with a fresh AUD while
AITELIER_CF_AUD still names the old one authenticates the browser and is then
rejected at the origin, silently.
"""
from fastapi.testclient import TestClient


class TestWhoAmISignin:
    def test_signin_url_is_empty_unless_the_deployment_declares_one(
            self, client: TestClient, monkeypatch):
        monkeypatch.delenv("AITELIER_SIGNIN_URL", raising=False)
        body = client.get("/api/me").json()
        # A sign-in button pointing at an Access application that does not
        # exist is a button to nowhere; the UI keys off this being empty.
        assert body["signin_url"] == ""

    def test_signin_url_is_reported_when_set(self, client: TestClient, monkeypatch):
        monkeypatch.setenv("AITELIER_SIGNIN_URL", "https://example.test/signin")
        body = client.get("/api/me").json()
        assert body["signin_url"] == "https://example.test/signin"

    def test_no_credential_is_not_an_error(self, client: TestClient):
        body = client.get("/api/me").json()
        assert body["auth_error"] is None
        assert body["email"] is None

    def test_a_refused_credential_is_reported_as_one(self, client: TestClient):
        # A cookie that cannot be verified (no team/AUD configured here, so
        # verification fails exactly as it does against a dead audience).
        resp = client.get("/api/me", cookies={"CF_Authorization": "not-a-valid-jwt"})
        body = resp.json()
        assert body["email"] is None
        assert body["auth_error"] == "credential_rejected"

    def test_a_refused_credential_leaks_no_detail(self, client: TestClient):
        body = client.get("/api/me", cookies={"CF_Authorization": "x.y.z"}).json()
        # Never say WHICH check failed: audience, issuer and expiry are the
        # operator's business, not an unauthenticated caller's. The response
        # carries these five keys and nothing else — no claim, no reason.
        assert set(body) == {"email", "can_write", "gate_enabled",
                             "signin_url", "auth_error"}
        assert body["auth_error"] == "credential_rejected"
