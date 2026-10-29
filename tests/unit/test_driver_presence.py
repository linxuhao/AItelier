"""Driver presence v1: derived, information-only; plus a reliable last_seen_at."""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import drivers
from core.drivers import DriverRegistry, presence_of
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

ROOT = Path(__file__).resolve().parents[2]
PEPPER = "presence-test-pepper-" * 3
NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    drivers._LAST_SEEN.clear()
    for name in (drivers.PRESENCE_ONLINE_ENV, drivers.PRESENCE_OFFLINE_ENV):
        monkeypatch.delenv(name, raising=False)
    yield
    drivers._LAST_SEEN.clear()


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat()


def p(seconds, online=900, stale=21600):
    return presence_of(ago(seconds), NOW, online_seconds=online, stale_seconds=stale)


# -- the pure function ---------------------------------------------------------
@pytest.mark.parametrize("age,status", [
    (0, "online"), (899, "online"), (900, "online"),        # online bound is inclusive
    (901, "away"), (21599, "away"), (21600, "away"),        # stale bound is inclusive
    (21601, "offline"), (3 * 86400, "offline"),
])
def test_thresholds_and_boundaries(age, status):
    out = p(age)
    assert out["status"] == status
    assert out["seconds_since_seen"] == age
    assert out["last_seen_at"] == ago(age)


def test_never_seen_and_unreadable_are_offline():
    assert presence_of(None, NOW, online_seconds=900, stale_seconds=21600) == {
        "status": "offline", "last_seen_at": None, "seconds_since_seen": None}
    assert presence_of("garbage", NOW, online_seconds=900, stale_seconds=21600)["status"] == "offline"
    assert presence_of("", NOW, online_seconds=900, stale_seconds=21600)["status"] == "offline"


def test_far_future_or_garbage_stamps_are_unknown_never_online():
    far = p(-3600)
    assert far == {"status": "offline", "last_seen_at": ago(-3600), "seconds_since_seen": None}
    junk = presence_of("garbage", NOW, online_seconds=900, stale_seconds=21600)
    assert junk == {"status": "offline", "last_seen_at": "garbage", "seconds_since_seen": None}
    assert drivers.presence_advice("bob", junk) == (
        "recipient bob is offline (last seen time unknown); the message is queued in its inbox; "
        "do not block waiting for a reply")
    assert "last seen time unknown" in drivers.presence_advice("bob", far)
    assert "never seen" not in drivers.presence_advice("bob", far)


def test_small_clock_skew_is_age_zero_and_z_suffix_and_naive_are_read():
    assert p(-30)["seconds_since_seen"] == 0 and p(-30)["status"] == "online"
    assert p(-60)["status"] == "online" and p(-61)["status"] == "offline"
    z = presence_of("2026-10-29T11:59:00Z", NOW, online_seconds=900, stale_seconds=21600)
    assert z["status"] == "online" and z["seconds_since_seen"] == 60
    naive = presence_of("2026-10-29T11:00:00", NOW.isoformat(), online_seconds=900, stale_seconds=21600)
    assert naive["status"] == "online" or naive["status"] == "away"
    assert naive["seconds_since_seen"] == 3600


def test_custom_bounds_are_honoured():
    assert p(100, online=60, stale=120)["status"] == "away"
    assert p(121, online=60, stale=120)["status"] == "offline"


