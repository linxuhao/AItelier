"""Private driver deliveries. State project mailboxes remain separate."""
from __future__ import annotations
import asyncio
import base64
import json
import re
import time
from uuid import uuid4
from core.driver_questions import CONVERSATION, all_questions, conversation_rows, open_question_count
from core.drivers import CLI_OWNER_ACTOR, recipient_presence_block, touch_driver
from core.message_body_guard import refuse_credential_body
from core.message_secret_guard import refuse_credential_values
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
# Every delivery status the schema's CHECK admits, and so every value an inbox
# reader's `statuses` filter may name: one list, so a filter cannot ask for a
# status no row can carry.
DELIVERY_STATUSES = ("unread", "acknowledged", "resolved")

# The user's conversation: ONE thread per project holding BOTH directions.
# A driver -> user message is recognised by `audience='user'`; a user -> driver
# message is a driver-addressed row (it is delivered to a driver's inbox) marked
# `channel='user'`. `audience`/`channel` are additive columns, so a row written
# before either existed still reads back through its own half. `CONVERSATION`
# itself is declared beside the open-question rule (core/driver_questions.py),
# which scopes its rows the same way, and is imported above so the two agree.
# A segment id is a short token a reply can cite: letters, digits, and . _ : -.
_SEGMENT_ID = re.compile(r"[A-Za-z0-9._:-]+")
# The subject of a user's message that names none (the dashboard's, a chat channel's).
USER_SUBJECT = "Message from the user"


