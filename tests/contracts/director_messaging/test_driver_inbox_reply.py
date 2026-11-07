"""A driver can reply to a delivery addressed to its own driver inbox.

Witnesses, all through the production entry points (``StateService`` and the
``send_*`` actions), never a hand-built fixture for the reply itself:

1. A driver-addressed delivery is replied to: the reply lands in the cited
   message's project on the cited thread and records the cited delivery id.
2. The selector does NOT widen: a delivery addressed to another driver, an id
   in no inbox, and a project-inbox delivery cited by a non-addressee project
   each refuse ``unknown_delivery`` and create no delivery row.
3. The reply FK is gone from fresh and from populated pre-existing databases,
   every pre-existing row survives with identical values, and a second open is a
   no-op.
"""
import json
import sqlite3

import pytest

from core.director_messaging_protocol import DirectorMessageError
from core.drivers import DriverRegistry
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

PEPPER = "test-only-pepper-not-a-production-secret"


@pytest.fixture
def world(tmp_path):
    """Two projects, three drivers. ada is a member of alpha; bob and carol of beta."""
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "alpha")
    seed.create_project("beta", "beta")
    registry = DriverRegistry(db, PEPPER)
    for driver_id in ["ada", "bob", "carol"]:
        registry.register(driver_id, driver_id, actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    registry.set_membership("beta", "bob", "member", 0, "fixture", actor="test")
    registry.set_membership("beta", "carol", "member", 0, "fixture", actor="test")
    services = {driver_id: StateService(db, actor="driver:" + driver_id, driver_id=driver_id,
                                        project_read_trusted=True)
                for driver_id in ["ada", "bob", "carol"]}
    return db, services


def _delivery_rows(db):
    with db.get_connection() as conn:
        project = conn.execute("SELECT COUNT(*) FROM state_director_deliveries").fetchone()[0]
        driver = conn.execute("SELECT COUNT(*) FROM driver_inbox_deliveries").fetchone()[0]
    return project, driver


def _send_to_driver(service, target, project_id="alpha", request_key="note"):
    """A driver-addressed message from ``service`` to ``target``'s driver inbox,
    about ``project_id`` (the project a reply to it is routed to)."""
    return service.driver_inbox.send_driver_message(
        request_key, "note subject", "note body", target_driver_id=target,
        project_id=project_id)


def _reply(service, cited, request_key="reply"):
    return service.director_messages.send_director_message(
        "beta", "bob", request_key, "reply subject", "reply body",
        reply_to_delivery_id=cited)


def test_reply_to_driver_addressed_delivery_lands_on_sender_project_and_thread(world):
    db, services = world
    # ada (of alpha) sends bob a driver-inbox note about alpha. The reply from
    # bob (of beta) must route to alpha, the cited message's project, on the
    # cited message's thread. beta is the replier's own project, so a reply that
    # wrongly routed to the caller's project would be caught here.
    note = _send_to_driver(services["ada"], "bob", project_id="alpha")
    cited = note["deliveries"][0]["delivery_id"]
    with db.get_connection() as conn:
        note_thread = conn.execute("SELECT thread_id FROM driver_inbox_messages WHERE message_id=?",
                                   (note["message_id"],)).fetchone()[0]
    before_project, _ = _delivery_rows(db)

    reply = _reply(services["bob"], cited)

    result = reply["result"]
    message, deliveries = result["message"], result["deliveries"]
    # Read-back of the message row the reply wrote.
    with db.get_connection() as conn:
        row = conn.execute("SELECT reply_to_delivery_id,thread_id,sender_project_id "
                           "FROM state_director_messages WHERE message_id=?",
                           (message["message_id"],)).fetchone()
    assert row["reply_to_delivery_id"] == cited
    assert row["thread_id"] == note_thread
    assert row["sender_project_id"] == "beta"
    # The delivery the reply created is in alpha's project inbox, one new row.
    assert [d["target_project_id"] for d in deliveries] == ["alpha"]
    after_project, _ = _delivery_rows(db)
    assert after_project == before_project + 1
    with db.get_connection() as conn:
        landed = conn.execute("SELECT target_project_id FROM state_director_deliveries WHERE delivery_id=?",
                              (deliveries[0]["delivery_id"],)).fetchone()
    assert landed["target_project_id"] == "alpha"


def test_refuses_delivery_addressed_to_a_different_driver(world):
    db, services = world
    # ada addresses carol. bob, who is not the addressee, cites it.
    note = _send_to_driver(services["ada"], "carol")
    cited = note["deliveries"][0]["delivery_id"]
    before = _delivery_rows(db)
    with pytest.raises(DirectorMessageError) as refused:
        _reply(services["bob"], cited)
    assert refused.value.code == "unknown_delivery"
    assert _delivery_rows(db) == before


def test_refuses_id_present_in_no_inbox(world):
    db, services = world
    before = _delivery_rows(db)
    with pytest.raises(DirectorMessageError) as refused:
        _reply(services["bob"], "00000000-0000-4000-8000-000000000000")
    assert refused.value.code == "unknown_delivery"
    assert _delivery_rows(db) == before


def test_refuses_project_inbox_delivery_cited_by_a_non_addressee_project(world):
    db, services = world
    # ada, a member of alpha, sends a project-inbox message to beta. beta replies
    # to it. The cited delivery's target is beta. alpha is not its addressee.
    sent = services["ada"].director_messages.send_director_message(
        "alpha", "ada", "to-beta", "subject", "body", target_project_id="beta")
    cited = sent["result"]["deliveries"][0]["delivery_id"]
    before = _delivery_rows(db)
    with pytest.raises(DirectorMessageError) as refused:
        # alpha is the sender; the cited delivery targets beta, so alpha may not cite it.
        services["ada"].director_messages.send_director_message(
            "alpha", "ada", "wrong-project", "subject", "body", reply_to_delivery_id=cited)
    assert refused.value.code == "unknown_delivery"
    assert _delivery_rows(db) == before


# ---------------------------------------------------------------------------
# Schema: the reply FK is gone from fresh and from populated databases.
# ---------------------------------------------------------------------------

OLD_MESSAGES_DDL = """
CREATE TABLE state_director_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    sender_project_id TEXT NOT NULL,
    director_identity TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reply_to_delivery_id TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'transient'
        CHECK(delivery_mode IN ('transient','standing')),
    FOREIGN KEY(sender_project_id) REFERENCES state_projects(project_id),
    FOREIGN KEY(reply_to_delivery_id) REFERENCES state_director_deliveries(delivery_id)
);
"""


def _reply_fk(conn):
    return [row["from"] for row in conn.execute("PRAGMA foreign_key_list(state_director_messages)")
            if row["table"] == "state_director_deliveries"]


def test_fresh_database_never_carries_the_reply_fk(tmp_path):
    db = StateDatabase(str(tmp_path / "fresh.sqlite"))
    StateService(db, actor="fixture", project_read_trusted=True)
    with db.get_connection() as conn:
        assert _reply_fk(conn) == []
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='state_director_messages'").fetchone()[0]
    assert "REFERENCES state_director_deliveries" not in sql


def _populate_old_shape(path):
    """Build the pre-change schema inline and fill it with real, related rows."""
    db = StateDatabase(str(path))
    store_service = StateService(db, actor="fixture", project_read_trusted=True)
    store_service.create_project("alpha", "alpha")
    store_service.create_project("beta", "beta")
    with db.get_connection() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS state_director_deliveries (
            delivery_id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
            target_project_id TEXT NOT NULL, delivery_seq INTEGER NOT NULL CHECK(delivery_seq > 0),
            status TEXT NOT NULL CHECK(status IN ('unread','acknowledged','resolved')),
            version INTEGER NOT NULL CHECK(version > 0),
            UNIQUE(message_id,target_project_id), UNIQUE(target_project_id,delivery_seq),
            FOREIGN KEY(message_id) REFERENCES state_director_messages(message_id),
            FOREIGN KEY(target_project_id) REFERENCES state_projects(project_id));
        """)
        conn.execute("DROP TABLE IF EXISTS state_director_messages")
        conn.executescript(OLD_MESSAGES_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS state_director_messages_thread ON state_director_messages(thread_id)")
        msg1, msg2 = "11111111-1111-4111-8111-111111111111", "33333333-3333-4333-8333-333333333333"
        del1, del2 = "22222222-2222-4222-8222-222222222222", "44444444-4444-4444-8444-444444444444"
        conn.execute("INSERT INTO state_director_messages(message_id,thread_id,sender_project_id,"
                     "director_identity,actor,subject,body,created_at,reply_to_delivery_id,delivery_mode) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (msg1, msg1, "alpha", "ada", "driver:ada", "first", "body one",
                      "2026-09-14T00:00:00.000000Z", None, "transient"))
        conn.execute("INSERT INTO state_director_deliveries VALUES(?,?,?,?,?,?)",
                     (del1, msg1, "beta", 1, "acknowledged", 2))
        # A second message that cites the first delivery (a real reply in the old shape).
        conn.execute("INSERT INTO state_director_messages(message_id,thread_id,sender_project_id,"
                     "director_identity,actor,subject,body,created_at,reply_to_delivery_id,delivery_mode) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (msg2, msg1, "beta", "bob", "driver:bob", "reply", "body two",
                      "2026-09-14T00:01:00.000000Z", del1, "standing"))
        conn.execute("INSERT INTO state_director_deliveries VALUES(?,?,?,?,?,?)",
                     (del2, msg2, "alpha", 1, "unread", 1))
        conn.execute("INSERT INTO state_director_inbox_sequences VALUES('beta',2)")
        conn.execute("INSERT INTO state_director_inbox_sequences VALUES('alpha',2)")
        conn.commit()
    return db


OLD_COLUMNS = ("message_id", "thread_id", "sender_project_id", "director_identity", "actor",
               "subject", "body", "created_at", "reply_to_delivery_id", "delivery_mode")


def _snapshot_messages(db):
    """Pre-existing columns only: the first open legitimately ADDs ack columns."""
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT " + ",".join(OLD_COLUMNS) + " FROM state_director_messages ORDER BY message_id").fetchall()
        return [tuple(row) for row in rows]


def test_populated_old_database_loses_fk_keeps_rows_and_second_open_is_noop(tmp_path):
    path = tmp_path / "populated-old.sqlite"
    db = _populate_old_shape(path)
    before_rows = _snapshot_messages(db)
    with db.get_connection() as conn:
        assert _reply_fk(conn) == ["reply_to_delivery_id"]
        assert len(before_rows) == 2
    before_deliveries = []
    with db.get_connection() as conn:
        before_deliveries = [tuple(r) for r in conn.execute(
            "SELECT * FROM state_director_deliveries ORDER BY delivery_id").fetchall()]
    before_sequences = []
    with db.get_connection() as conn:
        before_sequences = [tuple(r) for r in conn.execute(
            "SELECT * FROM state_director_inbox_sequences ORDER BY project_id").fetchall()]

    # Open through the normal production entry point.
    reopened = StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)

    with reopened.db.get_connection() as conn:
        # (i) the FK is gone from sqlite_master and from the live table.
        assert _reply_fk(conn) == []
        master_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='state_director_messages'").fetchone()[0]
        assert "REFERENCES state_director_deliveries" not in master_sql
        # Index on the table survives the rebuild.
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='state_director_messages'")}
        assert "state_director_messages_thread" in indexes
    # (ii) every pre-existing row survives with identical values.
    after_rows = _snapshot_messages(reopened.db)
    assert after_rows == before_rows
    with reopened.db.get_connection() as conn:
        after_deliveries = [tuple(r) for r in conn.execute(
            "SELECT * FROM state_director_deliveries ORDER BY delivery_id").fetchall()]
        after_sequences = [tuple(r) for r in conn.execute(
            "SELECT * FROM state_director_inbox_sequences ORDER BY project_id").fetchall()]
    assert after_deliveries == before_deliveries
    assert after_sequences == before_sequences

    # (iii) a second open is a no-op: same schema, same rows.
    schema_after_first = _schema_text(reopened.db)
    StateService(StateDatabase(str(path)), actor="fixture", project_read_trusted=True)
    assert _schema_text(reopened.db) == schema_after_first
    assert _snapshot_messages(reopened.db) == before_rows


def _schema_text(db):
    with db.get_connection() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'state_director_%' "
            "ORDER BY type,name").fetchall()]


def test_populated_old_database_still_resolves_a_driver_reply_after_migration(tmp_path):
    """The migrated table accepts a reply citing a driver-inbox delivery: the FK was
    the only thing refusing it. Uses the production send path end to end."""
    path = tmp_path / "populated-reply.sqlite"
    db = _populate_old_shape(path)
    registry = DriverRegistry(db, PEPPER)
    for driver_id in ["ada", "bob"]:
        registry.register(driver_id, driver_id, actor="test")
    # alpha needs an active member so the reply clears the quorum check and
    # reaches the insert, where the old FK would have refused it.
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    registry.set_membership("beta", "bob", "member", 0, "fixture", actor="test")
    service_ada = StateService(StateDatabase(str(path)), actor="driver:ada", driver_id="ada",
                               project_read_trusted=True)
    service_bob = StateService(StateDatabase(str(path)), actor="driver:bob", driver_id="bob",
                               project_read_trusted=True)
    note = service_ada.driver_inbox.send_driver_message(
        "post-migration-note", "note", "body", target_driver_id="bob", project_id="alpha")
    cited = note["deliveries"][0]["delivery_id"]
    reply = _reply(service_bob, cited, request_key="post-migration-reply")
    with service_bob.db.get_connection() as conn:
        row = conn.execute("SELECT reply_to_delivery_id FROM state_director_messages WHERE message_id=?",
                           (reply["result"]["message"]["message_id"],)).fetchone()
    assert row["reply_to_delivery_id"] == cited
