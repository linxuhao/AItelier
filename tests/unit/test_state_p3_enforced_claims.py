"""Multi-driver P3 (slim): enforced claims, reclaim, handoff, subagent record
(design/multi-driver-coop.md §4.4, §4.6, §6, §7.3).

One test class per acceptance item of State node
driver.multi-driver-p3-enforced-claims (P3_SLIM.md section d): migration and
reclaim, mutual exclusion (enforced dispatch, owner/fence), point-to-point
handoff, compatibility with enforcement off; plus the record-only subagent
registry, the transports and the status enumeration sweep.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from core import drivers, state_claims
from core.drivers import DriverRegistry
from core.state_claims import ClaimError
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService

NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}
LEASE, GRACE = 7200, 900


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


@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.sqlite"


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


def project(db, enforce=False):
    owner = svc(db, "owner-cli", admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": "a", **NODE}, {"key": "b", **NODE},
                                {"key": "c", **NODE, "dependencies": ["a"]}])
    if enforce:
        write(owner, "set_claim_enforcement", project_id="p", claim_enforcement="on", expected_revision=0,
              reason="P3 test")
    return owner


def claim(service, node="a", purpose="implement", request_key=None, **extra):
    return write(service, "claim_node", project_id="p", node_key=node, purpose=purpose, expected_revision=1,
                 request_key=request_key or f"{service.driver_id}-{node}-{purpose}", **extra)


def external(service, node="a", key="rk-1", **extra):
    return write(service, "start_external_attempt", project_id="p", node_key=node, expected_revision=1,
                 harness="h", external_id="job-" + key, request_key=key, **extra)


def report(service, attempt, status="running", fence=None, version=None, oid="o1"):
    args = dict(attempt_id=attempt["attempt_id"], observation_id=oid,
                expected_version=attempt["observation_version"] if version is None else version,
                context_hash=attempt["context_hash"], status=status, report_ref="/tmp/not-retained-for-running",
                report_sha256="0" * 64)
    if fence is not None:
        args["fence"] = fence
    return write(service, "report_external_attempt", **args)


def a_file(tmp_path, name, payload):
    path = tmp_path / name
    data = json.dumps(payload).encode()
    path.write_bytes(data)
    return str(path), hashlib.sha256(data).hexdigest()


def events(db, event_type):
    conn = sqlite3.connect(str(db))
    try:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM state_events WHERE event_type=? ORDER BY seq", (event_type,))]
    finally:
        conn.close()


def registered(db, *ids):
    """Put drivers in the P0 registry: only a registered driver has a P2 inbox."""
    registry = DriverRegistry(StateDatabase(str(db)), "p3-test-pepper-not-a-secret-padded-to-32-chars")
    for who in ids:
        registry.register(who, who.title(), actor="t")


def notices(db, driver, kind=None):
    """The P2 inbox messages delivered to ``driver`` (where every P3 notice goes)."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT m.*,d.status,d.seq FROM driver_inbox_messages m JOIN driver_inbox_deliveries d USING(message_id) "
            "WHERE d.target_driver_id=? ORDER BY d.seq", (driver,))]
    finally:
        conn.close()
    for row in rows:
        row["refs"] = json.loads(row.pop("refs_json"))
    return [r for r in rows if kind is None or r["kind"] == kind]


def register(service, attempt, label="w1", workspace=None):
    return write(service, "register_subagent", project_id="p", attempt_id=attempt["attempt_id"], label=label,
                 workspace=workspace or f"macbook:/w/{label}#{label}")


def reclaimable(clock):
    clock.advance(LEASE + GRACE)


# ---------------------------------------------------------------------------
# 1. abandoned-and-reclaim
# ---------------------------------------------------------------------------
def _downgrade_to_pre_p3(path):
    """Rewrite a current database into the P1-era shape: no `abandoned`, no
    abandon_kind, no observation fence columns, no P3 tables or policy column."""
    from core.state_attempt_schema import ATTEMPT_TABLE, OWNERS_TABLE
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA foreign_keys=OFF")

    def rebuild(table, ddl):
        custom = conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL "
                              "AND type IN ('index','trigger')", (table,)).fetchall()
        conn.execute(ddl.format(name="old_" + table))
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info(old_{table})")]
        conn.execute(f"INSERT INTO old_{table}({','.join(cols)}) SELECT {','.join(cols)} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE old_{table} RENAME TO {table}")
        for (sql,) in custom:
            conn.execute(sql)

    rebuild("state_attempts", ATTEMPT_TABLE.replace(",'abandoned'", "").replace(
        "    abandon_kind TEXT CHECK(abandon_kind IN ('confirmed_stopped','unknown')),\n", ""))
    rebuild("state_external_owners", OWNERS_TABLE.replace(",'abandoned'", ""))
    conn.execute("CREATE INDEX custom_pre_p3_index ON state_attempts(harness)")
    conn.execute("UPDATE sqlite_sequence SET seq=999 WHERE name='state_attempts'")
    conn.execute("ALTER TABLE state_external_observations DROP COLUMN late_after_abandon")
    conn.execute("ALTER TABLE state_external_observations DROP COLUMN fence")
    conn.executescript("DROP TABLE state_handoffs; DROP TABLE driver_subagents; DROP TABLE state_project_enforcement;")
    conn.commit()
    conn.close()


