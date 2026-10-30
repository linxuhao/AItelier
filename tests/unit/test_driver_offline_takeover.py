"""Presence v2: a driver declares itself offline; another member may then take
over its claims (iss-ff73340f04c24858). Waiting never counts as offline.

Covers: the declaration (only for oneself, idempotent, audited without prose),
the effective-presence matrix (wait > declaration > last_seen), take_over_claim
(happy path, every refusal, fences, idempotency, races), the surfaces
(holder_presence, hints), the claim-status migration, attempts (unchanged,
documented) and the guard that presence decides nothing else.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import driver_presence, drivers, state_claims
from core.drivers import DriverRegistry
from core.state_claims import ClaimError
from core.state_commands import WRITE_REQUESTS, execute
from core.state_database import StateDatabase
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService

ROOT = Path(__file__).resolve().parents[2]
PEPPER = "offline-takeover-test-pepper-0123456789"
NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}
REASON = "bob declared offline for the weekend; carol continues node a"
SIX_HOURS = 21600


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    drivers._LAST_SEEN.clear()
    for name in (drivers.PRESENCE_ONLINE_ENV, drivers.PRESENCE_OFFLINE_ENV, driver_presence.WAIT_GRACE_ENV):
        monkeypatch.delenv(name, raising=False)
    yield
    drivers._LAST_SEEN.clear()


def set_seen(db, driver_id, seconds_ago):
    stamp = None if seconds_ago is None else (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    with db.get_connection() as conn:
        conn.execute("UPDATE drivers SET last_seen_at=? WHERE driver_id=?", (stamp, driver_id))
        conn.commit()


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    owner = StateService(db, actor="owner:o@example.com", project_read_trusted=True, is_admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": "a", **NODE}, {"key": "b", **NODE}])
    reg = DriverRegistry(db, PEPPER)
    reg.seed(None)
    tokens = {d: reg.register(d, d.title(), actor="test")["token"] for d in ("bob", "carol", "dave", "erin")}
    for d in ("bob", "carol", "erin"):                  # dave is registered but not a member of p
        reg.set_membership("p", d, "member", 0, "fixture", actor="test")
    svc = {d: StateService(db, actor="driver:" + d, driver_id=d, project_read_trusted=True) for d in tokens}
    for d in tokens:
        set_seen(db, d, 10)
    return SimpleNamespace(db=db, reg=reg, tokens=tokens, svc=svc, owner=owner)


def write(service, action, **arguments):
    return execute(service, action, arguments, allow_write=True)


def read(service, action, **arguments):
    return execute(service, action, arguments)


def refusal(call):
    with pytest.raises(ClaimError) as caught:
        call()
    return caught.value


def claim(service, node="a", purpose="implement", key=None, **extra):
    return write(service, "claim_node", project_id="p", node_key=node, purpose=purpose, expected_revision=1,
                 request_key=key or f"{service.driver_id}-{node}-{purpose}", workspace="host:/w#b", **extra)


def take(service, held, fence=None, key="t1", reason=REASON):
    return write(service, "take_over_claim", project_id="p", claim_id=held["claim_id"],
                 expected_fence=held["fence"] if fence is None else fence, reason=reason, request_key=key)


def offline(world, who="bob", reason="weekend", expected_return=None):
    return driver_presence.declare(world.db, who, "offline", reason, expected_return)


def presence(world, who):
    return driver_presence.presence_for(world.db, who)


def audit(world, who):
    with world.db.get_connection() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM driver_audit WHERE driver_id=? AND operation='presence' ORDER BY seq", (who,))]


def events(world, event_type):
    with world.db.get_connection() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM state_events WHERE event_type=? ORDER BY seq", (event_type,))]


def hold_row(world):
    with world.db.get_connection() as conn:
        return tuple(conn.execute("SELECT revision, held, reason, actor FROM state_node_holds "
                                  "WHERE project_id='p' AND node_key='a'").fetchone())


def put_wait(world, who, until, ended, wait_id=None):
    """One wait row (driver_presence_waits holds one row per wait)."""
    with world.db.get_connection() as conn:
        driver_presence._ensure(conn)
        conn.execute("INSERT OR REPLACE INTO driver_presence_waits(wait_id,driver_id,started_at,until,ended_at) "
                     "VALUES(?,?,?,?,?)",
                     (wait_id or f"w-{who}-{until.timestamp()}", who, driver_presence._stamp(until),
                      driver_presence._stamp(until), driver_presence._stamp(ended) if ended else None))
        conn.commit()


# ---------------------------------------------------------------------------
# A. the declaration
# ---------------------------------------------------------------------------
def test_offline_declaration_is_stored_idempotent_and_cleared_by_online(world):
    first = offline(world, reason="weekend", expected_return="2026-11-02T08:00:00Z")
    assert first["changed"] is True
    view = first["presence"]
    assert view["status"] == "offline" and view["basis"] == "declared" and view["declared"] is True
    assert view["reason"] == "weekend" and view["expected_return"] == "2026-11-02T08:00:00.000000+00:00"
    since = view["since"]
    again = offline(world, reason="weekend", expected_return="2026-11-02T08:00:00+00:00")
    assert again["changed"] is False and again["presence"]["since"] == since
    moved = offline(world, reason="long weekend")
    assert moved["changed"] is True and moved["presence"]["since"] == since          # since is kept
    assert moved["presence"]["reason"] == "long weekend" and moved["presence"]["expected_return"] is None
    back = driver_presence.declare(world.db, "bob", "online")
    assert back["changed"] is True and back["presence"]["declared"] is False
    assert back["presence"]["status"] == "online" and back["presence"]["basis"] == "last_seen"
    assert driver_presence.declare(world.db, "bob", "online")["changed"] is False
    assert audit(world, "bob") == [{"cause": "declared", "status": "offline"}] * 2 + [
        {"cause": "declared", "status": "online"}]


@pytest.mark.parametrize("args,needle", [
    (("asleep",), "status must be"),
    (("offline", "x" * 201), "at most 200"),
    (("offline", "r", "next tuesday"), "ISO-8601"),
    (("online", "", "2026-11-02T08:00:00Z"), "offline declaration"),
])
def test_declaration_validation(world, args, needle):
    with pytest.raises(drivers.DriverError, match=needle):
        driver_presence.declare(world.db, "bob", *args)
    assert presence(world, "bob")["declared"] is False


def test_the_audit_carries_enumerations_never_the_reason(world):
    offline(world, reason="secret-ish prose aitd_not_a_token")
    with world.db.get_connection() as conn:
        rows = conn.execute("SELECT payload_json, actor FROM driver_audit WHERE operation='presence'").fetchall()
    assert [json.loads(r[0]) for r in rows] == [{"cause": "declared", "status": "offline"}]
    assert "prose" not in rows[0][0] and rows[0][1] == "driver:bob"
    with world.db.get_connection() as conn, pytest.raises(drivers.DriverError):
        drivers.presence_audit(conn, "bob", "offline", "because I said so", "driver:bob")


def test_mcp_tool_declares_only_for_the_calling_driver(world):
    from api.state_graph_tools import register_state_tools
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = (kind, fn)
            return fn
        return deco

    class FakeMCP:
        def prompt(self, **_):
            return lambda fn: fn

        def resource(self, *_, **__):
            return lambda fn: fn

    register_state_tools(tool, FakeMCP(), service_factory=lambda: world.svc["bob"])
    kind, declare = captured["declare_driver_presence"]
    assert kind == "write"
    import inspect
    assert list(inspect.signature(declare).parameters) == ["status", "reason", "expected_return"]
    out = declare("offline", "lunch")
    assert out["driver_id"] == "bob" and presence(world, "bob")["declared"] is True
    assert presence(world, "carol")["declared"] is False
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="status must be"):
        declare("gone")
    anonymous = StateService(world.db, actor="authenticated-state-token", project_read_trusted=True)
    register_state_tools(tool, FakeMCP(), service_factory=lambda: anonymous)
    with pytest.raises(ToolError, match="driver_identity_required"):
        captured["declare_driver_presence"][1]("offline")


def test_rest_route_declares_for_the_token_holder_and_has_no_driver_argument(world, monkeypatch):
    from api import authz, driver_routers
    monkeypatch.setattr(authz, "driver_registry", lambda: world.reg)
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a: None)
    app = FastAPI()
    app.include_router(driver_routers.router)
    with TestClient(app, base_url="http://localhost") as client:
        bob = {"X-AItelier-Driver-Token": world.tokens["bob"]}
        ok = client.post("/api/drivers/me/presence", json={"status": "offline", "reason": "train"}, headers=bob)
        assert ok.status_code == 200, ok.text
        assert ok.json()["driver_id"] == "bob" and ok.json()["presence"]["basis"] == "declared"
        spoof = client.post("/api/drivers/me/presence", json={"status": "offline", "driver_id": "carol"},
                            headers=bob)
        assert spoof.status_code == 422
        assert presence(world, "carol")["declared"] is False
        assert client.post("/api/drivers/me/presence", json={"status": "offline"}).status_code == 403
        seen = client.get("/api/drivers/bob", headers={"X-AItelier-Driver-Token": world.tokens["carol"]}).json()
        assert seen["presence"]["status"] == "offline" and seen["presence"]["reason"] == "train"
        assert {"status", "last_seen_at", "seconds_since_seen"} <= set(seen["presence"])


def test_registry_views_carry_the_effective_presence(world):
    offline(world, reason="dentist", expected_return="2026-11-02T08:00:00Z")
    view = world.reg.get("bob")["presence"]
    assert view["status"] == "offline" and view["derived_status"] == "online"
    assert {"declared", "reason", "since", "expected_return", "basis", "waiting", "last_wait_end",
            "last_seen_at", "seconds_since_seen"} <= set(view)
    listed = {d["driver_id"]: d["presence"] for d in world.reg.list()}
    assert listed["bob"]["declared"] is True and listed["carol"]["declared"] is False


# ---------------------------------------------------------------------------
# B. the effective-presence matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("seen,status", [(30, "online"), (3600, "away"), (SIX_HOURS + 60, "offline"),
                                         (None, "offline")])
def test_without_declaration_or_wait_presence_is_derived(world, seen, status):
    set_seen(world.db, "bob", seen)
    view = presence(world, "bob")
    assert view["status"] == status and view["basis"] == "last_seen" and view["derived_status"] == status


def test_declared_offline_holds_against_recent_activity_from_reads(world):
    offline(world)
    world.svc["bob"].driver_inbox.list_driver_messages()        # an ordinary read stamps last_seen
    read(world.svc["bob"], "list_claims", project_id="p")
    view = presence(world, "bob")
    assert view["status"] == "offline" and view["basis"] == "declared" and view["derived_status"] == "online"


def _wait_and_observe(service, wait, declare_during=None):
    async def run():
        task = asyncio.create_task(wait())
        seen = None
        for _ in range(200):
            await asyncio.sleep(0.02)
            seen = driver_presence.presence_for(service.store.db, service.driver_id)
            if seen["waiting"]:
                break
        during = None
        if declare_during is not None:
            await asyncio.to_thread(declare_during)
            during = driver_presence.presence_for(service.store.db, service.driver_id)
        await task
        return seen, during
    return asyncio.run(run())


@pytest.mark.parametrize("which", ["inbox", "state"])
def test_a_wait_overrides_and_clears_a_declared_offline(world, which):
    offline(world)
    bob = world.svc["bob"]
    wait = ((lambda: bob.driver_inbox.wait_for_driver_inbox(timeout_seconds=1.0)) if which == "inbox" else
            (lambda: bob.wait_for_state_change("p", after=10 ** 6, timeout_seconds=1.0)))
    seen, _ = _wait_and_observe(bob, wait)
    assert seen["waiting"] is True and seen["status"] == "online" and seen["basis"] == "wait"
    assert seen["declared"] is False                                   # the wait cleared the declaration
    after = presence(world, "bob")
    assert after["waiting"] is False and after["status"] == "online" and after["basis"] == "wait"
    assert after["last_wait_end"] is not None
    assert {"cause": "wait", "status": "online"} in audit(world, "bob")


def test_a_wait_in_progress_overrides_a_declaration_made_during_it(world):
    bob = world.svc["bob"]
    _, during = _wait_and_observe(bob, lambda: bob.driver_inbox.wait_for_driver_inbox(timeout_seconds=1.0),
                                  declare_during=lambda: offline(world))
    assert during["declared"] is True and during["waiting"] is True
    assert during["status"] == "online" and during["basis"] == "wait"


def test_grace_after_a_wait_is_inclusive_and_then_the_declaration_or_derived_applies(world):
    now = datetime.now(UTC)
    put_wait(world, "bob", now - timedelta(seconds=200), now - timedelta(seconds=120))
    with world.db.get_connection() as conn:
        assert driver_presence.effective_presence(conn, "bob", now)["basis"] == "wait"
        assert driver_presence.effective_presence(conn, "bob", now + timedelta(seconds=1))["basis"] == "last_seen"
    offline(world)
    with world.db.get_connection() as conn:
        assert driver_presence.effective_presence(conn, "bob", now)["status"] == "online"
        late = driver_presence.effective_presence(conn, "bob", now + timedelta(seconds=1))
    assert late["status"] == "offline" and late["basis"] == "declared"


def test_grace_is_env_overridable_and_bad_values_fall_back(world, monkeypatch):
    now = datetime.now(UTC)
    put_wait(world, "bob", now - timedelta(seconds=60), now - timedelta(seconds=30))
    monkeypatch.setenv(driver_presence.WAIT_GRACE_ENV, "10")
    with world.db.get_connection() as conn:
        assert driver_presence.effective_presence(conn, "bob", now)["basis"] == "last_seen"
    for bad in ("abc", "0", "-3", ""):
        monkeypatch.setenv(driver_presence.WAIT_GRACE_ENV, bad)
        assert driver_presence.wait_grace_seconds() == 120
        with world.db.get_connection() as conn:
            assert driver_presence.effective_presence(conn, "bob", now)["basis"] == "wait"


def test_a_wait_marker_left_by_a_dead_process_is_bounded_by_its_timeout(world):
    now = datetime.now(UTC)
    offline(world)
    put_wait(world, "bob", now - timedelta(seconds=500), None)       # never recorded its end
    view = presence(world, "bob")
    assert view["waiting"] is False and view["status"] == "offline" and view["basis"] == "declared"
    put_wait(world, "bob", now + timedelta(seconds=300), None)       # still inside its timeout
    assert presence(world, "bob")["waiting"] is True


def test_overlapping_waits_never_undercount(world):
    """One wait outlives its own `until` (no end recorded), a second starts, then
    the first one ends: the second must still count as waiting."""
    first = driver_presence._wait_begin(world.db, "bob", 5)
    with world.db.get_connection() as conn:
        past = driver_presence._stamp(datetime.now(UTC) - timedelta(seconds=10))
        conn.execute("UPDATE driver_presence_waits SET until=? WHERE wait_id=?", (past, first))
        conn.commit()
    second = driver_presence._wait_begin(world.db, "bob", 60)
    driver_presence._wait_end(world.db, first)
    assert presence(world, "bob")["waiting"] is True
    third = driver_presence._wait_begin(world.db, "bob", 60)
    driver_presence._wait_end(world.db, second)
    assert presence(world, "bob")["waiting"] is True
    driver_presence._wait_end(world.db, third)
    after = presence(world, "bob")
    assert after["waiting"] is False and after["basis"] == "wait"


def _act(world, action):
    bob = world.svc["bob"]
    if action == "claim_node":
        claim(bob, node="b")
    elif action == "heartbeat":
        held = claim(bob)
        offline(world)
        write(bob, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": held["fence"]}])
    elif action == "start_external_attempt":
        write(bob, "start_external_attempt", project_id="p", node_key="a", expected_revision=1, harness="h",
              external_id="job-1", request_key="rk-1")
    else:
        attempt = write(bob, "start_external_attempt", project_id="p", node_key="a", expected_revision=1,
                        harness="h", external_id="job-1", request_key="rk-1")
        offline(world)
        write(bob, "report_external_attempt", attempt_id=attempt["attempt_id"], observation_id="o1",
              expected_version=attempt["observation_version"], context_hash=attempt["context_hash"],
              status="running", report_ref="/tmp/r", report_sha256="0" * 64)


@pytest.mark.parametrize("action", ["claim_node", "heartbeat", "start_external_attempt", "report_external_attempt"])
def test_acting_on_state_brings_a_declared_driver_back(world, action):
    offline(world)
    _act(world, action)
    view = presence(world, "bob")
    assert view["declared"] is False and view["status"] == "online"
    assert audit(world, "bob")[-1] == {"cause": action, "status": "online"}


def test_reads_do_not_bring_a_declared_driver_back(world):
    held = claim(world.svc["bob"])
    offline(world)
    bob = world.svc["bob"]
    read(bob, "list_claims", project_id="p")
    read(bob, "get_claim", project_id="p", claim_id=held["claim_id"])
    read(bob, "get_node", project_id="p", node_key="a")
    read(bob, "list_driver_messages")
    assert presence(world, "bob")["declared"] is True


# ---------------------------------------------------------------------------
# C. take_over_claim
# ---------------------------------------------------------------------------
def test_takeover_of_a_declared_offline_owner(world):
    bob, carol = world.svc["bob"], world.svc["carol"]
    write(world.owner, "set_node_hold", project_id="p", node_key="a", held=True, expected_revision=0,
          reason="owner pause")
    hold_before = hold_row(world)
    held = claim(bob)
    offline(world, reason="weekend")
    out = take(carol, held)
    old, new = out["taken_over"], out["claim"]
    assert old["status"] == "taken_over" and old["claim_id"] == held["claim_id"]
    assert new["status"] == "live" and new["driver_id"] == "carol" and new["fence"] == held["fence"] + 1
    assert new["purpose"] == "implement" and new["workspace"] == "host:/w#b" and new["attempt_id"] is None
    assert new["lease_state"] == "healthy" and new["lease_seconds"] == held["lease_seconds"]
    assert out["idempotent"] is False and out["owner_presence"]["status"] == "offline"
    assert out["owner_presence"]["declared"] is True and out["owner_presence"]["last_seen_at"]
    history = read(carol, "get_claim", project_id="p", claim_id=held["claim_id"])["history"]
    assert [h["status"] for h in history] == ["live", "taken_over"] and REASON in history[-1]["reason"]
    assert history[-1]["actor"] == "driver:carol"
    taken = events(world, "claim_taken_over")
    assert len(taken) == 1 and taken[0]["from_driver_id"] == "bob" and taken[0]["to_driver_id"] == "carol"
    assert taken[0]["reason"] == REASON and taken[0]["new_fence"] == held["fence"] + 1
    evidence = taken[0]["owner_presence"]
    assert evidence["status"] == "offline" and evidence["declared"] is True and "last_seen_at" in evidence
    assert events(world, "claim_acquired")[-1]["taken_over_from"] == held["claim_id"]
    inbox = bob.driver_inbox.list_driver_messages()["messages"]
    assert [m["kind"] for m in inbox] == ["takeover_notice"] and "taken_over" in inbox[0]["body"]
    assert hold_row(world) == hold_before and hold_before[2] == "owner pause"   # hold and reason untouched
    live = read(carol, "list_claims", project_id="p")["claims"]
    assert [(c["driver_id"], c["fence"]) for c in live] == [("carol", held["fence"] + 1)]


def test_takeover_of_a_derived_offline_owner(world):
    held = claim(world.svc["bob"])
    set_seen(world.db, "bob", SIX_HOURS + 60)
    out = take(world.svc["carol"], held)
    assert out["owner_presence"]["basis"] == "last_seen" and out["owner_presence"]["declared"] is False


@pytest.mark.parametrize("seen,status", [(10, "online"), (3600, "away"), (SIX_HOURS, "away")])
def test_takeover_is_never_allowed_against_an_online_or_away_owner(world, seen, status):
    held = claim(world.svc["bob"])
    set_seen(world.db, "bob", seen)
    refused = refusal(lambda: take(world.svc["carol"], held))
    assert refused.code == "owner_not_offline" and f"is {status}" in str(refused)
    assert refused.facts["owner_presence"]["status"] == status
    assert read(world.svc["carol"], "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == "live"


def test_takeover_refused_while_the_owner_waits_even_if_declared(world):
    bob, carol = world.svc["bob"], world.svc["carol"]
    held = claim(bob)
    results = {}

    def attempt_takeover():
        offline(world)                                  # declared during the wait: the wait still wins
        try:
            take(carol, held)
        except ClaimError as exc:
            results["code"], results["facts"] = exc.code, exc.facts

    _wait_and_observe(bob, lambda: bob.driver_inbox.wait_for_driver_inbox(timeout_seconds=1.0),
                      declare_during=attempt_takeover)
    assert results["code"] == "owner_not_offline" and results["facts"]["owner_presence"]["basis"] == "wait"


def test_refusal_not_project_member(world):
    held = claim(world.svc["bob"])
    offline(world)
    assert refusal(lambda: take(world.svc["dave"], held)).code == "not_project_member"
    world.reg.set_membership("p", "erin", "removed", 1, "left", actor="test")
    assert refusal(lambda: take(world.svc["erin"], held)).code == "not_project_member"
    world.reg.set_status("carol", "suspended", 1, "paused", actor="test")
    assert refusal(lambda: take(world.svc["carol"], held)).code == "not_project_member"


def test_refusal_stale_fence_claim_not_live_and_own_claim(world):
    bob, carol = world.svc["bob"], world.svc["carol"]
    held = claim(bob)
    offline(world)
    stale = refusal(lambda: take(carol, held, fence=held["fence"] + 7))
    assert stale.code == "stale_fence" and stale.facts["fence"] == held["fence"]
    assert refusal(lambda: take(bob, held)).code == "already_held"
    write(bob, "release_claim", project_id="p", claim_id=held["claim_id"], fence=held["fence"], reason="done")
    offline(world)                                      # release_claim does not bring bob back by itself
    gone = refusal(lambda: take(carol, held))
    assert gone.code == "claim_not_live" and gone.facts["status"] == "released"


def test_refusal_of_unknown_claim_and_short_reason(world):
    held = claim(world.svc["bob"])
    offline(world)
    with pytest.raises(StateGraphError):
        take(world.svc["carol"], held, reason="too short")
    from core.state_graph import StateNotFound
    with pytest.raises(StateNotFound):
        take(world.svc["carol"], {"claim_id": "claim-nope", "fence": 1})


def test_the_previous_owner_is_fenced_out(world):
    bob, carol = world.svc["bob"], world.svc["carol"]
    write(world.owner, "set_claim_enforcement", project_id="p", claim_enforcement="on", expected_revision=0,
          reason="enforce for the fence test")
    held = claim(bob)
    offline(world)
    take(carol, held)
    beat = write(bob, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": held["fence"]}])
    assert beat["renewed"] == [] and beat["refused"] == [{"kind": "claim", "id": held["claim_id"],
                                                         "error": "stale_fence"}]
    released = refusal(lambda: write(bob, "release_claim", project_id="p", claim_id=held["claim_id"],
                                     fence=held["fence"], reason="back"))
    assert released.code == "stale_fence"
    dispatched = refusal(lambda: write(bob, "start_external_attempt", project_id="p", node_key="a",
                                       expected_revision=1, harness="h", external_id="j", request_key="k",
                                       claim_id=held["claim_id"], fence=held["fence"]))
    assert dispatched.code == "stale_fence"
    unnamed = refusal(lambda: write(bob, "start_external_attempt", project_id="p", node_key="a",
                                    expected_revision=1, harness="h", external_id="j", request_key="k"))
    assert unnamed.code == "claimed_by_other" and unnamed.facts["holder"] == "carol"
    again = refusal(lambda: claim(bob, key="bob-again"))
    assert again.code == "claimed_by_other" and again.facts["holder"] == "carol"


def test_request_key_idempotency(world):
    bob, carol = world.svc["bob"], world.svc["carol"]
    held = claim(bob)
    offline(world)
    first = take(carol, held, key="same")
    driver_presence.declare(world.db, "bob", "online")             # the owner is back: a replay still answers
    replay = take(carol, held, key="same")
    assert replay["idempotent"] is True and replay["claim"]["claim_id"] == first["claim"]["claim_id"]
    assert replay["taken_over"]["status"] == "taken_over"
    reused = refusal(lambda: take(carol, held, key="same", reason=REASON + " (edited)"))
    assert reused.code == "request_key_reused"
    assert len(events(world, "claim_taken_over")) == 1


def test_two_takeovers_race_and_exactly_one_wins(world):
    bob = world.svc["bob"]
    for node in ("a", "b"):
        held = claim(bob, node=node)
        offline(world)
        barrier = threading.Barrier(2)
        outcomes = []

        def go(who):
            service = StateService(world.db, actor="driver:" + who, driver_id=who, project_read_trusted=True)
            barrier.wait()
            try:
                outcomes.append(("ok", take(service, held, key=f"race-{node}")["claim"]["driver_id"]))
            except ClaimError as exc:
                outcomes.append((exc.code, who))

        threads = [threading.Thread(target=go, args=(who,)) for who in ("carol", "erin")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(o[0] for o in outcomes) == ["claim_not_live", "ok"], outcomes
        live = [c for c in read(bob, "list_claims", project_id="p", node_keys=[node])["claims"]]
        assert len(live) == 1 and live[0]["fence"] == held["fence"] + 1


def test_takeover_against_the_owners_heartbeat_has_one_consistent_outcome(world):
    bob, carol = world.svc["bob"], world.svc["carol"]
    # deterministic orders first
    held = claim(bob)
    offline(world)
    write(bob, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": held["fence"]}])
    assert refusal(lambda: take(carol, held)).code == "owner_not_offline"
    offline(world)
    take(carol, held, key="after")
    beat = write(bob, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": held["fence"]}])
    assert beat["refused"][0]["error"] == "stale_fence"
    # then a real race, repeated: never both, never neither
    for round_ in range(6):
        drivers._LAST_SEEN.clear()
        node = "b"
        mine = claim(bob, node=node, key=f"race-hb-{round_}")
        offline(world)
        barrier = threading.Barrier(2)
        result = {}

        def heartbeat():
            barrier.wait()
            out = write(StateService(world.db, actor="driver:bob", driver_id="bob", project_read_trusted=True),
                        "heartbeat", project_id="p", claims=[{"claim_id": mine["claim_id"], "fence": mine["fence"]}])
            result["renewed"] = bool(out["renewed"])

        def takeover():
            barrier.wait()
            try:
                take(StateService(world.db, actor="driver:carol", driver_id="carol", project_read_trusted=True),
                     mine, key=f"race-take-{round_}")
                result["taken"] = True
            except ClaimError as exc:
                assert exc.code == "owner_not_offline", exc
                result["taken"] = False

        threads = [threading.Thread(target=heartbeat), threading.Thread(target=takeover)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert result["renewed"] != result["taken"], (round_, result)
        status = read(bob, "get_claim", project_id="p", claim_id=mine["claim_id"])["claim"]["status"]
        assert status == ("taken_over" if result["taken"] else "live")
        # reset node b for the next round
        live = read(bob, "list_claims", project_id="p", node_keys=[node])["claims"][0]
        holder = world.svc[live["driver_id"]]
        write(holder, "release_claim", project_id="p", claim_id=live["claim_id"], fence=live["fence"], reason="reset")


def test_takeover_rides_the_same_transports(world):
    """execute() is what REST /api/state/commands and MCP state_graph_write call."""
    held = claim(world.svc["bob"])
    offline(world)
    assert "take_over_claim" in WRITE_REQUESTS
    with pytest.raises(StateGraphError):        # strict schema: reason 20..300, request_key required
        execute(world.svc["carol"], "take_over_claim", {"project_id": "p", "claim_id": held["claim_id"],
                                                        "expected_fence": 1, "reason": "x"}, allow_write=True)
    with pytest.raises(StateGraphError):        # a write, not on the read surface
        execute(world.svc["carol"], "take_over_claim", {}, allow_write=False)


# ---------------------------------------------------------------------------
# D. surfaces
# ---------------------------------------------------------------------------
def test_list_claims_and_get_claim_carry_holder_presence_and_a_hint_only_when_offline(world):
    held = claim(world.svc["bob"])
    claim(world.svc["carol"], node="b")
    listed = {c["driver_id"]: c for c in read(world.svc["erin"], "list_claims", project_id="p")["claims"]}
    assert listed["bob"]["holder_presence"]["status"] == "online" and "takeover_hint" not in listed["bob"]
    offline(world, reason="flight to a private place")
    listed = {c["driver_id"]: c for c in read(world.svc["erin"], "list_claims", project_id="p")["claims"]}
    hint = listed["bob"]["takeover_hint"]
    assert "take_over_claim" in hint and held["claim_id"] in hint and "flight" not in hint
    assert "reason" not in listed["bob"]["holder_presence"]           # the owner's prose is not copied
    assert listed["bob"]["holder_presence"]["basis"] == "declared"
    assert "takeover_hint" not in listed["carol"]
    one = read(world.svc["erin"], "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]
    assert one["holder_presence"]["status"] == "offline" and "take_over_claim" in one["takeover_hint"]


def test_claimed_by_other_names_holder_presence_and_the_takeover(world):
    held = claim(world.svc["bob"])
    online = refusal(lambda: claim(world.svc["carol"]))
    assert online.code == "claimed_by_other" and online.facts["holder_presence"]["status"] == "online"
    assert "holder is online" in str(online) and "take_over_claim" not in str(online)
    offline(world)
    gone = refusal(lambda: claim(world.svc["carol"], key="carol-2"))
    assert "take_over_claim" in str(gone) and gone.facts["holder_presence"]["status"] == "offline"
    assert f"expected_fence={held['fence']}" in gone.facts["takeover_hint"]
    write(world.owner, "set_claim_enforcement", project_id="p", claim_enforcement="on", expected_revision=0,
          reason="enforce for the dispatch refusal")
    dispatch = refusal(lambda: write(world.svc["carol"], "start_external_attempt", project_id="p", node_key="a",
                                     expected_revision=1, harness="h", external_id="j", request_key="k"))
    assert dispatch.code == "claimed_by_other" and "take_over_claim" in str(dispatch)


# ---------------------------------------------------------------------------
# E. attempts: claim takeover names the blocking attempt; take_over_attempt of an offline owner
# ---------------------------------------------------------------------------
def enforced(world):
    write(world.owner, "set_claim_enforcement", project_id="p", claim_enforcement="on", expected_revision=0,
          reason="enforce for the attempt tests")


def bob_attempt(world, node="a"):
    held = claim(world.svc["bob"], node=node)
    attempt = write(world.svc["bob"], "start_external_attempt", project_id="p", node_key=node, expected_revision=1,
                    harness="h", external_id="job-" + node, request_key="rk-" + node, claim_id=held["claim_id"],
                    fence=held["fence"])
    return held, attempt


def take_attempt(service, attempt, fence=1, reason="owner offline; continuing"):
    return write(service, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=fence,
                 reason=reason)


def bob_report(world, attempt, fence=1, oid="o1", version=None, who="bob"):
    return write(world.svc[who], "report_external_attempt", attempt_id=attempt["attempt_id"], observation_id=oid,
                 expected_version=attempt["observation_version"] if version is None else version,
                 context_hash=attempt["context_hash"], status="running", report_ref="/tmp/r",
                 report_sha256="0" * 64, fence=fence)


def test_claim_takeover_names_the_blocking_attempt_and_its_next_command_which_succeeds(world):
    bob, carol = world.svc["bob"], world.svc["carol"]
    enforced(world)
    held, attempt = bob_attempt(world)
    # Before any takeover the old owner's report path is unchanged.
    assert bob_report(world, attempt)["status"] == "running"
    attempt = read(bob, "get_attempt", attempt_id=attempt["attempt_id"])
    offline(world)
    out = take(carol, held)
    blocking = out["active_attempts"]
    assert [a["attempt_id"] for a in blocking] == [attempt["attempt_id"]] and blocking[0]["owner_driver_id"] == "bob"
    command = blocking[0]["next_command"]
    assert command == {"action": "take_over_attempt", "arguments": {
        "attempt_id": attempt["attempt_id"], "expected_owner_fence": 1, "reason": REASON}}
    assert "next_command" in out["note"]
    new = out["claim"]
    with pytest.raises(StateConflict, match="node has an active attempt"):
        write(carol, "start_external_attempt", project_id="p", node_key="a", expected_revision=1, harness="h",
              external_id="job-c", request_key="rk-c", claim_id=new["claim_id"], fence=new["fence"])
    moved = write(carol, command["action"], **command["arguments"])
    assert moved["owner_driver_id"] == "carol" and moved["owner_fence"] == 2 and moved["lease_state"] == "healthy"
    assert moved["owner_presence"]["status"] == "offline" and moved["owner_presence"]["declared"] is True
    assert moved["claim"]["driver_id"] == "carol" and moved["claim"]["attempt_id"] == attempt["attempt_id"]
    event = events(world, "attempt_ownership_transferred")[-1]
    assert event["owner_offline"] is True and event["owner_presence"]["basis"] == "declared"
    assert event["from_driver_id"] == "bob" and event["to_driver_id"] == "carol" and event["fence"] == 2
    assert "while you were offline" in [m for m in bob.driver_inbox.list_driver_messages()["messages"]
                                        if "attempt" in m["subject"]][-1]["body"]
    # The previous owner is fenced out of the attempt.
    assert refusal(lambda: bob_report(world, attempt, fence=1, oid="o2")).code == "stale_fence"
    assert refusal(lambda: bob_report(world, attempt, fence=2, oid="o3")).code == "stale_fence"
    beat = write(bob, "heartbeat", project_id="p", attempts=[{"attempt_id": attempt["attempt_id"], "fence": 1}])
    assert beat["refused"] == [{"kind": "attempt", "id": attempt["attempt_id"], "error": "not_attempt_owner"}]
    assert bob_report(world, attempt, fence=2, oid="o4", who="carol")["status"] == "running"


def test_take_over_attempt_of_an_offline_owner_without_a_claim_takeover(world):
    enforced(world)
    held, attempt = bob_attempt(world)
    set_seen(world.db, "bob", SIX_HOURS + 60)                     # derived offline, nothing declared
    moved = take_attempt(world.svc["erin"], attempt)
    assert moved["owner_driver_id"] == "erin" and moved["owner_fence"] == 2
    assert moved["owner_presence"]["basis"] == "last_seen"
    assert read(world.svc["erin"], "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == \
        "transferred"


@pytest.mark.parametrize("seen", [10, 3600, SIX_HOURS])
def test_online_or_away_owner_keeps_the_unchanged_healthy_lease_refusal(world, seen):
    enforced(world)
    _, attempt = bob_attempt(world)
    set_seen(world.db, "bob", seen)
    refused = refusal(lambda: take_attempt(world.svc["carol"], attempt))
    assert refused.code == "lease_not_expired"
    assert re.match(r"lease_not_expired: lease is healthy; reclaimable at \S+ \(expiry \S+ \+ 900s grace\)$",
                    str(refused)), str(refused)
    assert read(world.svc["carol"], "get_attempt", attempt_id=attempt["attempt_id"])["owner_driver_id"] == "bob"


def test_a_waiting_owner_keeps_its_attempt(world):
    enforced(world)
    _, attempt = bob_attempt(world)
    bob = world.svc["bob"]
    result = {}

    def try_take():
        offline(world)
        result["code"] = refusal(lambda: take_attempt(world.svc["carol"], attempt)).code

    _wait_and_observe(bob, lambda: bob.driver_inbox.wait_for_driver_inbox(timeout_seconds=1.0),
                      declare_during=try_take)
    assert result["code"] == "lease_not_expired"


def test_take_over_attempt_refusals_for_an_offline_owner(world):
    enforced(world)
    _, attempt = bob_attempt(world)
    offline(world)
    # (This fixture has no live registry: the strict check of the offline path answers, with the
    # same code as the P3 helper - see test_live_registry_non_member_is_not_project_member.)
    assert refusal(lambda: take_attempt(world.svc["dave"], attempt)).code == "not_project_member"
    world.reg.set_membership("p", "erin", "removed", 1, "left", actor="test")
    assert refusal(lambda: take_attempt(world.svc["erin"], attempt)).code == "not_project_member"
    world.reg.set_status("carol", "suspended", 1, "paused", actor="test")      # member row kept, driver inactive
    assert refusal(lambda: take_attempt(world.svc["carol"], attempt)).code == "not_project_member"
    world.reg.set_status("carol", "active", 2, "back", actor="test")
    assert refusal(lambda: take_attempt(world.svc["bob"], attempt)).code == "already_owner"
    stale = refusal(lambda: take_attempt(world.svc["carol"], attempt, fence=5))
    assert stale.code == "stale_fence"
    take_attempt(world.svc["carol"], attempt)
    offline(world, who="carol")
    again = refusal(lambda: take_attempt(world.svc["bob"], attempt, fence=1))
    assert again.code == "stale_fence"                               # the fence moved to 2


def test_take_over_attempt_refuses_a_non_member_only_through_the_strict_registry_check(world, monkeypatch):
    """With the P3 membership helper unable to judge (no live registry), the
    offline path still fails closed on the transaction's own registry rows."""
    from core import state_enforcement
    enforced(world)
    _, attempt = bob_attempt(world)
    offline(world)
    monkeypatch.setattr(state_enforcement, "project_membership", lambda *a: None)
    assert refusal(lambda: take_attempt(world.svc["dave"], attempt)).code == "not_project_member"


