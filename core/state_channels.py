"""The owner's chat channels to the steward: Slack and WeChat (step 19, design-steward 7.2).

The steward's host receives a message on a channel and relays it here
(`relay_owner_message`, steward only). The backend trusts ONE bound account per
channel (`state_owner_channels`); the owner binds it with a one-time code issued
on the dashboard (`issue_code`, owner session only) and sent to the bot from that
account (`bind_owner_channel`). An accepted message is written into the steward's
conversation exactly as a dashboard message is (core.driver_inbox.write_user_half:
same row, same delivery, so the steward wakes the same way), and its source goes to
the side table `state_message_sources` - never onto the message row, which is on
the 10-second unread poll path. A message with no source row came from the
dashboard; "dashboard" is never written as a value.

The inbound door is bounded (M3): a body is at most BODY_MAX_BYTES, and each channel
takes at most RATE_PER_MINUTE / RATE_PER_HOUR accepted messages, because every one
is a paid steward turn and a taken-over account must not become endless wakes. A
refused body is logged by its length only, never its text (B3).

Chat approval: an owner message (relayed, or written on the dashboard into the
steward's conversation) whose body, stripped of surrounding whitespace, EQUALS
`同意 proposal <id>` or `拒绝 proposal <id>` for an existing proposal approves or
declines it through the dashboard's own path, as of the proposal version current
when the message was written. This is an intent check, not authentication: no
case folding, no full-width spaces, no synonyms, and it must never be widened into
a fuzzy match. The model never decides: no steward command approves; this code does,
on the owner's exact words.

All three tables are private State tables (core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from core.driver_inbox import USER_SUBJECT
from core.message_body_guard import refuse_credential_body
from core.state_graph import StateConflict, StateGraphError, StateNotFound, canonical, now
from core.state_privacy import UntrustedDatabase, writer_only_read
from core.state_steward import (ARCHIVE_KINDS, ProposalConflict, _hash, impact_is_empty, steward_project,
                                stop_impact)

log = logging.getLogger(__name__)

CHANNELS = ("slack", "wechat")
SCHEMA = """
CREATE TABLE IF NOT EXISTS state_owner_channels (
 channel TEXT PRIMARY KEY CHECK(channel IN ('slack','wechat')),
 sender_ref TEXT CHECK(sender_ref IS NULL OR sender_ref<>''),
 bound_at TEXT, bound_by TEXT, revoked_at TEXT,
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)));
CREATE UNIQUE INDEX IF NOT EXISTS state_owner_channels_one_default ON state_owner_channels(is_default)
 WHERE is_default=1;
CREATE TABLE IF NOT EXISTS state_owner_channel_codes (
 code_hash TEXT PRIMARY KEY,
 channel TEXT NOT NULL CHECK(channel IN ('slack','wechat')),
 issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT);
