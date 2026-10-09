"""Multi-driver P1: node claims, leases, heartbeats (design/multi-driver-coop.md §4).

One test class per acceptance criterion of State node driver.multi-driver-p1-leases:
claim-schema-and-exclusivity, lease-lifecycle, overview-and-wait, record-only;
plus the additive migration and the REST/MCP surface.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core import drivers, state_claims
from core.state_claims import ClaimError
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}


class Clock:
    def __init__(self):
        self.moment = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.moment

    def advance(self, seconds):
        self.moment += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(state_claims, "_clock", fake)
    return fake


def svc(db, driver=None, *, admin=False, trusted=True):
    actor = f"driver:{driver}" if driver else "authorized-state-operator"
    return StateService(StateDatabase(str(db)), actor=actor, project_read_trusted=trusted,
                        driver_id=driver, is_admin=admin)


def write(service, action, **arguments):
    return execute(service, action, arguments, allow_write=True)


def read(service, action, **arguments):
    return execute(service, action, arguments)


def code_of(call):
    with pytest.raises(ClaimError) as caught:
        call()
    return caught.value.code


@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.sqlite"


def project(db, multi_driver=True):
    owner = svc(db, "owner-cli", admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": k, **NODE} for k in ("a", "b", "c")])
    if multi_driver:
        write(owner, "set_multi_driver", project_id="p", multi_driver="on",
              expected_revision=0, reason="P1 test")
    return owner


def claim(service, node="a", purpose="implement", request_key=None, **extra):
    return write(service, "claim_node", project_id="p", node_key=node, purpose=purpose,
                 expected_revision=1, request_key=request_key or f"{service.driver_id}-{node}-{purpose}",
                 **extra)


def external(service, node="a", key="rk-1"):
    return write(service, "start_external_attempt", project_id="p", node_key=node, expected_revision=1,
                 harness="h", external_id="job-" + key, request_key=key)


def max_seq(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COALESCE(MAX(seq),0) FROM state_events").fetchone()[0]
    finally:
        conn.close()


def events(db, event_type):
    conn = sqlite3.connect(str(db))
    try:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM state_events WHERE event_type=? ORDER BY seq", (event_type,))]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
class TestClaimSchemaAndExclusivity:
    def test_claim_records_row_history_and_acquired_event(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        out = claim(grok, workspace="linxuhaserver:/w#b")
        assert out["status"] == "live" and out["fence"] == 1 and out["driver_id"] == "grok"
        assert out["lease_seconds"] == 7200 and out["lease_state"] == "healthy"
        assert out["lease_expires_at"] == state_claims.stamp(clock.moment + timedelta(seconds=7200))
        assert out["workspace"] == "linxuhaserver:/w#b" and "request_hash" not in out
        info = read(grok, "get_claim", project_id="p", claim_id=out["claim_id"])
        assert [h["status"] for h in info["history"]] == ["live"]
        acquired = events(db, "claim_acquired")
        assert acquired[-1]["claim_id"] == out["claim_id"] and acquired[-1]["driver_id"] == "grok"

    @pytest.mark.parametrize("seconds", [59, 86401])
    def test_lease_seconds_bounds(self, db, seconds):
        project(db)
        with pytest.raises(StateGraphError):
            claim(svc(db, "grok"), lease_seconds=seconds)

    def test_lease_seconds_limits_are_accepted(self, db):
        project(db)
        assert claim(svc(db, "grok"), "a", "review", lease_seconds=60)["lease_seconds"] == 60
        assert claim(svc(db, "grok"), "b", "review", lease_seconds=86400)["lease_seconds"] == 86400

    def test_unknown_purpose_is_refused(self, db):
        project(db)
        with pytest.raises(StateGraphError):
            claim(svc(db, "grok"), purpose="own")

    def test_request_key_is_idempotent_and_bound_to_its_request(self, db):
        project(db)
        grok = svc(db, "grok")
        first = claim(grok, request_key="k1")
        again = claim(grok, request_key="k1")
        assert again["claim_id"] == first["claim_id"] and again["idempotent"] is True
        assert code_of(lambda: claim(grok, purpose="plan", request_key="k1")) == "request_key_reused"

    def test_review_and_investigate_coexist_but_implement_and_plan_are_exclusive(self, db):
        project(db)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(grok, "a", "implement")
        assert claim(codex, "a", "review")["status"] == "live"
        assert claim(codex, "a", "investigate")["status"] == "live"
        assert claim(grok, "a", "review")["status"] == "live"
        with pytest.raises(ClaimError) as caught:
            claim(codex, "a", "implement")
        assert caught.value.code == "claimed_by_other"
        assert caught.value.facts["holder"] == "grok"
        assert caught.value.facts["lease_expires_at"] == held["lease_expires_at"]
        assert "grok" in str(caught.value) and held["lease_expires_at"] in str(caught.value)
        assert code_of(lambda: claim(codex, "a", "plan")) == "claimed_by_other"
        assert code_of(lambda: claim(grok, "a", "plan")) == "already_held"
        claim(codex, "b", "plan")
        assert code_of(lambda: claim(grok, "b", "implement")) == "claimed_by_other"

    def test_the_partial_unique_index_refuses_a_second_live_exclusive_row(self, db):
        project(db)
        held = claim(svc(db, "grok"))
        conn = sqlite3.connect(str(db))
        columns = [r[1] for r in conn.execute("PRAGMA table_info(state_node_claims)")]
        row = dict(zip(columns, conn.execute("SELECT * FROM state_node_claims WHERE claim_id=?",
                                             (held["claim_id"],)).fetchone()))
        row.update(claim_id="claim-forged", driver_id="codex", request_key="forged", fence=2, purpose="plan")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO state_node_claims(" + ",".join(row) + ") VALUES("
                         + ",".join("?" * len(row)) + ")", tuple(row.values()))
        row.update(status="released")
        conn.execute("INSERT INTO state_node_claims(" + ",".join(row) + ") VALUES("
                     + ",".join("?" * len(row)) + ")", tuple(row.values()))
        conn.close()

    def test_concurrent_claims_have_exactly_one_winner(self, db):
        project(db)
        contenders = [svc(db, f"driver-{i}") for i in range(8)]
        barrier = threading.Barrier(len(contenders))
        results = {}

        def race(service):
            barrier.wait()
            try:
                results[service.driver_id] = ("won", claim(service, request_key="race"))
            except ClaimError as exc:
                results[service.driver_id] = (exc.code, exc.facts)

        threads = [threading.Thread(target=race, args=(s,)) for s in contenders]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert len(results) == len(contenders)
        winners = [d for d, (outcome, _) in results.items() if outcome == "won"]
        assert len(winners) == 1, results
        winner = results[winners[0]][1]
        for driver, (outcome, facts) in results.items():
            if driver != winners[0]:
                assert outcome == "claimed_by_other"
                assert facts["holder"] == winners[0]
                assert facts["lease_expires_at"] == winner["lease_expires_at"]
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM state_node_claims WHERE status='live'").fetchone()[0] == 1
        conn.close()

    def test_claim_history_is_append_only_and_claims_are_never_deleted(self, db):
        project(db)
        held = claim(svc(db, "grok"))
        conn = sqlite3.connect(str(db))
        for sql in ("UPDATE state_claim_history SET reason='x'", "DELETE FROM state_claim_history",
                    "DELETE FROM state_node_claims"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only|never deleted"):
                conn.execute(sql)
        assert conn.execute("SELECT COUNT(*) FROM state_claim_history WHERE claim_id=?",
                            (held["claim_id"],)).fetchone()[0] == 1
        conn.close()

    def test_claims_need_a_registered_driver_and_a_current_open_node(self, db):
        owner = project(db)
        anonymous_operator = svc(db)
        assert code_of(lambda: claim(anonymous_operator)) == "driver_identity_required"
        grok = svc(db, "grok")
        with pytest.raises(ClaimError) as caught:
            write(grok, "claim_node", project_id="p", node_key="a", purpose="implement",
                  expected_revision=2, request_key="stale")
        assert caught.value.code == "revision_changed"
        owner.store.supersede_node("p", "c", 1, "gone")
        assert code_of(lambda: claim(grok, "c", "review")) == "node_closed"
        assert code_of(lambda: claim(grok, "a", subagent="codex/x")) == "not_your_subagent"


# ---------------------------------------------------------------------------
class TestLeaseLifecycle:
    def test_heartbeat_extends_without_events_or_observation_version(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        held = claim(grok, "a", "review")
        attempt = external(grok, "b")
        assert attempt["owner_driver_id"] == "grok" and attempt["owner_fence"] == 1
        before_seq, before_version = max_seq(db), attempt["observation_version"]
        clock.advance(1800)
        out = write(grok, "heartbeat", project_id="p",
                    claims=[{"claim_id": held["claim_id"], "fence": 1}],
                    attempts=[{"attempt_id": attempt["attempt_id"], "fence": 1}])
        assert out["refused"] == []
        expected = state_claims.stamp(clock.moment + timedelta(seconds=7200))
        assert {r["id"]: r["lease_expires_at"] for r in out["renewed"]} == {
            held["claim_id"]: expected, attempt["attempt_id"]: expected}
        assert max_seq(db) == before_seq
        now = read(grok, "get_attempt", attempt_id=attempt["attempt_id"])
        assert now["observation_version"] == before_version and now["lease_expires_at"] == expected
        assert now["status"] == "running" and now["updated_at"] == attempt["updated_at"]
        info = read(grok, "get_claim", project_id="p", claim_id=held["claim_id"])
        assert info["claim"]["last_heartbeat_at"] == state_claims.stamp(clock.moment)
        assert len(info["history"]) == 1

    def test_a_parent_renews_its_subagents_in_one_batch(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        first = claim(grok, "a", "review", subagent="grok/sub-1")
        second = claim(grok, "b", "investigate", subagent="grok/sub-1")
        third = claim(grok, "c", "review", subagent="grok/sub-2")
        attempt = external(grok, "a")
        clock.advance(3600)
        before = max_seq(db)
        out = write(grok, "heartbeat", project_id="p",
                    subagents=[{"subagent_id": "grok/sub-1"}, {"subagent_id": "grok/sub-2"},
                               {"subagent_id": "codex/sub-1"}, {"subagent_id": "grok/idle"}],
                    attempts=[{"attempt_id": attempt["attempt_id"], "fence": 1}])
        assert {r["id"] for r in out["renewed"]} == {first["claim_id"], second["claim_id"],
                                                    third["claim_id"], attempt["attempt_id"]}
        assert {(r["id"], r["error"]) for r in out["refused"]} == {
            ("codex/sub-1", "not_your_subagent"), ("grok/idle", "no_live_claims")}
        assert max_seq(db) == before

    def test_batch_size_is_bounded(self, db):
        project(db)
        grok = svc(db, "grok")
        with pytest.raises(StateGraphError):
            write(grok, "heartbeat", project_id="p")
        with pytest.raises(StateGraphError):
            write(grok, "heartbeat", project_id="p",
                  claims=[{"claim_id": f"claim-{i}", "fence": 1} for i in range(101)])
        out = write(grok, "heartbeat", project_id="p",
                    claims=[{"claim_id": f"claim-{i}", "fence": 1} for i in range(100)])
        assert len(out["refused"]) == 100 and {r["error"] for r in out["refused"]} == {"not_found"}

    def test_stale_fence_is_refused(self, db):
        project(db)
        grok = svc(db, "grok")
        held = claim(grok)
        attempt = external(grok, "b")
        out = write(grok, "heartbeat", project_id="p",
                    claims=[{"claim_id": held["claim_id"], "fence": 2}],
                    attempts=[{"attempt_id": attempt["attempt_id"], "fence": 2}])
        assert [r["error"] for r in out["refused"]] == ["stale_fence", "stale_fence"]
        assert out["renewed"] == []
        assert code_of(lambda: write(grok, "release_claim", project_id="p", claim_id=held["claim_id"],
                                     fence=2, reason="done")) == "stale_fence"

    def test_only_the_owner_renews_or_releases_and_admin_release_is_break_glass(self, db):
        owner = project(db)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(grok)
        out = write(codex, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": 1}])
        assert out["refused"][0]["error"] == "not_claim_owner"
        assert code_of(lambda: write(codex, "release_claim", project_id="p", claim_id=held["claim_id"],
                                     fence=1, reason="mine now")) == "not_claim_owner"
        released = write(owner, "release_claim", project_id="p", claim_id=held["claim_id"], fence=1,
                         reason="owner reassigns")
        assert released["status"] == "released"
        assert events(db, "claim_released")[-1]["break_glass"] is True

    def test_release_is_a_state_change_and_idempotent(self, db):
        project(db)
        grok = svc(db, "grok")
        held = claim(grok)
        out = write(grok, "release_claim", project_id="p", claim_id=held["claim_id"], fence=1, reason="done")
        assert out["status"] == "released" and out["lease_state"] is None
        assert events(db, "claim_released")[-1]["break_glass"] is False
        again = write(grok, "release_claim", project_id="p", claim_id=held["claim_id"], fence=1, reason="done")
        assert again["idempotent"] is True
        refused = write(grok, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": 1}])
        assert refused["refused"][0]["error"] == "claim_not_live"
        assert claim(svc(db, "codex"))["fence"] == 2

    def test_expiry_grace_and_reclaim_emit_events_but_never_terminate(self, db, clock):
        project(db)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(grok, "a")
        attempt = external(grok, "a", "rk-a")
        clock.advance(7200)
        listed = read(grok, "list_claims", project_id="p")["claims"]
        assert [c["lease_state"] for c in listed] == ["expired"]
        expired = events(db, "lease_expired")
        assert {(e["subject"], e["phase"]) for e in expired} == {("claim", "expired"), ("attempt", "expired")}
        assert all(e["reclaimable_at"] == state_claims.stamp(clock.moment + timedelta(seconds=900))
                   for e in expired)
        grok.claims.sweep("p")
        assert len(events(db, "lease_expired")) == 2, "each phase is emitted once"
        assert code_of(lambda: claim(codex, "a")) == "claimed_by_other"
        clock.advance(900)
        out = claim(codex, "a")
        assert out["fence"] == 2 and out["driver_id"] == "codex"
        phases = [(e["subject"], e["phase"]) for e in events(db, "lease_expired")]
        assert ("claim", "reclaimable") in phases and ("attempt", "reclaimable") in phases
        info = read(grok, "get_claim", project_id="p", claim_id=held["claim_id"])
        assert info["claim"]["status"] == "expired"
        assert [h["status"] for h in info["history"]] == ["live", "expired"]
        still = read(grok, "get_attempt", attempt_id=attempt["attempt_id"])
        assert still["status"] == "running" and still["lease_state"] == "reclaimable"
        assert still["observation_version"] == 0
        stale = write(grok, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": 1}])
        assert stale["refused"][0]["error"] == "claim_not_live"

    def test_heartbeat_inside_grace_restores_a_healthy_lease(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        held = claim(grok)
        clock.advance(7300)
        grok.claims.sweep("p")
        out = write(grok, "heartbeat", project_id="p", claims=[{"claim_id": held["claim_id"], "fence": 1}])
        assert out["renewed"] and not out["refused"]
        assert read(grok, "list_claims", project_id="p")["claims"][0]["lease_state"] == "healthy"

    def test_legacy_attempts_are_unleased_and_never_expire(self, db, clock):
        project(db)
        legacy = external(svc(db), "a")
        assert legacy["owner_driver_id"] is None and legacy["lease_expires_at"] is None
        assert legacy["owner_fence"] == 0 and legacy["lease_state"] == "legacy_unleased"
        grok = svc(db, "grok")
        clock.advance(10 * 365 * 86400)
        grok.claims.sweep("p")
        assert events(db, "lease_expired") == []
        assert read(grok, "get_attempt", attempt_id=legacy["attempt_id"])["lease_state"] == "legacy_unleased"
        assert read(grok, "get_node", project_id="p", node_key="a")["node"]["readiness"] == "in_progress"
        out = write(grok, "heartbeat", project_id="p", attempts=[{"attempt_id": legacy["attempt_id"], "fence": 0}])
        assert out["refused"][0]["error"] == "legacy_unleased"


# ---------------------------------------------------------------------------
class TestOverviewAndWait:
    def test_overview_get_node_and_run_summary_expose_owner_and_lease(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        held = claim(grok, "a", "review")
        attempt = external(grok, "a")
        overview = read(grok, "project_overview", project_id="p")
        node = next(n for n in overview["nodes"] if n["node_key"] == "a")
        assert overview["policy"]["multi_driver"] == "on"
        assert [c["claim_id"] for c in node["claims"]] == [held["claim_id"]]
        assert node["claims"][0]["driver_id"] == "grok" and node["claims"][0]["lease_state"] == "healthy"
        latest = node["latest_attempt"]
        assert latest["owner_driver_id"] == "grok" and latest["lease_state"] == "healthy"
        assert latest["lease_expires_at"] == attempt["lease_expires_at"]
        context = read(grok, "get_node", project_id="p", node_key="a")
        assert context["claims"][0]["claim_id"] == held["claim_id"]
        assert context["attempts"][0]["owner_driver_id"] == "grok"
        assert context["attempts"][0]["lease_state"] == "healthy"
        summary = read(grok, "project_run_summary", project_id="p")
        running = summary["running_external"][0]
        assert running["owner_driver_id"] == "grok" and running["lease_state"] == "healthy"
        assert running["lease_expires_at"] == attempt["lease_expires_at"]
        assert summary["live_claims"][0]["claim_id"] == held["claim_id"]
        clock.advance(7200)
        overview = read(grok, "project_overview", project_id="p")
        node = next(n for n in overview["nodes"] if n["node_key"] == "a")
        assert node["readiness"] == "in_progress_lease_expired"
        assert node["latest_attempt"]["lease_state"] == "expired"
        assert overview["readiness_counts"]["in_progress_lease_expired"] == 1
        assert read(grok, "project_run_summary", project_id="p")["running_external"][0]["lease_state"] == "expired"

    def test_public_reads_show_the_same_claim_subset_to_every_reader(self, db, clock):
        owner = project(db)
        grok = svc(db, "grok")
        held = claim(grok, "a", "review", workspace="secret-host:/private/path#b", request_key="private-rk")
        owner.open_project("p")
        anonymous = svc(db, trusted=False)
        for action, args in (("project_overview", {"project_id": "p"}),
                             ("get_node", {"project_id": "p", "node_key": "a"}),
                             ("project_run_summary", {"project_id": "p"})):
            public, writer = read(anonymous, action, **args), read(grok, action, **args)
            public.pop("observed_at", None), writer.pop("observed_at", None)
            assert public == writer, action
            text = json.dumps(public)
            assert held["claim_id"] in text and "secret-host" not in text and "private-rk" not in text
        node = next(n for n in read(anonymous, "project_overview", project_id="p")["nodes"] if n["node_key"] == "a")
        assert node["claims"][0]["driver_id"] == "grok" and node["claims"][0]["lease_state"] == "healthy"
        before = max_seq(db)
        clock.advance(7200 + 900)
        assert read(grok, "project_overview", project_id="p")["nodes"][0]["claims"][0]["lease_state"] == \
            "reclaimable"
        assert max_seq(db) == before, "a public read never writes"
        from core.state_commands import ProjectPrivate
        for action in ("list_claims", "get_claim"):
            with pytest.raises(ProjectPrivate):
                read(anonymous, action, project_id="p", claim_id=held["claim_id"]) if action == "get_claim" \
                    else read(anonymous, action, project_id="p")

    async def test_wait_wakes_on_lease_expired(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        attempt = external(grok, "a")
        cursor = max_seq(db)

        async def expire_soon():
            await asyncio.sleep(0.3)
            clock.advance(7200)

        expiring = asyncio.create_task(expire_soon())
        out = await grok.wait_for_state_change("p", after=cursor, timeout_seconds=10)
        await expiring
        assert not out["timed_out"]
        assert [e["event_type"] for e in out["events"]] == ["lease_expired"]
        assert out["events"][0]["payload"]["attempt_id"] == attempt["attempt_id"]

    async def test_include_lease_events_false_filters_them(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        external(grok, "a")
        cursor = max_seq(db)
        clock.advance(7200)
        out = await grok.wait_for_state_change("p", after=cursor, timeout_seconds=0.2,
                                               include_lease_events=False)
        assert out["events"] == [] and out["timed_out"]
        assert events(db, "lease_expired"), "the sweep still records the event durably"
        again = await grok.wait_for_state_change("p", after=cursor, timeout_seconds=0)
        assert [e["event_type"] for e in again["events"]] == ["lease_expired"]

    async def test_idle_wait_reports_only_reclaimable_attempts_as_action_required(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        attempt = external(grok, "a")
        cursor = max_seq(db)
        out = await grok.wait_for_state_change("p", after=cursor, timeout_seconds=0, return_when_idle=True)
        assert out.get("reason") is None and out["timed_out"]
        clock.advance(7200 + 900)
        first = await grok.wait_for_state_change("p", after=cursor, timeout_seconds=0, return_when_idle=True)
        assert {e["payload"]["phase"] for e in first["events"]} == {"reclaimable"}
        out = await grok.wait_for_state_change("p", after=first["next_after"], timeout_seconds=0,
                                               return_when_idle=True)
        assert out["reason"] == "action_required"
        assert out["attempts"][0]["attempt_id"] == attempt["attempt_id"]
        assert out["attempts"][0]["lease_state"] == "reclaimable"
        assert out["attempts"][0]["owner_driver_id"] == "grok"
        quiet = await grok.wait_for_state_change("p", after=first["next_after"], timeout_seconds=0,
                                                 return_when_idle=True, include_lease_events=False)
        assert quiet.get("reason") is None


# ---------------------------------------------------------------------------
class TestRecordOnly:
    def test_flag_off_refuses_claims_and_records_no_lease(self, db, clock):
        project(db, multi_driver=False)
        grok = svc(db, "grok")
        assert code_of(lambda: claim(grok)) == "multi_driver_off"
        attempt = external(grok, "a")
        assert attempt["owner_driver_id"] is None and attempt["lease_expires_at"] is None
        assert attempt["lease_state"] == "legacy_unleased"
        registered = events(db, "external_attempt_registered")[-1]
        assert "owner_driver_id" not in registered and "lease_expires_at" not in registered
        clock.advance(10 * 86400)
        assert read(grok, "get_node", project_id="p", node_key="a")["node"]["readiness"] == "in_progress"
        assert events(db, "lease_expired") == []

    def test_claims_are_recorded_not_enforced(self, db):
        owner = project(db)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        claim(grok, "a")
        attempt = external(codex, "a")
        assert attempt["owner_driver_id"] == "codex"
        owner.store.revise_node("p", "b", 1, "structural write over a claim", goal="G2")
        claim(grok, "c")
        write(codex, "set_node_hold", project_id="p", node_key="c", held=True, expected_revision=0,
              reason="hold is unaffected by claims")

    def test_set_multi_driver_needs_admin_and_cas(self, db):
        owner = project(db, multi_driver=False)
        grok = svc(db, "grok")
        assert code_of(lambda: write(grok, "set_multi_driver", project_id="p", multi_driver="on",
                                     expected_revision=0, reason="r")) == "admin_required"
        with pytest.raises(StateGraphError):
            write(owner, "set_multi_driver", project_id="p", multi_driver="on", expected_revision=5, reason="r")
        policy = write(owner, "set_multi_driver", project_id="p", multi_driver="on", expected_revision=0,
                       reason="r")
        assert policy["multi_driver"] == "on" and policy["dispatch"] == "active" and policy["revision"] == 1
        held = write(owner, "set_dispatch", project_id="p", dispatch="hold", expected_revision=1, reason="h")
        assert held["multi_driver"] == "on", "set_dispatch keeps the multi_driver switch"


# ---------------------------------------------------------------------------
def _downgrade_to_pre_p1(path):
    """Rewrite a current database into the exact pre-P1 shape (base DDL)."""
    from core.state_attempt_schema import LEASE_COLUMNS
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP INDEX state_attempts_lease")
    for column, _ in LEASE_COLUMNS:
        conn.execute(f"ALTER TABLE state_attempts DROP COLUMN {column}")
    conn.executescript("""
        CREATE TABLE policy_old (
            project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
            dispatch TEXT NOT NULL CHECK(dispatch IN ('active','hold','archive')),
            reason TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES state_projects(project_id));
        INSERT INTO policy_old SELECT project_id,revision,dispatch,reason,actor,updated_at FROM state_project_policy;
        DROP TABLE state_project_policy;
        ALTER TABLE policy_old RENAME TO state_project_policy;
        DROP TABLE state_claim_history;
        DROP TABLE state_node_claims;
    """)
    conn.commit()
    conn.close()


def _snapshot(path):
    conn = sqlite3.connect(str(path))
    try:
        tables = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                    "AND name LIKE 'state_%' ORDER BY name"):
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({name})")]
            tables[name] = (columns, conn.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall())
        sequence = dict(conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
        return tables, sequence
    finally:
        conn.close()


class TestMigration:
    def test_pre_p1_database_migrates_additively_and_idempotently(self, db, clock):
        owner = project(db, multi_driver=False)
        write(owner, "set_dispatch", project_id="p", dispatch="hold", expected_revision=0, reason="h")
        write(owner, "set_dispatch", project_id="p", dispatch="active", expected_revision=1, reason="a")
        legacy = external(svc(db), "a")
        external(svc(db), "b", "rk-b")
        _downgrade_to_pre_p1(db)
        before, sequence = _snapshot(db)
        assert "owner_driver_id" not in before["state_attempts"][0]
        assert "state_node_claims" not in before

        migrated = svc(db, "grok")
        after, sequence_after = _snapshot(db)
        assert sequence_after == sequence
        for name, (columns, rows) in before.items():
            new_columns, new_rows = after[name]
            assert new_columns[:len(columns)] == columns, name
            assert [tuple(r[:len(columns)]) for r in new_rows] == [tuple(r) for r in rows], name
        assert after["state_attempts"][0][-4:] == ["owner_driver_id", "owner_fence",
                                                  "lease_expires_at", "last_heartbeat_at"]
        assert all(r[-4:] == (None, 0, None, None) for r in after["state_attempts"][1])
        assert after["state_project_policy"][0][-1] == "multi_driver"
        assert [r[-1] for r in after["state_project_policy"][1]] == ["off"]
        assert after["state_node_claims"][1] == [] and after["state_claim_history"][1] == []
        assert read(migrated, "get_attempt", attempt_id=legacy["attempt_id"])["lease_state"] == "legacy_unleased"
        conn = sqlite3.connect(str(db))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()

        svc(db, "codex")
        assert _snapshot(db) == (after, sequence_after), "a second start changes nothing"


# ---------------------------------------------------------------------------
class TestTransports:
    TOKEN = "s" * 40
    ADMIN = "owner-admin-token-" + "o" * 30

    @pytest.fixture
    def world(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path))
        monkeypatch.setenv(drivers.FEATURE_ENV, "on")
        monkeypatch.setenv("AITELIER_ADMIN_TOKEN", self.ADMIN)
        (tmp_path / drivers.PEPPER_SECRET_NAME).write_text("p" * 48)
        drivers._REGISTRIES.clear()
        from api.state_only import create_app
        path = str(tmp_path / "s.sqlite")
        app = create_app(path, self.TOKEN, with_mcp=True)
        registry = drivers.registry_for(StateDatabase(path))
        tokens = {"owner": self.ADMIN,
                  "grok": registry.register("grok", "Grok", actor="t")["token"],
                  "codex": registry.register("codex", "Codex", actor="t")["token"]}
        with TestClient(app) as client:
            yield client, tokens
        drivers._REGISTRIES.clear()

    @staticmethod
    def _post(client, token, path, body):
        return client.post(path, json=body, headers={"Authorization": "Bearer " + token})

    def test_rest_claim_heartbeat_list_and_release(self, world):
        client, tokens = world
        for action, body in (("create_project", {"project_id": "p", "title": "P"}),
                             ("add_nodes", {"project_id": "p", "nodes": [{"key": "a", **NODE}]}),
                             ("set_multi_driver", {"project_id": "p", "multi_driver": "on",
                                                   "expected_revision": 0, "reason": "r"})):
            response = self._post(client, tokens["owner"], "/api/state/commands/" + action, body)
            assert response.status_code == 200, response.text
        claim_body = {"project_id": "p", "node_key": "a", "purpose": "implement", "expected_revision": 1,
                      "request_key": "k"}
        won = self._post(client, tokens["grok"], "/api/state/commands/claim_node", claim_body)
        assert won.status_code == 200, won.text
        claim_id = won.json()["claim_id"]
        lost = self._post(client, tokens["codex"], "/api/state/commands/claim_node", claim_body)
        assert lost.status_code == 409 and lost.json()["detail"].startswith("claimed_by_other")
        beat = self._post(client, tokens["grok"], "/api/state/commands/heartbeat",
                          {"project_id": "p", "claims": [{"claim_id": claim_id, "fence": 1}]})
        assert beat.status_code == 200 and beat.json()["renewed"][0]["id"] == claim_id
        auth = {"Authorization": "Bearer " + tokens["codex"]}
        listed = client.get("/api/state/projects/p/claims", headers=auth)
        assert listed.status_code == 200 and listed.json()["claims"][0]["driver_id"] == "grok"
        one = client.get(f"/api/state/projects/p/claims/{claim_id}", headers=auth)
        assert one.status_code == 200 and one.json()["history"][0]["status"] == "live"
        query = self._post(client, tokens["codex"], "/api/state/query/list_claims",
                           {"project_id": "p", "driver_id": "grok"})
        assert query.status_code == 200 and len(query.json()["claims"]) == 1
        stale = self._post(client, tokens["grok"], "/api/state/commands/release_claim",
                           {"project_id": "p", "claim_id": claim_id, "fence": 7, "reason": "done"})
        assert stale.status_code == 409 and stale.json()["detail"].startswith("stale_fence")
        done = self._post(client, tokens["grok"], "/api/state/commands/release_claim",
                          {"project_id": "p", "claim_id": claim_id, "fence": 1, "reason": "done"})
        assert done.status_code == 200 and done.json()["status"] == "released"
        refused = self._post(client, tokens["grok"], "/api/state/commands/set_multi_driver",
                             {"project_id": "p", "multi_driver": "off", "expected_revision": 1, "reason": "r"})
        assert refused.status_code == 409 and refused.json()["detail"].startswith("admin_required")

    def test_mcp_claim_and_list_use_the_same_contract(self, world):
        client, tokens = world

        def call(token, tool, action, arguments):
            body = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": tool, "arguments": {"action": action, "arguments": arguments}}},
                headers={"Authorization": "Bearer " + token,
                         "Accept": "application/json, text/event-stream"}).json()["result"]
            return body, (None if body.get("isError") else json.loads(body["content"][0]["text"])["result"])

        call(tokens["owner"], "state_graph_write", "create_project", {"project_id": "p", "title": "P"})
        call(tokens["owner"], "state_graph_write", "add_nodes", {"project_id": "p", "nodes": [{"key": "a", **NODE}]})
        call(tokens["owner"], "state_graph_write", "set_multi_driver",
             {"project_id": "p", "multi_driver": "on", "expected_revision": 0, "reason": "r"})
        _, held = call(tokens["grok"], "state_graph_write", "claim_node",
                       {"project_id": "p", "node_key": "a", "purpose": "plan", "expected_revision": 1,
                        "request_key": "k"})
        assert held["driver_id"] == "grok" and held["purpose"] == "plan"
        raw, _ = call(tokens["codex"], "state_graph_write", "claim_node",
                      {"project_id": "p", "node_key": "a", "purpose": "implement", "expected_revision": 1,
                       "request_key": "k"})
        assert raw["isError"] is True and "claimed_by_other" in raw["content"][0]["text"]
        _, beat = call(tokens["grok"], "state_graph_write", "heartbeat",
                       {"project_id": "p", "claims": [{"claim_id": held["claim_id"], "fence": 1}]})
        assert beat["renewed"][0]["id"] == held["claim_id"]
        _, listed = call(tokens["codex"], "state_graph_read", "list_claims", {"project_id": "p"})
        assert listed["claims"][0]["claim_id"] == held["claim_id"]
        _, info = call(tokens["codex"], "state_graph_read", "get_claim",
                       {"project_id": "p", "claim_id": held["claim_id"]})
        assert info["claim"]["lease_state"] == "healthy"
        raw, _ = call(tokens["grok"], "state_graph_read", "claim_node", {"project_id": "p"})
        assert raw["isError"] is True
        _, released = call(tokens["grok"], "state_graph_write", "release_claim",
                           {"project_id": "p", "claim_id": held["claim_id"], "fence": 1, "reason": "done"})
        assert released["status"] == "released"


def test_product_transport_derives_admin_like_require_admin(monkeypatch):
    from api import state_graph_routers as routers
    identities = {
        "lan-admin": drivers.Identity("driver", "driver:owner-cli", "owner-cli", is_admin=True),
        "lan-plain": drivers.Identity("driver", "driver:grok", "grok"),
        "owner": drivers.Identity("owner", "owner:o@example.com", is_admin=True, email="o@example.com"),
    }
    for name, tunnel, expected in (("lan-admin", False, True), ("lan-admin", True, False),
                                   ("lan-plain", False, False), ("owner", True, True)):
        monkeypatch.setattr(routers, "request_identity", lambda request, n=name: identities[n])
        request = SimpleNamespace(headers={"Cf-Ray": "x"} if tunnel else {})
        assert routers.authenticated_is_admin(request) is expected, (name, tunnel)