def test_abandon_is_not_extended_and_keeps_its_attestation(world):
    enforced(world)
    _, attempt = bob_attempt(world)
    offline(world)
    early = refusal(lambda: write(world.svc["carol"], "abandon_external_attempt", attempt_id=attempt["attempt_id"],
                                  expected_owner_fence=1, abandon_kind="unknown", reason="owner offline"))
    assert early.code == "lease_not_expired"
    with pytest.raises(StateGraphError, match="attestation"):
        write(world.svc["carol"], "abandon_external_attempt", attempt_id=attempt["attempt_id"],
              expected_owner_fence=1, abandon_kind="confirmed_stopped", reason="owner offline")


def test_two_attempt_takeovers_race_and_exactly_one_wins(world):
    enforced(world)
    _, attempt = bob_attempt(world)
    offline(world)
    barrier, outcomes = threading.Barrier(2), []

    def go(who):
        service = StateService(world.db, actor="driver:" + who, driver_id=who, project_read_trusted=True)
        barrier.wait()
        try:
            outcomes.append(("ok", take_attempt(service, attempt)["owner_driver_id"]))
        except ClaimError as exc:
            outcomes.append((exc.code, who))

    threads = [threading.Thread(target=go, args=(who,)) for who in ("carol", "erin")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o[0] for o in outcomes) == ["ok", "stale_fence"], outcomes
    assert read(world.svc["bob"], "get_attempt", attempt_id=attempt["attempt_id"])["owner_fence"] == 2