def test_environment_defaults_overrides_and_bad_values(monkeypatch):
    assert drivers.presence_thresholds() == (900, 21600)
    monkeypatch.setenv(drivers.PRESENCE_ONLINE_ENV, "60")
    monkeypatch.setenv(drivers.PRESENCE_OFFLINE_ENV, "600")
    assert drivers.presence_thresholds() == (60, 600)
    for bad in ("abc", "", "0", "-5", "1.5", "  "):
        monkeypatch.setenv(drivers.PRESENCE_ONLINE_ENV, bad)
        monkeypatch.setenv(drivers.PRESENCE_OFFLINE_ENV, "600")
        assert drivers.presence_thresholds() == (900, 21600), bad
    monkeypatch.setenv(drivers.PRESENCE_ONLINE_ENV, "60")
    monkeypatch.setenv(drivers.PRESENCE_OFFLINE_ENV, "garbage")
    assert drivers.presence_thresholds() == (60, 21600)
    monkeypatch.setenv(drivers.PRESENCE_ONLINE_ENV, "7200")  # online >= stale default: inconsistent
    monkeypatch.setenv(drivers.PRESENCE_OFFLINE_ENV, "3600")
    assert drivers.presence_thresholds() == (900, 21600)


# -- fixtures ------------------------------------------------------------------
@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "alpha")
    seed.create_project("beta", "beta")
    seed.create_project("empty", "empty")
    reg = DriverRegistry(db, PEPPER)
    reg.seed("legacy-admin-token-0123456789abcdef")
    tokens = {d: reg.register(d, d, actor="test", is_admin=(d == "ada"))["token"] for d in ("ada", "bob", "carol")}
    for project, members in (("alpha", ["ada"]), ("beta", ["bob", "carol"])):
        for d in members:
            reg.set_membership(project, d, "member", 0, "fixture", actor="test")
    services = {d: StateService(db, actor="driver:" + d, driver_id=d, project_read_trusted=True)
                for d in ("ada", "bob", "carol")}
    return db, reg, tokens, services


