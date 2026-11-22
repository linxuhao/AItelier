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
- matches the requester's exact reply `确认 <code>` / `取消 <code>` inside the
  transaction that writes the reply message (core.state_channels; a group's:
  core.state_groups.relay_group_message, before its quotas), on the same channel,
  conversation and sender, within 10 minutes;
- lets the gateway CONSUME one confirmed record whose hash equals the call it is about
  to forward (api/steward_gateway.py): one confirmation, one forward, never retried;
- in a group turn runs only that group's records (step 23.1, design-steward 7.8.9 step 5,
  7.7.24 item 4): `get` (run_confirmed) names the turn and is refused not_this_conversation
  for another group's record or the owner's, and `consume` takes no record of another
  conversation than the recorded group turn's. An owner turn may run a group's record
  (platform director 581): `get` then names the group and who confirmed it.

Both tables are private (core.state_privacy.PRIVATE_STATE_TABLES); records are kept 30 days.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
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
TEXT_MAX = 4000                         # a private chat or the dashboard (a group: GROUP_LIMITS reply_chars)
SHORT_MAX = 200
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE = re.compile(r"K[0-9A-HJKMNP-TV-Z]{4}")
CONFIRM, CANCEL = "确认 ", "取消 "
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
    reaches the steward, and the bridge compares what follows it."""
    short, long = [], []
    for key in sorted(arguments):
        text = _value_text(arguments[key])
        if not any(b in text for b in _BREAKS) and len(text) <= SHORT_MAX:
            short.append(f"{_shown(key)}：{_shown(text)}")
        else:
            long.append(f"{_shown(key)}（长文）：\n{_shown(text, keep_newlines=True)}")
    at = f"{_shown(mention)} " if mention else ""
    header = (f"请确认（{code}，10 分钟内有效）：用「{_shown(label)}」执行 {_shown(raw_name)}。"
              f"回复「{at}{CONFIRM}{code}」执行，「{at}{CANCEL}{code}」作废。")
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
def match_reply(conn, body, *, channel: str, sender_ref, written_at: datetime | None, message_id: str | None = None,
                conversation_ref=None):
    """In the transaction that writes a person's message: when the whole message (ends
    stripped) is `确认 <code>` / `取消 <code>` for a PENDING record of this very requester
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
    for prefix, action in ((CONFIRM, "confirm"), (CANCEL, "cancel")):
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
    generation - becomes `consumed`; its code, else None. Raises invalid_arguments."""
    if not _has_tables(conn):
        return None
    digest = args_hash(connection["conn_uid"], connection["generation"], raw_name, canonical_arguments(arguments))
    since = _iso(_utcnow() - TTL)
    # Step 23.1 (7.8.9 step 6): the gateway carries no turn, so it judges by the recorded
    # audience alone (7.7.8a "边界"): in a group turn only that group's records; in an owner
    # turn any record (director 581); no record of the turn is the owner's nothing (fail closed).
    scope, args = "", []
    try:
        audience = conn.execute("SELECT audience,channel,group_ref FROM state_steward_turn_audience WHERE id=1").fetchone()
    except sqlite3.OperationalError:  # no table yet: no turn was ever recorded
        audience = None
    if audience is None:
        return None
    if audience["audience"] != "owner":
        scope, args = " AND audience='group' AND channel=? AND conversation_ref=?", [audience["channel"], audience["group_ref"]]
    row = conn.execute("SELECT code FROM state_steward_confirmations WHERE conn_id=? AND raw_name=? AND args_sha256=? "
                       "AND status='confirmed' AND confirmed_at>=?" + scope + " ORDER BY confirmed_at LIMIT 1",
                       (connection["conn_id"], raw_name, digest, since, *args)).fetchone()
    if row is None:
        return None
    taken = conn.execute("UPDATE state_steward_confirmations SET status='consumed', consumed_at=? WHERE code=? "
                         "AND status='confirmed'", (_iso(_utcnow()), row["code"])).rowcount
    return row["code"] if taken else None


def _group_origin(conn, row) -> tuple:
    """A group record's group label and the label of the member who confirmed it."""
    from core.state_groups import StateGroups
    named = conn.execute("SELECT label FROM state_owner_groups WHERE channel=? AND group_ref=?",
                         (row["channel"], row["conversation_ref"])).fetchone()
    label = named[0] if named else row["conversation_ref"]
    return label, StateGroups._label_of(conn, row["channel"], row["conversation_ref"], row["sender_ref"],
                                        row["requester_kind"])


def owner_turn_note(conn, code: str) -> str | None:
    """In the gateway's transaction, after `consume`: when an OWNER turn took a record a family
    group confirmed (director 581 lets it), the line the forwarded result carries naming the
    group and who confirmed it - the same text run_confirmed shows, so a direct call with the
    same arguments is never a silent use of a group's confirmation. Else None."""
    row = conn.execute("SELECT * FROM state_steward_confirmations WHERE code=?", (code,)).fetchone()
    try:
        audience = conn.execute("SELECT audience FROM state_steward_turn_audience WHERE id=1").fetchone()
    except sqlite3.OperationalError:
        audience = None
    if row is None or row["audience"] != "group" or audience is None or audience["audience"] != "owner":
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
            ensure_schema(store.db)

    def create(self, name, arguments, requester_message_id, audience="owner", question_ids=None):
        """The host's request for one confirmation: {code, text, channel, reply_to_message_id,
        expires_at} (a group's also group_ref). Refused: no_requester, ambiguous_requester,
        not an allowed outward tool of a connection this turn may use, too many pending, too
        long to confirm, invalid arguments, a group's send budget or account cap; a group's
        text the exit check holds back answers {refused: withheld} (nothing created to confirm,
        nothing to send). `question_ids` (a group turn): every question the turn took - the
        audience record names only its first step's senders."""
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
                **({"group_ref": group["group_ref"]} if group is not None else {}),
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

    def get(self, code, turn):
        """One record for run_confirmed in the turn `turn` (the plugin's open turn): the stored
        public name and arguments, and its status. The turn must be the one the host recorded
        last (else not_this_conversation: whose turn this is cannot be told). In a group turn
        only a record of that group (channel, conversation_ref) is given - another group's or
        the owner's is not_this_conversation (step 23.1, 7.8.9 step 5); in an owner turn any
        record, and a group's names its group and who confirmed it (`group`, `confirmed_by`,
        platform director 581: the owner runs it knowingly)."""
        from core import state_steward_connections as sc
        if not isinstance(code, str) or not CODE.fullmatch(code):
            raise StateGraphError("invalid_code")
        group_label = confirmer = None
        with self.store.transaction() as conn:
            self.steward._steward(conn)
            row = conn.execute("SELECT * FROM state_steward_confirmations WHERE code=?", (code,)).fetchone() \
                if _has_tables(conn) else None
            if row is None:
                raise StateNotFound(f"unknown_code: no confirmation {code}")
            try:
                audience = conn.execute("SELECT * FROM state_steward_turn_audience WHERE id=1").fetchone()
            except sqlite3.OperationalError:  # no table yet: no turn was ever recorded
                audience = None
            if audience is None or not isinstance(turn, str) or audience["turn"] != turn:
                raise StateGraphError("not_this_conversation: this request is not the recorded turn's")
            if audience["audience"] != "owner" and (row["audience"] != "group" or row["channel"] != audience["channel"]
                                                    or row["conversation_ref"] != audience["group_ref"]):
                raise StateGraphError("not_this_conversation: a confirmation runs only in the conversation it was "
                                      "asked for")
            if audience["audience"] == "owner" and row["audience"] == "group":
                group_label, confirmer = _group_origin(conn, row)
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