def test_attempt_takeover_against_the_owners_heartbeat_has_one_consistent_outcome(world):
    enforced(world)
    for round_, node in enumerate(("a", "b")):
        drivers._LAST_SEEN.clear()
        _, attempt = bob_attempt(world, node=node)
        offline(world)
        barrier, result = threading.Barrier(2), {}

        def heartbeat():
            barrier.wait()
            out = write(StateService(world.db, actor="driver:bob", driver_id="bob", project_read_trusted=True),
                        "heartbeat", project_id="p", attempts=[{"attempt_id": attempt["attempt_id"], "fence": 1}])
            result["renewed"] = bool(out["renewed"])

        def takeover():
            barrier.wait()
            try:
                take_attempt(StateService(world.db, actor="driver:carol", driver_id="carol",
                                          project_read_trusted=True), attempt)
                result["taken"] = True
            except ClaimError as exc:
                assert exc.code == "lease_not_expired", exc
                result["taken"] = False

        threads = [threading.Thread(target=heartbeat), threading.Thread(target=takeover)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert result["renewed"] != result["taken"], (round_, result)
        owner = read(world.svc["bob"], "get_attempt", attempt_id=attempt["attempt_id"])["owner_driver_id"]
        assert owner == ("carol" if result["taken"] else "bob")


# ---------------------------------------------------------------------------
# F. migration of the claim status CHECK
# ---------------------------------------------------------------------------
# The claim tables exactly as main (67cb96ecd8, presence v1) creates them - copied
# verbatim from `git show 67cb96ecd8:core/state_claims.py` (SCHEMA); the test
# below re-checks the copy against git when the repository is reachable.
MAIN_COMMIT = "67cb96ecd8"
MAIN_CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_node_claims (
    claim_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    driver_id TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('implement','review','investigate','plan')),
    status TEXT NOT NULL CHECK(status IN ('live','released','expired','transferred','revoked')),
    fence INTEGER NOT NULL CHECK(fence >= 1),
    node_revision INTEGER NOT NULL,
    attempt_id TEXT,
    workspace TEXT NOT NULL DEFAULT '',
    subagent TEXT,
    lease_seconds INTEGER NOT NULL CHECK(lease_seconds BETWEEN 60 AND 86400),
    lease_expires_at TEXT NOT NULL, last_heartbeat_at TEXT NOT NULL,
    request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id, node_key, driver_id, request_key),
    FOREIGN KEY(project_id, node_key) REFERENCES state_nodes(project_id, node_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS state_node_claims_one_exclusive
ON state_node_claims(project_id, node_key) WHERE status='live' AND purpose IN ('implement','plan');
CREATE INDEX IF NOT EXISTS state_node_claims_live
ON state_node_claims(project_id, status, lease_expires_at);
CREATE TRIGGER IF NOT EXISTS state_node_claims_no_delete BEFORE DELETE ON state_node_claims
BEGIN SELECT RAISE(ABORT,'claims are never deleted; release them'); END;
CREATE TABLE IF NOT EXISTS state_claim_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL, project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    driver_id TEXT NOT NULL, purpose TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('live','released','expired','transferred','revoked')),
    fence INTEGER NOT NULL, lease_expires_at TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    FOREIGN KEY(claim_id) REFERENCES state_node_claims(claim_id)
);
CREATE INDEX IF NOT EXISTS state_claim_history_claim ON state_claim_history(claim_id, seq);
CREATE TRIGGER IF NOT EXISTS state_claim_history_no_update BEFORE UPDATE ON state_claim_history
BEGIN SELECT RAISE(ABORT,'claim history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_claim_history_no_delete BEFORE DELETE ON state_claim_history
BEGIN SELECT RAISE(ABORT,'claim history is append-only'); END;
"""


def test_the_main_schema_copy_is_main_verbatim():
    import subprocess
    try:
        source = subprocess.run(["git", "-C", str(ROOT), "show", f"{MAIN_COMMIT}:core/state_claims.py"],
                                capture_output=True, text=True, timeout=30, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip("the repository history is not reachable here")
    start = source.index('SCHEMA = """') + len('SCHEMA = """')
    assert source[start:source.index('"""', start)] == MAIN_CLAIMS_SCHEMA


def downgrade_to_main_schema(db):
    """Rebuild the claim tables with main's real DDL, keeping their rows."""
    raw = sqlite3.connect(db.db_path)
    raw.execute("PRAGMA foreign_keys=OFF")
    for table in ("state_node_claims", "state_claim_history"):
        raw.execute(f"CREATE TABLE keep_{table} AS SELECT * FROM {table}")
        raw.execute(f"DROP TABLE {table}")
    raw.executescript(MAIN_CLAIMS_SCHEMA)
    for table in ("state_node_claims", "state_claim_history"):
        raw.execute(f"INSERT INTO {table} SELECT * FROM keep_{table}")
        raw.execute(f"DROP TABLE keep_{table}")
    raw.commit()
    raw.close()


def claim_sql(db, table="state_node_claims"):
    with db.get_connection() as conn:
        return conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]


