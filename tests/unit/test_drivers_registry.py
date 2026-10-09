"""Driver registry (multi-driver P0): schema, hashing, seeding, lifecycle, audit."""
from __future__ import annotations

import sqlite3

import pytest

from core import drivers
from core.state_database import StateDatabase

PEPPER = "p" * 48
LEGACY = "legacy-admin-token-0123456789abcdef"


@pytest.fixture
def db(tmp_path):
    return StateDatabase(str(tmp_path / "state.sqlite"))


@pytest.fixture
def registry(db):
    reg = drivers.DriverRegistry(db, PEPPER)
    reg.seed(LEGACY)
    return reg


def _all(db, sql, *args):
    with db.get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


class TestSchema:
    def test_tables_and_seed(self, registry, db):
        ids = {r["driver_id"]: r for r in _all(db, "SELECT * FROM drivers")}
        assert set(ids) == {"public", "owner-cli"}
        assert ids["public"]["kind"] == "public_cf" and ids["public"]["token_hash"] is None
        assert ids["owner-cli"]["is_admin"] == 1
        assert ids["owner-cli"]["token_hash"] == drivers.token_hash(PEPPER, LEGACY)
        assert LEGACY not in repr(ids)
        assert _all(db, "SELECT * FROM project_drivers") == []

    def test_seed_is_idempotent_and_follows_the_env_token(self, registry, db):
        assert registry.seed(LEGACY) == {"created": []}
        before = _all(db, "SELECT revision FROM drivers WHERE driver_id='owner-cli'")[0]["revision"]
        registry.seed("a-new-admin-token-0123456789abcdef")
        assert registry.lookup_token(LEGACY) is None
        assert registry.lookup_token("a-new-admin-token-0123456789abcdef")["driver_id"] == "owner-cli"
        after = _all(db, "SELECT revision FROM drivers WHERE driver_id='owner-cli'")[0]["revision"]
        assert after == before + 1

    def test_no_admin_token_means_no_owner_cli(self, db):
        reg = drivers.DriverRegistry(db, PEPPER)
        assert reg.seed(None) == {"created": ["public"]}

    def test_audit_is_append_only_and_drivers_are_never_deleted(self, registry, db):
        with db.get_connection() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM driver_audit")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE driver_audit SET actor='x'")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM drivers WHERE driver_id='public'")

    def test_check_constraints(self, registry, db):
        with db.get_connection() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO drivers(driver_id,display_name,kind,is_admin,token_hash,status,revision,"
                             "created_at) VALUES('x','x','lan',0,NULL,'active',1,'t')")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO drivers(driver_id,display_name,kind,is_admin,token_hash,status,revision,"
                             "created_at) VALUES('pub2','x','public_cf',0,NULL,'active',1,'t')")

    def test_short_pepper_is_refused(self, db):
        with pytest.raises(drivers.DriverError):
            drivers.DriverRegistry(db, "short")


class TestTokens:
    def test_register_returns_token_once_and_stores_only_hmac(self, registry, db):
        out = registry.register("codex", "Codex", actor="driver:owner-cli")
        token = out["token"]
        assert token.startswith(drivers.TOKEN_PREFIX)
        rows = _all(db, "SELECT * FROM drivers WHERE driver_id='codex'")
        assert rows[0]["token_hash"] == drivers.token_hash(PEPPER, token) != token
        assert token not in repr(registry.get("codex")) and "token_hash" not in registry.get("codex")
        assert all(token not in r["payload_json"] for r in _all(db, "SELECT * FROM driver_audit"))
        assert registry.lookup_token(token)["driver_id"] == "codex"

    def test_a_different_pepper_does_not_authenticate(self, registry, db):
        token = registry.register("codex", "Codex", actor="t")["token"]
        assert drivers.DriverRegistry(db, "q" * 48).lookup_token(token) is None

    def test_rotate_invalidates_the_old_token(self, registry):
        old = registry.register("codex", "Codex", actor="t")["token"]
        new = registry.rotate("codex", 1, actor="t")["token"]
        assert registry.lookup_token(old) is None
        assert registry.lookup_token(new)["driver_id"] == "codex"
        with pytest.raises(drivers.DriverConflict):
            registry.rotate("codex", 1, actor="t")

    def test_suspended_and_retired_drivers_do_not_authenticate(self, registry):
        token = registry.register("codex", "Codex", actor="t")["token"]
        registry.set_status("codex", "suspended", 1, "pause", actor="t")
        assert registry.lookup_token(token) is None
        registry.set_status("codex", "active", 2, "resume", actor="t")
        assert registry.lookup_token(token) is not None
        registry.set_status("codex", "retired", 3, "gone", actor="t")
        assert registry.lookup_token(token) is None
        with pytest.raises(drivers.DriverConflict):
            registry.set_status("codex", "active", 4, "back", actor="t")

    def test_public_cannot_be_registered_retired_or_authenticate_by_token(self, registry):
        with pytest.raises(drivers.DriverConflict):
            registry.register("public", "x", actor="t")
        with pytest.raises(drivers.DriverConflict):
            registry.set_status("public", "retired", 1, "no", actor="t")
        with pytest.raises(drivers.DriverError):
            registry.set_admin("public", True, 1, "no", actor="t")
        assert registry.lookup_token("") is None

    @pytest.mark.parametrize("bad", ["", "Upper", "-x", "a/b", "x" * 65])
    def test_driver_id_shape(self, registry, bad):
        with pytest.raises(drivers.DriverError):
            registry.register(bad, "x", actor="t")

    def test_audit_refuses_credentials(self, registry, db):
        with db.get_connection() as conn, pytest.raises(drivers.DriverError):
            registry._audit(conn, "codex", "x", {"token": "a"}, "t")


