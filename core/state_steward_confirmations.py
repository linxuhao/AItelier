"""Outward actions confirmed by the person who asked (step 24.1b, design-steward 7.8.9).

A connection tool the owner marked `outward` (send a mail, change or share a document)
never runs on the steward's word. The steward's HOST (code, never the model) asks for a
confirmation with the exact public tool name, the exact arguments and the owner message
that asked for it (`create_steward_confirmation`); this module

- derives the requester and the conversation from that message itself: a message a chat
  channel relayed (state_message_sources) is that channel's bound sender; a message the
  owner's browser session wrote on the dashboard (`state_steward_dashboard_messages`,
  recorded when it was written) is the dashboard; anything else - the owner's terminal
  (CLI_OWNER_ACTOR), a letter, a reminder - is `no_requester`;
- stores the canonical JSON of the arguments and its hash
  sha256(conn_uid \\0 generation \\0 raw_name \\0 canonical arguments);
- writes, by code, the text the requester reads: a fixed header that alone carries the
  reply phrase, then every argument (short ones first), every control, bidirectional or
  zero-width character shown as an escape;
- matches the requester's exact reply `确认 <code>` / `取消 <code>` inside the
  transaction that writes the reply message (core.state_channels), on the same
  channel and sender, within 10 minutes;
- lets the gateway CONSUME one confirmed record whose hash equals the call it is about
  to forward (api/steward_gateway.py): one confirmation, one forward, never retried.

Both tables are private (core.state_privacy.PRIVATE_STATE_TABLES); records are kept 30 days.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from core.state_graph import StateGraphError, StateNotFound
from core.state_privacy import UntrustedDatabase

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_steward_confirmations (
 code TEXT PRIMARY KEY, conn_id TEXT NOT NULL, raw_name TEXT NOT NULL, gw_name TEXT NOT NULL,
 args_json TEXT NOT NULL, args_sha256 TEXT NOT NULL,
 requester_kind TEXT NOT NULL CHECK(requester_kind IN ('owner','member')),
 channel TEXT NOT NULL, conversation_ref TEXT, sender_ref TEXT, requester_message_id TEXT NOT NULL,
 audience TEXT NOT NULL, message_text TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','confirmed','cancelled','expired','consumed','done','failed')),
 created_at TEXT NOT NULL, confirmed_at TEXT, confirm_message_ref TEXT, consumed_at TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS state_steward_confirmations_lookup
 ON state_steward_confirmations(conn_id, raw_name, args_sha256, status);
CREATE TABLE IF NOT EXISTS state_steward_dashboard_messages (
 message_id TEXT PRIMARY KEY, at TEXT NOT NULL);
"""
TABLES = ("state_steward_confirmations", "state_steward_dashboard_messages")

TTL = timedelta(minutes=10)             # to confirm, and from confirming to running
CLOCK_SKEW = timedelta(minutes=2)       # a channel time this far ahead of now still counts
KEPT = timedelta(days=30)
PENDING_MAX = 3                         # per requester at a time
TEXT_MAX = 4000                         # a private chat or the dashboard (a group: 1400, step 23)
SHORT_MAX = 200
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE = re.compile(r"K[0-9A-HJKMNP-TV-Z]{4}")
CONFIRM, CANCEL = "确认 ", "取消 "
DASHBOARD = "dashboard"
# Shown as an escape wherever they occur: C0/C1 controls and DEL, the bidirectional
# controls, the zero-width characters. A newline stays a newline only inside a long field.
_HIDDEN = re.compile("[\u0000-\u001f\u007f-\u009f‪-‮⁦-⁩​-‍⁠﻿]")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---------------------------------------------------------------- canonical arguments
def canonical_arguments(arguments) -> bytes:
    """THE canonical form (one function for creating and for consuming): keys sorted by
    code point, no whitespace, UTF-8. A lone surrogate anywhere is `invalid_arguments`."""
    if not isinstance(arguments, dict):
        raise StateGraphError("invalid_arguments: arguments must be an object")
    try:
        return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError):
        raise StateGraphError("invalid_arguments: the arguments are not canonical JSON text") from None


def args_hash(conn_uid: str, generation: int, raw_name: str, canonical: bytes) -> str:
    head = f"{conn_uid}\0{int(generation)}\0{raw_name}\0".encode("utf-8")
    return hashlib.sha256(head + canonical).hexdigest()