def set_seen(db, driver_id, seconds_ago):
    stamp = None if seconds_ago is None else (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    with db.get_connection() as conn:
        conn.execute("UPDATE drivers SET last_seen_at=? WHERE driver_id=?", (stamp, driver_id))
        conn.commit()


def seen(db, driver_id):
    with db.get_connection() as conn:
        return conn.execute("SELECT last_seen_at FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()[0]


# -- views ---------------------------------------------------------------------
def test_registry_views_carry_presence_and_keep_every_existing_field(world):
    db, reg, _, _ = world
    set_seen(db, "bob", 30)
    set_seen(db, "carol", 7200)
    one = reg.get("bob")
    assert set(drivers.PUBLIC_FIELDS) | {"projects"} <= set(one)
    assert one["presence"]["status"] == "online" and one["last_seen_at"] == one["presence"]["last_seen_at"]
    listed = {d["driver_id"]: d for d in reg.list()}
    assert listed["carol"]["presence"]["status"] == "away"
    assert listed["ada"]["presence"] == {"status": "offline", "last_seen_at": None, "seconds_since_seen": None}
    assert all(set(drivers.PUBLIC_FIELDS) <= set(d) for d in listed.values())


def test_rest_views_expose_presence(world, monkeypatch):
    from api import authz, driver_routers
    db, reg, tokens, _ = world
    monkeypatch.setattr(authz, "driver_registry", lambda: reg)
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a: None)
    app = FastAPI()
    app.include_router(driver_routers.router)
    headers = {"X-AItelier-Driver-Token": tokens["ada"]}
    with TestClient(app, base_url="http://localhost") as client:
        set_seen(db, "carol", 100000)
        body = client.get("/api/drivers", headers=headers).json()
        by_id = {d["driver_id"]: d for d in body["drivers"]}
        assert by_id["carol"]["presence"]["status"] == "offline"
        assert by_id["ada"]["presence"]["status"] == "online"  # this very request refreshed it
        assert "revision" in by_id["ada"] and "token_hash" not in by_id["ada"]
        assert client.get("/api/drivers/carol", headers=headers).json()["presence"]["status"] == "offline"
        me = client.get("/api/drivers/me", headers=headers).json()
        assert me["driver_id"] == "ada" and me["driver"]["presence"]["status"] == "online"
        members = client.get("/api/drivers/projects/beta", headers=headers).json()["members"]
        assert {m["driver_id"]: m["presence"]["status"] for m in members} == {"bob": "offline", "carol": "offline"}
        assert {"project_id", "driver_id", "status", "revision", "updated_at"} <= set(members[0])


# -- messaging -----------------------------------------------------------------
def send_driver(svc, key="k1", **kw):
    args = {"request_key": key, "subject": "s", "body": "b", **kw}
    return execute(svc, "send_driver_message", args, allow_write=True)


ADVICE = re.compile(r"^recipient bob is (away|offline) \(last seen \S+, (\d+) min ago\); "
                    r"the message is queued in its inbox; do not block waiting for a reply$")


def test_driver_message_online_has_presence_but_no_advice(world):
    db, _, _, svc = world
    set_seen(db, "bob", 10)
    out = send_driver(svc["ada"], target_driver_id="bob")
    assert out["recipient_presence"]["driver_id"] == "bob"
    assert out["recipient_presence"]["status"] == "online"
    assert "advice" not in out


@pytest.mark.parametrize("age,status", [(3600, "away"), (200000, "offline")])
def test_driver_message_not_online_carries_exact_advice_and_is_still_delivered(world, age, status):
    db, _, _, svc = world
    set_seen(db, "bob", age)
    out = send_driver(svc["ada"], target_driver_id="bob")
    assert out["recipient_presence"]["status"] == status
    match = ADVICE.match(out["advice"])
    assert match and match.group(1) == status and int(match.group(2)) == age // 60
    # delivery is exactly as before: one unread delivery in bob's inbox
    assert [d["target_driver_id"] for d in out["deliveries"]] == ["bob"]
    inbox = svc["bob"].driver_inbox.list_driver_messages()["messages"]
    assert [m["message_id"] for m in inbox] == [out["message_id"]] and inbox[0]["status"] == "unread"


def test_never_seen_recipient_is_offline_with_advice(world):
    db, _, _, svc = world
    set_seen(db, "bob", None)
    out = send_driver(svc["ada"], target_driver_id="bob")
    assert out["recipient_presence"] == {"driver_id": "bob", "status": "offline",
                                         "last_seen_at": None, "seconds_since_seen": None}
    assert out["advice"].startswith("recipient bob is offline (never seen); the message is queued")


def test_project_fanout_lists_each_recipient_and_advises_only_for_those_not_online(world):
    db, _, _, svc = world
    set_seen(db, "bob", 5)
    set_seen(db, "carol", 7200)
    out = send_driver(svc["ada"], project_members="beta")
    by_id = {r["driver_id"]: r["status"] for r in out["recipient_presence"]}
    assert by_id == {"bob": "online", "carol": "away"}
    assert out["advice"].startswith("recipient carol is away (last seen") and "bob" not in out["advice"]
    assert len(out["deliveries"]) == 2


def test_replay_returns_the_stored_record_unchanged_and_never_stores_presence(world):
    db, _, _, svc = world
    set_seen(db, "bob", 200000)
    first = send_driver(svc["ada"], key="same", target_driver_id="bob")
    assert "advice" in first
    again = send_driver(svc["ada"], key="same", target_driver_id="bob")
    assert again == {"message_id": first["message_id"], "deliveries": first["deliveries"]}
    with db.get_connection() as conn:
        stored = conn.execute("SELECT result_json FROM driver_inbox_requests").fetchone()[0]
        assert "presence" not in stored and "advice" not in stored
        assert conn.execute("SELECT COUNT(*) FROM driver_inbox_deliveries").fetchone()[0] == 1


def test_system_notices_do_not_get_presence(world):
    _, _, _, svc = world
    out = svc["ada"].driver_inbox.system_notice(request_key="n", subject="s", body="b",
                                                target_driver_id="bob", kind="lease_notice")
    assert "recipient_presence" not in out and "advice" not in out


def send_director(svc, key="d1", target="beta", sender="alpha", ident="ada", protocol_version="v3", **kw):
    return execute(svc, "send_director_message", {
        "sender_project_id": sender, "target_project_id": target, "protocol_version": protocol_version,
        "director_identity": ident, "request_key": key, "subject": "s", "body": "b", **kw},
        allow_write=True)["result"]


def test_director_message_reports_member_presence_and_stays_delivered(world):  # v3
    db, _, _, svc = world
    set_seen(db, "bob", 5)
    set_seen(db, "carol", 200000)
    out = send_director(svc["ada"])
    assert {r["driver_id"]: r["status"] for r in out["recipient_presence"]} == {"bob": "online", "carol": "offline"}
    assert out["advice"].startswith("recipient carol is offline")
    assert out["message"]["subject"] == "s" and out["deliveries"][0]["target_project_id"] == "beta"
    listed = svc["bob"].director_messages.list_director_messages("beta", protocol_version="v3")["result"]["items"]
    assert [i["message"]["message_id"] for i in listed] == [out["message"]["message_id"]]


def test_director_fan_out_to_one_member_is_still_a_list_and_all_online_has_no_advice(world):
    db, _, _, svc = world
    set_seen(db, "ada", 5)
    out = send_director(svc["bob"], key="back", target="alpha", sender="beta", ident="bob")
    assert isinstance(out["recipient_presence"], list) and len(out["recipient_presence"]) == 1
    assert out["recipient_presence"][0]["driver_id"] == "ada" and out["recipient_presence"][0]["status"] == "online"
    assert "advice" not in out


def test_director_advice_says_project_mailbox_and_skips_suspended_members(world):
    db, reg, _, svc = world
    set_seen(db, "bob", 200000)
    set_seen(db, "carol", 200000)
    reg.set_status("carol", "suspended", 1, "fixture", actor="test")
    out = send_director(svc["ada"], key="susp")
    assert [r["driver_id"] for r in out["recipient_presence"]] == ["bob"]
    assert "carol" not in out["advice"]
    assert "queued in the project mailbox" in out["advice"] and "inbox" not in out["advice"]


def test_project_fan_out_driver_message_to_one_member_is_a_list(world):
    db, _, _, svc = world
    out = send_driver(svc["bob"], key="one", project_members="alpha")
    assert isinstance(out["recipient_presence"], list) and [r["driver_id"] for r in out["recipient_presence"]] == ["ada"]


def test_director_union_is_capped_at_200_with_a_total(world, monkeypatch):
    db, _, _, svc = world
    with db.get_connection() as conn:
        for i in range(250):
            conn.execute("INSERT INTO drivers(driver_id,display_name,kind,is_admin,token_hash,host_label,"
                         "capabilities_json,status,revision,created_at,last_seen_at) "
                         "VALUES(?,?,'lan',0,?, '', '{}', 'active', 1, '2026-01-01T00:00:00+00:00', NULL)",
                         (f"x{i:03d}", f"x{i}", f"hash{i}"))
            conn.execute("INSERT INTO project_drivers VALUES('beta',?, 'member', 1, 't', '2026-01-01')", (f"x{i:03d}",))
        conn.commit()
    set_seen(db, "bob", 5)
    from jsonschema import Draft202012Validator
    v3 = Draft202012Validator(json.loads((ROOT / "contracts/director_messaging/v3/schema.json").read_text()))
    out = svc["ada"].director_messages.send_director_message(
        "alpha", "ada", "big", "s", "b", target_project_id="beta", protocol_version="v3", ack_quorum=1)
    v3.validate(out)
    result = out["result"]
    assert len(result["recipient_presence"]) == 200 and result["recipient_presence_total"] == 252
    assert all(r["status"] == "offline" for r in result["recipient_presence"])  # not-online listed first


def test_director_message_with_no_recipient_besides_the_sender_adds_nothing(world):
    _, reg, _, svc = world
    reg.set_membership("empty", "ada", "member", 0, "fixture", actor="test")   # the sender is the only member
    out = send_director(svc["ada"], key="none", target="empty")
    assert "recipient_presence" not in out and "advice" not in out
    assert out["deliveries"][0]["target_project_id"] == "empty"


def test_director_replay_returns_the_stored_record_and_never_stores_presence(world):
    db, _, _, svc = world
    set_seen(db, "bob", 5)
    first = send_director(svc["ada"], key="rp")
    assert "recipient_presence" in first
    again = send_director(svc["ada"], key="rp")
    assert again["replayed"] is True and "recipient_presence" not in again
    with db.get_connection() as conn:
        stored = conn.execute("SELECT result_json FROM state_director_idempotency").fetchone()[0]
        assert "presence" not in stored and "advice" not in stored


def test_v2_director_response_is_untouched_and_v3_result_validates_against_the_schema(world):
    from jsonschema import Draft202012Validator
    db, _, _, svc = world
    set_seen(db, "carol", 200000)
    v3 = Draft202012Validator(json.loads((ROOT / "contracts/director_messaging/v3/schema.json").read_text()))
    v2 = Draft202012Validator(json.loads((ROOT / "contracts/director_messaging/v2/schema.json").read_text()))
    rich = svc["ada"].director_messages.send_director_message(
        "alpha", "ada", "r1", "s", "b", target_project_id="beta", protocol_version="v3")
    assert "recipient_presence" in rich["result"] and "advice" in rich["result"]
    v3.validate(rich)
    legacy = StateService(db, actor="authorized-state-operator", project_read_trusted=True)
    plain = legacy.director_messages.send_director_message("alpha", "op", "r2", "s", "b", target_project_id="beta")
    assert set(plain["result"]) == {"message", "deliveries", "replayed"}
    v2.validate(plain)


# -- last_seen_at is a reliable activity signal --------------------------------
def test_lookup_token_refreshes_and_is_throttled(world):
    db, reg, tokens, _ = world
    assert seen(db, "bob") is None
    assert reg.lookup_token(tokens["bob"])["driver_id"] == "bob"
    first = seen(db, "bob")
    assert first is not None
    set_seen(db, "bob", 3600)
    reg.lookup_token(tokens["bob"])            # inside the 60 s throttle: no write
    assert seen(db, "bob") != first and drivers.presence_now(seen(db, "bob"))["status"] == "away"


def test_listing_the_inbox_refreshes_the_callers_last_seen(world):
    db, _, _, svc = world
    assert seen(db, "bob") is None
    svc["bob"].driver_inbox.list_driver_messages()
    assert seen(db, "bob") is not None and seen(db, "carol") is None


def count_stamps(db):
    with db.get_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS stamps(n INTEGER)")
        conn.execute("CREATE TRIGGER IF NOT EXISTS count_seen AFTER UPDATE OF last_seen_at ON drivers "
                     "BEGIN INSERT INTO stamps VALUES(1); END")
        conn.commit()


def stamps(db):
    with db.get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM stamps").fetchone()[0]


def test_a_blocked_inbox_wait_keeps_refreshing_within_the_throttle(world, monkeypatch):
    db, _, _, svc = world
    count_stamps(db)
    monkeypatch.setattr(drivers, "LAST_SEEN_THROTTLE_SECONDS", 0)

    async def run():
        return await svc["bob"].driver_inbox.wait_for_driver_inbox(timeout_seconds=2.2)

    assert asyncio.run(run())["messages"] == []
    assert stamps(db) >= 2            # the long poll itself re-stamps, not only its first request


def test_the_throttle_still_limits_writes_during_a_wait(world):
    db, _, _, svc = world
    count_stamps(db)

    async def run():
        return await svc["bob"].driver_inbox.wait_for_driver_inbox(timeout_seconds=2.2)

    asyncio.run(run())
    assert stamps(db) == 1


def test_combined_state_wait_with_inbox_also_stamps(world):
    db, _, _, svc = world
    cursor = svc["bob"].store.events("beta")[-1]["seq"]
    asyncio.run(svc["bob"].wait_for_state_change("beta", after=cursor, timeout_seconds=0,
                                                 include_driver_inbox=True))
    assert seen(db, "bob") is not None


def test_authenticated_state_rest_call_refreshes_last_seen(world, monkeypatch):
    from api import authz, state_graph_routers as sr
    db, reg, tokens, _ = world
    monkeypatch.setattr(authz, "driver_registry", lambda: reg)
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a: None)
    app = FastAPI()
    app.include_router(sr.router)
    app.dependency_overrides[sr.get_db_manager] = lambda: db
    app.dependency_overrides[sr.get_workspace_manager] = lambda: None
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/state/projects", headers={"X-AItelier-Driver-Token": "nope"}).status_code in (200, 401, 403)
        assert seen(db, "carol") is None
        assert client.get("/api/state/projects", headers={"X-AItelier-Driver-Token": tokens["carol"]}).status_code == 200
        assert seen(db, "carol") is not None


