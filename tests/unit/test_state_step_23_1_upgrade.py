"""A database of before step 23.1 is upgraded once, through the current code.

Step 23.1's schema work (several bound groups per channel, unique live group names,
the confirmation arm columns, list items per group) runs inside each store's
once()-guarded setup (core.state_schema_setup). These tests build the four tables
as step 23 shipped them, replayed here as static DDL rather than from old code,
fill them with rows, open the file with a fresh DBManager and pin that every
migration ran exactly once, what it did to the rows, and that a second StateService
construction on the same handle runs no schema statement at all.
"""
import sqlite3

import pytest

from core.db_manager import DBManager
from core.state_service import StateService

SCHEMA_STATEMENT = ("CREATE", "ALTER", "DROP")

# The tables and indexes of step 23, as its stores created them.
STEP_23_DDL = """
CREATE TABLE state_owner_groups (
 channel TEXT NOT NULL CHECK(channel IN ('slack','wechat')),
 group_ref TEXT NOT NULL CHECK(group_ref<>''),
 label TEXT, mention_prefix TEXT NOT NULL, bound_at TEXT NOT NULL, revoked_at TEXT,
 PRIMARY KEY(channel, group_ref));
CREATE UNIQUE INDEX state_owner_groups_one_live ON state_owner_groups(channel)
 WHERE revoked_at IS NULL;
CREATE TABLE state_steward_list_items (
 item_id TEXT PRIMARY KEY,
 list TEXT NOT NULL CHECK(list IN ('shopping','todo')),
 text TEXT NOT NULL,
 added_by_role TEXT NOT NULL CHECK(added_by_role IN ('owner','member','steward')),
 added_by_ref TEXT, added_by_label TEXT,
 added_via TEXT NOT NULL CHECK(added_via IN ('group','owner_dm','dashboard')),
 added_at TEXT NOT NULL,
 done_at TEXT, done_by_role TEXT, done_by_ref TEXT, done_by_label TEXT,
 deleted_at TEXT);
CREATE INDEX state_steward_list_items_open ON state_steward_list_items(list, deleted_at, done_at);
CREATE TABLE state_steward_confirmations (
 code TEXT PRIMARY KEY, conn_id TEXT NOT NULL, raw_name TEXT NOT NULL, gw_name TEXT NOT NULL,
 args_json TEXT NOT NULL, args_sha256 TEXT NOT NULL,
 requester_kind TEXT NOT NULL CHECK(requester_kind IN ('owner','member')),
 channel TEXT NOT NULL, conversation_ref TEXT, sender_ref TEXT, requester_message_id TEXT NOT NULL,
 audience TEXT NOT NULL, message_text TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','confirmed','cancelled','expired','consumed','done','failed')),
 created_at TEXT NOT NULL, confirmed_at TEXT, confirm_message_ref TEXT, consumed_at TEXT, result TEXT);
CREATE INDEX state_steward_confirmations_lookup
 ON state_steward_confirmations(conn_id, raw_name, args_sha256, status);
CREATE TABLE state_group_messages (
 message_id TEXT PRIMARY KEY,
 channel TEXT NOT NULL, group_ref TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 kind TEXT NOT NULL,
 accepted INTEGER NOT NULL DEFAULT 0 CHECK(accepted IN (0,1)),
 sender_ref TEXT, sender_role TEXT CHECK(sender_role IS NULL OR sender_role IN ('owner','member')),
 channel_message_ref TEXT, body TEXT, original TEXT, withheld_reason TEXT,
 reply_to TEXT, answered_by TEXT, request_key TEXT, at TEXT NOT NULL, attachments_json TEXT);
CREATE UNIQUE INDEX state_group_messages_inbound
 ON state_group_messages(channel, group_ref, channel_message_ref) WHERE direction='in';
CREATE UNIQUE INDEX state_group_messages_reply_key
 ON state_group_messages(request_key) WHERE direction='out' AND request_key IS NOT NULL;
CREATE INDEX state_group_messages_window ON state_group_messages(channel, direction, at);
"""

# Each step 23.1 migration, by the one statement that carries it out on a step 23 file.
MIGRATIONS = {
    "drop the one-live-group index": "DROP INDEX IF EXISTS state_owner_groups_one_live",
    "unique live group name": "CREATE UNIQUE INDEX IF NOT EXISTS state_owner_groups_live_label",
    "confirmation armed_turn": "state_steward_confirmations ADD COLUMN armed_turn",
    "confirmation armed_at": "state_steward_confirmations ADD COLUMN armed_at",
    "confirmation armed_owner": "state_steward_confirmations ADD COLUMN armed_owner",
    "list item channel": "state_steward_list_items ADD COLUMN channel",
    "list item group_ref": "state_steward_list_items ADD COLUMN group_ref",
    "group message turn": "state_group_messages ADD COLUMN turn",
    "group message owner_notice": "state_group_messages ADD COLUMN owner_notice",
}

DAY = "2026-11-01T00:00:00+00:00"