# ---------------------------------------------------------------- the text a person reads
def _shown(text: str, *, keep_newlines: bool = False) -> str:
    def escape(match):
        char = match.group(0)
        if keep_newlines and char == "\n":
            return char
        return "\\u%04x" % ord(char)
    return _HIDDEN.sub(escape, text)


def _value_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def confirmation_text(code: str, label: str, raw_name: str, arguments: dict) -> str:
    """Written by code, never the model: the header (the only place the reply phrase is),
    then the short fields (one line, at most 200 characters) and then the long ones."""
    short, long = [], []
    for key in sorted(arguments):
        text = _value_text(arguments[key])
        if "\n" not in text and "\r" not in text and len(text) <= SHORT_MAX:
            short.append(f"{_shown(key)}：{_shown(text)}")
        else:
            long.append(f"{_shown(key)}（长文）：\n{_shown(text, keep_newlines=True)}")
    header = (f"请确认（{code}，10 分钟内有效）：用「{label}」执行 {_shown(raw_name)}。"
              f"回复「{CONFIRM}{code}」执行，「{CANCEL}{code}」作废。")
    return "\n".join([header, *short, *long, "—— 以上是全部内容 ——"])


def _new_code(conn) -> str:
    for _ in range(50):
        code = "K" + "".join(_secrets.choice(CROCKFORD) for _ in range(4))
        if conn.execute("SELECT 1 FROM state_steward_confirmations WHERE code=?", (code,)).fetchone() is None:
            return code
    raise StateGraphError("confirmation_codes_exhausted")


def ensure_schema(db) -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _has_tables(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_steward_confirmations'"
                        ).fetchone() is not None


# ---------------------------------------------------------------- the requester
def record_dashboard_message(conn, message_id: str) -> None:
    """The owner's BROWSER session wrote this steward-conversation message (called from
    core.state_channels inside the write): it can be a requester, on the dashboard."""
    ensure_tables(conn)
    conn.execute("INSERT OR IGNORE INTO state_steward_dashboard_messages VALUES(?,?)", (message_id, _iso(_utcnow())))


def ensure_tables(conn) -> None:
    """Inside a caller's transaction (executescript would commit it): one statement at a time."""
    if not _has_tables(conn):
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)


def requester_of(conn, message_id: str, home: str) -> dict:
    """{kind, channel, conversation_ref, sender_ref} of an owner message in the steward's
    conversation, from the message itself; `no_requester` for anything else."""
    row = conn.execute("SELECT project_id, sender_driver_id, audience, channel FROM driver_inbox_messages "
                       "WHERE message_id=?", (message_id,)).fetchone() if isinstance(message_id, str) else None
    if row is None or row["project_id"] != home or row["sender_driver_id"] is not None or row["channel"] != "user":
        raise StateGraphError("no_requester: an outward action starts only from a person's message in this turn")
    source = conn.execute("SELECT channel, sender_ref FROM state_message_sources WHERE message_id=? AND direction='in'",
                          (message_id,)).fetchone()
    if source is not None and source["sender_ref"]:
        return {"kind": "owner", "channel": source["channel"], "conversation_ref": source["sender_ref"],
                "sender_ref": source["sender_ref"]}
    if conn.execute("SELECT 1 FROM state_steward_dashboard_messages WHERE message_id=?", (message_id,)).fetchone():
        return {"kind": "owner", "channel": DASHBOARD, "conversation_ref": home, "sender_ref": None}
    # The owner's terminal talks to the steward but never approves nor confirms.
    raise StateGraphError("no_requester: this message cannot confirm an outward action (not a chat channel or "
                          "the dashboard)")


def _requester_key(record) -> tuple:
    return (record["channel"], record["conversation_ref"], record["sender_ref"])