def _rows(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {}
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                    "ORDER BY name"):
            tables[name] = [dict(r) for r in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]
        schema = {r["name"]: r["sql"] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL")}
        sequence = dict(conn.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
        return tables, schema, sequence
    finally:
        conn.close()


class TestAbandonedAndReclaim:
    def test_rebuild_keeps_rows_watermark_indexes_triggers_and_is_idempotent(self, db, clock):
        project(db)
        grok = svc(db, "grok")
        first = external(grok, "a")
        report(grok, first, "running")
        external(grok, "b", "rk-b")
        _downgrade_to_pre_p3(db)
        before, schema_before, seq_before = _rows(db)
        assert "'abandoned'" not in schema_before["state_attempts"]
        assert "state_handoffs" not in before and "abandon_kind" not in before["state_attempts"][0]

        svc(db, "codex")                       # migration runs on construction
        after, schema_after, seq_after = _rows(db)
        for name, rows in before.items():
            assert len(after[name]) == len(rows), name
            for old, new in zip(rows, after[name]):
                assert all(new[k] == v for k, v in old.items()), (name, old, new)
        assert "'abandoned'" in schema_after["state_attempts"] and "'abandoned'" in schema_after["state_external_owners"]
        assert "abandon_kind" in after["state_attempts"][0] and after["state_attempts"][0]["abandon_kind"] is None
        for kept in ("state_attempts_one_active", "state_attempts_node", "state_attempts_lease",
                     "state_attempt_external_identity", "state_attempt_executor_immutable", "custom_pre_p3_index",
                     "state_external_owners_status", "state_external_owners_no_delete"):
            assert kept in schema_after, kept
        assert seq_after["state_attempts"] == seq_before["state_attempts"] == 999
        obs = after["state_external_observations"][0]
        assert obs["late_after_abandon"] == 0 and obs["fence"] is None
        assert {"state_handoffs", "driver_subagents", "state_project_enforcement"} <= set(after)
        conn = sqlite3.connect(str(db))
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert [r[1] for r in conn.execute("PRAGMA table_info(state_project_policy)")][-1] == "multi_driver"
        conn.close()
        fresh = svc(db, "codex")
        fresh.store.add_nodes("p", [{"key": "d", **NODE}])
        assert external(fresh, "d", "rk-d")["seq"] == 1000, "the seq high-water mark survived"

        twice = _rows(db)
        svc(db, "grok")
        assert _rows(db) == twice, "a second start changes nothing"

    def test_damaged_table_fails_the_rebuild_and_keeps_the_old_rows(self, db, clock):
        project(db)
        external(svc(db, "grok"), "a")
        _downgrade_to_pre_p3(db)
        conn = sqlite3.connect(str(db))
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute("UPDATE state_attempts SET status='damaged'")
        conn.commit()
        conn.close()
        with pytest.raises(sqlite3.IntegrityError):
            svc(db, "codex")
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT status FROM state_attempts").fetchone()[0] == "damaged"
        assert "'abandoned'" not in conn.execute("SELECT sql FROM sqlite_master WHERE name='state_attempts'").fetchone()[0]
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='state_attempts_rebuild'").fetchone()
        conn.close()

    def test_tables_of_removed_mechanisms_are_dropped_only_while_empty(self, db, clock):
        project(db)
        conn = sqlite3.connect(str(db))
        # The pre-slimming P3 shapes (deployed 2026-10-10), all empty, plus one
        # retired table that holds a row.
        conn.executescript("""
            DROP TABLE driver_subagents;
            CREATE TABLE driver_subagents (subagent_id TEXT PRIMARY KEY, owner_driver_id TEXT NOT NULL,
                context_ref TEXT NOT NULL, context_sha256 TEXT NOT NULL);
            CREATE INDEX driver_subagents_attempt ON driver_subagents(owner_driver_id);
            CREATE TABLE state_subagent_contexts (context_sha256 TEXT PRIMARY KEY, context_bytes BLOB NOT NULL);
            CREATE TABLE driver_checkpoint_decisions (decision_id TEXT PRIMARY KEY);
            CREATE TABLE driver_notices (notice_id TEXT PRIMARY KEY);
            INSERT INTO driver_notices VALUES ('kept');
        """)
        conn.commit()
        conn.close()
        svc(db, "codex")                       # initialization runs on construction
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"state_subagent_contexts", "driver_checkpoint_decisions"} & tables
        assert conn.execute("SELECT notice_id FROM driver_notices").fetchall() == [("kept",)]
        assert "parent_driver_id" in {r[1] for r in conn.execute("PRAGMA table_info(driver_subagents)")}
        conn.close()
        # An old-shape registry holding rows is renamed aside, never dropped.
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            DROP TABLE driver_subagents;
            CREATE TABLE driver_subagents (subagent_id TEXT PRIMARY KEY, owner_driver_id TEXT NOT NULL,
                context_ref TEXT NOT NULL, context_sha256 TEXT NOT NULL);
            INSERT INTO driver_subagents VALUES ('codex/w1','codex','/ctx','0');
        """)
        conn.commit()
        conn.close()
        svc(db, "codex")
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT subagent_id FROM driver_subagents_p3_takeover").fetchall() == [("codex/w1",)]
        assert "parent_driver_id" in {r[1] for r in conn.execute("PRAGMA table_info(driver_subagents)")}
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        conn.close()
        before = _rows(db)
        svc(db, "grok")
        assert _rows(db) == before, "a second start changes nothing"

    def test_abandon_refused_until_grace_then_allowed_at_once(self, db, clock):
        project(db)
        registered(db, "grok", "codex")
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(grok, workspace="h:/w#g")
        attempt = external(grok, "a", claim_id=held["claim_id"], fence=1)   # bound explicitly (record-only project)
        assert attempt["owner_driver_id"] == "grok" and attempt["owner_fence"] == 1

        def abandon(**extra):
            return write(codex, "abandon_external_attempt", attempt_id=attempt["attempt_id"],
                         expected_owner_fence=1, abandon_kind="unknown", reason="no heartbeat", **extra)

        assert code_of(abandon) == "lease_not_expired"
        clock.advance(LEASE + GRACE - 1)
        assert code_of(abandon) == "lease_not_expired"
        clock.advance(1)
        assert code_of(lambda: write(codex, "abandon_external_attempt", attempt_id=attempt["attempt_id"],
                                     expected_owner_fence=7, abandon_kind="unknown", reason="r")) == "stale_fence"
        out = abandon()
        assert out["status"] == "abandoned" and out["owner_fence"] == 2 and out["abandon_kind"] == "unknown"
        assert out["lease_state"] is None and out["break_glass"] is False
        assert events(db, "attempt_abandoned")[-1]["previous_owner_driver_id"] == "grok"
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT status FROM state_external_owners WHERE attempt_id=?",
                            (attempt["attempt_id"],)).fetchone()[0] == "abandoned"
        conn.close()
        # The bound claim was released with the attempt.
        assert read(grok, "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == "released"
        # The previous owner is told in its P2 driver inbox (a system notice: no sender).
        mine = notices(db, "grok", "takeover_notice")
        assert len(mine) == 1 and mine[0]["sender_driver_id"] is None and mine[0]["status"] == "unread"
        assert mine[0]["refs"] == {"attempt_id": attempt["attempt_id"], "node_key": "a"} and "fence is now 2" in mine[0]["body"]
        assert notices(db, "codex") == []
        # Twice: nothing to reclaim.
        assert code_of(abandon) == "attempt_not_active"

    def test_abandoned_is_consistent_in_readiness_frontier_wait_and_run_summary(self, db, clock):
        project(db)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        attempt = external(grok, "a")
        overview = read(codex, "project_overview", project_id="p")
        assert {n["node_key"]: n["readiness"] for n in overview["nodes"]}["a"] == "in_progress"
        reclaimable(clock)
        assert {n["node_key"]: n["readiness"] for n in read(codex, "project_overview", project_id="p")["nodes"]}["a"] \
            == "in_progress_lease_expired"
        write(codex, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
              abandon_kind="unknown", reason="gone")
        overview = read(codex, "project_overview", project_id="p")
        by_key = {n["node_key"]: n for n in overview["nodes"]}
        assert by_key["a"]["readiness"] == "ready" and by_key["a"]["latest_attempt"]["status"] == "abandoned"
        assert by_key["a"]["latest_attempt"]["lease_state"] is None
        assert overview["readiness_counts"] == {"ready": 2, "blocked": 1}
        assert "a" in [n["node_key"] for n in read(codex, "frontier", project_id="p")["nodes"]]
        assert read(codex, "get_attempt", attempt_id=attempt["attempt_id"])["lease_state"] is None
        summary = read(codex, "project_run_summary", project_id="p")
        assert summary["abandoned_external"] == 1 and summary["external_counts"]["active"] == 0
        assert summary["running_external"] == [] and summary["execution_counts"]["other"] == 1
        idle = asyncio.run(codex.wait_for_state_change("p", after=overview["event_seq"], timeout_seconds=0,
                                                       return_when_idle=True))
        assert idle["reason"] == "nothing_to_wait", idle
        # Abandoned frees the slot: a new attempt is admitted.
        assert external(codex, "a", "rk-2", base_sha=None)["status"] == "running"

    def test_late_observation_from_the_old_owner_is_recorded_as_superseded_never_current(self, db, clock):
        project(db)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        attempt = external(grok, "a")
        reclaimable(clock)
        write(codex, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
              abandon_kind="unknown", reason="gone")
        late = report(grok, attempt, "running", fence=1)
        assert late["late_after_abandon"] is True and late["status"] == "abandoned"
        assert late["observation"]["late_after_abandon"] == 1 and late["observation"]["resulting_status"] == "superseded"
        assert late["observation"]["status"] == "running" and late["observation"]["fence"] == 1
        node = read(codex, "get_node", project_id="p", node_key="a")["node"]
        assert node["status"] == "OPEN" and node["readiness"] == "ready"
        assert events(db, "external_attempt_observed")[-1]["late_after_abandon"] is True
        # Someone else is not the reporter.
        with pytest.raises(StateConflict):
            report(codex, {**attempt, "observation_version": 1}, "running", fence=2, oid="o2")

    def test_confirmed_stopped_needs_a_report(self, db, clock, tmp_path):
        project(db)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        attempt = external(grok, "a")
        reclaimable(clock)
        with pytest.raises(StateGraphError):
            write(codex, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
                  abandon_kind="confirmed_stopped", reason="r")
        ref, sha = a_file(tmp_path, "quiescence.json", {"workers": "exited", "worktree": "settled"})
        out = write(codex, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
                    abandon_kind="confirmed_stopped", reason="codex confirmed", report_ref=ref, report_sha256=sha)
        assert out["abandon_kind"] == "confirmed_stopped" and out["owner_fence"] == 2
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT 1 FROM state_external_report_blobs WHERE report_sha256=?", (sha,)).fetchone()
        conn.close()

    def test_take_over_moves_owner_fence_claim_and_refuses_the_old_owner(self, db, clock):
        project(db, enforce=True)
        registered(db, "grok", "codex")
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(grok, workspace="h:/w#g")
        attempt = external(grok, "a", claim_id=held["claim_id"], fence=1)
        take = lambda **e: write(codex, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
                                 reason="grok vanished", **e)
        assert code_of(take) == "lease_not_expired"
        reclaimable(clock)
        out = take()
        assert out["owner_driver_id"] == "codex" and out["owner_fence"] == 2 and out["lease_state"] == "healthy"
        assert out["claim"]["driver_id"] == "codex" and out["claim"]["status"] == "live" and out["claim"]["fence"] == 2
        assert read(codex, "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == "transferred"
        assert events(db, "attempt_ownership_transferred")[-1]["mode"] == "takeover"
        assert "taken over by codex" in notices(db, "grok", "takeover_notice")[0]["subject"]
        # The old owner is fenced out everywhere.
        beat = write(grok, "heartbeat", project_id="p", attempts=[{"attempt_id": attempt["attempt_id"], "fence": 1}])
        assert beat["refused"][0]["error"] == "not_attempt_owner"
        assert code_of(lambda: report(grok, attempt, "running", fence=1)) == "stale_fence"
        assert code_of(lambda: report(grok, attempt, "running", fence=2)) == "stale_fence"
        assert code_of(lambda: report(codex, attempt, "running")) == "fence_required"
        assert code_of(lambda: report(codex, attempt, "running", fence=1)) == "stale_fence"
        assert report(codex, attempt, "running", fence=2)["observation"]["fence"] == 2
        assert code_of(lambda: write(codex, "take_over_attempt", attempt_id=attempt["attempt_id"],
                                     expected_owner_fence=2, reason="again")) == "already_owner"

    def test_legacy_unleased_attempt_needs_override_reason_and_admin_is_break_glass(self, db, clock):
        project(db)
        legacy = external(svc(db), "a")             # no registered driver behind it: unleased
        assert legacy["owner_driver_id"] is None and legacy["lease_state"] == "legacy_unleased"
        owner = svc(db, "owner-cli", admin=True)
        codex = svc(db, "codex")
        assert code_of(lambda: write(codex, "take_over_attempt", attempt_id=legacy["attempt_id"],
                                     expected_owner_fence=0, reason="AMI")) == "override_reason_required"
        out = write(codex, "take_over_attempt", attempt_id=legacy["attempt_id"], expected_owner_fence=0, reason="AMI",
                    override_reason="owner confirmed 2026-10-09")
        assert out["owner_driver_id"] == "codex" and out["owner_fence"] == 1 and out["break_glass"] is False
        # An admin reclaims a HEALTHY lease: allowed and flagged; the owner gets the
        # ordinary reclaim notice (break_glass is an audit flag, not a notice kind).
        registered(db, "grok")
        grok = svc(db, "grok")
        second = external(grok, "b", "rk-b")
        out = write(owner, "abandon_external_attempt", attempt_id=second["attempt_id"], expected_owner_fence=1,
                    abandon_kind="unknown", reason="owner decision")
        assert out["break_glass"] is True and events(db, "attempt_abandoned")[-1]["break_glass"] is True
        assert [n["refs"]["attempt_id"] for n in notices(db, "grok")] == [second["attempt_id"]]
        assert notices(db, "grok")[0]["kind"] == "takeover_notice"

    def test_abandon_unknown_binds_the_next_attempt_to_a_base_and_another_workspace(self, db, clock):
        project(db, enforce=True)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(grok, workspace="h:/w#g")
        attempt = external(grok, "a", claim_id=held["claim_id"], fence=1)
        reclaimable(clock)
        write(codex, "abandon_external_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
              abandon_kind="unknown", reason="gone")
        # The old worker may still write h:/w: the node's next attempt cannot ride a
        # claim on that checkout (another branch of it is the same writer) ...
        same = claim(codex, workspace="h:/w#g2")
        assert code_of(lambda: external(codex, "a", "rk-2", claim_id=same["claim_id"], fence=same["fence"],
                                        base_sha="a" * 40)) == "workspace_in_use"
        write(codex, "release_claim", project_id="p", claim_id=same["claim_id"], fence=same["fence"], reason="moving")
        # ... and must declare its base.
        other = claim(codex, workspace="h:/w2#c", request_key="codex-a-2")
        assert code_of(lambda: external(codex, "a", "rk-3", claim_id=other["claim_id"], fence=other["fence"])
                       ) == "workspace_in_use"       # still no base_sha
        with pytest.raises(StateConflict):          # base_sha is verified against the bound source; none here
            external(codex, "a", "rk-4", claim_id=other["claim_id"], fence=other["fence"], base_sha="a" * 40)

    def test_skillflow_attempt_cannot_be_abandoned_only_taken_over(self, db, clock):
        project(db)
        codex = svc(db, "codex")
        _insert_skillflow_attempt(db, "codex")
        with pytest.raises(ClaimError) as caught:
            write(codex, "abandon_external_attempt", attempt_id="attempt-sf", expected_owner_fence=1,
                  abandon_kind="unknown", reason="r")
        assert caught.value.code == "not_external"


def _insert_skillflow_attempt(db, owner, run_id="run-1", node="b"):
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
                 "dependency_snapshot,context_json,request_key,request_hash,workflow,execution_project_id,run_id,"
                 "status,created_at,updated_at,execution_kind,owner_driver_id,owner_fence,lease_expires_at,"
                 "last_heartbeat_at) VALUES('attempt-sf','p',?,1,'h','{}','{}','rk','rh','w','sg-x',?,'paused','t','t',"
                 "'skillflow',?,1,'2999-01-01T00:00:00.000000+00:00','2026-01-01T00:00:00.000000+00:00')",
                 (node, run_id, owner))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 2. enforced-dispatch
# ---------------------------------------------------------------------------
class TestEnforcedDispatch:
    def test_recorded_claims_alone_do_not_enforce(self, db, clock):
        project(db)                                  # enforcement off (the default)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        claim(codex, "a")                            # somebody else's claim
        attempt = external(grok, "a")                # still admitted, as in P1
        assert attempt["owner_driver_id"] == "grok"
        assert report(grok, attempt, "running")["observation"]["fence"] is None
        assert write(codex, "revise_node", project_id="p", node_key="b", expected_revision=1, reason="r")["revision"] == 2
        policy = read(grok, "project_overview", project_id="p")["policy"]
        assert policy["multi_driver"] == "on" and policy["claim_enforcement"] == "off"

    def test_non_driver_callers_are_untouched_and_enforcement_needs_only_admin(self, db, clock):
        project(db)
        plain = svc(db)
        attempt = external(plain, "a")
        assert attempt["owner_driver_id"] is None and "claim_id" not in events(db, "external_attempt_registered")[-1]
        assert report(plain, attempt, "running")["observation"]["fence"] is None
        codex = svc(db, "codex")
        assert code_of(lambda: write(codex, "set_claim_enforcement", project_id="p", claim_enforcement="on",
                                     expected_revision=0, reason="r")) == "admin_required"
        owner = svc(db, "owner-cli", admin=True)
        policy = write(owner, "set_claim_enforcement", project_id="p", claim_enforcement="on",
                       expected_revision=0, reason="r")
        assert policy["claim_enforcement"] == "on" and policy["multi_driver"] == "on"

    def test_the_legacy_multi_driver_column_is_ignored(self, db, clock):
        owner = project(db, enforce=True)
        conn = sqlite3.connect(str(db))     # a stale legacy value: multi_driver off
        conn.execute("UPDATE state_project_policy SET multi_driver='off'")
        conn.commit()
        conn.close()
        policy = read(owner, "project_overview", project_id="p")["policy"]
        assert policy["multi_driver"] == "on" and policy["claim_enforcement"] == "on"
        from core.state_enforcement import enforced
        with owner.store.transaction() as conn:
            assert enforced(conn, "p") is True
        assert code_of(lambda: external(svc(db, "grok"), "a")) == "claim_required"

    def test_dispatch_needs_your_live_implement_claim(self, db, clock):
        project(db, enforce=True)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        assert code_of(lambda: external(grok, "a")) == "claim_required"
        held = claim(codex, "a", workspace="h:/w#c")
        assert code_of(lambda: external(grok, "a")) == "claimed_by_other"
        assert claim(grok, "a", "review")["status"] == "live"               # review coexists, grants nothing
        assert code_of(lambda: external(grok, "a")) == "claimed_by_other"
        assert code_of(lambda: external(codex, "a", claim_id=held["claim_id"], fence=9)) == "stale_fence"
        assert code_of(lambda: external(codex, "a", claim_id="claim-nope", fence=1)) == "stale_fence"
        attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
        assert attempt["owner_driver_id"] == "codex"
        bound = read(codex, "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]
        assert bound["attempt_id"] == attempt["attempt_id"]
        assert events(db, "external_attempt_registered")[-1]["claim_id"] == held["claim_id"]
        # A claim without the explicit ids is enough: the live implement claim is yours.
        claim(codex, "b", workspace="h:/w2#c")
        assert external(codex, "b", "rk-b")["owner_driver_id"] == "codex"

    def test_report_needs_owner_and_fence(self, db, clock):
        project(db, enforce=True)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(codex, "a")
        attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
        assert code_of(lambda: report(codex, attempt, "running")) == "fence_required"
        assert code_of(lambda: report(codex, attempt, "running", fence=2)) == "stale_fence"
        assert code_of(lambda: report(grok, attempt, "running", fence=1)) == "not_attempt_owner"
        out = report(codex, attempt, "running", fence=1)
        assert out["observation"]["fence"] == 1 and out["observation"]["late_after_abandon"] == 0

    def test_structural_write_over_another_drivers_work_is_refused(self, db, clock):
        project(db, enforce=True)
        registered(db, "grok", "codex")
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(codex, "a", workspace="h:/w#c")
        revise = lambda who=grok, **e: write(who, "revise_node", project_id="p", node_key="a", expected_revision=1,
                                             reason="tighten", **e)
        assert code_of(revise) == "not_owner"
        assert read(codex, "get_node", project_id="p", node_key="a")["node"]["revision"] == 1
        with pytest.raises(StateGraphError):            # the override path is gone
            revise(override_reason="contract defect found in review")
        # c depends on a: a revision of a invalidates c, so a claim on c counts too.
        on_c = claim(codex, "c", "plan", workspace="h:/w3#c")
        write(codex, "release_claim", project_id="p", claim_id=held["claim_id"], fence=held["fence"], reason="done")
        assert code_of(revise) == "not_owner"
        for action, extra in (("split_node", {"expected_revision": 1, "children": [{"key": "a1", **NODE}],
                                              "reason": "r"}),
                              ("supersede_node", {"expected_revision": 1, "reason": "r"}),
                              ("set_node_facet", {"facet": "contract"})):
            assert code_of(lambda: write(grok, action, project_id="p", node_key="a", **extra)) == "not_owner"
        # The holder itself, and a node nobody holds, are never refused.
        assert write(grok, "supersede_node", project_id="p", node_key="b", expected_revision=1, reason="r")["status"] \
            == "SUPERSEDED"
        assert write(codex, "set_node_facet", project_id="p", node_key="a", facet="contract")["changed"] is True
        assert revise(codex)["revision"] == 2
        # An admin passes: the write is recorded break_glass, nobody is notified.
        owner = svc(db, "owner-cli", admin=True)
        write(owner, "revise_node", project_id="p", node_key="a", expected_revision=2, reason="owner edit")
        overridden = events(db, "claim_overridden")[-1]
        assert overridden["action"] == "revise_node" and overridden["break_glass"] is True
        assert [h["id"] for h in overridden["held"]] == [on_c["claim_id"]]
        assert notices(db, "codex") == []
        # Enforcement off: the same write over a held node proceeds, as in P1.
        write(owner, "set_claim_enforcement", project_id="p", claim_enforcement="off", expected_revision=1, reason="off")
        assert write(grok, "revise_node", project_id="p", node_key="a", expected_revision=3, reason="r")["revision"] == 4

    def test_releasing_another_drivers_hold_needs_override(self, db, clock):
        project(db, enforce=True)
        registered(db, "grok", "codex")
        grok, codex = svc(db, "grok"), svc(db, "codex")
        write(codex, "set_node_hold", project_id="p", node_key="a", held=True, expected_revision=0, reason="wait")
        assert code_of(lambda: write(grok, "set_node_hold", project_id="p", node_key="a", held=False,
                                     expected_revision=1, reason="go")) == "override_reason_required"
        out = write(grok, "set_node_hold", project_id="p", node_key="a", held=False, expected_revision=1, reason="go",
                    override_reason="owner asked")
        assert out["held"] == 0
        told = notices(db, "codex", "lease_notice")
        assert len(told) == 1 and "override_reason: owner asked" in told[0]["body"] and told[0]["refs"] == {"node_key": "a"}
        assert events(db, "claim_overridden")[-1]["action"] == "set_node_hold"
        # Your own hold, or the hold of a project with enforcement off, needs nothing.
        write(grok, "set_node_hold", project_id="p", node_key="a", held=True, expected_revision=2, reason="w")
        assert write(grok, "set_node_hold", project_id="p", node_key="a", held=False, expected_revision=3,
                     reason="g")["held"] == 0

    def test_admin_dispatch_bypasses_claims_as_break_glass(self, db, clock):
        project(db, enforce=True)
        codex, owner = svc(db, "codex"), svc(db, "owner-cli", admin=True)
        held = claim(codex, "a")
        attempt = external(owner, "a")
        assert attempt["owner_driver_id"] == "owner-cli"
        assert events(db, "external_attempt_registered")[-1]["break_glass"] is True
        assert read(codex, "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == "live"

    def test_checkpoint_is_decided_by_the_attempt_owner(self, db, clock):
        project(db, enforce=True)
        _insert_skillflow_attempt(db, "codex")
        from core.state_enforcement import checkpoint_controller
        database = StateDatabase(str(db))
        owner = checkpoint_controller(database, "run-1", "codex", False, "driver:codex")
        assert owner["break_glass"] is False and owner["owner_fence"] == 1
        with pytest.raises(ClaimError) as caught:
            checkpoint_controller(database, "run-1", "grok", False, "driver:grok")
        assert caught.value.code == "not_attempt_owner"
        assert checkpoint_controller(database, "run-unbound", "grok", False, "driver:grok") == {"enforced": False}
        glass = checkpoint_controller(database, "run-1", "owner-cli", True, "driver:owner-cli")
        assert glass["break_glass"] is True
        assert events(db, "checkpoint_break_glass")[-1]["attempt_id"] == "attempt-sf"
        # One check at entry: nothing is held open between the check and the engine call.
        conn = sqlite3.connect(str(db))
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='driver_checkpoint_decisions'").fetchone()
        conn.close()
        # The REST door calls the same function and maps the refusal to 409.
        from api.meta_routers import state_checkpoint_controller
        from fastapi import HTTPException
        from types import SimpleNamespace
        from api import state_graph_routers as routers
        import pytest as _pytest
        monkey = _pytest.MonkeyPatch()
        monkey.setattr(routers, "request_identity",
                       lambda request: drivers.Identity("driver", "driver:grok", "grok"))
        monkey.setattr(routers, "request_actor", lambda request: "driver:grok")
        try:
            with pytest.raises(HTTPException) as refused:
                state_checkpoint_controller(SimpleNamespace(headers={}), "run-1", database)
            assert refused.value.status_code == 409 and "not_attempt_owner" in refused.value.detail
        finally:
            monkey.undo()

    def test_not_enforced_project_does_not_constrain_checkpoints(self, db, clock):
        project(db)
        _insert_skillflow_attempt(db, "codex")
        from core.state_enforcement import checkpoint_controller
        assert checkpoint_controller(StateDatabase(str(db)), "run-1", "grok", False, "driver:grok")["enforced"] is False


# ---------------------------------------------------------------------------
# 3. handoff
# ---------------------------------------------------------------------------
class TestHandoff:
    def offer(self, service, attempt, to="grok", package=None, key="h1", fence=1):
        return write(service, "offer_handoff", project_id="p", request_key=key, expected_owner_fence=fence,
                     attempt_id=attempt["attempt_id"], to_driver_id=to,
                     package=package if package is not None else {"next_step": "finish C1 cleanup",
                                                                  "context_hash": attempt["context_hash"],
                                                                  "observation_version": attempt["observation_version"],
                                                                  "workers": {"quiescent": True}})

    def test_offer_and_accept_move_attempt_and_claim_atomically(self, db, clock):
        project(db, enforce=True)
        registered(db, "grok", "codex")
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(codex, "a", workspace="h:/w#c")
        attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
        offer = self.offer(codex, attempt, package={"next_step": "finish", "event_cursor": 0,
                                                   "workers": {"quiescent": True}})
        assert offer["status"] == "offered" and offer["to_driver_id"] == "grok"
        told = notices(db, "grok", "handoff_offer")
        assert len(told) == 1 and told[0]["sender_driver_id"] == "codex" and offer["handoff_id"] in told[0]["body"]
        assert told[0]["refs"]["handoff_id"] == offer["handoff_id"]
        assert read(grok, "get_handoff", project_id="p", handoff_id=offer["handoff_id"])["handoff"]["package"]["next_step"] == "finish"
        # Wrong fence, wrong driver, offerer itself.
        assert code_of(lambda: write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"],
                                     expected_owner_fence=2)) == "stale_fence"
        assert code_of(lambda: write(svc(db, "third"), "accept_handoff", project_id="p",
                                     handoff_id=offer["handoff_id"], expected_owner_fence=1)) == "not_handoff_target"
        assert code_of(lambda: write(codex, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"],
                                     expected_owner_fence=1)) == "not_handoff_target"
        assert read(grok, "get_attempt", attempt_id=attempt["attempt_id"])["owner_driver_id"] == "codex"
        out = write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=1)
        assert out["status"] == "accepted" and out["attempt"]["owner_driver_id"] == "grok" and out["attempt"]["owner_fence"] == 2
        assert out["claim"]["driver_id"] == "grok" and out["claim"]["fence"] == 2 and out["claim"]["workspace"] == "h:/w#c"
        assert out["quiescence_warning"] is None
        assert read(grok, "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == "transferred"
        assert events(db, "attempt_ownership_transferred")[-1]["mode"] == "handoff"
        reply = notices(db, "codex", "handoff_reply")
        assert len(reply) == 1 and reply[0]["sender_driver_id"] == "grok" and "accepted" in reply[0]["subject"]
        # The receiver now reports with its fence; the offerer is stale.
        assert report(grok, attempt, "running", fence=2)["observation"]["fence"] == 2
        assert code_of(lambda: report(codex, attempt, "running", fence=1, oid="o2")) == "stale_fence"
        assert code_of(lambda: report(codex, attempt, "running", fence=2, oid="o3")) == "stale_fence"

    def test_accept_with_workers_not_declared_quiescent_warns_and_records(self, db, clock):
        project(db)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        attempt = external(codex, "a")
        register(codex, attempt, "w1")
        offer = self.offer(codex, attempt, package={"workers": {"quiescent": False, "detail": "codex thread running"},
                                                   "next_step": "x"})
        out = write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=1)
        assert out["attempt"]["owner_driver_id"] == "grok", "a warning, never a refusal"
        warning = out["quiescence_warning"]
        assert warning["workers_quiescent"] is False and warning["registered_subagents"] == ["codex/w1"]
        assert events(db, "attempt_ownership_transferred")[-1]["quiescence_warning"] == warning
        # An undeclared quiescence warns the same way.
        second = external(codex, "b", "rk-b")
        undeclared = self.offer(codex, second, package={"next_step": "y"}, key="h2")
        out = write(grok, "accept_handoff", project_id="p", handoff_id=undeclared["handoff_id"], expected_owner_fence=1)
        assert out["quiescence_warning"]["workers_quiescent"] is None

    def test_package_is_bounded_checked_and_never_in_the_notebook(self, db, clock):
        project(db)
        codex = svc(db, "codex")
        attempt = external(codex, "a")
        with pytest.raises(StateGraphError):
            self.offer(codex, attempt, package={"next_step": "x" * 2001, "workers": {"quiescent": True}})
        with pytest.raises(StateGraphError):
            self.offer(codex, attempt, package={"next_step": "x", "workers": {"quiescent": True},
                                                "reports": [{"ref": "/r", "sha256": "0" * 64}] * 21})
        with pytest.raises(StateGraphError):
            self.offer(codex, attempt, package={"next_step": "x", "workers": {"quiescent": True},
                                                "source": {"blob": "y" * 17000}})
        with pytest.raises(StateGraphError):
            self.offer(codex, attempt, package={"transcript": "no"})
        quiet = {"workers": {"quiescent": True}}
        assert code_of(lambda: self.offer(codex, attempt, package={"context_hash": "0" * 64, **quiet})) == "package_mismatch"
        assert code_of(lambda: self.offer(codex, attempt, package={"observation_version": 5, **quiet})) == "package_mismatch"
        assert code_of(lambda: self.offer(codex, attempt, package={"event_cursor": 10 ** 6, **quiet})) == "package_mismatch"
        offer = self.offer(codex, attempt, package={"next_step": "where I am", "note_entries": ["note://p/abc123def456"],
                                                   "private_notes": ["dnote://codex/abc123def456"], **quiet})
        assert read(codex, "get_driver_note", project_id="p")["entry_count"] == 0
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM state_driver_note_entries").fetchone()[0] == 0
        assert conn.execute("SELECT package_json FROM state_handoffs WHERE handoff_id=?",
                            (offer["handoff_id"],)).fetchone()[0].startswith("{")
        conn.close()
        # Idempotent by request_key; a changed offer under the same key is refused.
        assert self.offer(codex, attempt, package={"next_step": "where I am", "note_entries": ["note://p/abc123def456"],
                                                   "private_notes": ["dnote://codex/abc123def456"], **quiet})["idempotent"] is True
        assert code_of(lambda: self.offer(codex, attempt, package={"next_step": "other", **quiet})) == "request_key_reused"
        assert code_of(lambda: self.offer(codex, attempt, key="h2")) == "handoff_pending"

    def test_decline_withdraw_expiry_and_stale_offers(self, db, clock):
        project(db)
        registered(db, "grok", "codex", "third")
        grok, codex, third = svc(db, "grok"), svc(db, "codex"), svc(db, "third")
        attempt = external(codex, "a")
        offer = self.offer(codex, attempt)
        assert code_of(lambda: write(grok, "withdraw_handoff", project_id="p", handoff_id=offer["handoff_id"])) \
            == "not_handoff_owner"
        declined = write(grok, "decline_handoff", project_id="p", handoff_id=offer["handoff_id"], reason="busy")
        assert declined["status"] == "declined" and notices(db, "codex", "handoff_reply")[0]["body"] == "busy"
        assert read(codex, "get_attempt", attempt_id=attempt["attempt_id"])["owner_fence"] == 1
        assert code_of(lambda: write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"],
                                     expected_owner_fence=1)) == "handoff_closed"
        # Point to point only: an offer names its receiver.
        with pytest.raises(StateGraphError):
            write(codex, "offer_handoff", project_id="p", request_key="h2", expected_owner_fence=1,
                  attempt_id=attempt["attempt_id"], package={"next_step": "anyone", "workers": {"quiescent": True}})
        withdrawn = self.offer(codex, attempt, to="third", key="h2b")
        assert write(codex, "withdraw_handoff", project_id="p", handoff_id=withdrawn["handoff_id"],
                     reason="changed my mind")["status"] == "withdrawn"
        assert code_of(lambda: write(third, "accept_handoff", project_id="p", handoff_id=withdrawn["handoff_id"],
                                     expected_owner_fence=1)) == "handoff_closed"
        assert read(codex, "get_attempt", attempt_id=attempt["attempt_id"])["owner_driver_id"] == "codex"
        # Expiry: 24 hours later the offer lapses and the owner is unchanged.
        late = self.offer(codex, attempt, to="third", key="h3")
        clock.advance(24 * 3600)
        assert read(codex, "list_handoffs", project_id="p", statuses=["expired"])["handoffs"][0]["handoff_id"] == late["handoff_id"]
        assert code_of(lambda: write(third, "accept_handoff", project_id="p", handoff_id=late["handoff_id"],
                                     expected_owner_fence=1)) == "handoff_expired"
        assert read(codex, "get_attempt", attempt_id=attempt["attempt_id"])["owner_driver_id"] == "codex"
        # Ownership moved under an open offer: the offer is voided and accept is stale.
        fresh = self.offer(codex, attempt, to="third", key="h4")
        reclaimable(clock)
        write(third, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
        assert read(codex, "get_handoff", project_id="p", handoff_id=fresh["handoff_id"])["handoff"]["status"] == "withdrawn"

    def test_claim_handoff(self, db, clock):
        project(db, enforce=True)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        held = claim(codex, "a", "plan", workspace="h:/w#c")
        offer = write(codex, "offer_handoff", project_id="p", request_key="c1", expected_owner_fence=held["fence"],
                      claim_id=held["claim_id"], to_driver_id="grok", package={"next_step": "split a"})
        out = write(grok, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=1)
        assert out["claim"]["driver_id"] == "grok" and out["claim"]["purpose"] == "plan" and out["claim"]["fence"] == 2
        assert out["claim"]["workspace"] == "h:/w#c"
        assert read(grok, "get_claim", project_id="p", claim_id=held["claim_id"])["claim"]["status"] == "transferred"
        assert code_of(lambda: claim(codex, "a", workspace="h:/w#c", request_key="again")) == "claimed_by_other"


# ---------------------------------------------------------------------------
# 4. subagent registry: a record, nothing more
# ---------------------------------------------------------------------------
class TestSubagentRecord:
    def test_registry_records_who_which_checkout_and_parent_and_grants_nothing(self, db, clock):
        owner = project(db, enforce=True)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        # Registration is required by nothing: a claim for an unregistered worker passes.
        held = claim(codex, "a", subagent="codex/worker", workspace="h:/w#c")
        attempt = external(codex, "a", claim_id=held["claim_id"], fence=1)
        sub = register(codex, attempt, "worker", workspace="h:/w#c")
        assert sub == {**sub, "subagent_id": "codex/worker", "parent_driver_id": "codex", "workspace": "h:/w#c",
                       "attempt_id": attempt["attempt_id"], "node_key": "a", "idempotent": False}
        assert set(sub) == {"subagent_id", "parent_driver_id", "project_id", "attempt_id", "node_key", "workspace",
                            "created_at", "idempotent"}, "no instructions, leases or takeover state"
        assert register(codex, attempt, "worker", workspace="h:/w#c")["idempotent"] is True
        assert code_of(lambda: register(codex, attempt, "worker", workspace="h:/elsewhere#c")) == "subagent_exists"
        assert code_of(lambda: register(grok, attempt, "w4")) == "not_attempt_owner"
        assert register(codex, attempt, "second", workspace="h:/w#c")["subagent_id"] == "codex/second"
        # After a takeover the record is unchanged and grants the old parent nothing:
        # the fence +1 is what refuses the old owner and its workers.
        reclaimable(clock)
        write(grok, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1, reason="gone")
        listed = read(grok, "list_subagents", project_id="p", attempt_id=attempt["attempt_id"])["subagents"]
        assert [(s["subagent_id"], s["parent_driver_id"]) for s in listed] == [("codex/second", "codex"),
                                                                              ("codex/worker", "codex")]
        assert code_of(lambda: report(codex, attempt, "running", fence=1)) == "stale_fence"
        assert code_of(lambda: register(codex, attempt, "late")) == "not_attempt_owner"
        assert register(grok, attempt, "mine")["parent_driver_id"] == "grok"
        assert read(grok, "list_subagents", project_id="p", parent_driver_id="grok")["subagents"][0]["subagent_id"] \
            == "grok/mine"
        # Private: an anonymous reader of the opened project sees no registry.
        owner.open_project("p")
        anonymous = svc(db, trusted=False)
        assert "orphaned_subagents" not in read(anonymous, "project_overview", project_id="p")
        with pytest.raises(Exception):
            read(anonymous, "list_subagents", project_id="p")
        # Every project records subagents (multi_driver is always on).
        project(db.parent / "o.sqlite")
        plain = svc(db.parent / "o.sqlite", "codex")
        own = external(plain, "a")
        assert write(plain, "register_subagent", project_id="p", attempt_id=own["attempt_id"],
                     label="w", workspace="h:/w#c")["subagent_id"] == "codex/w"


# ---------------------------------------------------------------------------
# status enumeration sweep: every place that lists terminal attempt statuses
# ---------------------------------------------------------------------------
def test_abandoned_is_in_every_terminal_enumeration():
    from pathlib import Path
    from core import state_changes, state_service
    assert "abandoned" in state_service.StateService._TERMINAL
    assert "abandoned" not in __import__("core.state_attempts", fromlist=["ACTIVE"]).ACTIVE
    changes = Path(state_changes.__file__).read_text()
    assert '"abandoned"' in changes, "wait_disposition must not wait on an abandoned attempt"
    schema = Path(__import__("core.state_attempt_schema", fromlist=["ATTEMPT_TABLE"]).__file__).read_text()
    assert schema.count("'abandoned'") >= 3, "attempt CHECK, owner CHECK and owner backfill all name it"
    external = Path(__import__("core.state_external", fromlist=["ExternalAttempts"]).__file__).read_text()
    assert "late_after_abandon" in external


# ---------------------------------------------------------------------------
# transports: REST and MCP ride the same typed contract
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
        for who in ("grok", "codex"):                       # reclaiming is for project MEMBERS (round 3)
            registry.set_membership("p", who, "member", 0, "member of p", actor="t")
        with TestClient(app) as client:
            yield client, tokens
        drivers._REGISTRIES.clear()

    @staticmethod
    def _post(client, token, path, body):
        return client.post(path, json=body, headers={"Authorization": "Bearer " + token})

    def test_rest_enforcement_reclaim_and_private_doors(self, world):
        client, tokens = world
        for action, body in (("create_project", {"project_id": "p", "title": "P"}),
                             ("add_nodes", {"project_id": "p", "nodes": [{"key": "a", **NODE}]}),
                             ("set_claim_enforcement", {"project_id": "p", "claim_enforcement": "on",
                                                        "expected_revision": 0, "reason": "r"})):
            response = self._post(client, tokens["owner"], "/api/state/commands/" + action, body)
            assert response.status_code == 200, response.text
        start = {"project_id": "p", "node_key": "a", "expected_revision": 1, "harness": "h", "external_id": "j",
                 "request_key": "k"}
        refused = self._post(client, tokens["codex"], "/api/state/commands/start_external_attempt", start)
        assert refused.status_code == 409 and refused.json()["detail"].startswith("claim_required")
        held = self._post(client, tokens["grok"], "/api/state/commands/claim_node",
                          {"project_id": "p", "node_key": "a", "purpose": "implement", "expected_revision": 1,
                           "request_key": "k", "workspace": "h:/w#g"}).json()
        started = self._post(client, tokens["grok"], "/api/state/commands/start_external_attempt",
                             {**start, "claim_id": held["claim_id"], "fence": 1})
        assert started.status_code == 200, started.text
        attempt_id = started.json()["attempt_id"]
        early = self._post(client, tokens["codex"], "/api/state/commands/abandon_external_attempt",
                           {"attempt_id": attempt_id, "expected_owner_fence": 1, "abandon_kind": "unknown",
                            "reason": "r"})
        assert early.status_code == 409 and early.json()["detail"].startswith("lease_not_expired")
        no_fence = self._post(client, tokens["grok"], "/api/state/commands/report_external_attempt",
                              {"attempt_id": attempt_id, "observation_id": "o", "expected_version": 0,
                               "context_hash": started.json()["context_hash"], "status": "running",
                               "report_ref": "/r", "report_sha256": "0" * 64})
        assert no_fence.status_code == 409 and no_fence.json()["detail"].startswith("fence_required")
        auth = {"Authorization": "Bearer " + tokens["grok"]}
        for path in ("/api/state/projects/p/subagents", "/api/state/projects/p/handoffs"):
            assert client.get(path, headers=auth).status_code == 200, path
            assert client.get(path).status_code in (401, 403), path
        assert client.get("/api/state/projects/p/driver-notices", headers=auth).status_code == 404
        offer = self._post(client, tokens["grok"], "/api/state/commands/offer_handoff",
                           {"project_id": "p", "request_key": "h", "expected_owner_fence": 1, "attempt_id": attempt_id,
                            "to_driver_id": "codex", "package": {"next_step": "take it",
                                                                 "workers": {"quiescent": True}}})
        assert offer.status_code == 200, offer.text
        assert offer.json()["notified"]["target_driver_id"] == "codex"          # a P2 inbox delivery
        accepted = self._post(client, tokens["codex"], "/api/state/commands/accept_handoff",
                              {"project_id": "p", "handoff_id": offer.json()["handoff_id"], "expected_owner_fence": 1})
        assert accepted.status_code == 200 and accepted.json()["attempt"]["owner_driver_id"] == "codex"

    def test_mcp_uses_the_same_contract(self, world):
        client, tokens = world

        def call(token, tool, action, arguments):
            body = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": tool, "arguments": {"action": action, "arguments": arguments}}},
                headers={"Authorization": "Bearer " + token,
                         "Accept": "application/json, text/event-stream"}).json()["result"]
            return body, (None if body.get("isError") else json.loads(body["content"][0]["text"])["result"])

        call(tokens["owner"], "state_graph_write", "create_project", {"project_id": "p", "title": "P"})
        call(tokens["owner"], "state_graph_write", "add_nodes", {"project_id": "p", "nodes": [{"key": "a", **NODE}]})
        _, policy = call(tokens["owner"], "state_graph_write", "set_claim_enforcement",
                         {"project_id": "p", "claim_enforcement": "on", "expected_revision": 0, "reason": "r"})
        assert policy["claim_enforcement"] == "on"
        raw, _ = call(tokens["grok"], "state_graph_write", "start_external_attempt",
                      {"project_id": "p", "node_key": "a", "expected_revision": 1, "harness": "h",
                       "external_id": "j", "request_key": "k"})
        assert raw["isError"] is True and "claim_required" in raw["content"][0]["text"]
        _, listed = call(tokens["grok"], "state_graph_read", "list_handoffs", {"project_id": "p"})
        assert listed["handoffs"] == []
        _, subs = call(tokens["grok"], "state_graph_read", "list_subagents", {"project_id": "p"})
        assert subs["subagents"] == []
        raw, _ = call(tokens["grok"], "state_graph_read", "abandon_external_attempt", {"attempt_id": "x"})
        assert raw["isError"] is True
