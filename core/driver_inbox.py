"""Private driver deliveries. State project mailboxes remain separate."""
from __future__ import annotations
import asyncio
import base64
import json
import re
import time
from uuid import uuid4
from core.drivers import recipient_presence_block, touch_driver
from core.state_graph import StateGraphError, now
from core.state_privacy import UntrustedDatabase, writer_only_read
from core.state_schema_migration import add_column_if_missing

SCHEMA = """
CREATE TABLE IF NOT EXISTS driver_inbox_messages (
 message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, sender_driver_id TEXT,
 project_id TEXT, kind TEXT NOT NULL CHECK(kind IN ('note','request','review_request','handoff_offer','handoff_reply','lease_notice','takeover_notice','subagent_orphaned')), delivery_mode TEXT NOT NULL,
 subject TEXT NOT NULL, body TEXT NOT NULL, refs_json TEXT NOT NULL,
 reply_to_message_id TEXT, created_at TEXT NOT NULL,
 audience TEXT NOT NULL DEFAULT 'driver' CHECK(audience IN ('driver','user')),
 channel TEXT CHECK(channel IN ('user')),
 CHECK(delivery_mode IN ('transient','standing')));
CREATE TABLE IF NOT EXISTS driver_inbox_deliveries (
 delivery_id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES driver_inbox_messages,
 target_driver_id TEXT NOT NULL REFERENCES drivers, seq INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('unread','acknowledged','resolved')),
 version INTEGER NOT NULL CHECK(version>0),
 UNIQUE(message_id,target_driver_id), UNIQUE(target_driver_id,seq));
CREATE TABLE IF NOT EXISTS driver_inbox_requests (
 actor TEXT NOT NULL, operation TEXT NOT NULL, request_key TEXT NOT NULL,
 payload_json TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(actor,operation,request_key));
CREATE TABLE IF NOT EXISTS driver_user_conversation_reads (
 project_id TEXT PRIMARY KEY,
 read_created_at TEXT NOT NULL, read_message_id TEXT,
 updated_at TEXT NOT NULL);
"""
KINDS = {"note", "request", "review_request", "handoff_offer", "handoff_reply"}
SYSTEM_KINDS = {"lease_notice", "takeover_notice", "subagent_orphaned", "handoff_offer"}

# The user's conversation: ONE thread per project holding BOTH directions.
# A driver -> user message is recognised by `audience='user'`; a user -> driver
# message is a driver-addressed row (it is delivered to a driver's inbox) marked
# `channel='user'`. `audience`/`channel` are additive columns, so a row written
# before either existed still reads back through its own half.
CONVERSATION = "(audience='user' OR channel='user')"
# A segment id is a short token a reply can cite: letters, digits, and . _ : -.
_SEGMENT_ID = re.compile(r"[A-Za-z0-9._:-]+")


def encode_conversation_cursor(created_at, message_id):
    """Opaque keyset position: the (created_at, message_id) of the last row read.

    Encoding, not signing: the pair names a position in a total order, so a
    hand-edited cursor can only move the window - it can never widen what the
    reader is allowed to see, which the action and project verdicts decide.
    """
    return base64.urlsafe_b64encode(
        json.dumps([created_at, message_id], separators=(",", ":")).encode("utf-8")).decode("ascii")


def decode_conversation_cursor(cursor):
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1000:
        raise StateGraphError("invalid_cursor")
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except Exception:
        raise StateGraphError("invalid_cursor") from None
    if (not isinstance(payload, list) or len(payload) != 2
            or not all(isinstance(part, str) and part for part in payload)):
        raise StateGraphError("invalid_cursor")
    return payload[0], payload[1]