# ---------------------------------------------------------------- matching a reply
def match_reply(conn, body, *, channel: str, sender_ref, written_at: datetime | None):
    """In the transaction that writes a person's message: when the whole message (ends
    stripped) is `确认 <code>` / `取消 <code>` for a PENDING record of this very requester
    (channel and sender; the dashboard: the owner's browser session, server time), decide
    it. None for an ordinary message - also a code of someone else, of another channel,
    or no longer pending. `written_at` is the channel's time (None: undated)."""
    if not isinstance(body, str) or not _has_tables(conn):
        return None
    phrase = body.strip()
    for prefix, action in ((CONFIRM, "confirm"), (CANCEL, "cancel")):
        if phrase.startswith(prefix) and CODE.fullmatch(phrase[len(prefix):]):
            code = phrase[len(prefix):]
            break
    else:
        return None
    row = conn.execute("SELECT * FROM state_steward_confirmations WHERE code=?", (code,)).fetchone()
    if row is None or row["status"] != "pending" or row["channel"] != channel or (
            channel != DASHBOARD and row["sender_ref"] != sender_ref):
        return None
    out = {"code": code, "action": action}
    now = _utcnow()
    if action == "cancel":
        conn.execute("UPDATE state_steward_confirmations SET status='cancelled' WHERE code=? AND status='pending'", (code,))
        return {**out, "outcome": "cancelled"}
    if written_at is None:
        return {**out, "outcome": "refused", "refusal": "confirmation_undated"}
    created = _parse(row["created_at"])
    if written_at - now > CLOCK_SKEW:
        return {**out, "outcome": "refused", "refusal": "confirmation_clock_skew"}
    if written_at > created + TTL or now - written_at > TTL or written_at < created - CLOCK_SKEW:
        conn.execute("UPDATE state_steward_confirmations SET status='expired' WHERE code=? AND status='pending'", (code,))
        return {**out, "outcome": "expired"}
    conn.execute("UPDATE state_steward_confirmations SET status='confirmed', confirmed_at=? WHERE code=? "
                 "AND status='pending'", (_iso(now), code))
    return {**out, "outcome": "confirmed"}


# ---------------------------------------------------------------- the gateway's consumption
def consume(conn, connection, raw_name: str, arguments) -> str | None:
    """In the gateway's write transaction: the confirmed record (within 10 minutes of its
    confirmation) whose hash equals this call - with the connection's CURRENT conn_uid and
    generation - becomes `consumed`; its code, else None. Raises invalid_arguments."""
    if not _has_tables(conn):
        return None
    digest = args_hash(connection["conn_uid"], connection["generation"], raw_name, canonical_arguments(arguments))
    since = _iso(_utcnow() - TTL)
    row = conn.execute("SELECT code FROM state_steward_confirmations WHERE conn_id=? AND raw_name=? AND args_sha256=? "
                       "AND status='confirmed' AND confirmed_at>=? ORDER BY confirmed_at LIMIT 1",
                       (connection["conn_id"], raw_name, digest, since)).fetchone()
    if row is None:
        return None
    taken = conn.execute("UPDATE state_steward_confirmations SET status='consumed', consumed_at=? WHERE code=? "
                         "AND status='confirmed'", (_iso(_utcnow()), row["code"])).rowcount
    return row["code"] if taken else None


def finish(conn, code: str, ok: bool, summary: str | None) -> None:
    """The forward's outcome on the record: done / failed and an error code, never a result body."""
    conn.execute("UPDATE state_steward_confirmations SET status=?, result=? WHERE code=? AND status='consumed'",
                 ("done" if ok else "failed", json.dumps({"error": summary} if summary else {}), code))


