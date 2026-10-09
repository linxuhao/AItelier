"""Driver identity inside State (multi-driver P0): director_identity pinning,
legacy attempt continuity, the State-only service, and client credentials."""
from __future__ import annotations

import os
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from core import drivers
from core.driver_credentials import CredentialError, auth_headers, driver_token
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService


def _service(path, actor, driver_id=None):
    return StateService(StateDatabase(str(path)), actor=actor, project_read_trusted=True,
                        driver_id=driver_id)


class TestDirectorIdentity:
    def _send(self, svc, identity):
        args = {"sender_project_id": "p", "request_key": "k-" + str(identity), "subject": "s",
                "body": "b", "broadcast": True}
        if identity is not None:
            args["director_identity"] = identity
        return execute(svc, "send_director_message", args, allow_write=True)

    def test_a_driver_cannot_claim_another_identity(self, tmp_path):
        db = tmp_path / "s.sqlite"
        _service(db, "t").create_project("p", "P")
        svc = _service(db, "driver:codex", "codex")
        assert self._send(svc, "grok-bot")["code"] == "invalid_request"

    def test_own_id_or_label_is_accepted(self, tmp_path):
        db = tmp_path / "s.sqlite"
        _service(db, "t").create_project("p", "P")
        svc = _service(db, "driver:codex", "codex")
        for identity in ("codex", "codex/sub-1"):
            out = self._send(svc, identity)
            assert out.get("code") != "invalid_request", out

    def test_without_driver_id_nothing_changes(self, tmp_path):
        db = tmp_path / "s.sqlite"
        _service(db, "t").create_project("p", "P")
        out = self._send(_service(db, "authorized-state-operator"), "anyone at all")
        assert out.get("code") != "invalid_request", out

    def test_non_director_action_raises(self, tmp_path):
        db = tmp_path / "s.sqlite"
        svc0 = _service(db, "t")
        svc0.create_project("p", "P")
        svc0.store.add_nodes("p", [{"key": "a", "goal": "A", "acceptance": [
            {"id": "c", "kind": "test", "description": "Check"}]}])
        svc = _service(db, "driver:codex", "codex")
        with pytest.raises(StateGraphError):
            execute(svc, "set_node_priority", {"project_id": "p", "node_key": "a", "priority": 5,
                                               "expected_priority": 0, "reason": "r",
                                               "director_identity": "grok-bot"}, allow_write=True)


class TestLegacyAttemptContinuity:
    def _start(self, svc):
        svc.create_project("p", "P")
        svc.store.add_nodes("p", [{"key": "a", "goal": "A", "acceptance": [
            {"id": "c", "kind": "test", "description": "Check"}]}])
        node = svc.store.get_node("p", "a")
        return execute(svc, "start_external_attempt", {
            "project_id": "p", "node_key": "a", "expected_revision": node["revision"],
            "harness": "h", "external_id": "e-1", "request_key": "rk-1"}, allow_write=True)

    def _report(self, svc, started, version=0):
        return execute(svc, "report_external_attempt", {
            "attempt_id": started["attempt_id"], "observation_id": "o-" + svc.actor.replace(":", "-") + f"-{version}",
            "expected_version": version, "context_hash": started["context_hash"],
            "status": "running", "report_ref": "/tmp/report.json", "report_sha256": "0" * 64},
            allow_write=True)

    def test_pre_p0_attempt_continues_under_a_driver(self, tmp_path):
        db = tmp_path / "s.sqlite"
        started = self._start(_service(db, drivers.LEGACY_ACTOR))
        self._report(_service(db, "driver:codex", "codex"), started)

    def test_a_driver_attempt_is_not_reportable_by_another_driver(self, tmp_path):
        db = tmp_path / "s.sqlite"
        codex = _service(db, "driver:codex", "codex")
        started = self._start(codex)
        from core.state_graph import StateConflict
        with pytest.raises(StateConflict):
            self._report(_service(db, "driver:grok-bot", "grok-bot"), started)
        self._report(codex, started)


