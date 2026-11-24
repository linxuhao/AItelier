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
- in a group turn (step 23's audience record is a bound group), the requester is the ONE
  sender of the turn's questions and the conversation is that group; questions of two
  senders are `ambiguous_requester`. The text says `<@管家> 确认 <code>`, is at most one
  WeChat message (1400 characters), passes the group exit check in the same transaction
  (else nothing is created or sent: `withheld`, the text kept for the owner) and spends
  the group's send budget (core.state_groups: an outbound row, request key confirm:<code>);
- stores the canonical JSON of the arguments and its hash
  sha256(conn_uid \\0 generation \\0 raw_name \\0 canonical arguments);
- writes, by code, the text the requester reads: a fixed header that alone carries the
  reply phrase, then every argument (short ones first), every control, bidirectional or
  zero-width character shown as an escape;
- matches the requester's exact reply `确认 <code>` / `取消 <code>`, or its fixed English
  alias `confirm <code>` / `cancel <code>` (same match, nothing looser), inside the
  transaction that writes the reply message (core.state_channels; a group's:
  core.state_groups.relay_group_message, before its quotas), on the same channel,
  conversation and sender, within 10 minutes;
- lets the gateway CONSUME one confirmed record whose hash equals the call it is about
  to forward (api/steward_gateway.py): one confirmation, one forward, never retried;
- in a group turn runs only that group's records (step 23.1, design-steward 7.8.9 step 5,
  7.7.24 item 4): `get` (run_confirmed) names the call's own turn and is refused
  not_this_conversation for a turn that is not the recorded one, or in a group turn for
  another group's record or the owner's. An owner turn may run a group's record (platform
  director 581): `get` then names the group and who confirmed it. A `get` that names its
  turn and passes ARMS the confirmed record for that turn (armed_turn, armed_at,
  armed_owner); `consume` (the gateway, whose request carries no turn) takes only a record
  armed within ARMED: whose turn may run a record was judged at arming, from the call's own
  turn (platform director 602 claim 7). An arming is valid only in the turn that made it:
  recording the next turn (core.state_groups.set_steward_turn_audience) disarms, in the same
  transaction, every record another turn armed (`disarm_other_turns`), and `consume` refuses
  an arming whose turn is not the recorded one - so no later turn (another group's, or an
  owner turn, silently) takes an arming it did not make. A direct call of an outward tool
  consumes only a record run_confirmed armed in that same turn; with none, it consumes
  nothing. A `get` without a turn (a caller that cannot name one) is the read of before step
  23.1: read-only, it arms nothing.

Both tables are private (core.state_privacy.PRIVATE_STATE_TABLES); records are kept 30 days.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import secrets as _secrets
from datetime import datetime, timedelta, timezone

from core import owner_text
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
# Step 23.1 (director 602 claim 7): run_confirmed's read arms a confirmed record for its turn.
ARM_COLUMNS = (("armed_turn", "TEXT"), ("armed_at", "TEXT"), ("armed_owner", "INTEGER"))

TTL = timedelta(minutes=10)             # to confirm, and from confirming to running
ARMED = timedelta(minutes=2)            # from run_confirmed's read to the gateway's consumption
CLOCK_SKEW = timedelta(minutes=2)       # a channel time this far ahead of now still counts
KEPT = timedelta(days=30)
PENDING_MAX = 3                         # per requester at a time
TEXT_MAX = 4000                         # a private chat or the dashboard (a group: GROUP_LIMITS reply_chars)
SHORT_MAX = 200
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE = re.compile(r"K[0-9A-HJKMNP-TV-Z]{4}")
CONFIRM, CANCEL = "确认 ", "取消 "
# The fixed English aliases (overfit review 重-17): the same whole-message exact match as the
# Chinese words - lower case only, one U+0020 space, the code as issued; never a synonym.
CONFIRM_EN, CANCEL_EN = "confirm ", "cancel "
REPLY_PREFIXES = ((CONFIRM, "confirm"), (CANCEL, "cancel"), (CONFIRM_EN, "confirm"), (CANCEL_EN, "cancel"))
DASHBOARD = "dashboard"
# Shown as an escape wherever they occur: C0/C1 controls and DEL, the bidirectional
# controls and marks (LRM, RLM, ALM), the zero-width characters, the line and paragraph
# separators. A newline stays a newline only inside a long field.
_BREAKS = ("\n", "\r", "\u2028", "\u2029")  # a field with one of these is never a one-line field
_HIDDEN = re.compile("[\u0000-\u001f\u007f-\u009f\u061c\u200b-\u200f\u2028-\u202e\u2060\u2066-\u2069\ufeff]")


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