@pytest.fixture(autouse=True)
def _fresh_migration_memory():
    state_claims._MIGRATION_FAILED.clear()
    yield
    state_claims._MIGRATION_FAILED.clear()


def test_main_schema_is_widened_preserving_rows_seq_indexes_and_triggers(world):
    bob = world.svc["bob"]
    held = claim(bob)
    write(bob, "release_claim", project_id="p", claim_id=held["claim_id"], fence=held["fence"], reason="x")
    claim(bob, node="b")
    downgrade_to_main_schema(world.db)
    raw = sqlite3.connect(world.db.db_path)
    raw.execute("UPDATE sqlite_sequence SET seq=40 WHERE name='state_claim_history'")
    raw.commit()
    assert "'taken_over'" not in claim_sql(world.db)
    before = {t: raw.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
              for t in ("state_node_claims", "state_claim_history")}
    objects = sorted(raw.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN "
                                 "('state_node_claims','state_claim_history') AND type!='table'").fetchall())
    raw.close()
    state_claims.initialize(world.db)
    state_claims.initialize(world.db)                                   # a second run changes nothing
    raw = sqlite3.connect(world.db.db_path)
    for table in ("state_node_claims", "state_claim_history"):
        assert "'taken_over'" in claim_sql(world.db, table)
        assert raw.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == before[table]
    assert sorted(raw.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN "
                              "('state_node_claims','state_claim_history') AND type!='table'").fetchall()) == objects
    assert raw.execute("SELECT seq FROM sqlite_sequence WHERE name='state_claim_history'").fetchone()[0] == 40
    assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        raw.execute("DELETE FROM state_node_claims")
    raw.close()
    offline(world)
    live = read(world.svc["carol"], "list_claims", project_id="p")["claims"][0]
    assert take(world.svc["carol"], live)["taken_over"]["status"] == "taken_over"