def test_state_only_service_refreshes_last_seen_for_driver_credentials(world, tmp_path, monkeypatch):
    from api.state_only import create_app
    db, reg, tokens, _ = world
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / drivers.PEPPER_SECRET_NAME).write_text(PEPPER)
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(secrets_dir))
    monkeypatch.setenv(drivers.FEATURE_ENV, "on")
    drivers._REGISTRIES.clear()
    app = create_app(db.db_path, "dedicated-state-bearer-token-0123456789abcdef", with_mcp=False)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.get("/api/state/projects", headers={"Authorization": "Bearer " + tokens["bob"]}).status_code == 200
        assert seen(db, "bob") is not None
        assert client.get("/api/state/projects", headers={"X-AItelier-Driver-Token": tokens["carol"]}).status_code == 200
        assert seen(db, "carol") is not None
        assert client.get("/api/state/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
    drivers._REGISTRIES.clear()


# -- presence is information, never authority ----------------------------------
PRESENCE_NAMES = {"presence_of", "presence_now", "presence_advice", "presence_thresholds",
                  "recipient_presence_block"}
PRESENCE_TOKEN = re.compile(r"presence_(of|now|thresholds|advice)|recipient_presence|seconds_since_seen"
                            r"|PRESENCE_(ONLINE|OFFLINE)|AITELIER_PRESENCE")
PERMISSION_TOKEN = re.compile(PRESENCE_TOKEN.pattern + r"""|["']presence["']|\.presence\b|\bpresence\s*=""")
PERMISSION_FILES = ["core/state_claims.py", "core/state_enforcement.py", "core/state_handoffs.py",
                    "core/state_subagents.py", "core/driver_notices.py", "core/state_attempts.py",
                    "core/state_service.py", "core/state_commands.py", "api/authz.py", "api/state_only.py",
                    "api/state_http.py", "api/state_graph_routers.py", "api/mcp_router.py"]
ALLOWED_PRESENCE_USERS = {"core/drivers.py", "core/driver_inbox.py", "core/director_messaging.py"}


def test_permission_claim_lease_and_fence_code_never_reads_presence_or_last_seen():
    for rel in PERMISSION_FILES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not PERMISSION_TOKEN.search(text), rel
        assert "last_seen" not in text, rel


@pytest.mark.parametrize("snippet", ['view["presence"]["status"]', "view['presence']", 'row.get("presence")',
                                     "d.presence", "presence = 1", "presence_now(x)", "x['seconds_since_seen']"])
def test_the_authorization_guard_pattern_catches_every_way_of_reading_presence(snippet):
    assert PERMISSION_TOKEN.search(snippet)


def test_only_the_reporting_modules_use_presence_helpers():
    users = {str(path.relative_to(ROOT)) for folder in ("core", "api") for path in (ROOT / folder).glob("*.py")
             if PRESENCE_TOKEN.search(path.read_text(encoding="utf-8"))}
    # api/driver_routers.py only reads the already-presence-carrying registry views.
    assert users - {"api/driver_routers.py"} == ALLOWED_PRESENCE_USERS


def test_inside_drivers_py_only_describing_views_call_the_presence_helpers():
    tree = ast.parse((ROOT / "core/drivers.py").read_text(encoding="utf-8"))
    callers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            owner = node.name + "."
            functions = [n for n in node.body if isinstance(n, ast.FunctionDef)]
        elif node is tree:
            owner, functions = "", [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        else:
            continue
        for fn in functions:
            used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            if used & PRESENCE_NAMES:
                callers.add(owner + fn.name)
    assert callers == {"presence_now", "recipient_presence_block",
                       "DriverRegistry.get", "DriverRegistry.list"}
    assert drivers.DriverRegistry.lookup_token.__code__.co_names.count("presence_now") == 0


# -- the real 60 s throttle, and the event loop ---------------------------------
def test_real_throttle_path_with_a_fake_clock(world, monkeypatch):
    db, reg, tokens, _ = world
    clock = [5000.0]
    monkeypatch.setattr(drivers, "_mono", lambda: clock[0])
    assert drivers.LAST_SEEN_THROTTLE_SECONDS == 60
    reg.lookup_token(tokens["bob"])
    first = seen(db, "bob")
    assert first is not None
    for offset, expect_write in ((1, False), (59, False), (60, True)):
        set_seen(db, "bob", 7200)
        stale = seen(db, "bob")
        clock[0] = 5000.0 + offset
        reg.lookup_token(tokens["bob"])
        assert (seen(db, "bob") != stale) is expect_write, offset
        if expect_write:
            break
    # the long-poll stamp shares the same budget
    set_seen(db, "bob", 7200)
    stale = seen(db, "bob")
    clock[0] += 30
    drivers.touch_driver(db, "bob")
    assert seen(db, "bob") == stale
    clock[0] += 31
    drivers.touch_driver(db, "bob")
    assert seen(db, "bob") != stale


def test_auth_path_keeps_the_full_backoff_after_a_failed_stamp_but_long_polls_retry(world, monkeypatch):
    db, reg, tokens, _ = world
    clock = [100.0]
    monkeypatch.setattr(drivers, "_mono", lambda: clock[0])
    with db.get_connection() as conn:
        conn.execute("CREATE TRIGGER block_seen BEFORE UPDATE OF last_seen_at ON drivers "
                     "BEGIN SELECT RAISE(ABORT,'database is locked'); END")
        conn.commit()
    assert reg.lookup_token(tokens["bob"])["driver_id"] == "bob"
    drivers.touch_driver(db, "carol")
    with db.get_connection() as conn:
        conn.execute("DROP TRIGGER block_seen")
        conn.commit()
    clock[0] += drivers.LAST_SEEN_RETRY_SECONDS + 1
    reg.lookup_token(tokens["bob"])
    assert seen(db, "bob") is None                  # request path: still backing off for 60 s
    drivers.touch_driver(db, "carol")
    assert seen(db, "carol") is not None            # long poll: retried after 5 s
    clock[0] += 60
    reg.lookup_token(tokens["bob"])
    assert seen(db, "bob") is not None


def test_a_blocked_inbox_wait_does_not_block_the_event_loop(world):
    db, _, _, svc = world
    release = threading.Event()
    locked = threading.Event()

    def hold_write_lock():
        with db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(5)
            conn.rollback()

    async def run():
        gaps = []
        stop = asyncio.Event()

        async def ticker():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now_ = time.monotonic()
                gaps.append(now_ - last)
                last = now_

        holder = threading.Thread(target=hold_write_lock)
        holder.start()
        assert locked.wait(2)
        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)               # let the ticker start before the wait
        try:
            await svc["bob"].driver_inbox.wait_for_driver_inbox(timeout_seconds=1.3)
        finally:
            stop.set()
            await task
            release.set()
            holder.join()
        return gaps

    gaps = asyncio.run(run())
    assert len(gaps) > 20 and max(gaps) < 0.25      # the 500 ms stamp wait ran off-loop
