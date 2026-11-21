"""One OPENED project, one UNOPENED project, one distinct canary per private table.

Test scaffolding shared by the row-verdict tests. It imports no name that only
exists on the round that added it, so the same test files run on the base and
report which attributes are absent there instead of crashing while seeding.

Where each canary goes is the row a reader of that table actually reads:

* the three notebook tables: the UNOPENED project's rows. The opened project's
  notebook is public by the owner's ruling (2026-09-22,
  note://aitelier/546f3b521eca), so its text is the positive control
  ``OPEN_NOTE`` and never a canary;
* ``state_project_access``: the private columns of the OPENED project's row;
* ``state_events``: an event of the OPENED project;
* the four director tables: rows that point at the OPENED project where the
  table has a project column, and the canary as the key where it has no other
  text column (``state_director_inbox_sequences``).

``plant`` writes them on a raw sqlite3 connection (the seeding side is a test
fixture, a legitimate trusted construction point) and ``canary_presence`` reads
them back on another raw connection: the positive control that every canary is
really in the database before any anonymous read is judged.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone

from core.state_privacy import PRIVATE_STATE_TABLES
from tests.support.legacy_driver_note import seed_section

OPEN = "canary-open"          # opened by a writer
SHUT = "canary-shut"          # never opened
SENDER = "canary-sender"      # sends director mail to OPEN
OPEN_NOTE = "OPEN-NOTEBOOK-PUBLIC-TEXT-51d2"
NOTEBOOK_TABLES = ("state_driver_notes", "state_driver_note_revisions",
                   "state_driver_note_entries")
ADMIN_TOKEN = "state-canaries-admin-token-not-a-secret"
STEWARD_PROPOSAL = "proposal-canary"
STEWARD_MEMORY_REQUEST = "memreq-canary"
STEWARD_SCHEDULE = "sch-canary"
CHANNEL_MESSAGE = "message-source-canary"
STEWARD_CONNECTION = "canaryconn"


def canary(table: str) -> str:
    return f"CANARY-{table}-7e3b"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan():
    """Table -> (SQL, parameters). Keys must equal the private classification."""
    now = _now()
    c = canary
    message_id = c("state_director_messages") + "-message"
    return {
        "state_driver_notes": (
            "UPDATE state_driver_notes SET temporary_text=? WHERE project_id=?",
            (c("state_driver_notes"), SHUT)),
        "state_driver_note_revisions": (
            "INSERT INTO state_driver_note_revisions(project_id,revision,permanent_text,"
            "temporary_text,actor,director_identity,operation,section,created_at) "
            "VALUES(?,?,?,?,?,?,'replace','permanent',?)",
            (SHUT, 900, c("state_driver_note_revisions"), c("state_driver_note_revisions"),
             "seeder", "seeder", now)),
        "state_driver_note_entries": (
            "INSERT INTO state_driver_note_entries(project_id,entry_id,assertion,body,force,"
            "landed,listing,superseded_by,supersede_reason,delist_reason,actor,"
            "director_identity,created_at,updated_at) "
            "VALUES(?,?,?,?,'in_force','','listed',NULL,'','',?,?,?,?)",
            (SHUT, "c0ffee0c0ffe", c("state_driver_note_entries"),
             c("state_driver_note_entries"), "seeder", "seeder", now, now)),
        "state_events": (
            "INSERT INTO state_events(project_id,node_key,event_type,payload_json,created_at) "
            "VALUES(?,NULL,'canary',?,?)",
            (OPEN, '{"canary": "%s"}' % c("state_events"), now)),
        "state_project_access": (
            "UPDATE state_project_access SET opened_by=?, changed_by=? WHERE project_id=?",
            (c("state_project_access"), c("state_project_access"), OPEN)),
        "state_director_messages": (
            "INSERT INTO state_director_messages(message_id,thread_id,sender_project_id,"
            "director_identity,actor,subject,body,created_at,reply_to_delivery_id,delivery_mode) "
            "VALUES(?,?,?,?,?,?,?,?,NULL,'transient')",
            (message_id, message_id, SENDER, "seeder", "seeder",
             c("state_director_messages"), c("state_director_messages"), now)),
        "state_director_deliveries": (
            "INSERT INTO state_director_deliveries(delivery_id,message_id,target_project_id,"
            "delivery_seq,status,version) VALUES(?,?,?,900,'unread',1)",
            (c("state_director_deliveries"), message_id, OPEN)),
        "state_director_delivery_acks": (
            "INSERT INTO state_director_delivery_acks(delivery_id,driver_id,required,acked_at) "
            "VALUES(?,?,0,?)",
            (c("state_director_deliveries"), c("state_director_delivery_acks"), now)),
        "state_director_legacy_acks": (
            "INSERT INTO state_director_legacy_acks(delivery_id) VALUES(?)",
            (c("state_director_legacy_acks"),)),
        "state_director_inbox_sequences": (
            "INSERT INTO state_director_inbox_sequences(project_id,next_seq) VALUES(?,900)",
            (c("state_director_inbox_sequences"),)),
        "state_director_idempotency": (
            "INSERT INTO state_director_idempotency(actor,scope_project_id,operation,"
            "request_key,payload_json,result_json) VALUES(?,?,'canary',?,?,?)",
            (c("state_director_idempotency"), OPEN, c("state_director_idempotency"),
             '{"canary": "%s"}' % c("state_director_idempotency"),
             '{"canary": "%s"}' % c("state_director_idempotency"))),
        # A retained candidate's bundle_bytes ARE the private Git bytes that
        # reconstruct it. The canary is written as the commit key AND as the
        # blob, so a raw read (or an armed read without the column projection)
        # carries it; the projection filters this row out because no opened
        # project's attempt references the canary commit.
        "state_git_artifacts": (
            "INSERT INTO state_git_artifacts(commit_sha,tree_sha,bundle_sha256,"
            "retained_ref,bundle_bytes,created_at) VALUES(?,?,?,?,?,?)",
            (c("state_git_artifacts"), c("state_git_artifacts") + "-tree",
             c("state_git_artifacts") + "-sha256", "refs/vault/canary",
             c("state_git_artifacts").encode(), now)),
        # The bundle store holds the same private bytes, content-addressed. Its
        # key must be a hex digest (schema CHECK), so the canary is the payload:
        # a raw keyframe whose bytes ARE the canary.
        "state_git_bundle_store": (
            "INSERT INTO state_git_bundle_store(bundle_sha256,codec,base_sha256,raw_size,"
            "payload,created_at) VALUES(?,'raw',NULL,?,?,?)",
            (hashlib.sha256(c("state_git_bundle_store").encode()).hexdigest(),
             len(c("state_git_bundle_store")), c("state_git_bundle_store").encode(), now)),
        # The release ledger: which candidate's bytes State stopped holding, when
        # and by whom. Its key must be a hex digest (schema CHECK), so the canary
        # is the ACTOR - the private fact that would tell an untrusted caller who
        # retired a candidate of a project it cannot open.
        "state_git_bundle_releases": (
            "INSERT INTO state_git_bundle_releases(bundle_sha256,commit_sha,codec,base_sha256,"
            "raw_size,payload_bytes,released_at,actor,rule,rows_before,payload_before,rows_after,"
            "payload_after) VALUES(?,?,'raw',NULL,1,1,?,?,'no-live-consumer@1',1,1,0,0)",
            (hashlib.sha256(c("state_git_bundle_releases").encode()).hexdigest(),
             c("state_git_bundle_releases") + "-commit", now, c("state_git_bundle_releases"))),
        # A LIVE claim on the OPENED project's public node. Its holder and lease
        # are public there; the canary sits only in the PRIVATE columns
        # (workspace, request key) that no public read may carry.
        "state_node_claims": (
            ("INSERT INTO state_node_claims(claim_id,project_id,node_key,driver_id,purpose,status,fence,"
             "node_revision,attempt_id,workspace,subagent,lease_seconds,lease_expires_at,last_heartbeat_at,"
             "request_key,request_hash,created_at,updated_at) "
             "VALUES(?,?,'a','seeder-driver','implement','live',1,1,NULL,?,NULL,7200,?,?,?,'0',?,?)"),
            ("claim-canary-public-id", OPEN, c("state_node_claims"),
             "2999-01-01T00:00:00.000000+00:00", now, c("state_node_claims"), now, now)),
        "state_claim_history": (
            ("INSERT INTO state_claim_history(claim_id,project_id,node_key,driver_id,purpose,status,fence,"
             "lease_expires_at,actor,reason,created_at) VALUES(?,?,'a',?,'implement','released',1,?,?,?,?)"),
            ("claim-canary-public-id", OPEN, c("state_claim_history"), now,
             c("state_claim_history"), c("state_claim_history"), now)),
        # A handoff OFFERED on the opened project: its package carries the
        # offerer's in-flight references and must never reach an anonymous reader.
        "state_handoffs": (
            ("INSERT INTO state_handoffs(handoff_id,project_id,subject_kind,attempt_id,claim_id,node_key,"
             "from_driver_id,to_driver_id,status,package_json,expected_owner_fence,request_key,request_hash,"
             "lease_expires_at,created_at,updated_at) "
             "VALUES(?,?,'claim',NULL,'claim-canary-public-id','a',?,NULL,'offered',?,1,?,'0',?,?,?)"),
            ("handoff-canary", OPEN, c("state_handoffs"), '{"next_step": "%s"}' % c("state_handoffs"),
             c("state_handoffs"), "2999-01-01T00:00:00.000000+00:00", now, now)),
        # A correction retiring the scheduling protection of a run reference on
        # the OPENED project's public node (its parent row is written by
        # ``_prerequisites``). The public projection carries only the project,
        # reference id and original payload hash; the canary sits in the private
        # audit columns (reason, request identity, foreign binding witness).
        "state_context_run_protection_retirements": (
            ("INSERT INTO state_context_run_protection_retirements(project_id,reference_id,node_key,"
             "node_revision,original_payload_hash,run_id,foreign_project_id,foreign_attempt_id,"
             "foreign_execution_project_id,request_key,request_hash,actor,reason,created_at) "
             "VALUES(?,?,'a',1,?,?,?,?,?,?,'0',?,?,?)"),
            (OPEN, "retirement-canary-reference", "retirement-canary-payload-hash",
             "retirement-canary-run", SHUT, c("state_context_run_protection_retirements"),
             SHUT, c("state_context_run_protection_retirements"), "seeder",
             c("state_context_run_protection_retirements"), now)),
        # A steward proposal about the OPENED project, the canary in its payload
        # and reason; and a done owner memory request whose result carries it
        # (as a profile_read result carries the owner's profile text).
        "state_steward_proposals": (
            ("INSERT INTO state_steward_proposals(proposal_id,version,kind,payload_json,reason,status,"
             "created_by,created_at) VALUES(?,1,'start_driver',?,?,'open','seeder',?)"),
            (STEWARD_PROPOSAL, '{"project_id": "%s", "note": "%s"}' % (OPEN, c("state_steward_proposals")),
             c("state_steward_proposals"), now)),
        "state_steward_memory_requests": (
            ("INSERT INTO state_steward_memory_requests(request_id,kind,payload_json,status,result_json,"
             "created_at,completed_at,created_by) VALUES(?,'profile_read','{}','done',?,?,?,'owner:seeder')"),
            (STEWARD_MEMORY_REQUEST, '{"text": "%s"}' % c("state_steward_memory_requests"), now, now)),
        # A live, enabled reminder whose title and instruction carry the canary.
        "state_steward_schedules": (
            ("INSERT INTO state_steward_schedules(schedule_id,version,title,instruction,kind,at,expr,tz,enabled,"
             "next_run_at,created_by,updated_by,created_at,updated_at) "
             "VALUES(?,1,?,?,'once',?,NULL,'Europe/Paris',1,?,'owner','owner',?,?)"),
            (STEWARD_SCHEDULE, c("state_steward_schedules"), c("state_steward_schedules"),
             "2999-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00", now, now)),
        # Step 19: the owner's bound Slack account (the steward's trust anchor), an
        # outstanding binding code, and the source row of an owner message - each
        # with the canary in its identifying columns.
        "state_owner_channels": (
            ("INSERT INTO state_owner_channels(channel,sender_ref,bound_at,bound_by,revoked_at,is_default) "
             "VALUES('slack',?,?,?,NULL,1)"),
            (c("state_owner_channels"), now, c("state_owner_channels"))),
        "state_owner_channel_codes": (
            ("INSERT INTO state_owner_channel_codes(code_hash,channel,issued_at,expires_at) "
             "VALUES(?,'wechat',?,'2999-01-01T00:00:00+00:00')"),
            (c("state_owner_channel_codes"), now)),
        "state_message_sources": (
            ("INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,channel_message_ref,at,error) "
             "VALUES(?,'in','slack',?,?,?,?)"),
            (CHANNEL_MESSAGE, c("state_message_sources"), c("state_message_sources"), now,
             c("state_message_sources"))),
        # A remembered binding attempt: the canary as its sender and channel message.
        "state_owner_channel_bind_attempts": (
            ("INSERT INTO state_owner_channel_bind_attempts(channel,sender_ref,channel_message_ref,outcome,at) "
             "VALUES('slack',?,?,'invalid_code',?)"),
            (c("state_owner_channel_bind_attempts"), c("state_owner_channel_bind_attempts"), now)),
        # The WeChat status as the host reported it: the canary in the detail and the QR.
        "state_owner_channel_status": (
            ("INSERT INTO state_owner_channel_status(channel,connected,detail,qr_svg,at) "
             "VALUES('wechat',0,?,?,?)"),
            (c("state_owner_channel_status"), c("state_owner_channel_status"), now)),
        # A channel-down alert's ledger row (step 25): the canary as the channel's error.
        "state_channel_alerts": (
            ("INSERT INTO state_channel_alerts(channel,kind,down_since,state,attempt,via,claimed_at,finished_at,"
             "error) VALUES('wechat','down',?,'failed',1,'slack',?,?,?)"),
            (now, now, now, c("state_channel_alerts"))),
        # The owner's request for a new WeChat login QR: the canary as its id, its
        # requester and its result; dated ahead so it stays pending (listed) all test long.
        "state_owner_channel_login_requests": (
            ("INSERT INTO state_owner_channel_login_requests(request_id,channel,requested_at,requested_by,status,"
             "result_json) VALUES(?,'wechat','2999-01-01T00:00:00+00:00',?,'pending',?)"),
            (c("state_owner_channel_login_requests"), c("state_owner_channel_login_requests"),
             '{"refused": "%s"}' % c("state_owner_channel_login_requests"))),
        # Step 23 (core/state_groups.py): the bound family group, one of its @-messages, a
        # member's dashboard name, a family memory entry, the owner's family note, whose turn
        # the steward is in, and a group awaiting the owner's pick - the canary in each.
        "state_owner_groups": (
            ("INSERT INTO state_owner_groups(channel,group_ref,label,mention_prefix,bound_at) "
             "VALUES('wechat',?,?,?,?)"),
            (c("state_owner_groups"), c("state_owner_groups"), c("state_owner_groups"), now)),
        "state_group_messages": (
            ("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,sender_ref,"
             "sender_role,channel_message_ref,body,at) VALUES(?,'wechat',?,'in','question',1,?,'member',?,?,?)"),
            (c("state_group_messages"), c("state_owner_groups"), c("state_group_messages"),
             c("state_group_messages"), c("state_group_messages"), "2999-01-01T00:00:00+00:00")),
        "state_group_members": (
            ("INSERT INTO state_group_members(channel,group_ref,sender_ref,ordinal,label) VALUES('wechat',?,?,1,?)"),
            (c("state_owner_groups"), c("state_group_members"), c("state_group_members"))),
        "state_family_memory": (
            ("INSERT INTO state_family_memory(entry_id,channel,group_ref,text,added_by_role,added_by_ref,at) "
             "VALUES(?,'wechat',?,?,'member',?,?)"),
            (c("state_family_memory"), c("state_owner_groups"), c("state_family_memory"), c("state_family_memory"),
             "2999-01-01T00:00:00+00:00")),
        "state_family_note": (
            ("INSERT INTO state_family_note(channel,group_ref,text,updated_at) VALUES('wechat',?,?,?)"),
            (c("state_owner_groups"), c("state_family_note"), now)),
        "state_steward_turn_audience": (
            ("INSERT INTO state_steward_turn_audience(id,turn,audience,channel,group_ref,sender_refs_json,at) "
             "VALUES(1,?,'group','wechat',?,?,?)"),
            (c("state_steward_turn_audience"), c("state_owner_groups"), '["%s"]' % c("state_steward_turn_audience"),
             now)),
        "state_pending_groups": (
            ("INSERT INTO state_pending_groups(channel,room_ref,mention_prefix,code_hash,at) VALUES('wechat',?,?,?,?)"),
            (c("state_pending_groups"), c("state_pending_groups"), c("state_pending_groups"), now)),
        # Step 24.1a: one of the steward's connections (the canary in its name, address and
        # the server's instructions; off, so no gateway serves it), one tool of it (the
        # canary in its definition), the served hash, a restart marker, a restart record and
        # a line of the call ledger (the turn audience's canary is step 23's, above).
        "state_steward_connections": (
            ("INSERT INTO state_steward_connections(conn_id,conn_uid,label,url,auth_kind,header_name,visibility,"
             "enabled,generation,config_version,auth_identity,created_at,updated_at,last_probe_at,last_error,"
             "instructions,last_change) VALUES(?,?,?,?,'none',NULL,'private',0,1,1,NULL,?,?,NULL,NULL,?,'dashboard')"),
            (STEWARD_CONNECTION, c("state_steward_connections"), c("state_steward_connections")[:40],
             "https://%s.example/mcp" % c("state_steward_connections").lower(), now, now, c("state_steward_connections"))),
        "state_steward_connection_tools": (
            ("INSERT INTO state_steward_connection_tools(conn_id,raw_name,gw_name,definition_json,schema_sha256,"
             "allowed,allowed_sha256,outward,egress,blocked_args,daily_cap,first_seen_at,last_seen_at) "
             "VALUES(?,'tool','tool',?,'0',0,NULL,1,1,'[]',NULL,?,?)"),
            (STEWARD_CONNECTION, '{"description": "%s", "inputSchema": {"type": "object"}, "name": "tool"}'
             % c("state_steward_connection_tools"), now, now)),
        "state_steward_tools_served": (
            "INSERT INTO state_steward_tools_served(driver_id,effective_hash,served_at) VALUES(?,?,?)",
            (c("state_steward_tools_served"), c("state_steward_tools_served"), now)),
        "state_steward_restart_due": (
            "INSERT INTO state_steward_restart_due(driver_id,reason,effective_hash,due_at,created_at) VALUES(?,?,?,?,?)",
            (c("state_steward_restart_due"), c("state_steward_restart_due"), c("state_steward_restart_due"),
             "2999-01-01T00:00:00.000000Z", now)),
        "state_steward_restart_done": (
            "INSERT INTO state_steward_restart_done(driver_id,reasons_json,restarted_at,error) VALUES(?,?,?,?)",
            (c("state_steward_restart_done"), '[{"reason": "%s", "effective_hash": "x"}]' % c("state_steward_restart_done"),
             "2999-01-01T00:00:00.000000Z", c("state_steward_restart_done"))),
        "state_steward_tool_calls": (
            ("INSERT INTO state_steward_tool_calls(conn_id,raw_name,at,local_day,rpc_error,is_error) "
             "VALUES(?,'tool',?,'2999-01-01',?,0)"),
            (STEWARD_CONNECTION, now, c("state_steward_tool_calls"))),
    }


def _prerequisites():
    """Parent rows the plan's rows reference by foreign key. They carry no canary."""
    now = _now()
    return [(
        "INSERT INTO state_history_links(project_id,reference_id,node_key,kind,ref,label,"
        "observed_status,artifact_ref,report_sha256,provenance_actor,recorded_by,node_revision,"
        "protection,payload_hash,created_at) "
        "VALUES(?,?,'a','run',?,'canary run','completed',NULL,NULL,'seeder','seeder',1,1,?,?)",
        (OPEN, "retirement-canary-reference", "retirement-canary-run",
         "retirement-canary-payload-hash", now))]