# ---------------------------------------------------------------- the service
class StateStewardConfirmations:
    """The steward host's two commands and the owner's list (7.8.9, 7.8.10)."""

    def __init__(self, store, actor, driver_id=None, steward=None):
        self.store, self.actor, self.driver_id, self.steward = store, actor, driver_id, steward
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            ensure_schema(store.db)

    def create(self, name, arguments, requester_message_id, audience="owner"):
        """The host's request for one confirmation: {code, text, channel, reply_to_message_id,
        expires_at}. Refused: no_requester, not an allowed outward tool of a connection this
        turn may use, too many pending, too long to confirm, invalid arguments."""
        from core import state_steward_connections as sc
        from core.state_steward import steward_project
        if audience != "owner":
            # Group turns and their confirmations arrive with step 23 (7.7) / 24.2.
            raise StateGraphError("ambiguous_requester: confirmations in a group arrive with group turns")
        if not isinstance(name, str) or not name.startswith(sc.PUBLIC_PREFIX):
            raise StateGraphError("not_outward: not a connection tool")
        parts = sc.split_name(name[len(sc.PUBLIC_PREFIX):])
        if parts is None:
            raise StateGraphError("not_outward: not a connection tool")
        conn_id, gw = parts
        canonical = canonical_arguments(arguments)
        home = steward_project()
        now = _utcnow()
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn)
            ensure_tables(conn)
            requester = requester_of(conn, requester_message_id, home)
            connection = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
            entry = next((e for e in sc.effective(conn) if e["conn_id"] == conn_id and e["gw_name"] == gw), None)
            if connection is None or entry is None:
                raise StateGraphError("not_outward: not an allowed tool of a usable connection")
            if not entry["outward"]:
                raise StateGraphError("not_outward: this tool is not outward; call it directly")
            if entry["visibility"] == "private" and audience != "owner":
                raise StateGraphError("not_visible_in_this_turn")
            # The gateway's own argument checks (at any depth; on a public connection no
            # url-named key unless unblocked): never ask a person to confirm a refused call.
            if sc.blocked_present(arguments, entry["blocked_args"]) or (
                    connection["visibility"] == "public"
                    and sc.url_argument_present(arguments, sc.unblocked_url_paths(entry))):
                raise StateGraphError("argument_blocked")
            conn.execute("DELETE FROM state_steward_confirmations WHERE created_at<?", (_iso(now - KEPT),))
            conn.execute("DELETE FROM state_steward_dashboard_messages WHERE at<?", (_iso(now - KEPT),))
            pending = conn.execute(
                "SELECT COUNT(*) FROM state_steward_confirmations WHERE status='pending' AND created_at>=? AND channel=? "
                "AND conversation_ref IS ? AND sender_ref IS ?",
                (_iso(now - TTL), requester["channel"], requester["conversation_ref"], requester["sender_ref"])
            ).fetchone()[0]
            if pending >= PENDING_MAX:
                raise StateGraphError(f"too_many_pending: at most {PENDING_MAX} confirmations wait for one person")
            code = _new_code(conn)
            text = confirmation_text(code, connection["label"], entry["raw_name"], arguments)
            if len(text) > TEXT_MAX:
                raise StateGraphError(f"too_long_to_confirm: the confirmation would be {len(text)} characters; "
                                      f"at most {TEXT_MAX} - ask for a shorter text, or the owner does it on the dashboard")
            conn.execute("INSERT INTO state_steward_confirmations(code,conn_id,raw_name,gw_name,args_json,args_sha256,"
                         "requester_kind,channel,conversation_ref,sender_ref,requester_message_id,audience,message_text,"
                         "status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                         (code, conn_id, entry["raw_name"], gw, canonical.decode("utf-8"),
                          args_hash(connection["conn_uid"], connection["generation"], entry["raw_name"], canonical),
                          requester["kind"], requester["channel"], requester["conversation_ref"], requester["sender_ref"],
                          requester_message_id, audience, text, _iso(now)))
        return {"code": code, "status": "pending", "text": text, "channel": requester["channel"],
                "reply_to_message_id": requester_message_id, "expires_at": _iso(now + TTL)}

    def get(self, code):
        """One record for run_confirmed: the stored public name and arguments, and its status."""
        from core import state_steward_connections as sc
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise StateGraphError("invalid_code")
        with self.store.transaction() as conn:
            self.steward._steward(conn)
            row = conn.execute("SELECT * FROM state_steward_confirmations WHERE code=?", (code,)).fetchone() \
                if _has_tables(conn) else None
        if row is None:
            raise StateNotFound(f"unknown_code: no confirmation {code}")
        status = row["status"]
        if status == "pending" and _utcnow() - _parse(row["created_at"]) > TTL:
            status = "expired"
        if status == "confirmed" and _utcnow() - _parse(row["confirmed_at"]) > TTL:
            status = "expired"
        return {"code": code, "status": status, "name": sc.public_name(row["conn_id"], row["gw_name"]),
                "arguments": json.loads(row["args_json"]), "created_at": row["created_at"],
                "confirmed_at": row["confirmed_at"]}

    def list_recent(self):
        """The owner's panel (its own route, no State query): the last 30 days, with the
        text that went out. The owner's browser session alone (`_owner` refuses an
        untrusted service as well)."""
        self.steward._owner()
        with self.store.transaction() as conn:
            if not _has_tables(conn):
                return {"confirmations": []}
            rows = conn.execute("SELECT code,conn_id,raw_name,gw_name,requester_kind,channel,status,created_at,"
                                "confirmed_at,consumed_at,result,message_text FROM state_steward_confirmations "
                                "WHERE created_at>=? ORDER BY created_at DESC LIMIT 200",
                                (_iso(_utcnow() - KEPT),)).fetchall()
        return {"confirmations": [{**dict(r), "result": json.loads(r["result"]) if r["result"] else None} for r in rows]}
