"""Driver identity on the wire (multi-driver P0): token matrix, actors, routes.

Every credential x path combination, with the feature on and off. The feature
off rows pin the pre-P0 behavior byte for byte.
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from api import authz, driver_routers
from api import state_graph_routers as state_routes
from core import drivers
from core.state_database import StateDatabase

PEPPER = "p" * 48
LEGACY = "legacy-admin-token-0123456789abcdef"
EXTERNAL = "external-token-0123456789abcdef0123"
OWNER = "owner@example.com"
CF = {"Cf-Ray": "abc-CDG"}


def _app():
    app = FastAPI()
    app.include_router(driver_routers.router)

    @app.post("/probe/write", dependencies=[Depends(authz.require_writer)])
    def write(request: Request):
        return {"actor": state_routes.authenticated_actor(request),
                "driver_id": state_routes.authenticated_driver_id(request)}

    @app.get("/probe/admin", dependencies=[Depends(authz.require_admin)])
    def admin():
        return {"ok": True}

    @app.get("/probe/local-admin")
    def local_admin(request: Request):
        return {"ok": authz.local_admin_authority(request)}
    return app


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", {OWNER})
    monkeypatch.setattr(authz, "ADMIN_TOKEN", LEGACY)

    def email(headers, cookies=None):
        return headers.get("X-Test-Email")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", email)
    from api import mcp_router
    monkeypatch.setattr(mcp_router, "_EXTERNAL_TOKEN", EXTERNAL)
    return monkeypatch


@pytest.fixture
def off(base):
    base.setattr(authz, "driver_registry", lambda: None)
    with TestClient(_app()) as client:
        yield client


@pytest.fixture
def on(base, tmp_path):
    registry = drivers.DriverRegistry(StateDatabase(str(tmp_path / "s.sqlite")), PEPPER)
    registry.seed(LEGACY)
    codex = registry.register("codex", "Codex", actor="t")["token"]
    grok = registry.register("grok-bot", "Grok Bot", actor="t")["token"]
    base.setattr(authz, "driver_registry", lambda: registry)
    with TestClient(_app()) as client:
        yield client, registry, {"codex": codex, "grok": grok}


def _drv(token):
    return {"X-AItelier-Driver-Token": token}


def _adm(token):
    return {"X-AItelier-Admin-Token": token}


class TestFeatureOffIsUnchanged:
    def test_admin_token_writes_as_the_shared_operator(self, off):
        r = off.post("/probe/write", headers=_adm(LEGACY))
        assert r.status_code == 200
        assert r.json() == {"actor": "authorized-state-operator", "driver_id": None}

    def test_admin_token_through_cloudflare_is_refused(self, off):
        r = off.post("/probe/write", headers={**_adm(LEGACY), **CF})
        assert r.status_code == 403

    def test_bad_token_and_no_token(self, off):
        assert off.post("/probe/write", headers=_adm("wrong")).status_code == 403
        assert off.post("/probe/write").status_code == 403

    def test_owner_email_actor_is_the_plain_email(self, off):
        r = off.post("/probe/write", headers={"X-Test-Email": OWNER, **CF})
        assert r.json()["actor"] == OWNER

    def test_registry_routes_are_404_and_whoami_says_disabled(self, off):
        assert off.get("/api/drivers", headers=_adm(LEGACY)).status_code == 404
        me = off.get("/api/drivers/me", headers=_adm(LEGACY)).json()
        assert me["enabled"] is False and me["actor"] == "authorized-state-operator"

    def test_local_admin_authority_is_the_env_token(self, off):
        assert off.get("/probe/local-admin", headers=_adm(LEGACY)).json()["ok"] is True
        assert off.get("/probe/local-admin", headers={**_adm(LEGACY), **CF}).json()["ok"] is False


class TestFeatureOnMatrix:
    def test_driver_token_is_driver_actor(self, on):
        client, _, tokens = on
        r = client.post("/probe/write", headers=_drv(tokens["codex"]))
        assert r.json() == {"actor": "driver:codex", "driver_id": "codex"}
        # The historical header still carries any driver token.
        r = client.post("/probe/write", headers=_adm(tokens["grok"]))
        assert r.json() == {"actor": "driver:grok-bot", "driver_id": "grok-bot"}

    def test_legacy_admin_token_is_owner_cli(self, on):
        client, _, _ = on
        r = client.post("/probe/write", headers=_adm(LEGACY))
        assert r.json() == {"actor": "driver:owner-cli", "driver_id": "owner-cli"}

    def test_driver_token_through_cloudflare_is_refused(self, on):
        client, _, tokens = on
        assert client.post("/probe/write", headers={**_drv(tokens["codex"]), **CF}).status_code == 403

    @pytest.mark.usefixtures("on")
    def test_external_token_through_the_tunnel_is_public(self):
        # The public driver is identified (attribution); its write authority is
        # still decided by the MCP external-token gate, unchanged.
        req = type("R", (), {"headers": {**CF, "X-AItelier-MCP-External-Token": EXTERNAL}, "cookies": {}})()
        assert authz.request_identity(req).actor == "driver:public"
        req.headers["X-AItelier-MCP-External-Token"] = "wrong"
        assert authz.request_identity(req) is None

    def test_owner_email_is_owner(self, on):
        client, _, _ = on
        r = client.post("/probe/write", headers={"X-Test-Email": OWNER, **CF})
        assert r.json() == {"actor": "owner:" + OWNER, "driver_id": None}

    def test_unknown_suspended_and_rotated_tokens_are_refused(self, on):
        client, registry, tokens = on
        assert client.post("/probe/write", headers=_drv("aitd_unknown")).status_code == 403
        registry.set_status("codex", "suspended", 1, "pause", actor="t")
        assert client.post("/probe/write", headers=_drv(tokens["codex"])).status_code == 403
        old = tokens["grok"]
        registry.rotate("grok-bot", 1, actor="t")
        assert client.post("/probe/write", headers=_drv(old)).status_code == 403

    def test_admin_is_owner_cli_or_owner_not_ordinary_drivers(self, on):
        client, _, tokens = on
        assert client.get("/probe/admin", headers=_adm(LEGACY)).status_code == 200
        assert client.get("/probe/admin", headers={"X-Test-Email": OWNER, **CF}).status_code == 200
        r = client.get("/probe/admin", headers=_drv(tokens["codex"]))
        assert r.status_code == 403 and r.headers["X-AItelier-Denial"] == authz.ADMIN_REQUIRED
        assert client.get("/probe/local-admin", headers=_drv(tokens["codex"])).json()["ok"] is False
        assert client.get("/probe/local-admin", headers=_adm(LEGACY)).json()["ok"] is True


class TestRegistryRoutes:
    def test_whoami(self, on):
        client, _, tokens = on
        me = client.get("/api/drivers/me", headers=_drv(tokens["codex"])).json()
        assert me["enabled"] is True and me["actor"] == "driver:codex" and me["is_admin"] is False
        assert me["driver"]["driver_id"] == "codex" and "token_hash" not in me["driver"]
        assert client.get("/api/drivers/me").status_code == 403

    def test_owner_lifecycle_over_http(self, on):
        client, _, tokens = on
        h = _adm(LEGACY)
        r = client.post("/api/drivers", headers=h, json={"driver_id": "claude", "display_name": "Claude"})
        assert r.status_code == 200
        token = r.json()["token"]
        assert client.get("/api/drivers/me", headers=_drv(token)).json()["actor"] == "driver:claude"
        assert client.post("/api/drivers", headers=h,
                           json={"driver_id": "claude", "display_name": "x"}).status_code == 409
        listed = client.get("/api/drivers", headers=_drv(tokens["codex"])).json()["drivers"]
        assert {d["driver_id"] for d in listed} == {"public", "owner-cli", "codex", "grok-bot", "claude"}
        assert all("token_hash" not in d for d in listed)
        r = client.post("/api/drivers/claude/rotate", headers=h, json={"expected_revision": 1})
        assert r.status_code == 200 and r.json()["token"] != token
        assert client.get("/api/drivers/me", headers=_drv(token)).status_code == 403
        r = client.put("/api/drivers/claude/projects/aitelier", headers=h,
                       json={"status": "member", "expected_revision": 0, "reason": "join"})
        # No state_projects table in this bare DB: refused as an unknown project.
        assert r.status_code == 422
        r = client.post("/api/drivers/claude/status", headers=h,
                        json={"status": "suspended", "expected_revision": 2, "reason": "pause"})
        assert r.status_code == 200 and r.json()["status"] == "suspended"
        audit = client.get("/api/drivers/claude/audit", headers=h).json()["audit"]
        assert [a["operation"] for a in audit] == ["suspend", "rotate", "register"]
        assert all(a["actor"] == "driver:owner-cli" for a in audit)

    def test_ordinary_driver_cannot_administer(self, on):
        client, _, tokens = on
        h = _drv(tokens["codex"])
        assert client.post("/api/drivers", headers=h,
                           json={"driver_id": "evil", "display_name": "x"}).status_code == 403
        assert client.post("/api/drivers/grok-bot/rotate", headers=h,
                           json={"expected_revision": 1}).status_code == 403
        assert client.get("/api/drivers/grok-bot/audit", headers=h).status_code == 403

    def test_unknown_fields_are_refused(self, on):
        client, _, _ = on
        r = client.post("/api/drivers", headers=_adm(LEGACY),
                        json={"driver_id": "x", "display_name": "x", "token": "chosen"})
        assert r.status_code == 422