class TestStateOnlyService:
    TOKEN = "s" * 40

    def _client(self, tmp_path, monkeypatch, enabled):
        monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path))
        monkeypatch.delenv("AITELIER_ADMIN_TOKEN", raising=False)
        if enabled:
            monkeypatch.setenv(drivers.FEATURE_ENV, "on")
            (tmp_path / drivers.PEPPER_SECRET_NAME).write_text("p" * 48)
        else:
            monkeypatch.delenv(drivers.FEATURE_ENV, raising=False)
        drivers._REGISTRIES.clear()
        from api.state_only import create_app
        db = str(tmp_path / "s.sqlite")
        app = create_app(db, self.TOKEN, with_mcp=False)
        return app, db

    def test_dedicated_token_unchanged_and_driver_token_refused_when_off(self, tmp_path, monkeypatch):
        app, _ = self._client(tmp_path, monkeypatch, enabled=False)
        with TestClient(app) as client:
            ok = client.get("/api/state/projects", headers={"Authorization": "Bearer " + self.TOKEN})
            assert ok.status_code == 200
            refused = client.get("/api/state/projects", headers={"X-AItelier-Driver-Token": "aitd_x"})
            assert refused.status_code == 401

    def test_driver_token_is_recorded_as_the_driver(self, tmp_path, monkeypatch):
        app, db = self._client(tmp_path, monkeypatch, enabled=True)
        registry = drivers.registry_for(StateDatabase(db))
        token = registry.register("codex", "Codex", actor="t")["token"]
        with TestClient(app) as client:
            for project, credential in (("p", token), ("q", self.TOKEN)):
                h = {"Authorization": "Bearer " + credential}
                r = client.post("/api/state/commands/create_project", headers=h,
                                json={"project_id": project, "title": project.upper()})
                assert r.status_code == 200, r.text
                r = client.post("/api/state/commands/open_project", headers=h, json={"project_id": project})
                assert r.status_code == 200, r.text
        import sqlite3
        conn = sqlite3.connect(db)
        actors = {row[0] for row in conn.execute("SELECT changed_by FROM state_project_access")}
        conn.close()
        assert "driver:codex" in actors and "authenticated-state-token" in actors
        drivers._REGISTRIES.clear()