def seed(db) -> None:
    """Create OPEN (opened), SHUT (never opened) and SENDER, write both
    notebooks and one director message through a TRUSTED service, then plant
    the canaries on a raw connection."""
    from core.state_service import StateService
    writer = StateService(db, actor="seeder", project_read_trusted=True)
    for pid in (OPEN, SHUT, SENDER):
        writer.create_project(pid, pid)
    writer.store.add_nodes(OPEN, [{"key": "a", "goal": "public goal", "acceptance": [
        {"id": "c", "kind": "test", "description": "public criterion"}]}])
    seed_section(writer, OPEN, "permanent", OPEN_NOTE, "seeder")
    writer.driver_notes.write_entry(OPEN, OPEN_NOTE + " assertion", OPEN_NOTE + " body", "seeder")
    seed_section(writer, SHUT, "permanent", "shut notebook", "seeder")
    writer.director_messages.send_director_message(
        SENDER, "seeder", "canary-rk-1", "natural subject", "natural body",
        target_project_id=OPEN)
    writer.open_project(OPEN)
    plant(db.db_path)


def plant(path: str) -> None:
    plan = _plan()
    assert set(plan) == set(PRIVATE_STATE_TABLES), (
        "every private table needs a canary plan; unplanned: "
        f"{sorted(set(PRIVATE_STATE_TABLES) - set(plan))}, stale: "
        f"{sorted(set(plan) - set(PRIVATE_STATE_TABLES))}")
    raw = sqlite3.connect(path)
    try:
        for sql, params in _prerequisites():
            cursor = raw.execute(sql, params)
            assert cursor.rowcount == 1, (sql, cursor.rowcount)
        for table in sorted(plan):
            sql, params = plan[table]
            cursor = raw.execute(sql, params)
            assert cursor.rowcount == 1, (table, cursor.rowcount)
        raw.commit()
    finally:
        raw.close()