def step_23_file(path, groups, items, confirmations):
    with sqlite3.connect(path) as conn:
        conn.executescript(STEP_23_DDL)
        conn.executemany("INSERT INTO state_owner_groups(channel,group_ref,label,mention_prefix,bound_at,"
                         "revoked_at) VALUES(?,?,?,?,?,?)", groups)
        conn.executemany("INSERT INTO state_steward_list_items(item_id,list,text,added_by_role,added_via,"
                         "added_at) VALUES(?,'shopping',?,'owner','dashboard',?)",
                         [(f"it{n}", text, DAY) for n, text in enumerate(items)])
        conn.executemany("INSERT INTO state_steward_confirmations(code,conn_id,raw_name,gw_name,args_json,"
                         "args_sha256,requester_kind,channel,requester_message_id,audience,message_text,"
                         "status,created_at) VALUES(?,'conn','raw','gw','{}','x','owner','wechat',?,'owner',"
                         "'t','pending',?)", [(f"c{n}", f"m{n}", DAY) for n in range(confirmations)])
    return str(path)


class Traced:
    """Every statement run on any sqlite3 connection opened while active."""

    def __init__(self, monkeypatch):
        self.statements = []
        original = sqlite3.connect

        def connect(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.set_trace_callback(lambda sql: self.statements.append(" ".join(sql.split())))
            return conn
        monkeypatch.setattr(sqlite3, "connect", connect)

    def schema_statements(self):
        return [s for s in self.statements
                if s.upper().startswith(SCHEMA_STATEMENT) or "TABLE_INFO" in s.upper()]


def upgrade(path, monkeypatch):
    """Open the file as the upgraded backend does; returns the first construction's statements."""
    db = DBManager(path)            # its own setup is not the State stores'
    traced = Traced(monkeypatch)
    StateService(db, None, actor="fixture", project_read_trusted=True)
    first = list(traced.statements)
    for kwargs in (dict(actor="fixture", project_read_trusted=True),
                   dict(actor="fixture", project_read_trusted=False),
                   dict(actor="driver:ada", project_read_trusted=True, driver_id="ada")):
        traced.statements.clear()
        StateService(db, None, **kwargs)
        assert traced.schema_statements() == [], (kwargs, traced.schema_statements()[:8])
    return first


def upgraded_rows(path):
    with sqlite3.connect(path) as conn:
        def columns(table):
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        assert {"armed_turn", "armed_at", "armed_owner"} <= columns("state_steward_confirmations")
        assert {"channel", "group_ref"} <= columns("state_steward_list_items")
        assert {"turn", "owner_notice"} <= columns("state_group_messages")
        index = conn.execute("SELECT sql FROM sqlite_master WHERE name='state_owner_groups_live_label'").fetchone()
        assert index and "UNIQUE" in index[0].upper() and "revoked_at IS NULL" in index[0], index
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='state_owner_groups_one_live'").fetchone() is None
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        return {
            "live": [r[0] for r in conn.execute(
                "SELECT label FROM state_owner_groups WHERE revoked_at IS NULL ORDER BY bound_at")],
            "revoked": [r[0] for r in conn.execute(
                "SELECT label FROM state_owner_groups WHERE revoked_at IS NOT NULL")],
            "items": conn.execute("SELECT channel, group_ref FROM state_steward_list_items ORDER BY item_id").fetchall(),
            "confirmations": conn.execute("SELECT count(*) FROM state_steward_confirmations").fetchone()[0],
        }


def assert_each_migration_ran_once(first):
    ran = {name: sum(needle in statement for statement in first) for name, needle in MIGRATIONS.items()}
    assert ran == dict.fromkeys(MIGRATIONS, 1), ran


def test_two_live_groups_sharing_a_name_on_two_channels(tmp_path, monkeypatch):
    path = step_23_file(tmp_path / "state.db", [
        ("slack", "S1", "家庭群", "@s", "2026-11-01T00:00:00+00:00", None),
        ("wechat", "W1", "家庭群", "@w", "2026-11-02T00:00:00+00:00", None),
        ("wechat", "W0", "家庭群", "@w", "2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00"),
    ], ["milk", "eggs"], 2)
    assert_each_migration_ran_once(upgrade(path, monkeypatch))
    rows = upgraded_rows(path)
    assert rows["live"] == ["家庭群", "家庭群2"], "the later group of a shared live name is renamed"
    assert rows["revoked"] == ["家庭群"], "a revoked group keeps its name"
    assert rows["items"] == [(None, None)] * 2, "two live groups: the older items stay unassigned"
    assert rows["confirmations"] == 2
    with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE state_owner_groups SET label='家庭群' WHERE group_ref='W1'")


def test_one_unnamed_live_group_takes_the_older_items(tmp_path, monkeypatch):
    path = step_23_file(tmp_path / "state.db", [
        ("wechat", "W9", None, "@w", "2026-11-02T00:00:00+00:00", None),
    ], ["rice", "tea", "salt"], 1)
    assert_each_migration_ran_once(upgrade(path, monkeypatch))
    rows = upgraded_rows(path)
    assert rows["live"] == ["家庭群"], "an unnamed live group gets the default name"
    assert rows["items"] == [("wechat", "W9")] * 3, "exactly one live group: the older items are its"
    assert rows["confirmations"] == 1
