"""SQLite binding for the versioned director-messaging contract."""
from __future__ import annotations

from copy import deepcopy
from functools import wraps
from datetime import datetime, timezone
import json
import unicodedata
from uuid import UUID, uuid4

from core.director_messaging_protocol import DirectorMessageError, SCHEMA_ID, V3_SCHEMA_ID, ERROR_CODES
from core.state_driver_notes import _redact
from core.state_privacy import UntrustedDatabase, writer_only_read


def _message_errors(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except DirectorMessageError as exc:
            schema = self.reply_schema if exc.code in ERROR_CODES else V3_SCHEMA_ID
            raise DirectorMessageError(exc.code, schema) from exc
    return invoke


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
    delivery_mode TEXT NOT NULL DEFAULT 'transient'
        CHECK(delivery_mode IN ('transient','standing')),
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
        "subject", "body", "created_at", "reply_to_delivery_id", "delivery_mode")}


def _row_delivery(row):
    return {key: row[key] for key in (
        "delivery_id", "message_id", "target_project_id", "delivery_seq", "status", "version")}


def initialize(db) -> None:
    """Create the mailbox tables on a real handle (the leaf's own, or the one
    an untrusted store runs ``initialize_state_schema`` on before dropping it)."""
    with db.get_connection() as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute(
            "PRAGMA table_info(state_director_messages)")}
        if "delivery_mode" not in columns:
            conn.execute(
                "ALTER TABLE state_director_messages ADD COLUMN "
                "delivery_mode TEXT NOT NULL DEFAULT 'transient' "
                "CHECK(delivery_mode IN ('transient','standing'))")
        from core import director_messaging_quorum as quorum
        migrate_acks = "ack_mode" not in columns
        for name, definition in [
            ("sender_driver_id", "TEXT"),
            ("ack_mode", "TEXT NOT NULL DEFAULT 'at_least_n' CHECK(ack_mode IN ('at_least_n','broadcast'))"),
            ("ack_quorum", "INTEGER NOT NULL DEFAULT 1 CHECK(ack_quorum>=1)"),
        ]:
            if name not in columns:
                conn.execute("ALTER TABLE state_director_messages ADD COLUMN " + name + " " + definition)
        conn.executescript(quorum.SCHEMA)
        if migrate_acks:
            conn.execute("INSERT OR IGNORE INTO state_director_legacy_acks SELECT delivery_id FROM state_director_deliveries WHERE status IN ('acknowledged','resolved')")
        conn.commit()


