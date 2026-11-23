"""Step 24.2b (design-steward 7.8.12a "上传 暂存", "消息带附件"): the schema the dashboard's
media needs, created on service init - the private table `state_conversation_uploads` and the
additive `attachments_json` column of the inbox's message rows. Rows written before the column
read exactly as they did."""
from __future__ import annotations

import sqlite3

import pytest

from core.drivers import DriverRegistry
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_privacy import PRIVATE_STATE_TABLES
from core.state_service import StateService
from tests.support import state_template

UPLOAD_COLUMNS = ["upload_id", "project_id", "user", "kind", "mime", "size", "name", "state", "refused",
                  "message_id", "store", "ref", "pending_json", "created_at"]


def columns(path, table):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


@pytest.fixture
def world(tmp_path, state_db_copy):
    path = str(tmp_path / "state.sqlite")
    db = StateDatabase(state_db_copy(state_template.state_registry, tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "Alpha")
    registry = DriverRegistry(db, "media-schema-pepper-not-a-secret")
    registry.register("ada", "ada", actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    return {"db": db, "path": path,
            "ada": StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True),
            "user": StateService(db, actor="owner:user@example.test", project_read_trusted=True)}


def test_a_fresh_database_gets_the_uploads_table_on_service_init(tmp_path):
    path = str(tmp_path / "fresh.sqlite")
    StateService(StateDatabase(path), actor="fixture", project_read_trusted=True)
    assert columns(path, "state_conversation_uploads") == UPLOAD_COLUMNS
    assert "state_conversation_uploads" in PRIVATE_STATE_TABLES
    conn = sqlite3.connect(path)
    try:
        insert = ("INSERT INTO state_conversation_uploads(upload_id,project_id,user,kind,mime,size,name,state,"
                  "created_at) VALUES(?,?,?,?,?,?,?,?,?)")
        conn.execute(insert, ("u_0000000000000001", "alpha", "owner", "image", "image/png", 1, "a.png",
                              "staged", "2026-11-23T00:00:00Z"))
        for bad in (("u_2", "alpha", "owner", "video", "image/png", 1, "a", "staged", "t"),
                    ("u_3", "alpha", "owner", "image", "image/png", 1, "a", "lost", "t")):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(insert, bad)
    finally:
        conn.close()


def test_the_attachments_column_is_added_and_old_rows_read_unchanged(world):
    assert "attachments_json" in columns(world["path"], "driver_inbox_messages")
    execute(world["ada"], "send_user_message", {"request_key": "d1", "project_id": "alpha", "subject": "Progress",
                                                "body": "half done"}, allow_write=True)
    execute(world["user"], "send_user_conversation_message", {"request_key": "u1", "project_id": "alpha",
                                                              "body": "look at a"}, allow_write=True)
    before_inbox = execute(world["ada"], "list_driver_messages", {})
    before_conversation = execute(world["user"], "list_user_conversation", {"project_id": "alpha"})
    assert before_inbox["messages"] and before_conversation["messages"]
    for message in before_inbox["messages"] + before_conversation["messages"]:
        assert "attachments" not in message and "attachments_json" not in message
    # The database as the code before this column left it: no such column.
    conn = sqlite3.connect(world["path"])
    conn.execute("ALTER TABLE driver_inbox_messages DROP COLUMN attachments_json")
    conn.commit()
    conn.close()
    assert "attachments_json" not in columns(world["path"], "driver_inbox_messages")
    # The upgraded code's next process: a new handle runs the schema setup again
    # (core.state_schema_setup.once runs it once per handle and file).
    db = StateDatabase(world["path"])
    ada = StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=True)
    user = StateService(db, actor="owner:user@example.test", project_read_trusted=True)
    assert "attachments_json" in columns(world["path"], "driver_inbox_messages")
    conn = sqlite3.connect(world["path"])
    try:
        assert {r[0] for r in conn.execute("SELECT attachments_json FROM driver_inbox_messages")} == {None}
    finally:
        conn.close()
    assert execute(ada, "list_driver_messages", {}) == before_inbox
    assert execute(user, "list_user_conversation", {"project_id": "alpha"}) == before_conversation
