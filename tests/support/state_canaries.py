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

import os
import sqlite3
from datetime import datetime, timezone

from core.state_privacy import PRIVATE_STATE_TABLES

OPEN = "canary-open"          # opened by a writer
SHUT = "canary-shut"          # never opened
SENDER = "canary-sender"      # sends director mail to OPEN
OPEN_NOTE = "OPEN-NOTEBOOK-PUBLIC-TEXT-51d2"
NOTEBOOK_TABLES = ("state_driver_notes", "state_driver_note_revisions",
                   "state_driver_note_entries")
ADMIN_TOKEN = "state-canaries-admin-token-not-a-secret"


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
        "state_director_inbox_sequences": (
            "INSERT INTO state_director_inbox_sequences(project_id,next_seq) VALUES(?,900)",
            (c("state_director_inbox_sequences"),)),
        "state_director_idempotency": (
            "INSERT INTO state_director_idempotency(actor,scope_project_id,operation,"
            "request_key,payload_json,result_json) VALUES(?,?,'canary',?,?,?)",
            (c("state_director_idempotency"), OPEN, c("state_director_idempotency"),
             '{"canary": "%s"}' % c("state_director_idempotency"),
             '{"canary": "%s"}' % c("state_director_idempotency"))),
    }


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
    writer.driver_notes.update(OPEN, "permanent", OPEN_NOTE, 0, "seeder")
    writer.driver_notes.write_entry(OPEN, OPEN_NOTE + " assertion", OPEN_NOTE + " body", "seeder")
    writer.driver_notes.update(SHUT, "permanent", "shut notebook", 0, "seeder")
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