def confirmation_text(code: str, label: str, raw_name: str, arguments: dict, mention: str | None = None) -> str:
    """Written by code, never the model: the header (the only place the reply phrase is),
    then the short fields (one line, at most 200 characters) and then the long ones. In a
    group the reply phrase starts with the group's @ prefix (`mention`): only an @-message
    reaches the steward, and the bridge compares what follows it. The words, and which of the
    two reply phrases (Chinese or its English alias; both are accepted) the header shows, are
    the owner's language's (core.owner_text)."""
    t = owner_text.texts()
    short, long = [], []
    for key in sorted(arguments):
        text = _value_text(arguments[key])
        if not any(b in text for b in _BREAKS) and len(text) <= SHORT_MAX:
            short.append(t["confirm_short"].format(key=_shown(key), text=_shown(text)))
        else:
            long.append(t["confirm_long"].format(key=_shown(key), text=_shown(text, keep_newlines=True)))
    at = f"{_shown(mention)} " if mention else ""
    header = t["confirm_header"].format(code=code, label=_shown(label), tool=_shown(raw_name), at=at,
                                        confirm=t["confirm_phrase"], cancel=t["cancel_phrase"])
    return "\n".join([header, *short, *long, t["confirm_footer"]])


def _new_code(conn) -> str:
    for _ in range(50):
        code = "K" + "".join(_secrets.choice(CROCKFORD) for _ in range(4))
        if conn.execute("SELECT 1 FROM state_steward_confirmations WHERE code=?", (code,)).fetchone() is None:
            return code
    raise StateGraphError("confirmation_codes_exhausted")


def _migrate(conn) -> None:
    from core.state_schema_migration import add_column_if_missing
    for column, definition in ARM_COLUMNS:
        add_column_if_missing(conn, "state_steward_confirmations", column, definition)


def ensure_schema(db) -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
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
    _migrate(conn)


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
def match_reply(conn, body, *, channel: str, sender_ref, written_at: datetime | None, message_id: str | None = None,
                conversation_ref=None):
    """In the transaction that writes a person's message: when the whole message (ends
    stripped) is `确认 <code>` / `取消 <code>` - or `confirm <code>` / `cancel <code>`, the same
    exact match in lower case - for a PENDING record of this very requester
    (channel, conversation and sender; the dashboard: the owner's browser session, server
    time), decide it. None for an ordinary message - also a code of someone else, of
    another channel or conversation (a group's code answered in a private chat, or the
    other way round), or no longer pending. `conversation_ref` is the group for a group
    message, else the sender's private chat (its sender_ref). `written_at` is the channel's
    time (None: undated); `message_id` the reply being written, kept as the record's
    confirm_message_ref when it decides."""
    if not isinstance(body, str) or not _has_tables(conn):
        return None
    phrase = body.strip()
    for prefix, action in REPLY_PREFIXES:
        if phrase.startswith(prefix) and CODE.fullmatch(phrase[len(prefix):]):
            code = phrase[len(prefix):]
            break
    else:
        return None
    row = conn.execute("SELECT * FROM state_steward_confirmations WHERE code=?", (code,)).fetchone()
    conversation = conversation_ref if conversation_ref is not None else sender_ref
    if row is None or row["status"] != "pending" or row["channel"] != channel or (
            channel != DASHBOARD and (row["sender_ref"] != sender_ref or row["conversation_ref"] != conversation)):
        return None
    out = {"code": code, "action": action}
    now = _utcnow()
    if action == "cancel":
        conn.execute("UPDATE state_steward_confirmations SET status='cancelled', confirm_message_ref=? WHERE code=? "
                     "AND status='pending'", (message_id, code))
        return {**out, "outcome": "cancelled"}
    if written_at is None:
        return {**out, "outcome": "refused", "refusal": "confirmation_undated"}
    created = _parse(row["created_at"])
    if written_at - now > CLOCK_SKEW:
        return {**out, "outcome": "refused", "refusal": "confirmation_clock_skew"}
    if written_at > created + TTL or now - written_at > TTL or written_at < created - CLOCK_SKEW:
        conn.execute("UPDATE state_steward_confirmations SET status='expired' WHERE code=? AND status='pending'", (code,))
        return {**out, "outcome": "expired"}
    conn.execute("UPDATE state_steward_confirmations SET status='confirmed', confirmed_at=?, confirm_message_ref=? "
                 "WHERE code=? AND status='pending'", (_iso(now), message_id, code))
    return {**out, "outcome": "confirmed"}