def citation_fields(row):
    """The citation part of ONE message row: its blocks, and the questions it answers.

    This is the SINGLE projection of `segments_json`/`answers_json`. Both reads of
    the same message build their citation here - the project's conversation
    (`list_user_conversation`) and the addressed driver's private inbox
    (`list_driver_messages`) - so one message can never carry one set of blocks in
    the user's view and another set in the driver's. Both reads decode the two
    columns only here, so they cannot drift apart one field at a time.

    Each key appears only when its column holds something: a row written before
    segments or answers existed gains no key, never an empty list, and the raw
    `*_json` strings never leave here. Whether a caller wants the empty `segments`
    list is that caller's business (`list_user_conversation` always shows it,
    because the conversation UI renders the blocks area; an inbox row keeps its
    historical shape instead).
    """
    fields = {}
    if row["segments_json"]:
        fields["segments"] = json.loads(row["segments_json"])
    if row["answers_json"]:
        fields["answers"] = json.loads(row["answers_json"])
    if "withdraws_json" in row.keys() and row["withdraws_json"]:
        fields["withdraws"] = json.loads(row["withdraws_json"])
    return fields


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
        # Called with (subject, body) of every text an OWNER writes here, before anything
        # is replayed or written: core.state_channels installs its live binding code check
        # (a hit retires the code and is refused code_sent_to_bound_account). None: no check.
        self.owner_text_guard = None
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
                # Additive (the driver's own questions a row withdraws). NULL means
                # the row withdraws nothing, which is every row before this column.
                add_column_if_missing(conn, "driver_inbox_messages", "withdraws_json", "TEXT")
                conn.commit()

    @staticmethod
    def _segments(segments):
        """Normalise the tagged blocks, refusing anything that would not render.

        Bounded on purpose: this is a glanceable summary, not a second body. The
        kind is closed to info/question so a later tag cannot appear in stored
        rows without someone deciding what it means to the reader.

        Each block carries a stable id, so a reply can cite the exact question
        it answers. A caller-supplied id is kept; a block without one gets a
        fresh 12-hex id, drawn from the same name space a caller may use. A
        duplicate id inside one message is refused, because a citation would
        then name two blocks at once - and a caller id equal to an id generated
        for an earlier block of the same message is refused the same way
        (`invalid_segments`), never resolved in the caller's favour. So a caller
        id is either kept exactly as sent or the message is refused, and nothing
        is stored on refusal.

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
        # Checked after ids are filled in, because that is when a duplicate can
        # be seen at all: a duplicate caller id, or a caller id that collides
        # with an id generated for an earlier block. Both are REFUSED as
        # invalid_segments - there is deliberately no rule saying the caller's
        # id wins, so nothing is ever silently renamed.
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
        # The one credential guard for this writer. Before the request key is
        # replayed or any row is written, so a refused write leaves nothing behind,
        # and it receives EVERY text field the row keeps: subject as well as body.
        refuse_credential_values((subject, body), lambda detail: StateGraphError("invalid_request: " + detail))
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
        if human_owner and not _system and self.owner_text_guard is not None:
            self.owner_text_guard(subject, body)
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
        # The one guard call for this writer: subject, body and the segment text
        # that lands in segments_json, before the INSERT below.
        refuse_credential_body(subject, body, segments)
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

    def withdraw_user_question(self, request_key, project_id, message_id, segment_id, reason):
        """Withdraw one of the calling driver's OWN open questions, as a record.

        Writes one driver row in the conversation (subject "Question withdrawn",
        the reason and the question as its body) whose `withdraws` cites the
        question; the question's own row is never touched, so the thread keeps
        both what was asked and that it was taken back. From then on the one rule
        (`core/driver_questions.py`) reads the question as `withdrawn`: it leaves
        the badge count, the questions box's open part and list_user_questions'
        `open`, everywhere at once.

        Refused, writing nothing: a message or segment that is not a question in
        this project's conversation (`unknown_question`, `not_a_question`), a
        question another driver asked (`not_own_question`), one a reply already
        answers (`question_answered`). Withdrawing a question already withdrawn
        is a no-op that names the existing withdrawal. Idempotent per (actor,
        request_key) like every write here. Presentation only: it grants nothing.
        """
        if type(request_key) is not str or not request_key or len(request_key) > 320:
            raise StateGraphError("invalid_request")
        if (type(message_id) is not str or not 1 <= len(message_id) <= 200
                or type(segment_id) is not str or not 1 <= len(segment_id) <= 64
                or type(reason) is not str or not 1 <= len(reason.strip()) or len(reason) > 2000):
            raise StateGraphError("invalid_request")
        refuse_credential_body("Question withdrawn", reason, None)
        if not self.driver_id:
            raise StateGraphError("driver_identity_required")
        payload = json.dumps([project_id, message_id, segment_id, reason, self.driver_id], sort_keys=True)
        with self.store.transaction(write=True) as conn:
            replay = self._replay(conn, "withdraw_user_question", request_key, payload)
            if replay is not None:
                return replay
            self._driver(conn, self.driver_id)
            if conn.execute("SELECT status FROM drivers WHERE driver_id=?", (self.driver_id,)).fetchone()[0] != "active":
                raise StateGraphError("inactive_driver")
            self.store._project(conn, project_id)
            asked = conn.execute(
                "SELECT sender_driver_id,thread_id,segments_json FROM driver_inbox_messages "
                f"WHERE message_id=? AND project_id=? AND {CONVERSATION} AND audience='user'",
                (message_id, project_id)).fetchone()
            segments = json.loads(asked["segments_json"]) if asked is not None and asked["segments_json"] else []
            question = next((s for s in segments if isinstance(s, dict) and s.get("id") == segment_id), None)
            if question is None:
                raise StateGraphError("unknown_question: message_id/segment_id must name a question block in this project's conversation")
            if question.get("kind") != "question":
                raise StateGraphError("not_a_question: only a question block can be withdrawn")
            if asked["sender_driver_id"] != self.driver_id:
                raise StateGraphError("not_own_question: a driver withdraws only the questions it asked")
            state = next((q for q in all_questions(conversation_rows(conn, project_id))
                          if q["message_id"] == message_id and q["segment_id"] == segment_id), None)
            if state is not None and state["status"] == "answered":
                raise StateGraphError("question_answered: a reply already answers this question; it cannot be withdrawn")
            if state is not None and state["status"] == "withdrawn":
                return {"withdrawn": False, "already_withdrawn": True, "project_id": project_id,
                        "message_id": message_id, "segment_id": segment_id,
                        "withdrawal_message_id": state["withdrawn"]["message_id"]}
            mid, created = str(uuid4()), now()
            body = reason + "\n\nWithdrawn question: " + str(question.get("text") or "")[:2000]
            conn.execute("INSERT INTO driver_inbox_messages(message_id,thread_id,sender_driver_id,project_id,"
                         "kind,delivery_mode,subject,body,refs_json,reply_to_message_id,created_at,audience,"
                         "withdraws_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (mid, asked["thread_id"] or message_id, self.driver_id, project_id, "note", "transient",
                          "Question withdrawn", body, json.dumps({}), None, created, "user",
                          json.dumps([{"message_id": message_id, "segment_id": segment_id, "reason": reason}])))
            return self._record(conn, "withdraw_user_question", request_key, payload,
                                {"withdrawn": True, "project_id": project_id, "message_id": message_id,
                                 "segment_id": segment_id, "withdrawal_message_id": mid, "created_at": created})

    def _project_user(self) -> bool:
        """Whether THIS session is the project's user — the writer of the user's half.

        Two sessions are the project's user:

        * the owner's browser (`owner:<email>`, api.authz's Cloudflare Access
          branch), and
        * the owner's own TERMINAL: the local CLI presenting its own env admin
          token (kind 'cli_owner', actor `core.drivers.CLI_OWNER_ACTOR`). The
          owner ruled it so (2026-11-18): the CLI already is the fixed cli owner,
          so it keeps that permission and speaks as the owner in the manager
          conversation. Note the actor carries no "owner:" prefix — that prefix
          opens every OTHER owner-only surface, and this terminal opens exactly
          these two doors.

        A DRIVER is never the user: it writes the other half of the thread
        (`send_user_message`), so a session carrying a driver_id is refused
        whatever its actor looks like. An unreadable project is refused too.
        """
        if not self.project_read_trusted or self.driver_id:
            return False
        return self.actor.startswith("owner:") or self.actor == CLI_OWNER_ACTOR

    def send_user_conversation_message(self, request_key, project_id, body, subject=None,
                                       reply_to_message_id=None, target_driver_id=None, answers=None,
                                       _written=None):
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

        `_written(conn, message_id)` (not a request field) runs inside the write
        transaction of a NEW message only - never on a replay: the steward's chat
        approval reads the proposal version as of this message there
        (core.state_channels).
        """
        if type(request_key) is not str or not request_key or len(request_key) > 320:
            raise StateGraphError("invalid_request")
        subject = USER_SUBJECT if subject is None else subject
        if (type(subject) is not str or not 1 <= len(subject) <= 200
                or type(body) is not str or len(body) > 8000):
            raise StateGraphError("invalid_request")
        # The one guard call for this writer: subject, body and the answer refs
        # that land in answers_json, before the INSERT below.
        refuse_credential_body(subject, body, answers)
        if not self._project_user():
            raise StateGraphError("user_identity_required")
        if self.owner_text_guard is not None:
            self.owner_text_guard(subject, body)
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
            mid, created, deliveries = self.write_user_half(conn, project_id, subject, body, reply_to_message_id,
                                                            target_driver_id, answers)
            if _written is not None:
                _written(conn, mid)
            result = self._record(conn, "send_user_conversation", request_key, payload,
                                  {"message_id": mid, "project_id": project_id, "sender_kind": "user",
                                   "created_at": created, "reply_to_message_id": reply_to_message_id,
                                   "deliveries": deliveries})
        from core.state_changes import notify
        notify(self.store.db.db_path)
        return result

    def write_user_half(self, conn, project_id, subject, body, reply_to_message_id=None, target_driver_id=None,
                        answers=None):
        """The user's message row and the deliveries that wake its driver(s), on the
        caller's write connection; (message_id, created_at, deliveries). The one
        writer of the user's half: the dashboard's message above and a message the
        owner sent from a chat channel (core.state_channels) land identically."""
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
        return mid, created, deliveries

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
                "reply_to_message_id,created_at,audience,segments_json,answers_json,withdraws_json FROM driver_inbox_messages "
                f"WHERE project_id=? AND {CONVERSATION} {bound} "
                "ORDER BY created_at DESC, message_id DESC LIMIT ?", (*params, limit + 1))]
            total = conn.execute("SELECT COUNT(*) FROM driver_inbox_messages "
                                 f"WHERE project_id=? AND {CONVERSATION}", (project_id,)).fetchone()[0]
            marker = self._read_marker(conn, project_id)
            unread = self._unread(conn, project_id)
        page, more = rows[:limit], len(rows) > limit
        messages = []
        for row in page:
            # Segments and answers come from the ONE shared citation projection,
            # which the driver's own inbox read calls too (see `citation_fields`).
            citation = citation_fields(row)
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
                # The conversation always shows the blocks area, so `segments` is
                # [] here even when the row carries none.
                "segments": citation.pop("segments", []),
                "reply_to_message_id": row["reply_to_message_id"],
                # Present only when the reply cites a question; a row written
                # before answers existed has no key, never an empty list.
                **citation,
                "created_at": row["created_at"]})
        next_cursor = (encode_conversation_cursor(page[-1]["created_at"], page[-1]["message_id"])
                       if more else None)
        return {"project_id": project_id, "messages": messages, "has_more": more,
                "next_cursor": next_cursor, "end_of_history": not more, "matched_total": total,
                "unread_count": unread,
                "read_upto_message_id": marker["read_message_id"] if marker else None,
                "read_upto_created_at": marker["read_created_at"] if marker else None}

    @writer_only_read('list_user_questions')
    def list_user_questions(self, project_id, status="open", limit=50):
        """The question blocks of ONE conversation, each with the replies citing it.

        The same rule as the unread badge's question count and the dashboard's
        questions box (`core/driver_questions.py`), read over the whole thread in
        one call, so neither the driver nor the dashboard has to page the
        conversation to learn what is still open. Newest question first;
        `status` picks `open`, `answered`, `withdrawn` or `all`; `counts` always
        covers all three.
        Rows only: the driver's presence is never consulted.
        """
        if status not in ("open", "answered", "withdrawn", "all") or type(limit) is not int or not 1 <= limit <= 200:
            raise StateGraphError("invalid_request")
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            found = all_questions(conversation_rows(conn, project_id, with_body=True))
        counts = {state: sum(q["status"] == state for q in found) for state in ("open", "answered", "withdrawn")}
        picked = [q for q in reversed(found) if status == "all" or q["status"] == status]
        return {"project_id": project_id, "status": status, "questions": picked[:limit],
                "matched_total": len(picked), "has_more": len(picked) > limit, "counts": counts}

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
        if not self._project_user():
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
        """Unread driver messages and open questions per project, from rows only.

        This is the badge the dashboard shows before the conversation is
        opened, so it must answer without reading any message body and without
        the sending driver being online.

        `projects`/`total` count the messages the DRIVER sent this project's
        user that are newer than the persisted read marker - a message-level
        badge the user clears by reading. `questions`/`questions_total` count
        something else on purpose: the `question` segments no reply has answered
        yet, from `core/driver_questions.py`, which is the same rule the
        conversation's questions box reads. Reading the thread does not close a
        question, so the two badges move independently. A project with no open
        question is absent from `questions` (and contributes 0 to its total),
        because that is the map's own meaning: only projects with a count.

        With no project named, a DRIVER sees only the projects it is a member
        of. Counting every row in the database would let a driver credential
        enumerate other projects' names and unread counts - a private project
        would leak through a badge that never shows its contents. A user/owner
        handle has no driver identity and keeps the whole-project view, which it
        can already read. The question counts obey the same visibility: they are
        taken over the projects this caller may already see.
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
            questions = {pid: open_question_count(conversation_rows(conn, pid))
                         for pid in projects}
        questions = {pid: n for pid, n in questions.items() if n}
        return {"projects": projects, "total": sum(projects.values()),
                "questions": questions, "questions_total": sum(questions.values())}

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
    def list_driver_messages(self, driver_id=None, after=0, limit=100, newest=False, statuses=None):
        """One page of the deliveries addressed to this driver, ordered by seq.

        A row is the delivery (`d.*`) plus the message's readable columns, so a
        message that carries tagged blocks or answers a question reaches the
        driver with the SAME `segments`/`answers` the conversation read projects
        for it - both are built by `citation_fields`. Those two keys appear only
        on a row that carries them: a plain message keeps exactly the shape it
        had before, with no empty list and no raw `*_json` string. By the same
        rule, an owner message relayed from a chat channel carries
        `source_channel` ('slack' / 'wechat', from the inbound row of
        `state_message_sources`); a dashboard message has no such key.

        The page is the OLDEST `limit` deliveries above `after` (the ascending
        forward keyset a waiting driver polls with) unless `newest` is true, which
        answers the NEWEST `limit` deliveries instead, newest first. `newest` is
        the one call a driver needs to ask "what did I miss, most recent first"
        without walking the whole backlog; `after` still bounds that window, so
        the same flag can also mean "the newest of what I have not read yet".
        The two modes differ ONLY in which end of the window they take and the
        order they return it in: the rows themselves are identical, and with
        `newest` false every field is what it was before the flag existed.

        `matched_total` is how many deliveries match the filters - not how many
        this page returned - so a driver can tell 3 rows from 3,000 in the same
        answer, and `has_more` says whether any matched row is outside the page.
        Both are a COUNT(*) over the SAME WHERE clause the page is read with, so
        neither can drift from the other mode's, the `after` window's, or a
        `statuses` filter's meaning. `truncated` is kept as the same statement
        `has_more` makes (today's callers read it) and `next_after` keeps its
        forward meaning: the seq a polling caller continues from, which in the
        newest mode is the newest delivery (the descending page has nothing
        newer to hand back).

        `statuses` optionally narrows the page AND `matched_total` to deliveries
        whose delivery status is one of them, so "the newest 5 unread" is one
        call and its total is the number of unread ones, not the size of the
        whole inbox.
        """
        driver_id = driver_id or self.driver_id
        self._authorize(driver_id)
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:
            raise StateGraphError("invalid_request")
        if type(newest) is not bool:
            raise StateGraphError("invalid_request")
        if statuses is not None:
            if (not isinstance(statuses,(list,tuple)) or not statuses
                    or any(type(status) is not str for status in statuses)
                    or len(set(statuses)) != len(statuses)
                    or any(status not in DELIVERY_STATUSES for status in statuses)):
                raise StateGraphError("invalid_request")
        # The page and the total share ONE WHERE clause, so matched_total can
        # never count a different set than the page was drawn from.
        where = "d.target_driver_id=? AND d.seq>?"
        bound = [driver_id, after]
        if statuses is not None:
            where += " AND d.status IN (" + ",".join("?" for _ in statuses) + ")"
            bound.extend(statuses)
        bound = tuple(bound)
        # Reading your inbox is activity; wait_for_driver_inbox loops through here.
        touch_driver(self.store.db, self.driver_id)
        with self.store.transaction() as conn:
            self._driver(conn, driver_id)
            high=conn.execute("SELECT COALESCE(MAX(seq),0) FROM driver_inbox_deliveries WHERE target_driver_id=?", (driver_id,)).fetchone()[0]
            # COUNT(*) over the same WHERE clause, never a page fetched into Python:
            # the answer must not cost the caller the whole backlog.
            matched_total=conn.execute("SELECT COUNT(*) FROM driver_inbox_deliveries d JOIN driver_inbox_messages m USING(message_id) WHERE "+where, bound).fetchone()[0]
            rows=[dict(r) for r in conn.execute("SELECT d.*,m.thread_id,m.sender_driver_id,m.project_id,m.kind,m.delivery_mode,m.subject,m.body,m.refs_json,m.segments_json,m.answers_json,m.withdraws_json,s.channel AS source_channel FROM driver_inbox_deliveries d JOIN driver_inbox_messages m USING(message_id) LEFT JOIN state_message_sources s ON s.message_id=m.message_id AND s.direction='in' WHERE "+where+" ORDER BY d.seq"+(" DESC" if newest else "")+" LIMIT ?", (*bound,limit+1))]
        page=rows[:limit]
        for row in page:
            citation=citation_fields(row)
            # The storage columns are not payload: the citation part above is the
            # only shape a reader sees them in.
            del row["segments_json"], row["answers_json"], row["withdraws_json"]
            if row["source_channel"] is None:
                del row["source_channel"]
            row.update(citation)
        truncated=len(rows)>limit
        next_after = max(after,high) if newest else (page[-1]["seq"] if truncated else max(after,high))
        return {"driver_id":driver_id,"messages":page,"next_after":next_after,"truncated":truncated,
                "matched_total":matched_total,"has_more":matched_total>len(page)}

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
        """The NEWEST eight standing notices, returned in delivery-seq order.

        This feeds the post-compaction guidance block, which is a resume prompt:
        a standing notice a driver has not worked under yet outranks one it was
        sent long ago, so the SELECTION runs backwards over the recency key
        (``ORDER BY d.seq DESC LIMIT 8``) — the forward LIMIT kept the first
        eight deliveries the driver ever received, i.e. exactly the ones it
        already knows. The rows are then flipped back into the order this has
        always returned them (ascending ``seq``, delivery order), so a reader
        walking ``items`` front to back still reads oldest -> newest.

        ``matched_total`` is unchanged: the count of every matching standing
        notice, not just the page — the caller turns the difference into
        ``omitted``.
        """
        self._authorize(self.driver_id)
        if not self.driver_id:
            return []
        with self.store.transaction() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT m.*,d.delivery_id,d.seq,d.status FROM driver_inbox_messages m JOIN driver_inbox_deliveries d USING(message_id) WHERE d.target_driver_id=? AND m.delivery_mode='standing' AND d.status IN ('unread','acknowledged') ORDER BY d.seq DESC LIMIT 8", (self.driver_id,))]
            rows.reverse()
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
    # The credential guard runs before the INSERT, so a refused notice leaves no
    # row and the caller's ownership write rolls back with it (StateGraphError).
    # One call, every text field the row keeps: the subject as well as the body.
    refuse_credential_body(message["subject"], message["body"])
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