def canary_presence(path: str) -> dict:
    """Positive control: read every private table back on a raw connection."""
    raw = sqlite3.connect(path)
    raw.row_factory = sqlite3.Row
    try:
        present = {}
        for table in sorted(PRIVATE_STATE_TABLES):
            if table in NOTEBOOK_TABLES:
                rows = raw.execute(f"SELECT * FROM {table} WHERE project_id=?", (SHUT,)).fetchall()
            else:
                rows = raw.execute(f"SELECT * FROM {table}").fetchall()
            present[table] = canary(table) in repr([tuple(r) for r in rows])
        return present
    finally:
        raw.close()


def leaked_tables(text: str) -> list:
    """The private tables whose canary appears in ``text``."""
    return sorted(t for t in PRIVATE_STATE_TABLES if canary(t) in text)


def new_database(tmp_path, name):
    from core.state_database import StateDatabase
    db = StateDatabase(str(tmp_path / name))
    seed(db)
    return db


# ---------------------------------------------------------------- HTTP harness
def arm(monkeypatch):
    """The gate is ON and the request carries no credential: anonymous."""
    from api import authz
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)


def app_for(mode, db):
    """``fresh``: a new FastAPI app with the product State router; ``main``:
    ``api.main.app``. Both use the product ``get_service``, which derives the
    service's trust from the raw request credential."""
    from fastapi import FastAPI
    from api.dependencies import get_db_manager, get_workspace_manager
    from api.state_graph_routers import router
    if mode == "main":
        from api import main as main_module
        app = main_module.app
    else:
        app = FastAPI()
        app.include_router(router)
    app.dependency_overrides[get_db_manager] = lambda: db
    app.dependency_overrides[get_workspace_manager] = lambda: None
    return app