class SQLiteDirectorMessaging:
    """Actor-bound provider sharing State's SQLite transaction/event boundary."""

    def __init__(self, store, actor: str, *, clock=None, id_factory=None, redactor=_redact,
                 project_read_trusted: bool = False, driver_id=None):
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be authenticated nonempty text")
        self.store = store
        self.actor = actor
        self.driver_id = driver_id
        self.v3 = bool(driver_id or actor.startswith("owner:"))
        self.reply_schema = SCHEMA_ID
        # Undeclared is UNTRUSTED: a provider rebuilt from an anonymous service's
        # store must not read the director inbox by staying silent.
        self.project_read_trusted = bool(project_read_trusted)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._redactor = redactor
        if not isinstance(store.db, UntrustedDatabase):
            initialize(store.db)

    def for_actor(self, actor):
        return type(self)(self.store, actor, clock=self._clock,
                          id_factory=self._id_factory, redactor=self._redactor,
                          project_read_trusted=self.project_read_trusted,
                          driver_id=self.driver_id if actor == self.actor else None)

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
        recorded_payload = row["payload_json"]
        # A v1 send record predates the default-expanded delivery mode.  Treat
        # that exact legacy payload as the same transient request while leaving
        # the durable audit row untouched.
        if recorded_payload != payload_json and operation == "send_director_message":
            legacy = json.loads(recorded_payload)
            current = json.loads(payload_json)
            if "delivery_mode" not in legacy and current.get("delivery_mode") == "transient":
                legacy["delivery_mode"] = "transient"
                recorded_payload = _canonical(legacy)
        if recorded_payload != payload_json:
            raise DirectorMessageError("idempotency_conflict")
        result = json.loads(row["result_json"])
        if operation == "send_director_message":
            result.get("message", {}).setdefault("delivery_mode", "transient")
        result["replayed"] = True
        return self._success(result)

    def _record(self, conn, scope, operation, request_key, payload_json, result):
        conn.execute(
            "INSERT INTO state_director_idempotency"
            "(actor,scope_project_id,operation,request_key,payload_json,result_json) VALUES(?,?,?,?,?,?)",
            (self.actor, scope, operation, request_key, payload_json, _canonical(result)))
        return self._success(deepcopy(result))

    def _set_reply_schema(self, protocol_version, extended=False):
        if protocol_version not in ("v2","v3"):
            _invalid()
        self.reply_schema = V3_SCHEMA_ID if protocol_version=="v3" or extended else SCHEMA_ID
        if protocol_version=="v3" or extended:
            self.v3 = True

    def _success(self, result):
        result = deepcopy(result)
        if self.reply_schema == SCHEMA_ID:
            if "message" in result:
                result["message"] = {k:v for k,v in result["message"].items()
                                     if k not in ("sender_driver_id","ack_mode","ack_quorum")}
            if "items" in result:
                result["items"] = [{"message":{k:v for k,v in i["message"].items()
                                    if k not in ("sender_driver_id","ack_mode","ack_quorum")},
                                   "delivery":i["delivery"]} for i in result["items"]]
            if "delivery" in result:
                result = {k:v for k,v in result.items() if k in ("delivery","replayed")}
        return {"schema": self.reply_schema, "result": result}

    def _acker(self, conn, project_id):
        from core.director_messaging_quorum import members
        if self.actor.startswith("owner:"):
            return "owner"
        if not self.driver_id or self.driver_id not in members(conn, project_id):
            raise DirectorMessageError("unauthorized", V3_SCHEMA_ID)
        row = conn.execute("SELECT status FROM drivers WHERE driver_id=?", (self.driver_id,)).fetchone()
        if row is None or row["status"] != "active":
            raise DirectorMessageError("unauthorized", V3_SCHEMA_ID)
        return self.driver_id

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

    @_message_errors
    def send_director_message(self, sender_project_id, director_identity, request_key,
                              subject, body, target_project_id=None, broadcast=False,
                              reply_to_delivery_id=None, delivery_mode="transient",
                              ack_mode=None, ack_quorum=None, protocol_version="v2"):
        self._set_reply_schema(protocol_version, ack_mode is not None or ack_quorum is not None)
        if ack_mode is not None or ack_quorum is not None:
            self.v3 = True
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
        if delivery_mode not in ("transient", "standing"):
            _invalid()
        targeted = not broadcast and target_project_id is not None and reply_to_delivery_id is None
        broadcasting = broadcast and target_project_id is None and reply_to_delivery_id is None
        replying = not broadcast and target_project_id is None and reply_to_delivery_id is not None
        if sum((targeted, broadcasting, replying)) != 1:
            _invalid()
        if targeted and target_project_id == sender_project_id:
            raise DirectorMessageError("use_driver_inbox", V3_SCHEMA_ID)
        payload = {
            "body": body, "broadcast": broadcast, "director_identity": director_identity,
            "reply_to_delivery_id": reply_to_delivery_id, "request_key": request_key,
            "sender_project_id": sender_project_id, "subject": subject,
            "target_project_id": target_project_id, "delivery_mode": delivery_mode,
        }
        if ack_mode is not None or ack_quorum is not None:
            self.v3 = True
            payload.update(ack_mode=ack_mode or "at_least_n", ack_quorum=ack_quorum if ack_quorum is not None else 1)
        ack_mode = "at_least_n" if ack_mode is None else ack_mode
        ack_quorum = 1 if ack_quorum is None else ack_quorum
        if ack_mode not in ("at_least_n","broadcast") or type(ack_quorum) is not int or ack_quorum<1:
            _invalid()
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

            from core.director_messaging_quorum import members
            if self.v3 and ack_mode == "at_least_n" and any(ack_quorum > len(members(conn,t)) for t in targets):
                raise DirectorMessageError("ack_quorum_unreachable", V3_SCHEMA_ID)
            message_id = self._new_id()
            thread_id = thread_id or message_id
            created_at = self._now()
            message = {
                "message_id": message_id, "thread_id": thread_id,
                "sender_project_id": sender_project_id, "director_identity": director_identity,
                "actor": self.actor, "subject": subject, "body": body,
                "created_at": created_at, "reply_to_delivery_id": reply_to_delivery_id,
                "delivery_mode": delivery_mode,
            }
            summary = self._redactor(subject + "\n" + body)[:320]
            conn.execute(
                "INSERT INTO state_director_messages"
                "(message_id,thread_id,sender_project_id,director_identity,actor,subject,body,"
                "created_at,reply_to_delivery_id,delivery_mode) VALUES(?,?,?,?,?,?,?,?,?,?)",
                tuple(message[key] for key in (
                    "message_id", "thread_id", "sender_project_id", "director_identity", "actor",
                    "subject", "body", "created_at", "reply_to_delivery_id", "delivery_mode")))
            conn.execute("UPDATE state_director_messages SET sender_driver_id=?,ack_mode=?,ack_quorum=? WHERE message_id=?",
                         (self.driver_id,ack_mode,ack_quorum,message_id))
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
                if self.v3 and ack_mode == "broadcast":
                    for member in members(conn,target):
                        conn.execute("INSERT INTO state_director_delivery_acks(delivery_id,driver_id,required) VALUES(?,?,1)", (delivery["delivery_id"],member))
                self.store._event(conn, target, None, "director_message_received", {
                    "message_id": message_id, "thread_id": thread_id,
                    "delivery_id": delivery["delivery_id"], "summary": summary})
                deliveries.append(delivery)
            if self.v3:
                message.update(sender_driver_id=self.driver_id,ack_mode=ack_mode,ack_quorum=ack_quorum)
            result = {"message": message, "deliveries": deliveries, "replayed": False}
            return self._record(conn, sender_project_id, operation, request_key, payload_json, result)

    @writer_only_read("list_director_messages")
    @_message_errors
    def list_director_messages(self, project_id, after=0, limit=100,
                               delivery_mode=None, statuses=None, needs_my_ack=False, protocol_version="v2", ack_mode=None):
        self._set_reply_schema(protocol_version, needs_my_ack or ack_mode is not None)
        if ack_mode not in (None,"at_least_n","broadcast"):
            _invalid()
        project_id = _db_id(project_id)
        after = _integer(after, 0)
        limit = _integer(limit, 1, 100)
        if delivery_mode is not None and delivery_mode not in ("transient", "standing"):
            _invalid()
        if statuses is not None:
            if (not isinstance(statuses, list) or not statuses
                    or any(not isinstance(status, str) for status in statuses)
                    or len(set(statuses)) != len(statuses)
                    or any(status not in ("unread", "acknowledged", "resolved")
                           for status in statuses)):
                _invalid()
        with self.store.transaction() as conn:
            self._project(conn, project_id)
            high = conn.execute(
                "SELECT COALESCE(MAX(delivery_seq),0) AS high "
                "FROM state_director_deliveries WHERE target_project_id=?",
                (project_id,)).fetchone()["high"]
            clauses = ["d.target_project_id=?", "d.delivery_seq>?", "d.delivery_seq<=?"]
            parameters = [project_id, after, high]
            if ack_mode is not None:
                clauses.append("m.ack_mode=?")
                parameters.append(ack_mode)
            if delivery_mode is not None:
                clauses.append("m.delivery_mode=?")
                parameters.append(delivery_mode)
            if statuses is not None:
                clauses.append("d.status IN (" + ",".join("?" for _ in statuses) + ")")
                parameters.extend(statuses)
            if needs_my_ack:
                self._acker(conn, project_id)
                if not self.v3:
                    raise DirectorMessageError("unauthorized")
                clauses.append("NOT EXISTS(SELECT 1 FROM state_director_legacy_acks l WHERE l.delivery_id=d.delivery_id)")
                clauses.append("d.status<>'resolved' AND NOT EXISTS(SELECT 1 FROM state_director_delivery_acks a WHERE a.delivery_id=d.delivery_id AND a.driver_id=? AND a.acked_at IS NOT NULL)")
                parameters.append(self.driver_id or "owner")
                clauses.append("(m.ack_mode='at_least_n' AND (SELECT COUNT(*) FROM state_director_delivery_acks a WHERE a.delivery_id=d.delivery_id AND a.acked_at IS NOT NULL)<m.ack_quorum OR m.ack_mode='broadcast' AND EXISTS(SELECT 1 FROM state_director_delivery_acks a WHERE a.delivery_id=d.delivery_id AND a.driver_id=? AND a.required=1 AND a.removed_at IS NULL AND a.acked_at IS NULL))")
                parameters.append(self.driver_id or "owner")
            where = " AND ".join(clauses)
            matched_total = conn.execute(
                "SELECT COUNT(*) AS count FROM state_director_deliveries d "
                "JOIN state_director_messages m ON m.message_id=d.message_id WHERE " + where,
                tuple(parameters)).fetchone()["count"]
            rows = conn.execute(
                "SELECT m.*,d.delivery_id,d.target_project_id,d.delivery_seq,d.status,d.version "
                "FROM state_director_deliveries d JOIN state_director_messages m ON m.message_id=d.message_id "
                "WHERE " + where + " ORDER BY d.delivery_seq LIMIT ?",
                (*parameters, limit)).fetchall()
            items = [{"message": _row_message(row), "delivery": _row_delivery(row)} for row in rows]
            if self.v3:
                from core.director_messaging_quorum import details
                for item in items:
                    item.update(details(conn,item["delivery"],self.driver_id or ("owner" if self.actor.startswith("owner:") else None)))
                    sender = conn.execute("SELECT sender_driver_id FROM state_director_messages WHERE message_id=?", (item["message"]["message_id"],)).fetchone()[0]
                    item["message"].update(sender_driver_id=sender,ack_mode=item["ack_mode"],ack_quorum=item["ack_quorum"])
            if needs_my_ack:
                items = [i for i in items if not i.get("acked_by_me") and i["delivery"]["status"]!="resolved"
                         and (i.get("ack_mode")=="broadcast" and self.driver_id in i.get("pending_drivers",[])
                              or i.get("ack_mode")=="at_least_n" and not i.get("quorum_met"))]
            has_more = matched_total > len(items)
            next_after = (items[-1]["delivery"]["delivery_seq"] if has_more
                          else max(after, high))
            return self._success({"project_id": project_id, "items": items,
                             "matched_total": matched_total, "has_more": has_more,
                             "next_after": next_after})

    def project_active_standing(self, project_id):
        """Return bounded, redacted active standing guidance without mutation."""
        result = self.list_director_messages(
            project_id, after=0, limit=8, delivery_mode="standing",
            statuses=["unread", "acknowledged"], protocol_version="v3" if self.v3 else "v2")["result"]
        if self.v3:
            transient = self.list_director_messages(project_id,limit=8,delivery_mode="transient", protocol_version="v3",
                                                   statuses=["unread","acknowledged"],needs_my_ack=True)["result"]
            selected = [i for i in transient["items"] if i.get("ack_mode")=="broadcast"]
            result["items"] = sorted(result["items"] + selected, key=lambda i:i["delivery"]["delivery_seq"])[:8]
            result["matched_total"] += len(selected)
        lines = []
        for item in result["items"]:
            message, delivery = item["message"], item["delivery"]
            lines.append(json.dumps({
                "message_id": message["message_id"],
                "thread_id": message["thread_id"],
                "delivery_id": delivery["delivery_id"],
                "sender_project_id": message["sender_project_id"],
                "delivery_seq": delivery["delivery_seq"],
                "status": delivery["status"],
                "version": delivery["version"],
                "subject": self._redactor(message["subject"]),
                "body_excerpt": self._redactor(message["body"])[:320],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        included = len(lines)
        while True:
            omitted = result["matched_total"] - included
            parts = lines[:included]
            if omitted:
                parts.append(f"[omitted_active_standing={omitted}]")
            projection = "\n".join(parts)
            if len(projection) <= 3000:
                return projection
            included -= 1

    @_message_errors
    def acknowledge_director_message(self, project_id, delivery_id, expected_version, request_key, protocol_version="v2"):
        self._set_reply_schema(protocol_version)
        return self._transition("acknowledge_director_message", project_id, delivery_id,
                                expected_version, request_key, "acknowledged")

    @_message_errors
    def resolve_director_message(self, project_id, delivery_id, expected_version, request_key, protocol_version="v2"):
        self._set_reply_schema(protocol_version)
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
            if self.v3:
                from core.director_messaging_quorum import details
                acker = self._acker(conn, project_id)
                prior = conn.execute("SELECT acked_at FROM state_director_delivery_acks WHERE delivery_id=? AND driver_id=?", (delivery_id,acker)).fetchone()
                if target_status == "acknowledged" and prior and prior["acked_at"]:
                    original = conn.execute("SELECT result_json FROM state_director_idempotency WHERE actor=? AND scope_project_id=? AND operation=? AND json_extract(payload_json,'$.delivery_id')=? ORDER BY rowid LIMIT 1",
                                            (self.actor,project_id,operation,delivery_id)).fetchone()
                    result = json.loads(original["result_json"]) if original else {"delivery":delivery}
                    result["replayed"] = True
                    return self._success(result)
                if delivery["version"] != expected_version:
                    raise DirectorMessageError("version_conflict", V3_SCHEMA_ID)
                if delivery["status"] == "resolved":
                    if target_status == "resolved":
                        return self._record(conn,project_id,operation,request_key,payload_json,{"delivery":delivery,"replayed":False,**details(conn,delivery,acker)})
                    raise DirectorMessageError("invalid_transition", V3_SCHEMA_ID)
                if target_status == "acknowledged":
                    conn.execute("INSERT INTO state_director_delivery_acks(delivery_id,driver_id,required,acked_at) VALUES(?,?,0,?) ON CONFLICT(delivery_id,driver_id) DO UPDATE SET acked_at=excluded.acked_at",
                                 (delivery_id,acker,self._now()))
                    meta = details(conn,delivery,acker)
                    mode = conn.execute("SELECT delivery_mode FROM state_director_messages WHERE message_id=?", (delivery["message_id"],)).fetchone()[0]
                    status = "resolved" if meta["quorum_met"] and mode=="transient" else "acknowledged"
                else:
                    status = "resolved"
                conn.execute("UPDATE state_director_deliveries SET status=?,version=version+1 WHERE delivery_id=? AND version=?", (status,delivery_id,expected_version))
                delivery.update(status=status,version=expected_version+1)
                self.store._event(conn,project_id,None,"director_message_acked" if target_status=="acknowledged" else "director_message_resolved",
                                  {"delivery_id":delivery_id,"driver_id":acker,"acks":details(conn,delivery,acker)["acks"]})
                if target_status == "acknowledged":
                    seq=conn.execute("SELECT MAX(seq) FROM state_events").fetchone()[0]
                    conn.execute("UPDATE state_director_delivery_acks SET ack_event_seq=? WHERE delivery_id=? AND driver_id=?", (seq,delivery_id,acker))
                return self._record(conn,project_id,operation,request_key,payload_json,
                                    {"delivery":delivery,"replayed":False,**details(conn,delivery,acker)})
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