CREATE TABLE IF NOT EXISTS state_message_sources (
 message_id TEXT PRIMARY KEY,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 channel TEXT NOT NULL CHECK(channel IN ('slack','wechat')),
 sender_ref TEXT CHECK(sender_ref IS NULL OR sender_ref<>''),
 channel_message_ref TEXT CHECK(channel_message_ref IS NULL OR channel_message_ref<>''),
 at TEXT NOT NULL, error TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS state_message_sources_inbound ON state_message_sources(channel,channel_message_ref)
 WHERE direction='in';
CREATE INDEX IF NOT EXISTS state_message_sources_rate ON state_message_sources(channel,direction,at);
"""

BODY_MAX_BYTES = 4096
CODE_TTL = timedelta(minutes=10)
MESSAGE_SOURCES_MAX = 200
APPROVE, DECLINE = "同意 proposal ", "拒绝 proposal "
DRIVER_KINDS = ("start_driver", "stop_driver")


def rate_limits() -> tuple[int, int]:
    """Accepted relays per channel per minute and per hour (env overrides allowed)."""
    return (int(os.environ.get("AITELIER_CHANNEL_RATE_PER_MINUTE") or 6),
            int(os.environ.get("AITELIER_CHANNEL_RATE_PER_HOUR") or 60))


def _now() -> datetime:
    """The channels' clock (aware UTC); tests replace it."""
    return datetime.now(timezone.utc)


def _code_hash(code: str) -> str:
    return hashlib.sha256(("owner-channel-code:" + code).encode("utf-8")).hexdigest()


def require_bound(conn, channel, sender_ref):
    """`sender_ref` is the channel's CURRENT binding, read on the caller's connection."""
    row = conn.execute("SELECT sender_ref FROM state_owner_channels WHERE channel=? AND revoked_at IS NULL",
                       (channel,)).fetchone()
    if row is None or row[0] is None or row[0] != sender_ref:
        raise StateGraphError("channel_sender_not_bound")


def match_phrase(conn, body):
    """{action, proposal_id, expected_version, phrase} when `body` is exactly an
    approval phrase for an existing proposal, else None (an ordinary message).

    str.strip() removes Unicode whitespace at the ENDS only; inside, the one space
    is U+0020 and the id is compared byte for byte (SQLite's default BINARY
    collation), so `同意 proposal P-1` does not name `p-1`."""
    phrase = body.strip()
    for prefix, action in ((APPROVE, "approve"), (DECLINE, "decline")):
        if phrase.startswith(prefix):
            row = conn.execute("SELECT version FROM state_steward_proposals WHERE proposal_id=?",
                               (phrase[len(prefix):],)).fetchone()
            if row is not None:
                return {"action": action, "proposal_id": phrase[len(prefix):], "expected_version": row[0],
                        "phrase": phrase}
    return None


def _refusal(exc) -> str:
    return str(exc).split(":", 1)[0]


class StateChannels:
    def __init__(self, store, actor, driver_id=None, steward=None, inbox=None):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        self.steward, self.inbox = steward, inbox
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            with store.db.get_connection() as conn:
                conn.executescript(SCHEMA)
                conn.commit()

    # ---------------------------------------------------------------- the steward's host
    def relay_owner_message(self, project_id, request_key, channel, sender_ref, channel_message_ref, body):
        """One owner message from a channel into the steward's conversation, then the
        chat approval check. Idempotent per request_key and per channel message."""
        if channel not in CHANNELS:
            raise StateGraphError(f"invalid_channel: channel must be one of {', '.join(CHANNELS)}")
        if not isinstance(body, str) or not body.strip():
            raise StateGraphError("empty_body: body must be nonempty text")
        size = len(body.encode("utf-8"))
        if size > BODY_MAX_BYTES:
            self._log_refusal("body_too_long", channel, sender_ref, size)
            raise StateGraphError(f"body_too_long: {size} bytes; at most {BODY_MAX_BYTES} bytes")
        refuse_credential_body(body)
        request_hash = _hash([channel, sender_ref, channel_message_ref, body])
        home = steward_project()
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "relay_owner_message", request_key, request_hash)
            if replay is not None:
                return replay
            seen = conn.execute("SELECT message_id FROM state_message_sources WHERE direction='in' AND channel=? "
                                "AND channel_message_ref=?", (channel, channel_message_ref)).fetchone()
            if seen is not None:
                first = conn.execute("SELECT result_json FROM driver_inbox_requests WHERE actor=? AND operation=? "
                                     "AND request_key=?", (self.actor, "relay_owner_message:ref",
                                                           f"{channel}:{channel_message_ref}")).fetchone()
                return {**(json.loads(first[0]) if first else {"message_id": seen[0], "approval": None}),
                        "duplicate": True}
            moment = _now()
            per_minute, per_hour = rate_limits()
            for window, limit in ((timedelta(minutes=1), per_minute), (timedelta(hours=1), per_hour)):
                accepted = conn.execute("SELECT COUNT(*) FROM state_message_sources WHERE channel=? "
                                        "AND direction='in' AND at>?",
                                        (channel, (moment - window).isoformat())).fetchone()[0]
                if accepted >= limit:
                    self._log_refusal("rate_limited", channel, sender_ref, size)
                    raise StateGraphError(f"rate_limited: {channel} took {accepted} messages in the last "
                                          f"{int(window.total_seconds())} s; at most {limit}")
            try:
                require_bound(conn, channel, sender_ref)
            except StateGraphError:
                self._log_refusal("channel_sender_not_bound", channel, sender_ref, size)
                raise
            self.store._project(conn, home)
            message_id, _, _ = self.inbox.write_user_half(conn, home, USER_SUBJECT, body)
            conn.execute("INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,"
                         "channel_message_ref,at) VALUES(?,'in',?,?,?,?)",
                         (message_id, channel, sender_ref, channel_message_ref, moment.isoformat()))
            matched = match_phrase(conn, body)
            result = {"message_id": message_id, "duplicate": False, "approval": None}
            self.steward._record(conn, "relay_owner_message", request_key, request_hash, result)
            # The per-message record: a second relay of this channel message (under any
            # request_key) answers the first result.
            self.steward._record(conn, "relay_owner_message:ref", f"{channel}:{channel_message_ref}",
                                 request_hash, result)
        self.steward._notify()
        if matched is not None:
            carrier = {"channel": channel, "sender_ref": sender_ref, "channel_message_ref": channel_message_ref,
                       "phrase": matched["phrase"]}
            result = {**result, "approval": self._decide(matched, f"owner:{channel}:{sender_ref}", carrier)}
            with self.store.transaction(write=True) as conn:
                conn.execute("UPDATE driver_inbox_requests SET result_json=? WHERE actor=? AND "
                             "((operation='relay_owner_message' AND request_key=?) OR "
                             "(operation='relay_owner_message:ref' AND request_key=?))",
                             (json.dumps(result), self.actor, request_key, f"{channel}:{channel_message_ref}"))
        return result

    @staticmethod
    def _log_refusal(code, channel, sender_ref, size):
        # B3: who, where, when and how long - never the body.
        log.warning("owner channel message refused: %s channel=%s sender_ref=%s at=%s bytes=%d",
                    code, channel, sender_ref, now(), size)

    def bind_owner_channel(self, project_id, request_key, channel, sender_ref, code):
        """Bind `sender_ref` as the channel's owner account with an unexpired, unused
        code issued for that channel. A previous binding of the channel is replaced."""
        request_hash = _hash([channel, sender_ref, code])
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "bind_owner_channel", request_key, request_hash)
            if replay is not None:
                return replay
            well_formed = len(code) == 6 and code.isascii() and code.isdigit()
            row = conn.execute("SELECT * FROM state_owner_channel_codes WHERE code_hash=?",
                               (_code_hash(code),)).fetchone() if well_formed else None
            if row is None or row["channel"] != channel:
                raise StateGraphError("invalid_code")
            if row["used_at"] is not None:
                raise StateGraphError("code_used")
            if datetime.fromisoformat(row["expires_at"]) <= _now():
                raise StateGraphError("code_expired")
            stamp = _now().isoformat()
            conn.execute("UPDATE state_owner_channel_codes SET used_at=? WHERE code_hash=?", (stamp, row["code_hash"]))
            conn.execute("INSERT INTO state_owner_channels(channel,sender_ref,bound_at,bound_by,revoked_at) "
                         "VALUES(?,?,?,?,NULL) ON CONFLICT(channel) DO UPDATE SET sender_ref=excluded.sender_ref,"
                         "bound_at=excluded.bound_at,bound_by=excluded.bound_by,revoked_at=NULL",
                         (channel, sender_ref, stamp, self.actor))
            out = self.steward._record(conn, "bind_owner_channel", request_key, request_hash,
                                       {"bound": True, "channel": channel, "sender_ref": sender_ref})
        self.steward._notify()
        return out

    def record_channel_delivery(self, project_id, request_key, message_id, channel, channel_message_ref=None,
                                error=None):
        """Where a steward message was mirrored to (or why it was not): once per message."""
        request_hash = _hash([message_id, channel, channel_message_ref, error])
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "record_channel_delivery", request_key, request_hash)
            if replay is not None:
                return replay
            sent = conn.execute("SELECT 1 FROM driver_inbox_messages WHERE message_id=? AND project_id=? "
                                "AND audience='user' AND sender_driver_id=?",
                                (message_id, steward_project(), self.driver_id)).fetchone()
            if sent is None:
                raise StateNotFound("unknown_message: not a message this steward sent the owner")
            if conn.execute("SELECT 1 FROM state_message_sources WHERE message_id=?", (message_id,)).fetchone():
                raise StateConflict("delivery_recorded: this message's delivery is already recorded")
            conn.execute("INSERT INTO state_message_sources(message_id,direction,channel,channel_message_ref,at,"
                         "error) VALUES(?,'out',?,?,?,?)",
                         (message_id, channel, channel_message_ref, _now().isoformat(), error))
            return self.steward._record(conn, "record_channel_delivery", request_key, request_hash,
                                        {"recorded": True, "message_id": message_id, "channel": channel})

    # ---------------------------------------------------------------- reads (owner or steward)
    def _reader(self, conn):
        if not self.steward.is_owner_session():
            try:
                self.steward._steward(conn)
            except StateGraphError:
                raise StateGraphError("owner_or_steward_required") from None

    @writer_only_read("list_owner_channels")
    def list_owner_channels(self):
        with self.store.transaction() as conn:
            self._reader(conn)
            rows = {r["channel"]: r for r in conn.execute("SELECT * FROM state_owner_channels")}
        channels, default = [], None
        for channel in CHANNELS:
            row = rows.get(channel)
            bound = row is not None and row["sender_ref"] is not None and row["revoked_at"] is None
            if row is not None and row["is_default"]:
                default = channel
            channels.append({"channel": channel, "bound": bound, "sender_ref": row["sender_ref"] if row else None,
                             "bound_at": row["bound_at"] if row else None,
                             "revoked_at": row["revoked_at"] if row else None,
                             "is_default": bool(row is not None and row["is_default"])})
        return {"channels": channels, "default_channel": default}

    @writer_only_read("message_sources")
    def message_sources(self, message_ids):
        if not isinstance(message_ids, list) or len(message_ids) > MESSAGE_SOURCES_MAX:
            raise StateGraphError(f"invalid_request: at most {MESSAGE_SOURCES_MAX} message_ids")
        with self.store.transaction() as conn:
            self._reader(conn)
            rows = conn.execute("SELECT * FROM state_message_sources WHERE message_id IN "
                                f"({','.join('?' * len(message_ids))})", message_ids).fetchall() if message_ids else []
        return {"sources": {r["message_id"]: {"direction": r["direction"], "channel": r["channel"],
                                              "sender_ref": r["sender_ref"],
                                              "channel_message_ref": r["channel_message_ref"], "at": r["at"],
                                              "error": r["error"]} for r in rows}}

    # ---------------------------------------------------------------- the owner (dashboard)
    def issue_code(self, channel):
        """A fresh 6-digit code for binding `channel`; the plaintext exists only in
        this answer. Any earlier unused code of the channel stops working."""
        self.steward._owner()
        moment = _now()
        with self.store.transaction(write=True) as conn:
            conn.execute("DELETE FROM state_owner_channel_codes WHERE channel=? AND used_at IS NULL", (channel,))
            while True:
                code = f"{secrets.randbelow(10 ** 6):06d}"
                if conn.execute("SELECT 1 FROM state_owner_channel_codes WHERE code_hash=?",
                                (_code_hash(code),)).fetchone() is None:
                    break
            conn.execute("INSERT INTO state_owner_channel_codes(code_hash,channel,issued_at,expires_at) "
                         "VALUES(?,?,?,?)",
                         (_code_hash(code), channel, moment.isoformat(), (moment + CODE_TTL).isoformat()))
        return {"code": code, "expires_at": (moment + CODE_TTL).isoformat()}

    def unbind(self, channel):
        self.steward._owner()
        with self.store.transaction(write=True) as conn:
            conn.execute("UPDATE state_owner_channels SET revoked_at=? WHERE channel=? AND revoked_at IS NULL "
                         "AND sender_ref IS NOT NULL", (_now().isoformat(), channel))
        self.steward._notify()
        return {"unbound": True, "channel": channel}

    def set_default(self, channel):
        """The channel the steward's unprompted messages also go to (None: dashboard only)."""
        self.steward._owner()
        with self.store.transaction(write=True) as conn:
            conn.execute("UPDATE state_owner_channels SET is_default=0 WHERE is_default=1")
            if channel is not None:
                conn.execute("INSERT INTO state_owner_channels(channel,is_default) VALUES(?,1) "
                             "ON CONFLICT(channel) DO UPDATE SET is_default=1", (channel,))
        self.steward._notify()
        return {"default_channel": channel}

    def send_owner_message(self, request_key, project_id, body, subject=None, reply_to_message_id=None,
                           target_driver_id=None, answers=None):
        """`send_user_conversation_message`: the inbox writes the dashboard message; an
        owner session's exact phrase in the steward's conversation then decides a
        proposal like a relayed one (the owner's terminal, CLI_OWNER_ACTOR, may talk
        to the steward but never approves - core.state_steward.is_owner_session)."""
        matched = {}

        def written(conn, message_id):
            if project_id == steward_project() and self.steward.is_owner_session():
                found = match_phrase(conn, body)
                if found is not None:
                    matched.update(found, message_id=message_id)

        result = self.inbox.send_user_conversation_message(
            request_key, project_id, body, subject=subject, reply_to_message_id=reply_to_message_id,
            target_driver_id=target_driver_id, answers=answers, _written=written)
        if matched:
            record = {"phrase": matched["phrase"], "message_id": matched["message_id"]}
            result = {**result, "approval": self._decide(matched, self.actor, record)}
        return result

    # ---------------------------------------------------------------- the decision
    def _decide(self, matched, decided_by, record):
        """The dashboard's approve / decline path (api.steward_routers) for an exact
        phrase. A refusal leaves the proposal as it was and is answered, not raised:
        the message itself was written."""
        steward, proposal_id = self.steward, matched["proposal_id"]
        version, carrier = matched["expected_version"], record if record.get("channel") else None
        out = {"proposal_id": proposal_id, "action": matched["action"]}
        try:
            if matched["action"] == "decline":
                return {**out, "outcome": "executed",
                        "result": steward.decide_decline(proposal_id, version, decided_by, record, carrier)}
            with self.store.transaction() as conn:
                row = steward._get(conn, proposal_id)
                impact = None
                if row["kind"] == "stop_driver" and row["status"] == "open" and row["version"] == version:
                    # The card's "stop anyway" box has no chat equivalent: refused, row open.
                    impact = stop_impact(conn, json.loads(row["payload_json"])["project_id"])
                    if not impact_is_empty(impact):
                        return {**out, "outcome": "refused", "refusal": "needs_dashboard_confirmation"}
            if row["kind"] in DRIVER_KINDS and steward.driver_runner is None:
                return {**out, "outcome": "refused", "refusal": "driver_runtime_unavailable"}
            row = steward.decide_begin(proposal_id, version, decided_by, record, carrier)
        except ProposalConflict as exc:
            return {**out, "outcome": "refused",
                    "refusal": "proposal_not_open" if exc.row["status"] != "open" else "proposal_changed"}
        except StateGraphError as exc:
            return {**out, "outcome": "refused", "refusal": _refusal(exc)}
        if row["kind"] == "create_project":
            result = steward.execute_create_project(proposal_id)
        elif row["kind"] in ARCHIVE_KINDS:
            result = steward.execute_archive(proposal_id)
        else:
            if row["kind"] == "stop_driver":
                steward.record_confirmation(proposal_id, False, impact)
            try:
                driver = steward.driver_runner(row["kind"], row["payload"]["project_id"], decided_by)
            except Exception as exc:  # noqa: BLE001 - any failure is the proposal's result
                result = steward.fail(proposal_id, f"{type(exc).__name__}: {exc}")
            else:
                result = steward.finish(proposal_id, "done", {"driver": driver})
        return {**out, "outcome": "executed", "result": result}