class TestMembership:
    def test_cas_and_unknown_project(self, registry):
        registry.register("codex", "Codex", actor="t")
        row = registry.set_membership("aitelier", "codex", "member", 0, "join", actor="t",
                                      project_exists=lambda p: p == "aitelier")
        assert row["status"] == "member" and row["revision"] == 1
        with pytest.raises(drivers.DriverConflict):
            registry.set_membership("aitelier", "codex", "removed", 0, "x", actor="t")
        assert registry.set_membership("aitelier", "codex", "removed", 1, "x", actor="t")["revision"] == 2
        with pytest.raises(drivers.DriverError):
            registry.set_membership("nope", "codex", "member", 0, "x", actor="t",
                                    project_exists=lambda p: False)
        assert registry.get("codex")["projects"][0]["project_id"] == "aitelier"

    def test_public_is_not_a_member_by_default(self, registry):
        assert registry.get("public")["projects"] == []


class TestFeatureFlag:
    def test_off_without_flag_or_pepper(self, monkeypatch, tmp_path, db):
        monkeypatch.delenv(drivers.FEATURE_ENV, raising=False)
        monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path))
        (tmp_path / drivers.PEPPER_SECRET_NAME).write_text(PEPPER)
        assert not drivers.feature_enabled()
        assert drivers.registry_for(db) is None
        monkeypatch.setenv(drivers.FEATURE_ENV, "on")
        (tmp_path / drivers.PEPPER_SECRET_NAME).write_text("short")
        assert not drivers.feature_enabled()
        assert drivers.registry_for(db) is None
        # Off means not even the tables exist.
        assert _all(db, "SELECT name FROM sqlite_master WHERE name='drivers'") == []

    def test_on_seeds_owner_cli_from_env(self, monkeypatch, tmp_path, db):
        monkeypatch.setenv(drivers.FEATURE_ENV, "on")
        monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path))
        monkeypatch.setenv("AITELIER_ADMIN_TOKEN", LEGACY)
        (tmp_path / drivers.PEPPER_SECRET_NAME).write_text(PEPPER + "\n")
        reg = drivers.registry_for(db)
        assert reg is not None and reg.lookup_token(LEGACY)["driver_id"] == "owner-cli"
        assert drivers.registry_for(db) is reg


class TestIdentityHelpers:
    @pytest.mark.parametrize("owner,actor,ok", [
        ("driver:codex", "driver:codex", True),
        ("driver:codex", "driver:grok-bot", False),
        ("authorized-state-operator", "driver:codex", True),
        ("authorized-state-operator", "owner:a@b.c", False),
        ("a@b.c", "owner:a@b.c", True),
        ("a@b.c", "owner:x@b.c", False),
        ("authorized-state-operator", "authorized-state-operator", True),
        (None, "driver:codex", False),
    ])
    def test_actor_continues(self, owner, actor, ok):
        assert drivers.actor_continues(owner, actor) is ok

    def test_director_identity(self):
        assert drivers.check_director_identity("codex", None) == "codex"
        assert drivers.check_director_identity("codex", "codex/sub-1") == "codex/sub-1"
        for bad in ("grok-bot", "codexx", "codex/", 5):
            with pytest.raises(drivers.DriverError):
                drivers.check_director_identity("codex", bad)


class TestMigrationPreservesRows:
    def test_existing_state_rows_are_untouched(self, tmp_path):
        from core.state_service import StateService
        path = tmp_path / "state.sqlite"
        svc = StateService(StateDatabase(str(path)), actor=drivers.LEGACY_ACTOR, project_read_trusted=True)
        svc.create_project("p", "P")
        svc.open_project("p")
        db = StateDatabase(str(path))

        def snapshot():
            with db.get_connection() as conn:
                return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t}").fetchall()]
                        for t in ("state_projects", "state_events", "state_project_access")}
        before = snapshot()
        for _ in range(2):
            reg = drivers.DriverRegistry(db, PEPPER)
            reg.seed(LEGACY)
        assert snapshot() == before
        with db.get_connection() as conn:
            assert conn.execute("SELECT changed_by FROM state_project_access").fetchone()[0] == drivers.LEGACY_ACTOR