class DriverInbox:
    def __init__(self, store, actor, driver_id=None):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            with store.db.get_connection() as conn:
                conn.executescript(SCHEMA)
                # Additive (driver -> user messages): every existing row is a
                # driver-addressed message, which is what 'driver' means.
                add_column_if_missing(conn, "driver_inbox_messages", "audience",
                                      "TEXT NOT NULL DEFAULT 'driver' CHECK(audience IN ('driver','user'))")
                # Additive (the user -> driver half of one conversation). Every
                # existing row keeps channel NULL: a driver-addressed message
                # that no human wrote is not part of the user's thread, and the
                # rows the driver sent the user are recognised by `audience`.
                add_column_if_missing(conn, "driver_inbox_messages", "channel",
                                      "TEXT CHECK(channel IN ('user'))")
                # Additive (tagged segments). NULL means the message was written
                # as one body, which is what every row before this column was.
                add_column_if_missing(conn, "driver_inbox_messages", "segments_json", "TEXT")
                # Additive (the questions a reply answers). NULL means the reply
                # cites none, which is every row written before this column.
                add_column_if_missing(conn, "driver_inbox_messages", "answers_json", "TEXT")
                conn.commit()

    @staticmethod
    def _segments(segments):
        """Normalise the tagged blocks, refusing anything that would not render.

        Bounded on purpose: this is a glanceable summary, not a second body. The
        kind is closed to info/question so a later tag cannot appear in stored
        rows without someone deciding what it means to the reader.

        Each block carries a stable id, so a reply can cite the exact question
        it answers. A caller-supplied id is kept; a block without one gets a
        fresh 12-hex id. A duplicate id inside one message is refused, because a
        citation would then name two blocks at once. Nothing is stored on refusal.

        Returns `(sent, stored)`: `sent` is what the caller wrote (blocks without
        an id stay without one, so an idempotent replay compares equal) and
        `stored` is what the row keeps, with every id filled in.
        """
        if segments is None:
            return [], []
        if not isinstance(segments, list) or len(segments) > 20:
            raise StateGraphError("invalid_segments")
        sent, stored, total, seen = [], [], 0, set()
        for item in segments:
            # The write surface may hand these over either as plain dicts or as
            # the validated model; both are the same fields.
            if hasattr(item, "model_dump"):
                item = item.model_dump()
            if not isinstance(item, dict):
                raise StateGraphError("invalid_segments")
            kind, text, sid = item.get("kind"), item.get("text"), item.get("id")
            if kind not in ("info", "question") or type(text) is not str or not 1 <= len(text) <= 2000:
                raise StateGraphError("invalid_segments")
            if sid is not None and (type(sid) is not str or not 1 <= len(sid) <= 64
                                    or not _SEGMENT_ID.fullmatch(sid)):
                raise StateGraphError("invalid_segments")
            total += len(text)
            sent.append({"kind": kind, "text": text, **({} if sid is None else {"id": sid})})
            stored.append({"id": sid if sid is not None else uuid4().hex[:12], "kind": kind, "text": text})
        if total > 8000:
            raise StateGraphError("invalid_segments")
        # Checked after ids are filled in, so a generated id can never collide
        # with a caller id and a duplicate caller id is refused as well.
        for block in stored:
            if block["id"] in seen:
                raise StateGraphError("invalid_segments")
            seen.add(block["id"])
        return sent, stored

    @staticmethod
    def _answers(answers):
        """Normalise the questions a reply cites, or refuse the whole reply.

        Shape only here; whether each ref names a real question in this project
        is checked against the rows by `_resolve_answers`, on the write's own
        connection, so a refusal can never leave a half-written reply behind.
        """
        if answers is None:
            return []
        if not isinstance(answers, list) or len(answers) > 20:
            raise StateGraphError("invalid_answers")
        normalised = []
        for item in answers:
            if hasattr(item, "model_dump"):
                item = item.model_dump()
            if not isinstance(item, dict):
                raise StateGraphError("invalid_answers")
            mid, sid = item.get("message_id"), item.get("segment_id")
            if (type(mid) is not str or not 1 <= len(mid) <= 200
                    or type(sid) is not str or not 1 <= len(sid) <= 64):
                raise StateGraphError("invalid_answers")
            normalised.append({"message_id": mid, "segment_id": sid})
        return normalised

    @staticmethod
    def _resolve_answers(conn, project_id, answers):
        """Check every cited question against the rows, and return the stored refs.

        Refuses with a typed error and writes nothing if a ref names a message
        outside this project's conversation, a segment that does not exist in
        that message, a segment whose kind is not `question`, or the same ref
        twice. Only a DRIVER message carries segments (the driver writes them
        with `send_user_message`, so its row is `audience='user'`), which is why
        the cited row must be the driver half of the conversation.
        """
        seen = set()
        for ref in answers:
            key = (ref["message_id"], ref["segment_id"])
            if key in seen:
                raise StateGraphError("invalid_answers: the same question is cited twice")
            seen.add(key)
            row = conn.execute(
                "SELECT segments_json FROM driver_inbox_messages "
                f"WHERE message_id=? AND project_id=? AND {CONVERSATION} AND audience='user'",
                (ref["message_id"], project_id)).fetchone()
            if row is None:
                raise StateGraphError("unknown_answer_message: answers must name a driver message in this project's conversation")
            segments = json.loads(row["segments_json"]) if row["segments_json"] else []
            match = next((s for s in segments if s.get("id") == ref["segment_id"]), None)
            if match is None:
                raise StateGraphError("unknown_answer_segment: answers must name a segment that the message carries")
            if match.get("kind") != "question":
                raise StateGraphError("answer_not_a_question: answers may only cite a question segment")
        return answers

    def _authorize(self, driver_id, *, write=False, break_glass_reason=None):
        if not self.store.project_read_trusted:
            raise StateGraphError("not_inbox_recipient")
        owner = self.actor.startswith("owner:")
        with self.store.transaction() as conn:
            caller = conn.execute("SELECT is_admin,status FROM drivers WHERE driver_id=?", (self.driver_id,)).fetchone()
        if self.driver_id == driver_id and self.driver_id and caller and caller["status"]=="active":
            return
        if owner and (not write or break_glass_reason):
            return
        if write and break_glass_reason and caller and caller["status"]=="active" and caller["is_admin"]:
            return
        raise StateGraphError("not_inbox_recipient")

    @staticmethod
    def _driver(conn, driver_id):
        row = conn.execute("SELECT driver_id FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
        if row is None:
            raise StateGraphError("unknown_driver: no driver with this driver_id is registered")

    def _refs(self, conn, refs, project_id):
        """Resolve every refs key to an existing record, or refuse the message."""
        for key, value in refs.items():
            tables = {"attempt_id": ("state_attempts","attempt_id"), "issue_id":("state_issues","issue_id"),
                      "claim_id":("state_node_claims","claim_id"), "subagent_id":("driver_subagents","subagent_id")}
            if key == "node_key" and project_id:
                self.store._node(conn, project_id, value)
            elif key in tables:
                if not isinstance(value,str) or not value:
                    raise StateGraphError(f"invalid_refs: refs.{key} must be a non-empty string id")
                table, column = tables[key]
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    raise StateGraphError(f"invalid_refs: refs.{key} cannot be resolved because the {table} table does not exist")
                if not conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (value,)).fetchone():
                    raise StateGraphError(f"invalid_refs: refs.{key} names no existing record in {table}")
            elif key == "node_key":
                raise StateGraphError("invalid_refs: refs.node_key needs project_id; set project_id or drop node_key")
            else:
                raise StateGraphError("invalid_refs: refs key " + repr(key) + " is not allowed; use one of "
                                      "node_key, attempt_id, issue_id, claim_id, subagent_id")

    def _replay(self, conn, operation, request_key, payload):
        row = conn.execute("SELECT payload_json,result_json FROM driver_inbox_requests WHERE actor=? AND operation=? AND request_key=?",
                           (self.actor, operation, request_key)).fetchone()
        if row:
            if row["payload_json"] != payload:
                raise StateGraphError("idempotency_conflict: request_key was already used with a different payload; use a new request_key")
            return json.loads(row["result_json"])

    def _record(self, conn, operation, request_key, payload, result):
        conn.execute("INSERT INTO driver_inbox_requests VALUES(?,?,?,?,?)",
                     (self.actor, operation, request_key, payload, json.dumps(result)))
        return result

    def _cited_inbox_message(self, conn, reply_to_delivery_id):
        """The message behind a delivery addressed to THE CALLING driver, or None.

        Only the caller's own deliveries resolve: an id addressed to another
        driver, an id in no driver inbox, and a project-inbox delivery
        (state_director_deliveries) all come back None, so the caller refuses
        with unknown_delivery and never reaches the project reply path.
        """
        if type(reply_to_delivery_id) is not str or not reply_to_delivery_id or len(reply_to_delivery_id) > 200:
            return None
        row = conn.execute(
            "SELECT d.message_id FROM driver_inbox_deliveries d "
            "WHERE d.delivery_id=? AND d.target_driver_id=?",
            (reply_to_delivery_id, self.driver_id)).fetchone()
        return None if row is None else row["message_id"]

    def send_driver_message(self, request_key, subject, body, target_driver_id=None,
                            project_members=None, project_id=None, kind="note",
                            delivery_mode="transient", refs=None, reply_to_message_id=None,
                            reply_to_delivery_id=None, _system=False):
        if reply_to_delivery_id is not None and reply_to_message_id is not None:
            raise StateGraphError("invalid_request: reply_to_delivery_id and reply_to_message_id are mutually "
                                  "exclusive; cite the delivery you hold or the message id, not both")
        if type(request_key) is not str or not request_key or len(request_key)>320:
            raise StateGraphError("invalid_request: request_key must be a non-empty string of at most 320 characters")
        if type(subject) is not str or not 1 <= len(subject) <= 200:
            raise StateGraphError("invalid_request: subject must be a non-empty string of at most 200 characters")
        if type(body) is not str or len(body)>8000:
            raise StateGraphError("invalid_request: body must be a string of at most 8000 characters")
        if bool(target_driver_id) and bool(project_members):
            raise StateGraphError("invalid_request: target_driver_id and project_members are mutually exclusive; set exactly one")
        if not target_driver_id and not project_members:
            raise StateGraphError("invalid_request: one of target_driver_id or project_members is required; set exactly one")
        if delivery_mode not in ("transient","standing"):
            raise StateGraphError("invalid_request: delivery_mode must be 'transient' or 'standing'")
        human_owner = self.project_read_trusted and self.actor.startswith("owner:")
        if kind not in (SYSTEM_KINDS if _system else KINDS):
            raise StateGraphError("invalid_request: kind must be one of " + ", ".join(sorted(SYSTEM_KINDS if _system else KINDS)))
        if not _system and not self.driver_id and not human_owner:
            raise StateGraphError("invalid_request: sending requires a driver identity or an owner actor")
        refs = {} if refs is None else refs
        if not isinstance(refs, dict):
            raise StateGraphError("invalid_refs: refs must be an object mapping allowed keys to ids")
        payload = [subject, body, target_driver_id, project_members, project_id, kind, delivery_mode, refs, reply_to_message_id, None if _system else self.driver_id]
        # The delivery selector is appended only when it is set, so the payload of
        # every request written before it existed still replays byte-identically.
        if reply_to_delivery_id is not None:
            payload.append(reply_to_delivery_id)
        payload = json.dumps(payload, sort_keys=True)
        with self.store.transaction(write=True) as conn:
            replay = self._replay(conn, "send", request_key, payload)
            if replay is not None:
                return replay
            if not _system and self.driver_id:
                self._driver(conn, self.driver_id)
                if conn.execute("SELECT status FROM drivers WHERE driver_id=?", (self.driver_id,)).fetchone()[0]!="active":
                    raise StateGraphError("inactive_driver: the sending driver is not active; only active drivers can send")
            if project_members:
                self.store._project(conn, project_members)
                targets = [r["driver_id"] for r in conn.execute(
                    "SELECT p.driver_id FROM project_drivers p JOIN drivers d USING(driver_id) WHERE p.project_id=? AND p.status='member' AND p.driver_id<>? ORDER BY p.driver_id",
                    (project_members, self.driver_id or ""))]
            else:
                targets = [target_driver_id]
            if not targets:
                raise StateGraphError("no_recipients: project_members resolved to no active member other than the sender; name a project with at least one other active member")
            if len(targets)>200:
                raise StateGraphError(f"no_recipients: project_members resolved to {len(targets)} recipients, above the limit of 200 per message")
            for target in targets:
                self._driver(conn, target)
            if project_id:
                self.store._project(conn, project_id)
            self._refs(conn, refs, project_id)
            if reply_to_delivery_id is not None:
                # Resolve the caller's own delivery to its message, then take the
                # existing reply_to_message_id path unchanged (same validation, same
                # thread copy). A delivery that is not the caller's refuses here and
                # never falls back to the project reply path.
                cited_message = self._cited_inbox_message(conn, reply_to_delivery_id)
                if cited_message is None:
                    raise StateGraphError("unknown_delivery: reply_to_delivery_id must name a delivery addressed "
                                          "to this driver")
                reply_to_message_id = cited_message
            thread = None
            if reply_to_message_id:
                prior = conn.execute("SELECT m.thread_id FROM driver_inbox_messages m JOIN driver_inbox_deliveries d USING(message_id) WHERE m.message_id=? AND d.target_driver_id=?",
                                     (reply_to_message_id, self.driver_id)).fetchone()
                if prior is None:
                    raise StateGraphError("unknown_message: reply_to_message_id must name a message delivered to this driver")
                thread = prior["thread_id"]
            mid = str(uuid4())
            conn.execute("INSERT INTO driver_inbox_messages(message_id,thread_id,sender_driver_id,project_id,"
                         "kind,delivery_mode,subject,body,refs_json,reply_to_message_id,created_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (mid,thread or mid,None if _system else self.driver_id,project_id,kind,delivery_mode,subject,body,json.dumps(refs),reply_to_message_id,now()))
            deliveries=[]
            for target in targets:
                seq=conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM driver_inbox_deliveries WHERE target_driver_id=?", (target,)).fetchone()[0]
                did=str(uuid4())
                conn.execute("INSERT INTO driver_inbox_deliveries VALUES(?,?,?,?,?,?)", (did,mid,target,seq,"unread",1))
                deliveries.append({"delivery_id":did,"target_driver_id":target,"seq":seq,"status":"unread","version":1})
            result=self._record(conn,"send",request_key,payload,{"message_id":mid,"deliveries":deliveries})
            result=self._presence(conn,result,_system,fan_out=bool(project_members))
        from core.state_changes import notify
        notify(self.store.db.db_path)
        return result

    @staticmethod
    def _presence(conn, result, system, fan_out):
        """Add the recipients' presence AFTER the idempotent record was written, so the
        record (and therefore every replay) is byte-identical to before."""
        if system:
            return result
        return {**result, **recipient_presence_block(
            conn, [d["target_driver_id"] for d in result["deliveries"]], fan_out=fan_out)}

    @staticmethod
    def _conversation_message(conn, project_id, message_id):
        """One row of THIS project's user conversation, or None."""
        if not isinstance(message_id, str) or not message_id or len(message_id) > 200:
            raise StateGraphError("invalid_request")
        row = conn.execute(
            "SELECT message_id,thread_id,sender_driver_id,audience,channel FROM driver_inbox_messages "
            f"WHERE message_id=? AND project_id=? AND {CONVERSATION}",
            (message_id, project_id)).fetchone()
        return None if row is None else dict(row)

    def send_user_message(self, request_key, subject, body, project_id, refs=None,
                          reply_to_message_id=None, segments=None):
        """Record one message from the calling driver to the project's user.

        PRESENTATION, never authority: this writes one message row with NO
        delivery (no driver is addressed) and touches nothing else. It is not
        evidence, it answers no checkpoint, and it cannot change a node.

        Idempotent per (actor, request_key): the same payload returns the same
        durable message identity, a different one under the same key is refused
        with idempotency_conflict. The row is what survives a backend restart.

        `reply_to_message_id` answers one of the USER's messages in this project;
        it is persisted on the row and joins that message's thread. It changes
        nothing about the message's standing: a reply is no more authoritative
        than the message it answers.
        """
        if type(request_key) is not str or not request_key or len(request_key)>320:
            raise StateGraphError("invalid_request")
        if type(subject) is not str or not 1 <= len(subject) <= 200 or type(body) is not str or len(body)>8000:
            raise StateGraphError("invalid_request")
        if not self.driver_id:
            raise StateGraphError("driver_identity_required")
        refs = {} if refs is None else refs
        if not isinstance(refs, dict):
            raise StateGraphError("invalid_refs")
        # `sent` is what the caller wrote (a block may carry no id); `segments`
        # is what gets stored, with every block id filled in. The replay
        # comparison uses `sent`, so a generated id never makes a replay look
        # like a different request. Segments and the reply link are appended only
        # when they are set, so every request written before either existed
        # replays byte-identically.
        sent, segments = self._segments(segments)
        payload = [subject, body, project_id, refs, self.driver_id]
        if reply_to_message_id is not None:
            payload.append(reply_to_message_id)
        if sent:
            payload.append(sent)
        payload = json.dumps(payload, sort_keys=True)
        with self.store.transaction(write=True) as conn:
            replay = self._replay(conn, "send_user", request_key, payload)
            if replay is not None:
                return replay
            self._driver(conn, self.driver_id)
            if conn.execute("SELECT status FROM drivers WHERE driver_id=?", (self.driver_id,)).fetchone()[0]!="active":
                raise StateGraphError("inactive_driver")
            self.store._project(conn, project_id)
            self._refs(conn, refs, project_id)
            parent = None
            if reply_to_message_id is not None:
                parent = self._conversation_message(conn, project_id, reply_to_message_id)
                if parent is None:
                    raise StateGraphError("unknown_message")
                if parent["audience"] != "driver" or parent["channel"] != "user":
                    raise StateGraphError("invalid_reply_target")
            mid, created = str(uuid4()), now()
            conn.execute("INSERT INTO driver_inbox_messages(message_id,thread_id,sender_driver_id,project_id,"
                         "kind,delivery_mode,subject,body,refs_json,reply_to_message_id,created_at,audience,"
                         "segments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (mid, parent["thread_id"] if parent else mid, self.driver_id, project_id, "note",
                          "transient", subject, body, json.dumps(refs), reply_to_message_id, created, "user",
                          json.dumps(segments) if segments else None))
            return self._record(conn, "send_user", request_key, payload,
                                {"message_id": mid, "project_id": project_id,
                                 "sender_driver_id": self.driver_id, "created_at": created})

    def send_user_conversation_message(self, request_key, project_id, body, subject=None,
                                       reply_to_message_id=None, target_driver_id=None, answers=None):
        """Record one message from the project's USER to its driver.

        The mirror of `send_user_message`, and equally PRESENTATION: this writes
        one message row plus the inbox deliveries that make it visible to the
        addressed driver. It is not evidence, it answers no checkpoint, it
        cannot verify a node, and it needs no driver to be online - the row and
        its delivery wait for the driver.

        `reply_to_message_id` must name a message the driver sent this project's
        user. The link is stored ON THE ROW, so it survives the request and is
        visible to everyone who reads the message back. It also decides the
        recipient (whoever wrote the message being answered) unless a target is
        named explicitly.

        `answers` names the driver's question blocks this reply answers, each as
        {message_id, segment_id}. Every ref is checked against the rows in this
        project's conversation before anything is written: a ref to another
        project, a missing block, a block that is not a question, or a repeated
        ref refuses the whole reply with nothing stored. The cited messages are
        not modified; the link lives on this reply's row.

        Idempotent per (actor, request_key), like every write here.
        """
        if type(request_key) is not str or not request_key or len(request_key) > 320:
            raise StateGraphError("invalid_request")
        subject = "Message from the user" if subject is None else subject
        if (type(subject) is not str or not 1 <= len(subject) <= 200
                or type(body) is not str or len(body) > 8000):
            raise StateGraphError("invalid_request")
        if self.driver_id or not self.project_read_trusted or not self.actor.startswith("owner:"):
            raise StateGraphError("user_identity_required")
        answers = self._answers(answers)
        # The answers are appended only when there are some, so the payload of
        # every request written before they existed still replays byte-identically.
        payload = [subject, body, project_id, reply_to_message_id, target_driver_id]
        if answers:
            payload.append(answers)
        payload = json.dumps(payload, sort_keys=True)
        with self.store.transaction(write=True) as conn:
            replay = self._replay(conn, "send_user_conversation", request_key, payload)
            if replay is not None:
                return replay
            self.store._project(conn, project_id)
            if answers:
                self._resolve_answers(conn, project_id, answers)
            parent = None
            if reply_to_message_id is not None:
                parent = self._conversation_message(conn, project_id, reply_to_message_id)
                if parent is None:
                    raise StateGraphError("unknown_message")
                if parent["audience"] != "user" or not parent["sender_driver_id"]:
                    raise StateGraphError("invalid_reply_target")
            if target_driver_id is not None:
                targets = [target_driver_id]
            elif parent is not None:
                targets = [parent["sender_driver_id"]]
            else:
                targets = [r["driver_id"] for r in conn.execute(
                    "SELECT p.driver_id FROM project_drivers p JOIN drivers d USING(driver_id) "
                    "WHERE p.project_id=? AND p.status='member' ORDER BY p.driver_id", (project_id,))]
            if not targets or len(targets) > 200:
                raise StateGraphError("no_recipients")
            for target in targets:
                self._driver(conn, target)
            mid, created = str(uuid4()), now()
            conn.execute("INSERT INTO driver_inbox_messages(message_id,thread_id,sender_driver_id,project_id,"
                         "kind,delivery_mode,subject,body,refs_json,reply_to_message_id,created_at,audience,channel,"
                         "answers_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (mid, parent["thread_id"] if parent else mid, None, project_id, "note",
                          "transient", subject, body, "{}", reply_to_message_id, created, "driver", "user",
                          json.dumps(answers) if answers else None))
            deliveries = []
            for target in targets:
                seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM driver_inbox_deliveries "
                                   "WHERE target_driver_id=?", (target,)).fetchone()[0]
                did = str(uuid4())
                conn.execute("INSERT INTO driver_inbox_deliveries VALUES(?,?,?,?,?,?)",
                             (did, mid, target, seq, "unread", 1))
                deliveries.append({"delivery_id": did, "target_driver_id": target, "seq": seq,
                                   "status": "unread", "version": 1})
            result = self._record(conn, "send_user_conversation", request_key, payload,
                                  {"message_id": mid, "project_id": project_id, "sender_kind": "user",
                                   "created_at": created, "reply_to_message_id": reply_to_message_id,
                                   "deliveries": deliveries})
        from core.state_changes import notify
        notify(self.store.db.db_path)
        return result

    @writer_only_read('list_user_conversation')
    def list_user_conversation(self, project_id, limit=30, cursor=None):
        """Newest-first page of ONE conversation: both senders, one order.

        Rows only: the driver's presence and runtime are never consulted, so a
        question can be read (and answered) while the driver is offline.

        Paging is keyset on `(created_at, message_id)` - the exact total order
        the page is sorted by - never OFFSET. `next_cursor` names the last row
        returned; the next page is STRICTLY older than that pair. Because
        message_id is unique, the pair is a strict total order: no row can be
        returned twice, and no row that existed when the walk started can be
        stepped over, whatever arrives between two page fetches. `has_more` is
        answered from a limit+1 fetch, so the end of history (`next_cursor:
        null`) is stated rather than inferred from a short page.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise StateGraphError("invalid_request")
        after = None if cursor is None else decode_conversation_cursor(cursor)
        bound, params = "", [project_id]
        if after is not None:
            bound = "AND (created_at < ? OR (created_at = ? AND message_id < ?))"
            params += [after[0], after[0], after[1]]
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            rows = [dict(r) for r in conn.execute(
                "SELECT message_id,thread_id,sender_driver_id,project_id,subject,body,refs_json,"
                "reply_to_message_id,created_at,audience,segments_json,answers_json FROM driver_inbox_messages "
                f"WHERE project_id=? AND {CONVERSATION} {bound} "
                "ORDER BY created_at DESC, message_id DESC LIMIT ?", (*params, limit + 1))]
            total = conn.execute("SELECT COUNT(*) FROM driver_inbox_messages "
                                 f"WHERE project_id=? AND {CONVERSATION}", (project_id,)).fetchone()[0]
            marker = self._read_marker(conn, project_id)
            unread = self._unread(conn, project_id)
        page, more = rows[:limit], len(rows) > limit
        messages = []
        for row in page:
            messages.append({
                "message_id": row["message_id"], "thread_id": row["thread_id"],
                "project_id": row["project_id"],
                # Who spoke: the driver's id, or the user. `sender_kind` is the
                # discriminator the UI renders; `sender_driver_id` is null for
                # the user, because a human has no driver id to report.
                "sender_kind": "driver" if row["audience"] == "user" else "user",
                "sender_driver_id": row["sender_driver_id"],
                "subject": row["subject"], "body": row["body"],
                "refs": json.loads(row["refs_json"]),
                "segments": json.loads(row["segments_json"]) if row["segments_json"] else [],
                "reply_to_message_id": row["reply_to_message_id"],
                # Present only when the reply cites a question; a row written
                # before answers existed has no key, never an empty list.
                **({"answers": json.loads(row["answers_json"])} if row["answers_json"] else {}),
                "created_at": row["created_at"]})
        next_cursor = (encode_conversation_cursor(page[-1]["created_at"], page[-1]["message_id"])
                       if more else None)
        return {"project_id": project_id, "messages": messages, "has_more": more,
                "next_cursor": next_cursor, "end_of_history": not more, "matched_total": total,
                "unread_count": unread,
                "read_upto_message_id": marker["read_message_id"] if marker else None,
                "read_upto_created_at": marker["read_created_at"] if marker else None}

    @staticmethod
    def _read_marker(conn, project_id):
        row = conn.execute("SELECT read_created_at,read_message_id,updated_at "
                           "FROM driver_user_conversation_reads WHERE project_id=?",
                           (project_id,)).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _unread(conn, project_id):
        """How many messages the DRIVER sent this project's user are newer than
        the persisted read marker.

        Persisted rows only: the sender's presence is never consulted, so the
        count is the same whether the driver is working or long gone. With no
        marker every message the driver sent is unread.
        """
        row = conn.execute(
            "SELECT COUNT(*) FROM driver_inbox_messages m "
            "LEFT JOIN driver_user_conversation_reads r ON r.project_id = m.project_id "
            "WHERE m.audience='user' AND m.project_id=? AND (r.read_created_at IS NULL "
            " OR m.created_at > r.read_created_at "
            " OR (m.created_at = r.read_created_at "
            "     AND m.message_id > COALESCE(r.read_message_id,'')))",
            (project_id,)).fetchone()
        return row[0]

    def mark_user_conversation_read(self, project_id, message_id):
        """Advance the user's persisted read marker to `message_id`.

        `message_id` must be a message of THIS project's conversation - the UI
        passes the newest one it actually loaded, never "now". That is what
        keeps a message the driver sends while the page is open unread: the
        marker can only ever move to a row the user was shown.

        Idempotent and monotonic: the marker never moves backwards, and
        re-marking the same position writes nothing and answers the same
        result, so a double click cannot double-count or lose a message.
        """
        if not isinstance(message_id, str) or not message_id:
            raise StateGraphError("invalid_request")
        if self.driver_id or not self.project_read_trusted or not self.actor.startswith("owner:"):
            raise StateGraphError("user_identity_required")
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            target = conn.execute(
                "SELECT created_at FROM driver_inbox_messages "
                f"WHERE message_id=? AND project_id=? AND {CONVERSATION}",
                (message_id, project_id)).fetchone()
            if target is None:
                raise StateGraphError("unknown_message")
            created = target["created_at"]
            marker = self._read_marker(conn, project_id)
            advanced = (marker is None
                        or created > marker["read_created_at"]
                        or (created == marker["read_created_at"]
                            and message_id > (marker["read_message_id"] or "")))
            if advanced:
                conn.execute(
                    "INSERT OR REPLACE INTO driver_user_conversation_reads"
                    "(project_id,read_created_at,read_message_id,updated_at) VALUES(?,?,?,?)",
                    (project_id, created, message_id, now()))
            marker = self._read_marker(conn, project_id)
            return {"project_id": project_id, "advanced": advanced,
                    "read_upto_message_id": marker["read_message_id"],
                    "read_upto_created_at": marker["read_created_at"],
                    "unread_count": self._unread(conn, project_id)}

    @writer_only_read('user_conversation_unread')
    def user_conversation_unread(self, project_id=None):
        """Unread driver messages per project, from persisted rows only.

        This is the badge the dashboard shows before the conversation is
        opened, so it must answer without reading any message body and without
        the sending driver being online.

        With no project named, a DRIVER sees only the projects it is a member
        of. Counting every row in the database would let a driver credential
        enumerate other projects' names and unread counts - a private project
        would leak through a badge that never shows its contents. A user/owner
        handle has no driver identity and keeps the whole-project view, which it
        can already read.
        """
        with self.store.transaction() as conn:
            if project_id is not None:
                self.store._project(conn, project_id)
                projects = {project_id: self._unread(conn, project_id)}
            elif self.driver_id:
                rows = conn.execute(
                    "SELECT DISTINCT m.project_id AS project_id "
                    "FROM driver_inbox_messages m "
                    "JOIN project_drivers d ON d.project_id = m.project_id "
                    "WHERE m.audience='user' AND d.driver_id=? AND d.status='member'",
                    (self.driver_id,)).fetchall()
                projects = {r["project_id"]: self._unread(conn, r["project_id"]) for r in rows}
            else:
                projects = {r["project_id"]: r["n"] for r in conn.execute(
                    "SELECT project_id, COUNT(*) AS n FROM driver_inbox_messages "
                    "WHERE audience='user' AND project_id IS NOT NULL GROUP BY project_id")}
                for pid in list(projects):
                    projects[pid] = self._unread(conn, pid)
        return {"projects": projects, "total": sum(projects.values())}

    @writer_only_read('list_user_messages')
    def list_user_messages(self, project_id, limit=50):
        """Newest-first page of the messages drivers sent this project's user.

        Reads persisted rows only: neither the sending driver's presence nor its
        runtime is consulted, so the user can read a message long after the
        process that wrote it is gone and while its sender is offline.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise StateGraphError("invalid_request")
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            rows = [dict(r) for r in conn.execute(
                "SELECT message_id,sender_driver_id,project_id,subject,body,refs_json,created_at "
                "FROM driver_inbox_messages WHERE audience='user' AND project_id=? "
                "ORDER BY created_at DESC, message_id DESC LIMIT ?", (project_id, limit+1))]
            total = conn.execute("SELECT COUNT(*) FROM driver_inbox_messages "
                                 "WHERE audience='user' AND project_id=?", (project_id,)).fetchone()[0]
        page = rows[:limit]
        for row in page:
            row["refs"] = json.loads(row.pop("refs_json"))
        return {"project_id": project_id, "messages": page, "matched_total": total,
                "truncated": len(rows) > limit}

    def system_notice(self, **kwargs):
        return self.send_driver_message(_system=True, **kwargs)

    @writer_only_read('list_driver_messages')
    def list_driver_messages(self, driver_id=None, after=0, limit=100):
        driver_id = driver_id or self.driver_id
        self._authorize(driver_id)
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:
            raise StateGraphError("invalid_request")
        # Reading your inbox is activity; wait_for_driver_inbox loops through here.
        touch_driver(self.store.db, self.driver_id)
        with self.store.transaction() as conn:
            self._driver(conn, driver_id)
            high=conn.execute("SELECT COALESCE(MAX(seq),0) FROM driver_inbox_deliveries WHERE target_driver_id=?", (driver_id,)).fetchone()[0]
            rows=[dict(r) for r in conn.execute("SELECT d.*,m.thread_id,m.sender_driver_id,m.project_id,m.kind,m.delivery_mode,m.subject,m.body,m.refs_json FROM driver_inbox_deliveries d JOIN driver_inbox_messages m USING(message_id) WHERE d.target_driver_id=? AND d.seq>? ORDER BY d.seq LIMIT ?", (driver_id,after,limit+1))]
        page=rows[:limit]
        return {"driver_id":driver_id,"messages":page,"next_after":page[-1]["seq"] if len(rows)>limit else max(after,high),"truncated":len(rows)>limit}

    def transition(self, delivery_id, expected_version, request_key, target_status, break_glass_reason=None):
        if target_status not in ("acknowledged","resolved") or type(expected_version) is not int or expected_version<1:
            raise StateGraphError("invalid_request")
        if not isinstance(request_key,str) or not 1<=len(request_key)<=320 or not isinstance(delivery_id,str):
            raise StateGraphError("invalid_request")
        payload=json.dumps([delivery_id,expected_version,target_status,break_glass_reason])
        with self.store.transaction(write=True) as conn:
            row=conn.execute("SELECT * FROM driver_inbox_deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
            if row is None:
                raise StateGraphError("unknown_delivery")
            self._authorize(row["target_driver_id"],write=True,break_glass_reason=break_glass_reason)
            replay=self._replay(conn,target_status,request_key,payload)
            if replay is not None:
                return replay
            if row["version"]!=expected_version:
                raise StateGraphError("version_conflict")
            if row["status"]!=("unread" if target_status=="acknowledged" else "acknowledged"):
                raise StateGraphError("invalid_transition")
            conn.execute("UPDATE driver_inbox_deliveries SET status=?,version=version+1 WHERE delivery_id=? AND version=?", (target_status,delivery_id,expected_version))
            result={**dict(row),"status":target_status,"version":expected_version+1}
            return self._record(conn,target_status,request_key,payload,result)

    @writer_only_read('wait_for_driver_inbox')
    async def wait_for_driver_inbox(self, after=0, timeout_seconds=30, return_when_idle=False, driver_id=None):
        if type(timeout_seconds) not in (int,float) or not 0<=timeout_seconds<=900:
            raise StateGraphError("invalid_request")
        from core.state_changes import subscribe
        from core.driver_presence import driver_waiting
        deadline=time.monotonic()+timeout_seconds
        # A driver blocked here is present (presence v2): online, and its offline declaration ends.
        async with driver_waiting(self.store.db, self.driver_id, timeout_seconds):
            with subscribe(self.store.db.db_path) as signal:
                while True:
                    signal.clear()
                    # list_driver_messages stamps last_seen (blocking SQLite): keep it off the event loop.
                    result=await asyncio.to_thread(self.list_driver_messages,driver_id,after)
                    if result["messages"] or return_when_idle or time.monotonic()>=deadline:
                        return result
                    try:
                        await asyncio.wait_for(signal.wait(),timeout=min(1,max(0,deadline-time.monotonic())))
                    except asyncio.TimeoutError:
                        pass


    def acknowledge_driver_message(self, delivery_id, expected_version, request_key, break_glass_reason=None):
        return self.transition(delivery_id, expected_version, request_key, "acknowledged", break_glass_reason)

    def resolve_driver_message(self, delivery_id, expected_version, request_key, break_glass_reason=None):
        return self.transition(delivery_id, expected_version, request_key, "resolved", break_glass_reason)


    def active_standing(self):
        self._authorize(self.driver_id)
        if not self.driver_id:
            return []
        with self.store.transaction() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT m.*,d.delivery_id,d.seq,d.status FROM driver_inbox_messages m JOIN driver_inbox_deliveries d USING(message_id) WHERE d.target_driver_id=? AND m.delivery_mode='standing' AND d.status IN ('unread','acknowledged') ORDER BY d.seq LIMIT 8", (self.driver_id,))]
            total=conn.execute("SELECT COUNT(*) FROM driver_inbox_messages m JOIN driver_inbox_deliveries d USING(message_id) WHERE d.target_driver_id=? AND m.delivery_mode='standing' AND d.status IN ('unread','acknowledged')", (self.driver_id,)).fetchone()[0]
            return {"items":rows,"matched_total":total}


# -- P3 notices into this inbox (core/driver_notices.py) ----------------------
def deliver_notice(conn, message):
    """Insert one P3 notice as one inbox message with one delivery, on the
    CALLER'S connection: it commits or rolls back with the ownership write
    (an abandon, a takeover, a handoff decision) that caused it.

    Returns the message and delivery ids, or ``{"skipped": reason}`` when the
    target has no inbox: the inbox is keyed to the P0 driver registry
    (``driver_inbox_deliveries.target_driver_id REFERENCES drivers``)."""
    present = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "('driver_inbox_messages','driver_inbox_deliveries','drivers')")}
    if not {"driver_inbox_messages", "driver_inbox_deliveries"} <= present:
        return {"skipped": "inbox_unavailable"}
    target = message["target_driver_id"]
    if "drivers" not in present or conn.execute("SELECT 1 FROM drivers WHERE driver_id=?", (target,)).fetchone() is None:
        return {"skipped": "unregistered_driver"}
    mid, did = str(uuid4()), str(uuid4())
    conn.execute("INSERT INTO driver_inbox_messages(message_id,thread_id,sender_driver_id,project_id,"
                 "kind,delivery_mode,subject,body,refs_json,reply_to_message_id,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (mid, mid, message["sender_driver_id"], message["project_id"], message["kind"], "transient",
                  message["subject"], message["body"], json.dumps(message["refs"]), None, now()))
    seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM driver_inbox_deliveries WHERE target_driver_id=?",
                       (target,)).fetchone()[0]
    conn.execute("INSERT INTO driver_inbox_deliveries VALUES(?,?,?,?,?,?)", (did, mid, target, seq, "unread", 1))
    return {"message_id": mid, "delivery_id": did, "target_driver_id": target, "seq": seq}