def client_for(app, mode):
    from fastapi.testclient import TestClient
    # api.main.app runs WITHOUT its lifespan: startup takes the server's
    # single-instance lock.
    # The client address is loopback: api.main.app refuses any other before
    # the State router runs.
    client = TestClient(app, client=("127.0.0.1", 51100))
    return client if mode == "main" else client.__enter__()


def mount(app, path, handler, guarded):
    """Put ``handler`` at the FRONT of the route table, optionally behind the
    product State router's guard."""
    from fastapi import Depends
    from api.state_graph_routers import router
    deps = [Depends(router.dependencies[0].dependency)] if guarded else []
    app.router.add_api_route(path, handler, methods=["GET"], dependencies=deps)
    app.router.routes.insert(0, app.router.routes.pop())


def unmount(app, handler):
    app.router.routes = [r for r in app.router.routes
                         if getattr(r, "endpoint", None) is not handler]


def release(app):
    from api.dependencies import get_db_manager, get_workspace_manager
    app.dependency_overrides.pop(get_db_manager, None)
    app.dependency_overrides.pop(get_workspace_manager, None)


def request_is_anonymous(app, client) -> bool:
    """Control: the product ``get_service`` really hands this request an
    untrusted service, and the admin token really makes it trusted."""
    from fastapi import Depends
    from api.state_graph_routers import get_service

    def trust(svc=Depends(get_service)):
        return {"trusted": svc.project_read_trusted}

    mount(app, "/state-canaries/trust", trust, guarded=False)
    try:
        anonymous = client.get("/state-canaries/trust").json()["trusted"]
        admin = client.get("/state-canaries/trust",
                           headers={"X-AItelier-Admin-Token": ADMIN_TOKEN}).json()["trusted"]
    finally:
        unmount(app, trust)
    return anonymous is False and admin is True


def path_of(db) -> str:
    return os.fspath(db.db_path)