# ---------------------------------------------------------------- the gateway's consumption
def consume(conn, connection, raw_name: str, arguments) -> str | None:
    """In the gateway's write transaction: the confirmed record (within 10 minutes of its
    confirmation) whose hash equals this call - with the connection's CURRENT conn_uid and
    generation - and that run_confirmed armed within ARMED becomes `consumed`; its code, else
    None. Raises invalid_arguments.

    Step 23.1 (director 602 claim 7): the gateway's request carries no turn, so it never judges
    whose turn this is from the one-row turn audience record: the record's arming (`get` with
    the call's own turn, which judged that turn may run it) is the only scope, and it is never
    empty. A record no turn armed is consumed by no call. The arming is valid only in its own
    turn: the next turn's record disarms it (`disarm_other_turns`), and, failing closed should
    that ever be missed, an arming whose turn is not the recorded one is not consumed - the
    recorded turn only ever narrows what the arming allows, never widens it."""
    if not _has_tables(conn):
        return None
    _migrate(conn)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_steward_turn_audience'"
                    ).fetchone() is None:
        return None  # no turn was ever recorded, so none armed a record
    digest = args_hash(connection["conn_uid"], connection["generation"], raw_name, canonical_arguments(arguments))
    now = _utcnow()
    row = conn.execute("SELECT code FROM state_steward_confirmations WHERE conn_id=? AND raw_name=? AND args_sha256=? "
                       "AND status='confirmed' AND confirmed_at>=? AND armed_turn IS NOT NULL AND armed_at>=? "
                       "AND armed_turn=(SELECT turn FROM state_steward_turn_audience WHERE id=1) "
                       "ORDER BY armed_at DESC LIMIT 1",
                       (connection["conn_id"], raw_name, digest, _iso(now - TTL), _iso(now - ARMED))).fetchone()
    if row is None:
        return None
    taken = conn.execute("UPDATE state_steward_confirmations SET status='consumed', consumed_at=? WHERE code=? "
                         "AND status='confirmed'", (_iso(_utcnow()), row["code"])).rowcount
    return row["code"] if taken else None


def disarm_other_turns(conn, turn: str) -> int:
    """In the transaction that records the steward's next turn (core.state_groups
    set_steward_turn_audience): every confirmed record armed by another turn is disarmed, so a
    turn never consumes an arming it did not make (director 602 claim 7: judged by the call's
    own turn, guaranteed by code though turns do not overlap). A consumed or finished record
    keeps its arming as history: it can be consumed no more. The number disarmed."""
    if not _has_tables(conn):
        return 0
    _migrate(conn)
    return conn.execute("UPDATE state_steward_confirmations SET armed_turn=NULL, armed_at=NULL, armed_owner=NULL "
                        "WHERE status='confirmed' AND armed_turn IS NOT NULL AND armed_turn<>?", (turn,)).rowcount