class TestClientCredentials:
    def test_order_and_legacy_fallback(self, tmp_path):
        d = tmp_path / ".aitelier-drivers"
        d.mkdir(mode=0o700)
        f = d / "codex.token"
        f.write_text("aitd_file\n")
        os.chmod(f, 0o600)
        assert auth_headers(environ={"AITELIER_ADMIN_TOKEN": "adm"}, home=tmp_path) == {
            "X-AItelier-Admin-Token": "adm"}
        assert auth_headers(environ={}, home=tmp_path) == {}
        env = {"AITELIER_ADMIN_TOKEN": "adm", "AITELIER_DRIVER_ID": "codex"}
        assert auth_headers(environ=env, home=tmp_path) == {"X-AItelier-Driver-Token": "aitd_file"}
        env["AITELIER_DRIVER_TOKEN_FILE"] = str(f)
        assert driver_token(env, tmp_path) == "aitd_file"
        env["AITELIER_DRIVER_TOKEN"] = "aitd_env"
        assert driver_token(env, tmp_path) == "aitd_env"

    def test_group_readable_file_is_refused(self, tmp_path):
        f = tmp_path / "t.token"
        f.write_text("aitd_x")
        os.chmod(f, 0o644)
        with pytest.raises(CredentialError) as info:
            driver_token({"AITELIER_DRIVER_TOKEN_FILE": str(f)})
        assert "aitd_x" not in str(info.value)

    def test_driver_token_script_writes_0600(self, tmp_path):
        import importlib.util
        from pathlib import Path
        spec = importlib.util.spec_from_file_location(
            "driver_token_script", Path(__file__).resolve().parents[2] / "scripts/driver_token.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        path = mod.write_token_file("codex", "aitd_secret", home=tmp_path)
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert driver_token({"AITELIER_DRIVER_ID": "codex"}, tmp_path) == "aitd_secret"


def test_child_processes_never_inherit_driver_credentials():
    from core.env_scrub import scrubbed_env
    env = scrubbed_env({"AITELIER_DRIVER_TOKEN": "aitd_x", "AITELIER_ADMIN_TOKEN": "a",
                        "AITELIER_DRIVER_TOKEN_FILE": "/x", "AITELIER_OWNER_TOKEN_FILE": "/y",
                        "AITELIER_DRIVER_ID": "codex", "PATH": "/bin"})
    assert env == {"AITELIER_DRIVER_ID": "codex", "PATH": "/bin"}


def _driver_token_script():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "driver_token_script_selfreg", Path(__file__).resolve().parents[2] / "scripts/driver_token.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeServer:
    """The /api/drivers surface self-register talks to, with tokens kept in memory."""

    OWNER: ClassVar[dict] = {"X-AItelier-Admin-Token": "owner"}

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.drivers = {}   # id -> {"revision": n, "token": t}
        self.calls = []

    def __call__(self, method, path, body, headers, missing_ok=False):
        self.calls.append((method, path))
        if path == "/api/drivers/me":
            if not self.enabled:
                return {"enabled": False}
            token = headers.get("X-AItelier-Driver-Token")
            for driver_id, row in self.drivers.items():
                if token and token == row["token"]:
                    return {"enabled": True, "driver_id": driver_id}
            return {"enabled": True, "driver_id": "owner-cli"} if headers == self.OWNER else None
        assert headers == self.OWNER, "only the owner credential may administer"
        if method == "GET":
            row = self.drivers.get(path.rsplit("/", 1)[1])
            return None if row is None else {"revision": row["revision"]}
        if path == "/api/drivers":
            assert body["is_admin"] is False
            self.drivers[body["driver_id"]] = {"revision": 1, "token": "aitd_" + body["driver_id"] + "_1"}
            return {"token": self.drivers[body["driver_id"]]["token"], "driver": {}}
        driver_id = path.split("/")[3]
        row = self.drivers[driver_id]
        assert body["expected_revision"] == row["revision"]
        row["revision"] += 1
        row["token"] = f"aitd_{driver_id}_{row['revision']}"
        return {"token": row["token"], "driver": {}}


class TestSelfRegister:
    def _run(self, mod, server, tmp_path, driver_id="newbie", **kw):
        return mod.self_register(driver_id, "New driver", home=tmp_path,
                                 owner_headers=lambda: dict(server.OWNER), call=server, **kw)

    def test_registers_once_then_is_idempotent(self, tmp_path):
        mod, server = _driver_token_script(), _FakeServer()
        first = self._run(mod, server, tmp_path)
        assert first["status"] == "registered"
        path = tmp_path / ".aitelier-drivers" / "newbie.token"
        assert path.stat().st_mode & 0o777 == 0o600
        assert driver_token({"AITELIER_DRIVER_ID": "newbie"}, tmp_path) == "aitd_newbie_1"
        assert "aitd_newbie_1" not in repr(first)
        again = self._run(mod, server, tmp_path)
        assert again["status"] == "already_registered"
        assert server.drivers["newbie"]["revision"] == 1

    def test_existing_driver_without_its_file_is_refused_unless_rotate(self, tmp_path):
        mod, server = _driver_token_script(), _FakeServer()
        server.drivers["codex"] = {"revision": 3, "token": "aitd_codex_3"}
        with pytest.raises(mod.SelfRegisterRefused) as info:
            self._run(mod, server, tmp_path, "codex")
        assert "--rotate" in info.value.message
        assert server.drivers["codex"]["token"] == "aitd_codex_3"
        assert not (tmp_path / ".aitelier-drivers" / "codex.token").exists()
        rotated = self._run(mod, server, tmp_path, "codex", rotate=True)
        assert rotated["status"] == "rotated"
        assert driver_token({"AITELIER_DRIVER_ID": "codex"}, tmp_path) == "aitd_codex_4"

    def test_stale_file_is_refused_unless_rotate(self, tmp_path):
        mod, server = _driver_token_script(), _FakeServer()
        mod.write_token_file("newbie", "aitd_stale", home=tmp_path)
        server.drivers["newbie"] = {"revision": 1, "token": "aitd_other"}
        with pytest.raises(mod.SelfRegisterRefused):
            self._run(mod, server, tmp_path)
        assert driver_token({"AITELIER_DRIVER_ID": "newbie"}, tmp_path) == "aitd_stale"

    def test_reserved_ids_and_disabled_feature_are_refused(self, tmp_path):
        mod = _driver_token_script()
        for reserved in ("owner-cli", "public"):
            with pytest.raises(mod.SelfRegisterRefused):
                self._run(mod, _FakeServer(), tmp_path, reserved)
        server = _FakeServer(enabled=False)
        with pytest.raises(mod.SelfRegisterRefused) as info:
            self._run(mod, server, tmp_path)
        assert "not enabled" in info.value.message
        assert all(method == "GET" for method, _ in server.calls)

    def test_cli_exit_code_and_no_token_in_output(self, tmp_path, monkeypatch, capsys):
        mod, server = _driver_token_script(), _FakeServer()
        monkeypatch.setattr(mod, "_call", server)
        monkeypatch.setattr(mod, "_owner_headers", lambda: dict(server.OWNER))
        monkeypatch.setattr(mod, "token_file_for",
                            lambda driver_id, home=None: tmp_path / f"{driver_id}.token")
        monkeypatch.setattr(mod, "drivers_dir", lambda home=None: tmp_path)
        assert mod.main(["self-register", "newbie"]) == 0
        assert mod.main(["self-register", "public"]) == 3
        out = capsys.readouterr()
        assert "aitd_" not in out.out + out.err