def test_a_failed_migration_degrades_only_the_takeover(world, caplog):
    bob, carol = world.svc["bob"], world.svc["carol"]
    held = claim(bob)
    downgrade_to_main_schema(world.db)
    raw = sqlite3.connect(world.db.db_path)
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("INSERT INTO state_claim_history(claim_id,project_id,node_key,driver_id,purpose,status,fence,"
                "lease_expires_at,actor,reason,created_at) VALUES('claim-ghost','p','a','bob','implement','live',1,"
                "'2026-01-01','x','synthetic violation','2026-01-01')")
    raw.commit()
    raw.close()
    with caplog.at_level("ERROR"):
        state_claims.initialize(world.db)                               # never raises
    assert "foreign-key violations" in caplog.text
    assert "'taken_over'" not in claim_sql(world.db)                   # skipped, not half-done
    StateService(world.db, actor="driver:carol", driver_id="carol", project_read_trusted=True)   # no retry, no raise
    beat = write(bob, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": held["fence"]}])
    assert beat["refused"] == []
    assert claim(carol, node="b")["status"] == "live"
    assert len(read(carol, "list_claims", project_id="p")["claims"]) == 2
    offline(world)
    assert refusal(lambda: take(carol, held)).code == "takeover_unavailable"


def test_the_host_migrates_claim_tables_at_startup():
    source = (ROOT / "api/main.py").read_text(encoding="utf-8")
    lifespan = source[source.index("async def lifespan"):source.index("\n    recover_claims_on_startup()\n")]
    assert "_initialize_claims(_state_db())" in lifespan


# ---------------------------------------------------------------------------
# G. presence decides nothing else
# ---------------------------------------------------------------------------
HOOKS = ("annotate_claims", "holder_note", "mark_back", "driver_waiting")
_CONDITION_FIELDS = {ast.If: "test", ast.While: "test", ast.IfExp: "test", ast.Assert: "test"}


def _condition_nodes(tree):
    out = []
    for node in ast.walk(tree):
        field = _CONDITION_FIELDS.get(type(node))
        if field:
            out.append(getattr(node, field))
        if isinstance(node, ast.comprehension):
            out.extend(node.ifs)
        if isinstance(node, (ast.BoolOp, ast.Compare)):
            out.append(node)
    return out


PERMISSION_FILES = ["core/state_claims.py", "core/state_enforcement.py", "core/state_handoffs.py",
                    "core/state_subagents.py", "core/driver_notices.py", "core/state_attempts.py",
                    "core/state_service.py", "core/state_commands.py", "core/state_external.py",
                    "core/state_recovery.py", "core/state_changes.py", "api/authz.py", "api/state_only.py",
                    "api/state_http.py", "api/state_graph_routers.py", "api/mcp_router.py"]


@pytest.mark.parametrize("rel", PERMISSION_FILES)
def test_permission_code_uses_presence_only_through_describing_hooks(rel):
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    hooked_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "driver_presence":
            assert node.attr in HOOKS, f"{rel}: driver_presence.{node.attr} is not a describing hook"
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Attribute) and node.value.func.attr in HOOKS:
            for target in node.targets:
                hooked_names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = {a.name for a in node.names}
            assert not ({"effective_presence", "presence_for", "presence_now", "presence_of"} & names), rel
    for condition in _condition_nodes(tree):
        for inner in ast.walk(condition):
            if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
                assert inner.value.id != "driver_presence", f"{rel}: presence hook inside a condition"
            if isinstance(inner, ast.Name):
                assert inner.id not in hooked_names, f"{rel}: a presence hook's result {inner.id} decides"