def _group_origin(conn, row) -> tuple:
    """A group record's group label and the label of the member who confirmed it."""
    from core.state_groups import StateGroups
    named = conn.execute("SELECT label FROM state_owner_groups WHERE channel=? AND group_ref=?",
                         (row["channel"], row["conversation_ref"])).fetchone()
    label = named[0] if named else row["conversation_ref"]
    return label, StateGroups._label_of(conn, row["channel"], row["conversation_ref"], row["sender_ref"],
                                        row["requester_kind"])


def owner_turn_note(conn, code: str) -> str | None:
    """In the gateway's transaction, after `consume`: when an OWNER turn armed and took a record
    a family group confirmed (director 581 lets it), the line the forwarded result carries
    naming the group and who confirmed it - the same text run_confirmed shows. Whose turn armed
    it is the record's own (armed_owner), never the turn audience record. Else None."""
    row = conn.execute("SELECT * FROM state_steward_confirmations WHERE code=?", (code,)).fetchone()
    if row is None or row["audience"] != "group" or not row["armed_owner"]:
        return None
    label, confirmer = _group_origin(conn, row)
    return (f"[This action was asked for and confirmed in the family group 「{label}」 by "
            f"{confirmer or 'a member of that group'}; it ran in your owner's turn.]")


def finish(conn, code: str, ok: bool, summary: str | None) -> None:
    """The forward's outcome on the record: done / failed and an error code, never a result body."""
    conn.execute("UPDATE state_steward_confirmations SET status=?, result=? WHERE code=? AND status='consumed'",
                 ("done" if ok else "failed", json.dumps({"error": summary} if summary else {}), code))


