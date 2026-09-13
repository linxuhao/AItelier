"""SQLite binding for the versioned director-messaging contract."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import unicodedata
from uuid import UUID, uuid4

from core.director_messaging_protocol import DirectorMessageError, SCHEMA_ID
from core.state_driver_notes import _redact


SCHEMA = """
CREATE TABLE IF NOT EXISTS state_director_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    sender_project_id TEXT NOT NULL,
    director_identity TEXT NOT NULL,
    actor TEXT NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reply_to_delivery_id TEXT,
    FOREIGN KEY(sender_project_id) REFERENCES state_projects(project_id),
    FOREIGN KEY(reply_to_delivery_id) REFERENCES state_director_deliveries(delivery_id)
);
CREATE TABLE IF NOT EXISTS state_director_deliveries (
    delivery_id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    target_project_id TEXT NOT NULL,
    delivery_seq INTEGER NOT NULL CHECK(delivery_seq > 0),
    status TEXT NOT NULL CHECK(status IN ('unread','acknowledged','resolved')),
    version INTEGER NOT NULL CHECK(version > 0),
    UNIQUE(message_id,target_project_id),
    UNIQUE(target_project_id,delivery_seq),
    FOREIGN KEY(message_id) REFERENCES state_director_messages(message_id),
    FOREIGN KEY(target_project_id) REFERENCES state_projects(project_id)
);
CREATE INDEX IF NOT EXISTS state_director_deliveries_inbox
ON state_director_deliveries(target_project_id,delivery_seq);
CREATE TABLE IF NOT EXISTS state_director_inbox_sequences (
    project_id TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL CHECK(next_seq > 0),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TABLE IF NOT EXISTS state_director_idempotency (
    actor TEXT NOT NULL,
    scope_project_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(actor,scope_project_id,operation,request_key),
    FOREIGN KEY(scope_project_id) REFERENCES state_projects(project_id)
);
"""


def _invalid():
    raise DirectorMessageError("invalid_request")


def _db_id(value):
    if not isinstance(value, str) or not 1 <= len(unicodedata.normalize("NFC", value)) <= 320:
        _invalid()
    return value


def _nfc(value, minimum, maximum):
    if not isinstance(value, str):
        _invalid()
    value = unicodedata.normalize("NFC", value)
    if not minimum <= len(value) <= maximum:
        _invalid()
    return value


def _integer(value, minimum, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _invalid()
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _success(result):
    return {"schema": SCHEMA_ID, "result": result}


def _row_message(row):
    return {key: row[key] for key in (
        "message_id", "thread_id", "sender_project_id", "director_identity", "actor",
        "subject", "body", "created_at", "reply_to_delivery_id")}


def _row_delivery(row):
    return {key: row[key] for key in (
        "delivery_id", "message_id", "target_project_id", "delivery_seq", "status", "version")}


class SQLiteDirectorMessaging:
    """Actor-bound provider sharing State's SQLite transaction/event boundary."""

    def __init__(self, store, actor: str, *, clock=None, id_factory=None, redactor=_redact):
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be authenticated nonempty text")
        self.store = store
        self.actor = actor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._redactor = redactor
        with self.store.db.get_connection() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            conn.commit()

    def for_actor(self, actor):
        return type(self)(self.store, actor, clock=self._clock,
                          id_factory=self._id_factory, redactor=self._redactor)

    def _new_id(self):
        value = self._id_factory()
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError("id_factory must return a UUIDv4 string") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise RuntimeError("id_factory must return a lowercase canonical UUIDv4 string")
        return value

    def _now(self):
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    @staticmethod
    def _project(conn, project_id):
        if conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (project_id,)).fetchone() is None:
            raise DirectorMessageError("unknown_project")

    def _dedupe(self, conn, scope, operation, request_key, payload_json):
        row = conn.execute(
            "SELECT payload_json,result_json FROM state_director_idempotency "
            "WHERE actor=? AND scope_project_id=? AND operation=? AND request_key=?",
            (self.actor, scope, operation, request_key)).fetchone()
        if row is None:
            return None
        if row["payload_json"] != payload_json:
            raise DirectorMessageError("idempotency_conflict")
        result = json.loads(row["result_json"])
        result["replayed"] = True
        return _success(result)

    def _record(self, conn, scope, operation, request_key, payload_json, result):
        conn.execute(
            "INSERT INTO state_director_idempotency"
            "(actor,scope_project_id,operation,request_key,payload_json,result_json) VALUES(?,?,?,?,?,?)",
            (self.actor, scope, operation, request_key, payload_json, _canonical(result)))
        return _success(deepcopy(result))

    @staticmethod
    def _next_delivery_seq(conn, project_id):
        row = conn.execute(
            "SELECT next_seq FROM state_director_inbox_sequences WHERE project_id=?",
            (project_id,)).fetchone()
        if row is None:
            seq = 1
            conn.execute("INSERT INTO state_director_inbox_sequences VALUES(?,?)", (project_id, 2))
        else:
            seq = row["next_seq"]
            conn.execute("UPDATE state_director_inbox_sequences SET next_seq=? WHERE project_id=?",
                         (seq + 1, project_id))
        return seq

    def send_director_message(self, sender_project_id, director_identity, request_key,
                              subject, body, target_project_id=None, broadcast=False,
                              reply_to_delivery_id=None):
        sender_project_id = _db_id(sender_project_id)
        director_identity = _nfc(director_identity, 1, 320)
        request_key = _nfc(request_key, 1, 320)
        subject = _nfc(subject, 1, 200)
        body = _nfc(body, 0, 8000)
        if type(broadcast) is not bool:
            _invalid()
        if target_project_id is not None:
            target_project_id = _db_id(target_project_id)
        if reply_to_delivery_id is not None:
            reply_to_delivery_id = _db_id(reply_to_delivery_id)
        targeted = not broadcast and target_project_id is not None and reply_to_delivery_id is None
        broadcasting = broadcast and target_project_id is None and reply_to_delivery_id is None
        replying = not broadcast and target_project_id is None and reply_to_delivery_id is not None
        if sum((targeted, broadcasting, replying)) != 1:
            _invalid()
        if targeted and target_project_id == sender_project_id:
            _invalid()
        payload = {
            "body": body, "broadcast": broadcast, "director_identity": director_identity,
            "reply_to_delivery_id": reply_to_delivery_id, "request_key": request_key,
            "sender_project_id": sender_project_id, "subject": subject,
            "target_project_id": target_project_id,
        }
        payload_json = _canonical(payload)
        operation = "send_director_message"
        with self.store.transaction(write=True) as conn:
            replay = self._dedupe(conn, sender_project_id, operation, request_key, payload_json)
            if replay is not None:
                return replay
            self._project(conn, sender_project_id)
            thread_id = None
            if targeted:
                self._project(conn, target_project_id)
                targets = [target_project_id]
            elif broadcasting:
                targets = [row["project_id"] for row in conn.execute(
                    "SELECT project_id FROM state_projects WHERE project_id<>? ORDER BY project_id",
                    (sender_project_id,))]
                if not targets:
                    raise DirectorMessageError("no_recipients")
                if len(targets) > 200:
                    raise DirectorMessageError("recipient_limit")
            else:
                cited = conn.execute(
                    "SELECT d.target_project_id,m.sender_project_id,m.thread_id "
                    "FROM state_director_deliveries d JOIN state_director_messages m ON m.message_id=d.message_id "
                    "WHERE d.delivery_id=?", (reply_to_delivery_id,)).fetchone()
                if cited is None or cited["target_project_id"] != sender_project_id:
                    raise DirectorMessageError("unknown_delivery")
                self._project(conn, cited["sender_project_id"])
                targets = [cited["sender_project_id"]]
                thread_id = cited["thread_id"]

            message_id = self._new_id()
            thread_id = thread_id or message_id
            created_at = self._now()
            message = {
                "message_id": message_id, "thread_id": thread_id,
                "sender_project_id": sender_project_id, "director_identity": director_identity,
                "actor": self.actor, "subject": subject, "body": body,
                "created_at": created_at, "reply_to_delivery_id": reply_to_delivery_id,
            }
            summary = self._redactor(subject + "\n" + body)[:320]
            conn.execute(
                "INSERT INTO state_director_messages VALUES(?,?,?,?,?,?,?,?,?)",
                tuple(message[key] for key in (
                    "message_id", "thread_id", "sender_project_id", "director_identity", "actor",
                    "subject", "body", "created_at", "reply_to_delivery_id")))
            deliveries = []
            for target in targets:
                delivery = {
                    "delivery_id": self._new_id(), "message_id": message_id,
                    "target_project_id": target,
                    "delivery_seq": self._next_delivery_seq(conn, target),
                    "status": "unread", "version": 1,
                }
                conn.execute("INSERT INTO state_director_deliveries VALUES(?,?,?,?,?,?)",
                             tuple(delivery.values()))
                self.store._event(conn, target, None, "director_message_received", {
                    "message_id": message_id, "thread_id": thread_id,
                    "delivery_id": delivery["delivery_id"], "summary": summary})
                deliveries.append(delivery)
            result = {"message": message, "deliveries": deliveries, "replayed": False}
            return self._record(conn, sender_project_id, operation, request_key, payload_json, result)

    def list_director_messages(self, project_id, after=0, limit=100):
        project_id = _db_id(project_id)
        after = _integer(after, 0)
        limit = _integer(limit, 1, 100)
        with self.store.transaction() as conn:
            self._project(conn, project_id)
            rows = conn.execute(
                "SELECT m.*,d.delivery_id,d.target_project_id,d.delivery_seq,d.status,d.version "
                "FROM state_director_deliveries d JOIN state_director_messages m ON m.message_id=d.message_id "
                "WHERE d.target_project_id=? AND d.delivery_seq>? ORDER BY d.delivery_seq LIMIT ?",
                (project_id, after, limit)).fetchall()
            items = [{"message": _row_message(row), "delivery": _row_delivery(row)} for row in rows]
            return _success({"project_id": project_id, "items": items,
                             "next_after": items[-1]["delivery"]["delivery_seq"] if items else after})

    def acknowledge_director_message(self, project_id, delivery_id, expected_version, request_key):
        return self._transition("acknowledge_director_message", project_id, delivery_id,
                                expected_version, request_key, "acknowledged")

    def resolve_director_message(self, project_id, delivery_id, expected_version, request_key):
        return self._transition("resolve_director_message", project_id, delivery_id,
                                expected_version, request_key, "resolved")

    def _transition(self, operation, project_id, delivery_id, expected_version,
                    request_key, target_status):
        project_id = _db_id(project_id)
        delivery_id = _db_id(delivery_id)
        expected_version = _integer(expected_version, 1)
        request_key = _nfc(request_key, 1, 320)
        payload_json = _canonical({
            "delivery_id": delivery_id, "expected_version": expected_version,
            "project_id": project_id, "request_key": request_key})
        with self.store.transaction(write=True) as conn:
            replay = self._dedupe(conn, project_id, operation, request_key, payload_json)
            if replay is not None:
                return replay
            self._project(conn, project_id)
            row = conn.execute("SELECT * FROM state_director_deliveries WHERE delivery_id=?",
                               (delivery_id,)).fetchone()
            if row is None or row["target_project_id"] != project_id:
                raise DirectorMessageError("unknown_delivery")
            delivery = _row_delivery(row)
            if delivery["status"] == target_status:
                return self._record(conn, project_id, operation, request_key, payload_json,
                                    {"delivery": delivery, "replayed": False})
            legal_source = "unread" if target_status == "acknowledged" else "acknowledged"
            if delivery["status"] != legal_source:
                raise DirectorMessageError("invalid_transition")
            if delivery["version"] != expected_version:
                raise DirectorMessageError("version_conflict")
            cursor = conn.execute(
                "UPDATE state_director_deliveries SET status=?,version=version+1 "
                "WHERE delivery_id=? AND target_project_id=? AND version=?",
                (target_status, delivery_id, project_id, expected_version))
            if cursor.rowcount != 1:
                raise DirectorMessageError("version_conflict")
            delivery["status"] = target_status
            delivery["version"] += 1
            return self._record(conn, project_id, operation, request_key, payload_json,
                                {"delivery": delivery, "replayed": False})