def test_only_state_takeover_decides_on_effective_presence():
    users = set()
    for folder in ("core", "api"):
        for path in (ROOT / folder).glob("*.py"):
            if "effective_presence" in path.read_text(encoding="utf-8"):
                users.add(str(path.relative_to(ROOT)))
    assert users == {"core/driver_presence.py", "core/drivers.py", "core/state_takeover.py"}
    tree = ast.parse((ROOT / "core/state_takeover.py").read_text(encoding="utf-8"))
    calls = [(fn.name, node) for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
             for node in ast.walk(fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "effective_presence"]
    assert sorted(name for name, _ in calls) == ["offline_owner_evidence", "take_over_claim"]
    # offline_owner_evidence is reached only from StateRecovery._reclaimable, and only when
    # take_over_attempt passes offline_owner (abandon_external_attempt never does).
    referencing = {str(p.relative_to(ROOT)) for folder in ("core", "api") for p in (ROOT / folder).glob("*.py")
                   if "offline_owner_evidence" in p.read_text(encoding="utf-8")}
    assert referencing == {"core/state_takeover.py", "core/state_recovery.py"}
    rtree = ast.parse((ROOT / "core/state_recovery.py").read_text(encoding="utf-8"))
    users = {fn.name for fn in ast.walk(rtree) if isinstance(fn, ast.FunctionDef)
             and any(isinstance(n, ast.Name) and n.id == "offline_owner_evidence" for n in ast.walk(fn))}
    assert users == {"_reclaimable"}
    passing = {fn.name for fn in ast.walk(rtree) if isinstance(fn, ast.FunctionDef)
               and any(isinstance(n, ast.keyword) and n.arg == "offline_owner" for n in ast.walk(fn))}
    assert passing == {"take_over_attempt"}
    text = (ROOT / "core/state_takeover.py").read_text(encoding="utf-8")
    assert re.search(r'return presence\["status"\] == "offline" and presence\["determinable"\] is True', text)
    assert text.count("owner_is_offline(presence)") == 3          # the definition and its two uses
    # drivers.py only DESCRIBES it in its views
    dtree = ast.parse((ROOT / "core/drivers.py").read_text(encoding="utf-8"))
    describing = {fn.name for fn in ast.walk(dtree) if isinstance(fn, ast.FunctionDef)
                  and any(isinstance(n, ast.Name) and n.id == "effective_presence" for n in ast.walk(fn))}
    assert describing == {"_effective_view"}


# ---------------------------------------------------------------------------
# H. review fixes (2026-10-30): fail closed, collisions, public, privacy, wait_for_run
# ---------------------------------------------------------------------------
def hold_write_lock(world, seconds, started):
    def run():
        conn = sqlite3.connect(world.db.db_path, timeout=10)
        conn.execute("BEGIN IMMEDIATE")
        started.set()
        threading.Event().wait(seconds)
        conn.rollback()
        conn.close()
    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(5)
    return thread


def test_reviewer_scenario_a_wait_started_under_a_3s_write_lock_still_protects_the_owner(world):
    """bob declares offline and starts a 6 s inbox wait while another connection
    holds the write lock for 3 s. The wait registers after the lock (bounded
    retry), so carol's takeover during the wait is refused."""
    held = claim(world.svc["bob"])
    offline(world)
    bob = world.svc["bob"]
    started = threading.Event()
    locker = hold_write_lock(world, 3.0, started)
    result = {}

    def try_take():
        result["code"] = refusal(lambda: take(world.svc["carol"], held)).code

    seen, _ = _wait_and_observe(bob, lambda: bob.driver_inbox.wait_for_driver_inbox(timeout_seconds=4.0),
                                declare_during=try_take)
    locker.join()
    assert seen["waiting"] is True and seen["declared"] is False
    assert result["code"] == "owner_not_offline"


def test_a_wait_that_cannot_be_registered_fails_retryably_before_waiting(world, monkeypatch):
    monkeypatch.setattr(driver_presence, "WAIT_REGISTER_SECONDS", 0.6)
    held = claim(world.svc["bob"])
    offline(world)
    started = threading.Event()
    locker = hold_write_lock(world, 3.0, started)
    bob = world.svc["bob"]
    try:
        with pytest.raises(ClaimError) as caught:
            asyncio.run(bob.driver_inbox.wait_for_driver_inbox(timeout_seconds=30))
        assert caught.value.code == "wait_not_registered" and "retry" in str(caught.value)
        with pytest.raises(ClaimError) as caught:
            asyncio.run(bob.wait_for_state_change("p", after=10 ** 6, timeout_seconds=30))
        assert caught.value.code == "wait_not_registered"
    finally:
        locker.join()
    view = presence(world, "bob")
    assert view["declared"] is True and view["waiting"] is False          # nothing pretended
    assert take(world.svc["carol"], held)["taken_over"]["status"] == "taken_over"   # consistent: bob never waited


@pytest.mark.parametrize("action", ["claim_node", "heartbeat", "start_external_attempt", "report_external_attempt"])
def test_the_declaration_clear_commits_with_the_action_or_not_at_all(world, action):
    """If the clear cannot be written the action must not commit either: never
    'renewed (or claimed, started, reported) while still declared offline'."""
    bob = world.svc["bob"]
    held = claim(bob)
    attempt = None
    if action == "report_external_attempt":
        attempt = write(bob, "start_external_attempt", project_id="p", node_key="b", expected_revision=1,
                        harness="h", external_id="job-b", request_key="rk-b")
    offline(world)
    with world.db.get_connection() as conn:
        conn.execute("CREATE TRIGGER block_clear BEFORE DELETE ON driver_presence_declarations "
                     "BEGIN SELECT RAISE(ABORT,'synthetic: the clear cannot be written'); END")
        conn.commit()

    def state():
        with world.db.get_connection() as conn:
            return (conn.execute("SELECT lease_expires_at FROM state_node_claims WHERE claim_id=?",
                                 (held["claim_id"],)).fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM state_node_claims").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM state_attempts").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM state_external_observations").fetchone()[0])

    before = state()
    with pytest.raises(sqlite3.Error, match="synthetic"):
        if action == "claim_node":
            claim(bob, node="b")
        elif action == "heartbeat":
            write(bob, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": held["fence"]}])
        elif action == "start_external_attempt":
            write(bob, "start_external_attempt", project_id="p", node_key="a", expected_revision=1, harness="h",
                  external_id="job-a", request_key="rk-a")
        else:
            write(bob, "report_external_attempt", attempt_id=attempt["attempt_id"], observation_id="o1",
                  expected_version=attempt["observation_version"], context_hash=attempt["context_hash"],
                  status="running", report_ref="/tmp/r", report_sha256="0" * 64)
    assert state() == before and presence(world, "bob")["declared"] is True


@pytest.mark.parametrize("stamp", [None, "garbage", "2999-01-01T00:00:00+00:00"])
def test_an_owner_whose_presence_cannot_be_determined_is_never_taken_over(world, stamp):
    held = claim(world.svc["bob"])
    with world.db.get_connection() as conn:
        conn.execute("UPDATE drivers SET last_seen_at=? WHERE driver_id='bob'", (stamp,))
        conn.commit()
    view = presence(world, "bob")
    assert view["status"] == "offline" and view["determinable"] is False      # v1 says offline...
    refused = refusal(lambda: take(world.svc["carol"], held))                # ...the authority does not
    assert refused.code == "owner_not_offline" and "undeterminable" in str(refused)


def test_an_unregistered_owner_is_never_taken_over(world):
    ghost = StateService(world.db, actor="driver:ghost", driver_id="ghost", project_read_trusted=True)
    held = claim(ghost)
    assert presence(world, "ghost")["determinable"] is False
    assert refusal(lambda: take(world.svc["carol"], held)).code == "owner_not_offline"


def test_a_third_member_cannot_take_the_attempt_while_another_holds_the_taken_over_claim(world):
    enforced(world)
    held, attempt = bob_attempt(world)
    offline(world)
    take(world.svc["carol"], held)
    refused = refusal(lambda: take_attempt(world.svc["erin"], attempt))
    assert refused.code == "claimed_by_other" and refused.facts["holder"] == "carol"
    assert read(world.svc["erin"], "get_attempt", attempt_id=attempt["attempt_id"])["owner_driver_id"] == "bob"
    assert take_attempt(world.svc["carol"], attempt)["owner_driver_id"] == "carol"   # the claim holder can


def test_accepting_an_earlier_handoff_after_a_claim_takeover_is_claimed_by_other(world):
    enforced(world)
    held, attempt = bob_attempt(world)
    offer = write(world.svc["bob"], "offer_handoff", project_id="p", request_key="offer", expected_owner_fence=1,
                  attempt_id=attempt["attempt_id"], to_driver_id="erin",
                  package={"workers": {"quiescent": True}, "next_step": "continue"})
    offline(world)
    take(world.svc["carol"], held)
    refused = refusal(lambda: write(world.svc["erin"], "accept_handoff", project_id="p",
                                    handoff_id=offer["handoff_id"], expected_owner_fence=1))
    assert refused.code == "claimed_by_other" and refused.facts["holder"] == "carol"


def test_live_registry_non_member_is_not_project_member(world, monkeypatch):
    monkeypatch.setattr(drivers, "registry_for", lambda db: world.reg)
    enforced(world)
    held, attempt = bob_attempt(world)
    offline(world)
    assert refusal(lambda: take_attempt(world.svc["dave"], attempt)).code == "not_project_member"
    assert refusal(lambda: take(world.svc["dave"], held)).code == "not_project_member"


def test_dispatch_on_a_taken_over_review_claim_is_stale_fence(world):
    from core.state_enforcement import dispatch_claim
    review = claim(world.svc["bob"], purpose="review")
    offline(world)
    take(world.svc["carol"], review)
    with world.db.get_connection() as conn:
        with pytest.raises(ClaimError) as caught:
            dispatch_claim(conn, "p", "a", "bob", False, claim_id=review["claim_id"], fence=review["fence"],
                           allow_review=True)
    assert caught.value.code == "stale_fence"


def test_the_shared_public_identity_never_declares_or_takes_over(world):
    world.reg.set_membership("p", "public", "member", 0, "fixture", actor="test")
    public = StateService(world.db, actor="driver:public", driver_id="public", project_read_trusted=True)
    with pytest.raises(drivers.DriverError, match="shared_identity"):
        driver_presence.declare(world.db, "public", "offline")
    enforced(world)
    held, attempt = bob_attempt(world)
    offline(world)
    assert refusal(lambda: take(public, held)).code == "shared_identity"
    assert refusal(lambda: take_attempt(public, attempt)).code == "shared_identity"


def test_the_owner_reason_is_not_copied_and_the_result_names_the_previous_claim(world):
    held = claim(world.svc["bob"])
    offline(world, reason="private: dentist")
    refused = refusal(lambda: claim(world.svc["carol"]))
    assert "dentist" not in str(refused) and "reason" not in refused.facts["holder_presence"]
    assert "dentist" not in refused.facts["takeover_hint"]
    out = take(world.svc["carol"], held)
    assert out["previous_claim_id"] == held["claim_id"] and "host:/w#b" in out["workspace_warning"]
    assert "reason" not in out["owner_presence"]
    with world.db.get_connection() as conn:
        assert "dentist" not in json.dumps([dict(r) for r in conn.execute("SELECT payload_json FROM state_events")])


def test_wait_for_run_marks_the_calling_driver_as_waiting(world, monkeypatch, caplog):
    from api import dependencies, mcp_router, state_graph_routers

    class Notifications:
        def subscribe(self, cb):
            pass

        def unsubscribe(self, cb):
            pass

    class SF:
        notifications = Notifications()

        def get_run(self, run_id):
            return {"id": run_id, "status": "running", "graph_name": "g", "project_id": "x", "current_node": "s"}

        def list_runs(self, project_id=None):
            return []

    class Ctx:
        class request_context:
            request = object()

    class FakeMCP:
        def get_context(self):
            return Ctx()

    monkeypatch.setattr(dependencies, "get_skillflow", lambda: SF())
    monkeypatch.setattr(dependencies, "get_db_manager", lambda: world.db)
    monkeypatch.setattr(state_graph_routers, "authenticated_driver_id", lambda request: "bob")
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco

    mcp_router._register_wait_tool(tool, FakeMCP())
    offline(world)

    async def run():
        task = asyncio.create_task(captured["wait_for_run"]("r1", timeout_seconds=1))
        seen = None
        for _ in range(100):
            await asyncio.sleep(0.02)
            seen = driver_presence.presence_for(world.db, "bob")
            if seen["waiting"]:
                break
        out = await task
        return seen, out

    seen, out = asyncio.run(run())
    assert seen["waiting"] is True and seen["declared"] is False and out["timed_out"] is True
    # An identity that cannot be resolved is logged, never silently swallowed.
    monkeypatch.setattr(state_graph_routers, "authenticated_driver_id",
                        lambda request: (_ for _ in ()).throw(RuntimeError("boom")))
    captured.clear()
    mcp_router._register_wait_tool(tool, FakeMCP())
    with caplog.at_level("WARNING"):
        asyncio.run(captured["wait_for_run"]("r1", timeout_seconds=1))
    assert "could not resolve the waiting driver" in caplog.text