# ---------------------------------------------------------------- the service
class StateStewardConfirmations:
    """The steward host's two commands and the owner's list (7.8.9, 7.8.10)."""

    def __init__(self, store, actor, driver_id=None, steward=None, groups=None):
        self.store, self.actor, self.driver_id, self.steward, self.groups = store, actor, driver_id, steward, groups
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            # Once per process per database file (core.state_schema_setup).
            from core.state_schema_setup import once
            once(store.db, "steward_confirmations", ensure_schema, store.db)

    def create(self, name, arguments, requester_message_id, audience="owner", question_ids=None):
        """The host's request for one confirmation: {code, text, channel, reply_to_message_id,
        expires_at} (a group's also group_ref and reply_to_ref, its question's channel ref).
        Refused: no_requester, ambiguous_requester, not an allowed outward tool of a connection
        this turn may use, a dev-mcp tool the job `code` does not pin, too many pending, too long
        to confirm, invalid arguments, a group's send budget or account cap; a group's text the
        exit check holds back answers {refused: withheld} (nothing created to confirm, nothing to
        send). `question_ids` (a group turn): every question the turn took - the audience record
        names only its first step's senders."""
        from core import state_steward_capabilities as caps
        from core import state_steward_connections as sc
        from core.state_steward import steward_project
        if audience not in ("owner", "group"):
            raise StateGraphError("invalid_request: audience is owner or group")
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
            # The turn's audience is step 23's record (core.state_groups), the one the gateway
            # judges by; the host's word alone is not enough. No record is not the owner's.
            recorded = sc.audience_of(conn)
            if recorded != audience:
                raise StateGraphError(f"ambiguous_requester: this turn is not recorded as the {audience}'s")
            group = None
            if audience == "owner":
                requester = requester_of(conn, requester_message_id, home)
            else:
                requester, group = self._group_requester(conn, requester_message_id, question_ids)
            connection = conn.execute("SELECT * FROM state_steward_connections WHERE conn_id=?", (conn_id,)).fetchone()
            entry = next((e for e in sc.effective(conn) if e["conn_id"] == conn_id and e["gw_name"] == gw), None)
            if connection is None or entry is None:
                raise StateGraphError("not_outward: not an allowed tool of a usable connection")
            if not entry["outward"]:
                raise StateGraphError("not_outward: this tool is not outward; call it directly")
            if entry["visibility"] == "private" and audience != "owner":
                raise StateGraphError("not_visible_in_this_turn")
            # The gateway's dev-mcp condition (step 24.4c): only a tool the owner pinned on
            # `code` - never ask the owner to confirm a call the gateway refuses.
            if sc.is_dev_mcp(connection) and (
                    connection["visibility"] != "private" or entry["raw_name"].startswith(sc.DEV_MCP_SUBAGENT)
                    or not caps.code_pin(conn, connection["conn_uid"], entry["raw_name"])):
                raise StateGraphError("tool_not_allowed: this dev-mcp tool is not pinned on the job code")
            # The gateway's own argument checks (at any depth; on a public connection no
            # url-named key unless unblocked): never ask a person to confirm a refused call.
            if sc.blocked_present(arguments, entry["blocked_args"]) or (
                    connection["visibility"] == "public"
                    and sc.url_argument_present(arguments, sc.unblocked_url_paths(entry))):
                raise StateGraphError("argument_blocked")
            conn.execute("DELETE FROM state_steward_confirmations WHERE created_at<?", (_iso(now - KEPT),))
            conn.execute("DELETE FROM state_steward_dashboard_messages WHERE at<?", (_iso(now - KEPT),))
            moment = None
            if group is not None:
                from core import state_groups
                moment = state_groups._now()
                refusal = self.groups.send_refusal(conn, group["channel"], group["group_ref"], moment)
                if refusal is not None:
                    raise StateGraphError(refusal)
            pending = conn.execute(
                "SELECT COUNT(*) FROM state_steward_confirmations WHERE status='pending' AND created_at>=? AND channel=? "
                "AND conversation_ref IS ? AND sender_ref IS ?",
                (_iso(now - TTL), requester["channel"], requester["conversation_ref"], requester["sender_ref"])
            ).fetchone()[0]
            if pending >= PENDING_MAX:
                raise StateGraphError(f"too_many_pending: at most {PENDING_MAX} confirmations wait for one person")
            code = _new_code(conn)
            text = confirmation_text(code, connection["label"], entry["raw_name"], arguments,
                                     mention=group["mention_prefix"] if group is not None else None)
            limit = TEXT_MAX
            if group is not None:
                from core.state_groups import GROUP_LIMITS
                limit = GROUP_LIMITS["reply_chars"]  # one WeChat message (7.7.7)
            if len(text) > limit:
                raise StateGraphError(f"too_long_to_confirm: the confirmation would be {len(text)} characters; "
                                      f"at most {limit} - ask for a shorter text, or the owner does it on the dashboard")
            withheld = self.groups.exit_check(conn, text, moment) if group is not None else None
            conn.execute("INSERT INTO state_steward_confirmations(code,conn_id,raw_name,gw_name,args_json,args_sha256,"
                         "requester_kind,channel,conversation_ref,sender_ref,requester_message_id,audience,message_text,"
                         "status,created_at,result) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (code, conn_id, entry["raw_name"], gw, canonical.decode("utf-8"),
                          args_hash(connection["conn_uid"], connection["generation"], entry["raw_name"], canonical),
                          requester["kind"], requester["channel"], requester["conversation_ref"], requester["sender_ref"],
                          requester_message_id, audience, text,
                          # A withheld text is kept for the owner's panel and can never be confirmed.
                          "cancelled" if withheld else "pending", _iso(now),
                          json.dumps({"withheld": withheld}) if withheld else None))
            # The asker's question's channel ref (step 23 Slack, D8): a channel that threads
            # posts the text under it (WeChat ignores it).
            reply_to_ref = conn.execute("SELECT channel_message_ref FROM state_group_messages WHERE message_id=?",
                                        (requester_message_id,)).fetchone()[0] if group is not None else None
            if withheld:
                from core.state_groups import WITHHELD_REASONS
                result = {"refused": "withheld", "reason": WITHHELD_REASONS[withheld]}
            elif group is not None:
                # The group's copy is one outbound row first (the send budget and the account's
                # hourly cap count it); the host sends it with this request key.
                self.groups._out_row(conn, group["channel"], group["group_ref"], "confirmation", text, moment,
                                     request_key=f"confirm:{code}")
        if withheld:
            # Committed above (the code a live-code hit retired, the kept text); nothing is sent.
            return result
        return {"code": code, "status": "pending", "text": text, "channel": requester["channel"],
                **({"group_ref": group["group_ref"], "reply_to_ref": reply_to_ref} if group is not None else {}),
                "reply_to_message_id": requester_message_id, "expires_at": _iso(now + TTL)}

    def _group_requester(self, conn, message_id, question_ids):
        """(requester, group) of a group turn (7.8.9): the recorded audience is a group bound
        now; every sender of the turn - the record's and the turn's questions' - is ONE
        sender_ref, else ambiguous_requester; `message_id` is an accepted question of that
        group from that sender, else no_requester. The audience keeps the turn's senders, not
        its question ids: that `message_id` is a question OF THIS TURN rests on the plugin,
        which passes the turn's newest question."""
        from core.state_groups import StateGroups
        row = conn.execute("SELECT * FROM state_steward_turn_audience WHERE id=1").fetchone()
        group = StateGroups._live_group(conn, row["channel"], row["group_ref"]) if row is not None else None
        if group is None:
            raise StateGraphError("ambiguous_requester: this turn is not a bound group's")
        ids = [i for i in (question_ids or []) if isinstance(i, str) and i][:20]
        if isinstance(message_id, str) and message_id not in ids:
            ids.append(message_id)
        questions = conn.execute(
            f"SELECT message_id, sender_ref FROM state_group_messages WHERE message_id IN ({','.join('?' * len(ids))}) "
            "AND direction='in' AND accepted=1 AND channel=? AND group_ref=?",
            (*ids, row["channel"], row["group_ref"])).fetchall() if ids else []
        senders = set(json.loads(row["sender_refs_json"])) | {q["sender_ref"] for q in questions}
        if len(senders) != 1:
            raise StateGraphError("ambiguous_requester: the questions of this turn come from more than one person; "
                                  "the one who wants it asks again alone")
        sender = next(iter(senders))
        if not any(q["message_id"] == message_id and q["sender_ref"] == sender for q in questions):
            raise StateGraphError("no_requester: an outward action starts only from an accepted group question "
                                  "of the person who asked")
        account = conn.execute("SELECT sender_ref FROM state_owner_channels WHERE channel=? AND revoked_at IS NULL",
                               (row["channel"],)).fetchone()
        kind = "owner" if account is not None and account[0] == sender else "member"
        return ({"kind": kind, "channel": row["channel"], "conversation_ref": row["group_ref"], "sender_ref": sender},
                dict(group))

    def get(self, code, turn=None):
        """One record for run_confirmed in the turn `turn` (the plugin's open turn): the stored
        public name and arguments, and its status. The turn must be the one the host recorded
        last (else not_this_conversation: whose turn this is cannot be told); its audience is
        read only from the record of THAT turn. In a group turn only a record of that group
        (channel, conversation_ref) is given - another group's or the owner's is
        not_this_conversation (step 23.1, 7.8.9 step 5); in an owner turn any record, and a
        group's names its group and who confirmed it (`group`, `confirmed_by`, platform
        director 581: the owner runs it knowingly). A confirmed record still in time is armed
        for this turn: the gateway consumes only an armed record (director 602 claim 7).

        Without `turn` (a caller that cannot name one, director 602 defect 8): the read of
        before step 23.1 - the record's name, arguments and status, read-only; nothing is armed,
        so nothing it returns can be consumed."""
        from core import state_steward_connections as sc
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise StateGraphError("invalid_code")
        if turn is not None and (not isinstance(turn, str) or not turn):
            raise StateGraphError("invalid_request: turn is the plugin's open turn")
        group_label = confirmer = None
        with self.store.transaction(write=turn is not None) as conn:
            self.steward._steward(conn)
            row = conn.execute("SELECT * FROM state_steward_confirmations WHERE code=?", (code,)).fetchone() \
                if _has_tables(conn) else None
            if row is None:
                raise StateNotFound(f"unknown_code: no confirmation {code}")
            if turn is not None:
                try:
                    audience = conn.execute("SELECT * FROM state_steward_turn_audience WHERE id=1 AND turn=?",
                                            (turn,)).fetchone()
                except sqlite3.OperationalError:  # no table yet: no turn was ever recorded
                    audience = None
                if audience is None:
                    raise StateGraphError("not_this_conversation: this request is not the recorded turn's")
                owner = audience["audience"] == "owner"
                if not owner and (row["audience"] != "group" or row["channel"] != audience["channel"]
                                  or row["conversation_ref"] != audience["group_ref"]):
                    raise StateGraphError("not_this_conversation: a confirmation runs only in the conversation it was "
                                          "asked for")
                if owner and row["audience"] == "group":
                    group_label, confirmer = _group_origin(conn, row)
                now = _utcnow()
                if row["status"] == "confirmed" and now - _parse(row["confirmed_at"]) <= TTL:
                    _migrate(conn)
                    conn.execute("UPDATE state_steward_confirmations SET armed_turn=?, armed_at=?, armed_owner=? "
                                 "WHERE code=? AND status='confirmed'", (turn, _iso(now), 1 if owner else 0, code))
        status = row["status"]
        if status == "pending" and _utcnow() - _parse(row["created_at"]) > TTL:
            status = "expired"
        if status == "confirmed" and _utcnow() - _parse(row["confirmed_at"]) > TTL:
            status = "expired"
        return {"code": code, "status": status, "name": sc.public_name(row["conn_id"], row["gw_name"]),
                "arguments": json.loads(row["args_json"]), "created_at": row["created_at"],
                "confirmed_at": row["confirmed_at"],
                **({"group": group_label, "confirmed_by": confirmer} if group_label is not None else {})}

    def cancel(self, code):
        """The host voids a PENDING record whose text never reached the requester (the
        message refused, the chat channel did not take it): nobody can confirm it then."""
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise StateGraphError("invalid_code")
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn)
            row = conn.execute("SELECT status FROM state_steward_confirmations WHERE code=?", (code,)).fetchone() \
                if _has_tables(conn) else None
            if row is None:
                raise StateNotFound(f"unknown_code: no confirmation {code}")
            voided = conn.execute("UPDATE state_steward_confirmations SET status='cancelled' WHERE code=? "
                                  "AND status='pending'", (code,)).rowcount
            if voided and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                       "name='state_group_messages'").fetchone() is not None:
                # A group's copy keeps its outbound row (the send budget still counts it); the
                # owner's transcript shows it never reached the group.
                conn.execute("UPDATE state_group_messages SET withheld_reason='not_delivered', original=body "
                             "WHERE direction='out' AND kind='confirmation' AND request_key=?", (f"confirm:{code}",))
            status = conn.execute("SELECT status FROM state_steward_confirmations WHERE code=?", (code,)).fetchone()[0]
        return {"code": code, "status": status}

    def list_recent(self):
        """The owner's panel (its own route, no State query): the last 30 days, with the
        text that went out. The owner's browser session alone (`_owner` refuses an
        untrusted service as well)."""
        self.steward._owner()
        with self.store.transaction() as conn:
            if not _has_tables(conn):
                return {"confirmations": []}
            rows = conn.execute("SELECT code,conn_id,raw_name,gw_name,requester_kind,channel,status,created_at,"
                                "confirmed_at,confirm_message_ref,consumed_at,result,message_text "
                                "FROM state_steward_confirmations "
                                "WHERE created_at>=? ORDER BY created_at DESC LIMIT 200",
                                (_iso(_utcnow() - KEPT),)).fetchall()
        return {"confirmations": [{**dict(r), "result": json.loads(r["result"]) if r["result"] else None} for r in rows]}
