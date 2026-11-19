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

The host also reports each channel's status (`report_channel_status`, steward only:
connected or not, a short detail, and the WeChat login QR as an SVG while a scan is
needed) into `state_owner_channel_status`, one row per channel, which only the owner's
dashboard reads. Neither the detail nor the QR is ever logged.

A binding attempt that names its channel message is remembered by that message
(`state_owner_channel_bind_attempts`: who, which message, bound or the refusal), so a
channel that delivers it again - the WeChat plugin replays the bridge's last minutes
after a restart - neither counts a refused guess twice nor relays a code that already
bound its sender as an owner message: bind and relay both answer it as a duplicate.

All five tables are private State tables (core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

from core import drivers
from core.driver_inbox import USER_SUBJECT
from core.message_body_guard import refuse_credential_body
from core.state_graph import StateConflict, StateGraphError, StateNotFound, canonical, now
from core.state_privacy import UntrustedDatabase, writer_only_read
from core.state_steward import (ARCHIVE_KINDS, ProposalConflict, _hash, _proposal, impact_is_empty,
                                steward_project, stop_impact)

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
 issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT,
 failed_attempts INTEGER NOT NULL DEFAULT 0, retired_at TEXT);
CREATE TABLE IF NOT EXISTS state_message_sources (
 message_id TEXT PRIMARY KEY,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 channel TEXT NOT NULL CHECK(channel IN ('slack','wechat')),
 sender_ref TEXT CHECK(sender_ref IS NULL OR sender_ref<>''),
 channel_message_ref TEXT CHECK(channel_message_ref IS NULL OR channel_message_ref<>''),
 at TEXT NOT NULL, error TEXT);
DROP INDEX IF EXISTS state_message_sources_inbound;
CREATE UNIQUE INDEX IF NOT EXISTS state_message_sources_inbound_by_sender
 ON state_message_sources(channel,sender_ref,channel_message_ref) WHERE direction='in';
CREATE INDEX IF NOT EXISTS state_message_sources_rate ON state_message_sources(channel,direction,at);
CREATE TABLE IF NOT EXISTS state_owner_channel_bind_attempts (
 channel TEXT NOT NULL CHECK(channel IN ('slack','wechat')),
 sender_ref TEXT NOT NULL CHECK(sender_ref<>''),
 channel_message_ref TEXT NOT NULL CHECK(channel_message_ref<>''),
 outcome TEXT NOT NULL, at TEXT NOT NULL,
 PRIMARY KEY(channel, sender_ref, channel_message_ref));
CREATE TABLE IF NOT EXISTS state_owner_channel_status (
 channel TEXT PRIMARY KEY CHECK(channel IN ('slack','wechat')),
 connected INTEGER NOT NULL CHECK(connected IN (0,1)),
 detail TEXT, qr_svg TEXT, at TEXT NOT NULL);
"""

BODY_MAX_BYTES = 4096
CODE_TTL = timedelta(minutes=10)
# Wrong binding attempts on a channel while its code is live; the last one retires the
# code (code_retired until the owner issues a new one): a stranger gets at most this
# many guesses at a code, whoever relays them.
BIND_FAILURES_MAX = 5
# How long a binding attempt is kept to answer its redelivery as a duplicate: far past
# any channel's replay (a bridge restart replays its last ~10 minutes). Older attempts
# are deleted by the next binding attempt, so the table does not grow for good.
BIND_ATTEMPTS_KEPT = timedelta(days=1)
# An approval phrase written (on the channel) longer ago than this decides nothing
# (approval_too_old): a channel that delivers late - a bridge that logs in again and
# drains what was written while it was down - must not execute an old decision.
APPROVAL_MAX_AGE = timedelta(minutes=10)
# ... and one dated more than this AHEAD of now decides nothing either
# (approval_clock_skew): without a bound on that side a skewed or wrong channel time
# (now + 365 days) would pass the age check forever and quietly switch it off.
APPROVAL_CLOCK_SKEW = timedelta(minutes=2)
# Both bounds need the channel's time: an approval phrase relayed WITHOUT channel_at
# decides nothing (approval_undated) - undated, it would execute however late it came.
# The message is written either way; an ordinary message needs no time.
MESSAGE_SOURCES_MAX = 200
APPROVE, DECLINE = "同意 proposal ", "拒绝 proposal "
DRIVER_KINDS = ("start_driver", "stop_driver")
# A channel's status as its host last reported it (report_channel_status): a short
# detail line, and the WeChat login QR as an SVG while a scan is needed.
STATUS_DETAIL_MAX = 300
QR_SVG_MAX_BYTES = 64 * 1024
# What a reported QR may be: an ALLOWLIST, never a list of known-bad markup (a denylist
# let <style>@import url(...)>, style="fill:url(...)", <animate>/<set> retargeting an href,
# xml:base, <iframe> and markup after </svg> through). One root <svg> in the SVG namespace
# holding only shapes, each with only presentational attributes whose values are plain
# numbers, path data and colours - so no url(, no scheme, no script, no styles. The
# bridge's drawing (integrations/wechat-bridge/qr-svg.mjs) is <svg viewBox
# shape-rendering> around two <path fill|stroke d>; g and rect are the other plain shapes.
_SVG_NS = "{http://www.w3.org/2000/svg}"
_QR_SHAPES = frozenset({"g", "path", "rect"})
_QR_ATTRIBUTES = frozenset({"viewBox", "width", "height", "d", "fill", "stroke", "stroke-width",
                            "shape-rendering", "x", "y"})
_QR_VALUE = re.compile(r"[A-Za-z0-9#., -]*")
# _own_decision: the proposal is executing under this very message's decision.
_RUNNING = object()


def rate_limits() -> tuple[int, int]:
    """Accepted relays per channel per minute and per hour (env overrides allowed)."""
    return (int(os.environ.get("AITELIER_CHANNEL_RATE_PER_MINUTE") or 6),
            int(os.environ.get("AITELIER_CHANNEL_RATE_PER_HOUR") or 60))


def _now() -> datetime:
    """The channels' clock (aware UTC); tests replace it."""
    return datetime.now(timezone.utc)


def _code_hash(pepper: str, channel: str, code: str) -> str:
    """A binding code as it is stored and looked up: HMAC-SHA256 over the code and the
    channel it was issued for, keyed with a subkey of the driver pepper for this one use,
    HMAC-SHA256(pepper, 'owner-channel-code') - never the pepper itself, so the driver
    tokens' digests (core.drivers.token_hash) stay a domain of their own. A copy of the
    State file (it is world-readable) must
    not give the code away: an unkeyed digest of a 6-digit code is undone offline in
    under a second, after which the copy's holder binds their own account and approves
    proposals over chat. Keyed, the 10^6 candidates cannot be tried without the pepper,
    and online guessing stays bounded at BIND_FAILURES_MAX per code.

    Rows written before this (an unkeyed sha256, or keyed with the pepper itself) match
    no code: they expire with their CODE_TTL, and the owner's next issue_code deletes an
    unused one anyway."""
    subkey = hmac.new(pepper.encode("utf-8"), b"owner-channel-code", hashlib.sha256).digest()
    return hmac.new(subkey, f"owner-channel-code:{channel}:{code}".encode("utf-8"), hashlib.sha256).hexdigest()


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


def _subject(proposal):
    """What a decided proposal was about, from its stored row: {kind, target}."""
    return {"kind": proposal["kind"], "target": proposal["payload"].get("project_id")}


def check_qr_svg(svg):
    """Refuse a QR image that is not a plain SVG document of at most QR_SVG_MAX_BYTES
    (the allowlist above). The refusal never quotes the image (it is a live login token)."""
    if not isinstance(svg, str):
        raise StateGraphError("qr_svg_refused: qr_svg must be an SVG document")
    size = len(svg.encode("utf-8"))
    if size > QR_SVG_MAX_BYTES:
        raise StateGraphError(f"qr_svg_too_large: {size} bytes; at most {QR_SVG_MAX_BYTES} bytes")
    refusal = StateGraphError("qr_svg_refused: qr_svg must be one <svg> document of plain shapes (svg, g, path, "
                              "rect) with presentational attributes only")
    # No DOCTYPE, entity, character reference, comment, CDATA or processing instruction
    # reaches the parser: without a DTD it defines no entity and resolves nothing.
    if "<!" in svg or "<?" in svg or "&" in svg:
        raise refusal
    # The parser takes namespace declarations out of the attributes, so they are read as
    # events: the only one a QR has is the default SVG namespace (no prefix to hide an
    # element or attribute behind, no other vocabulary declared).
    parser, root = ElementTree.XMLPullParser(events=("start-ns", "end")), None
    try:
        parser.feed(svg)
        parser.close()  # one root element; anything after it is a ParseError
        for event, item in parser.read_events():
            if event == "start-ns" and item != ("", _SVG_NS[1:-1]):
                raise refusal
            root = item if event == "end" else root
    except ElementTree.ParseError:
        raise refusal from None
    if root is None:
        raise refusal
    for element in root.iter():
        name = element.tag[len(_SVG_NS):] if element.tag.startswith(_SVG_NS) else None
        allowed = name == "svg" if element is root else name in _QR_SHAPES
        # Text inside an element (or between shapes) is content no QR drawing has.
        text = (element.text or "").strip() or (element is not root and (element.tail or "").strip())
        plain = all(key in _QR_ATTRIBUTES and _QR_VALUE.fullmatch(value) for key, value in element.attrib.items())
        if not allowed or text or not plain:
            raise refusal


def _written_at(channel_at):
    """The channel's own time of a message (ISO 8601; no offset = UTC), or None."""
    if channel_at is None:
        return None
    try:
        moment = datetime.fromisoformat(channel_at)
    except (TypeError, ValueError):
        raise StateGraphError("invalid_request: channel_at must be an ISO 8601 time") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _require_utf8(field, value, code="invalid_request"):
    """Refuse text that cannot be written as UTF-8: a lone UTF-16 surrogate (JSON's
    "\\ud800" decodes to one). Hashing it, measuring it or binding it into SQLite raises
    UnicodeEncodeError - a 500, which the host takes as an outage and retries, holding
    the channel's lane behind that one message - so it is refused first, like any other
    bad input. The refusal never quotes the value. Only the unbounded text fields need
    it: a field with a pydantic length bound (sender_ref, channel_message_ref, code, ...)
    is already refused there (pydantic cannot read a lone surrogate as a string)."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise StateGraphError(f"{code}: {field} is not valid Unicode text") from None


def _bind_attempt(conn, channel, sender_ref, channel_message_ref):
    """How an earlier binding attempt by this channel message ended ('bound' or its
    refusal code), or None when it was not one."""
    if channel_message_ref is None:
        return None
    row = conn.execute("SELECT outcome FROM state_owner_channel_bind_attempts WHERE channel=? AND sender_ref=? "
                       "AND channel_message_ref=?", (channel, sender_ref, channel_message_ref)).fetchone()
    return row[0] if row is not None else None


def _refusal(exc) -> str:
    return str(exc).split(":", 1)[0]


class StateChannels:
    def __init__(self, store, actor, driver_id=None, steward=None, inbox=None, pepper=None):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        self.steward, self.inbox = steward, inbox
        # The binding codes' key (_code_hash); None = the driver pepper secret file,
        # read when a code is issued or tried (core.drivers.read_pepper).
        self._pepper = pepper
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            with store.db.get_connection() as conn:
                conn.executescript(SCHEMA)
                conn.commit()

    # ---------------------------------------------------------------- the steward's host
    def relay_owner_message(self, project_id, request_key, channel, sender_ref, channel_message_ref, body,
                            channel_at=None):
        """One owner message from a channel into the steward's conversation, then the
        chat approval check. Idempotent per request_key and per channel message: either
        replay answers the first result with duplicate: true - except an approval whose
        answer the ledger does not hold yet (the process stopped, or the ledger write
        failed, between the message and the decision): the redelivery decides it, or
        finds it decided by this message, and answers {duplicate: true, decided_on_replay:
        true, approval} so the channel sends its line. A channel message is the
        sender's: the same ref from another sender is another message, which goes
        through every check (an unbound sender is refused, never handed the owner's
        first result). `channel_at` is when the
        channel says the message was written; it dates an approval phrase - an approval
        without it is refused approval_undated - and is not part of the request's
        identity (a redelivery may stamp it anew)."""
        # A direct caller's guard: over the command boundary the model's Literal refuses
        # another channel first (a field error, which the host answers invalid_request).
        if channel not in CHANNELS:
            raise StateGraphError(f"invalid_channel: channel must be one of {', '.join(CHANNELS)}")
        try:
            _require_utf8("body", body, "invalid_body")
        except StateGraphError:
            self._log_refusal("invalid_body", channel, sender_ref, len(body.encode("utf-8", "surrogatepass")))
            raise
        if not isinstance(body, str) or not body.strip():
            raise StateGraphError("empty_body: body must be nonempty text")
        size = len(body.encode("utf-8"))
        if size > BODY_MAX_BYTES:
            self._log_refusal("body_too_long", channel, sender_ref, size)
            raise StateGraphError(f"body_too_long: {size} bytes; at most {BODY_MAX_BYTES} bytes")
        refuse_credential_body(body)
        written_at = _written_at(channel_at)
        request_hash = _hash([channel, sender_ref, channel_message_ref, body])
        # The per-message record's key: the channel message of this sender.
        ref_key = f"{channel}:{_hash([sender_ref, channel_message_ref])}"
        home = steward_project()
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            first = self._relayed(conn, request_key, request_hash, channel, sender_ref, channel_message_ref,
                                  ref_key)
            if first is None:
                first = self._write_relay(conn, home, request_key, request_hash, channel, sender_ref,
                                          channel_message_ref, ref_key, body, size, written_at)
                fresh = True
            else:
                fresh = False
        if first.get("refused"):  # raised after the commit: the code's retirement must outlive it
            raise StateGraphError(f"{first['refused']}: a live binding code was sent from the bound account; "
                                  "it is retired - issue a new one on the dashboard")
        if fresh:
            self.steward._notify()
        if "pending" not in first:
            return first if fresh else {**first, "duplicate": True}
        # The approval this message carries is not settled in the ledger: decided now, or
        # - a redelivery after a stop or a failed ledger write - decided (or found
        # decided) again and answered decided_on_replay, so the channel sends its one line.
        settled = self._settle(first, channel, sender_ref, channel_message_ref)
        if settled is None:  # being decided by the first delivery right now
            return {"message_id": first["message_id"], "duplicate": True, "approval": None}
        return settled if fresh else {**settled, "duplicate": True, "decided_on_replay": True}

    def _relayed(self, conn, request_key, request_hash, channel, sender_ref, channel_message_ref, ref_key):
        """The ledger's record of this relay when it was relayed before (by request_key,
        or by its channel message under any key), else None."""
        replay = self.steward._replay(conn, "relay_owner_message", request_key, request_hash)
        if replay is not None:
            return replay
        seen = conn.execute("SELECT message_id FROM state_message_sources WHERE direction='in' AND channel=? "
                            "AND sender_ref=? AND channel_message_ref=?",
                            (channel, sender_ref, channel_message_ref)).fetchone()
        if seen is not None:
            first = conn.execute("SELECT result_json FROM driver_inbox_requests WHERE actor=? AND operation=? "
                                 "AND request_key=?", (self.actor, "relay_owner_message:ref", ref_key)).fetchone()
            return json.loads(first[0]) if first else {"message_id": seen[0], "approval": None}
        # A binding message delivered again after it bound its sender: the sender is
        # bound now, so the host relays it - it was never an owner message, and it
        # neither lands in the conversation nor wakes the steward.
        if _bind_attempt(conn, channel, sender_ref, channel_message_ref) is not None:
            return {"message_id": None, "approval": None}
        return None

    def _write_relay(self, conn, home, request_key, request_hash, channel, sender_ref, channel_message_ref,
                     ref_key, body, size, written_at):
        """The owner message, its source row and its two ledger records, in the caller's
        transaction. An approval phrase to decide is recorded `pending` (the phrase, the
        version it names and when it was written): the decision runs after this commits,
        and until the ledger holds its answer a redelivery decides it (_settle). A live
        binding code is not written: {refused: code_sent_to_bound_account} (see below)."""
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
        # A LIVE binding code the bound owner sent as a message is never written: the code
        # is retired and the relay refused code_sent_to_bound_account. The attempt is kept by
        # its channel message like a binding attempt, so a redelivery is a duplicate that
        # writes nothing (the digits, retired now, would otherwise land as a message). The
        # pepper is read only here, past the steward and binding checks; without it no code
        # can be checked or bound, and six digits are a plain message.
        digits, pepper = body.strip(), None
        if len(digits) == 6 and digits.isascii() and digits.isdigit():
            pepper = self._pepper_value()
        codes = [_code_hash(pepper, name, digits) for name in CHANNELS] if pepper else []
        live = [row for row in conn.execute(
            f"SELECT code_hash,channel,expires_at FROM state_owner_channel_codes WHERE code_hash IN "
            f"({','.join('?' * len(codes))}) AND used_at IS NULL AND retired_at IS NULL", codes).fetchall()
            if datetime.fromisoformat(row["expires_at"]) > moment] if codes else []
        if live:
            conn.execute("UPDATE state_owner_channel_codes SET retired_at=? WHERE code_hash=?",
                         (moment.isoformat(), live[0]["code_hash"]))
            conn.execute("INSERT INTO state_owner_channel_bind_attempts(channel,sender_ref,channel_message_ref,"
                         "outcome,at) VALUES(?,?,?,?,?)",
                         (channel, sender_ref, channel_message_ref, "code_sent_to_bound_account", moment.isoformat()))
            log.warning("owner channel code retired: sent as a message from the bound account channel=%s "
                        "code_channel=%s sender_ref=%s at=%s", channel, live[0]["channel"], sender_ref,
                        moment.isoformat())
            return {"refused": "code_sent_to_bound_account"}
        self.store._project(conn, home)
        message_id, _, _ = self.inbox.write_user_half(conn, home, USER_SUBJECT, body)
        conn.execute("INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,"
                     "channel_message_ref,at) VALUES(?,'in',?,?,?,?)",
                     (message_id, channel, sender_ref, channel_message_ref, moment.isoformat()))
        matched = match_phrase(conn, body)
        result = {"message_id": message_id, "duplicate": False, "approval": None}
        if matched is not None and written_at is None:
            result["approval"] = {"proposal_id": matched["proposal_id"], "action": matched["action"],
                                  "outcome": "refused", "refusal": "approval_undated"}
        elif matched is not None:
            result["pending"] = {**matched, "written_at": written_at.isoformat()}
        self.steward._record(conn, "relay_owner_message", request_key, request_hash, result)
        # The per-message record: a second relay of this channel message (under any
        # request_key) answers the first result.
        self.steward._record(conn, "relay_owner_message:ref", ref_key, request_hash, result)
        return result

    def _settle(self, first, channel, sender_ref, channel_message_ref):
        """Decide the approval a relayed message carries and put the answer in its two
        ledger records: {message_id, duplicate: false, approval}. Re-entry is safe - the
        decision checks the proposal's status and version in its own write transaction -
        and a proposal this very message already decided (the stop came after the
        decision, or the ledger write failed) is answered as that decision, not as
        proposal_not_open. None while the first delivery is still running it. A failing
        ledger write is raised: the decision stands in the proposal, the channel retries
        on the failure, and that redelivery records the answer and sends the line."""
        pending = first["pending"]
        carrier = {"channel": channel, "sender_ref": sender_ref, "channel_message_ref": channel_message_ref,
                   "phrase": pending["phrase"]}
        approval = self._own_decision(pending, carrier)
        if approval is None:
            approval = self._decide(pending, f"owner:{channel}:{sender_ref}", carrier,
                                    datetime.fromisoformat(pending["written_at"]))
            if approval.get("refusal") == "proposal_not_open":  # decided meanwhile, maybe by this message
                approval = self._own_decision(pending, carrier) or approval
        if approval is _RUNNING:
            return None
        result = {"message_id": first["message_id"], "duplicate": False, "approval": approval}
        with self.store.transaction(write=True) as conn:
            # Only records still pending: a concurrent settle that got there first stands.
            conn.execute("UPDATE driver_inbox_requests SET result_json=? WHERE actor=? AND operation IN "
                         "('relay_owner_message','relay_owner_message:ref') AND result_json=?",
                         (json.dumps(result), self.actor, json.dumps(first)))
        return result

    def _own_decision(self, pending, carrier):
        """The proposal as decided by THIS message (its stored record names this channel
        message and phrase): the executed answer, _RUNNING while it is executing, None
        when this message did not decide it."""
        with self.store.transaction() as conn:
            row = self.steward._get(conn, pending["proposal_id"])
        record = json.loads(row["result_json"]) if row["result_json"] else {}
        if row["status"] == "open" or not isinstance(record, dict) \
                or any(record.get(field) != value for field, value in carrier.items()):
            return None
        if row["status"] == "executing":
            return _RUNNING
        decided = _proposal(row)
        return {"proposal_id": pending["proposal_id"], "action": pending["action"], "outcome": "executed",
                **_subject(decided), "result": decided}

    def _pepper_value(self):
        """The pepper that keys the binding codes, or None when it is missing or short."""
        pepper = self._pepper if self._pepper is not None else drivers.read_pepper()
        return pepper if isinstance(pepper, str) and len(pepper) >= 32 else None

    def _code_key(self):
        """The pepper that keys the binding codes, or channel_secret_missing: without
        it no code is issued or tried - never an unkeyed digest in its place."""
        pepper = self._pepper_value()
        if pepper is None:
            raise StateGraphError(f"channel_secret_missing: binding codes need the {drivers.PEPPER_SECRET_NAME} "
                                  "secret file (at least 32 characters)")
        return pepper

    @staticmethod
    def _log_refusal(code, channel, sender_ref, size):
        # B3: who, where, when and how long - never the body.
        log.warning("owner channel message refused: %s channel=%s sender_ref=%s at=%s bytes=%d",
                    code, channel, sender_ref, now(), size)

    def bind_owner_channel(self, project_id, request_key, channel, sender_ref, code, channel_message_ref=None):
        """Bind `sender_ref` as the channel's owner account with an unexpired, unused
        code issued for that channel. A previous binding of the channel is replaced.
        Every refused attempt while the channel has a live code counts against that
        code; the BIND_FAILURES_MAX-th retires it (code_retired, even for the right
        code, until the owner issues a new one). An attempt that names its channel
        message is answered once: a second delivery of it (under any request_key)
        answers {bound, duplicate: true} and counts nothing."""
        refusal = None
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            conn.execute("DELETE FROM state_owner_channel_bind_attempts WHERE at<?",
                         ((_now() - BIND_ATTEMPTS_KEPT).isoformat(),))
            # Before the request_key replay: a binding message delivered again - also after
            # the channel was bound to another account since - is answered as the duplicate
            # it is ({bound: true, duplicate: true}, or its refusal), never as a fresh bind,
            # and binds and counts nothing.
            first = _bind_attempt(conn, channel, sender_ref, channel_message_ref)
            if first is not None:
                return {"bound": first == "bound", "channel": channel, "sender_ref": sender_ref, "duplicate": True,
                        **({} if first == "bound" else {"refusal": first})}
            # The pepper is read only now - after the steward check and the duplicate - and
            # right before the request_key replay, which needs the keyed digest to tell a
            # replay from a key reused with another code. Only the keyed digest of the code
            # enters the request ledger (driver_inbox_requests keeps it on a successful
            # bind): no plain or unkeyed digest of it is stored anywhere.
            digest = _code_hash(self._code_key(), channel, code)
            request_hash = _hash([channel, sender_ref, digest])
            replay = self.steward._replay(conn, "bind_owner_channel", request_key, request_hash)
            if replay is not None:
                return replay
            moment = _now()
            # At most one: issue_code deletes the channel's earlier unused codes.
            live = conn.execute("SELECT * FROM state_owner_channel_codes WHERE channel=? AND used_at IS NULL",
                                (channel,)).fetchone()
            if live is not None and datetime.fromisoformat(live["expires_at"]) <= moment:
                live = None
            well_formed = len(code) == 6 and code.isascii() and code.isdigit()
            row = conn.execute("SELECT * FROM state_owner_channel_codes WHERE code_hash=?",
                               (digest,)).fetchone() if well_formed else None
            if live is not None and live["retired_at"] is not None:
                refusal = "code_retired"
            elif row is None or row["channel"] != channel:
                refusal = "invalid_code"
            elif row["used_at"] is not None:
                refusal = "code_used"
            elif datetime.fromisoformat(row["expires_at"]) <= moment:
                refusal = "code_expired"
            if channel_message_ref is not None:
                conn.execute("INSERT INTO state_owner_channel_bind_attempts(channel,sender_ref,channel_message_ref,"
                             "outcome,at) VALUES(?,?,?,?,?)",
                             (channel, sender_ref, channel_message_ref, refusal or "bound", moment.isoformat()))
            if refusal is not None:
                if live is not None and live["retired_at"] is None:
                    failed = live["failed_attempts"] + 1
                    retired = moment.isoformat() if failed >= BIND_FAILURES_MAX else None
                    conn.execute("UPDATE state_owner_channel_codes SET failed_attempts=?, retired_at=? "
                                 "WHERE code_hash=?", (failed, retired, live["code_hash"]))
                    if retired:
                        log.warning("owner channel code retired after %d refused binding attempts: channel=%s "
                                    "sender_ref=%s at=%s", failed, channel, sender_ref, retired)
            else:
                stamp = moment.isoformat()
                conn.execute("UPDATE state_owner_channel_codes SET used_at=? WHERE code_hash=?",
                             (stamp, row["code_hash"]))
                conn.execute("INSERT INTO state_owner_channels(channel,sender_ref,bound_at,bound_by,revoked_at) "
                             "VALUES(?,?,?,?,NULL) ON CONFLICT(channel) DO UPDATE SET sender_ref=excluded.sender_ref,"
                             "bound_at=excluded.bound_at,bound_by=excluded.bound_by,revoked_at=NULL",
                             (channel, sender_ref, stamp, self.actor))
                out = self.steward._record(conn, "bind_owner_channel", request_key, request_hash,
                                           {"bound": True, "channel": channel, "sender_ref": sender_ref})
        # Raised after the commit: the attempt's count must outlive its refusal.
        if refusal is not None:
            raise StateGraphError(refusal)
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

    def report_channel_status(self, project_id, request_key, channel, connected, detail=None, qr_svg=None):
        """The host's latest view of a channel: connected or not, a short detail and,
        while the WeChat login needs a scan, its QR as an SVG. The row is replaced each
        time; the dashboard shows it (the owner's GET /api/steward/channels). Neither the
        detail nor the image is ever logged."""
        _require_utf8("detail", detail)
        _require_utf8("qr_svg", qr_svg, "qr_svg_refused")
        request_hash = _hash([channel, connected, detail, qr_svg])
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "report_channel_status", request_key, request_hash)
            if replay is not None:
                return replay
            if channel not in CHANNELS:  # a direct caller's guard, as in relay_owner_message
                raise StateGraphError(f"invalid_channel: channel must be one of {', '.join(CHANNELS)}")
            if detail is not None and len(detail) > STATUS_DETAIL_MAX:
                raise StateGraphError(f"detail_too_long: {len(detail)} characters; at most {STATUS_DETAIL_MAX}")
            if qr_svg is not None:
                check_qr_svg(qr_svg)
            stamp = _now().isoformat()
            conn.execute("INSERT INTO state_owner_channel_status(channel,connected,detail,qr_svg,at) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(channel) DO UPDATE SET connected=excluded.connected,detail=excluded.detail,"
                         "qr_svg=excluded.qr_svg,at=excluded.at",
                         (channel, int(bool(connected)), detail, qr_svg, stamp))
            return self.steward._record(conn, "report_channel_status", request_key, request_hash,
                                        {"recorded": True, "channel": channel, "connected": bool(connected),
                                         "at": stamp})

    # ---------------------------------------------------------------- reads (owner or steward)
    def _reader(self, conn):
        if not self.steward.is_owner_session():
            try:
                self.steward._steward(conn)
            except StateGraphError:
                raise StateGraphError("owner_or_steward_required") from None

    @writer_only_read("list_owner_channels")
    def list_owner_channels(self, with_status=False):
        """`with_status` (the owner's dashboard only, never a State query): each channel
        also carries the status its host last reported, or None, and the login QR."""
        if with_status:
            self.steward._owner()
        with self.store.transaction() as conn:
            self._reader(conn)
            rows = {r["channel"]: r for r in conn.execute("SELECT * FROM state_owner_channels")}
            statuses = {r["channel"]: r for r in conn.execute("SELECT * FROM state_owner_channel_status")} \
                if with_status else {}
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
            if with_status:
                status = statuses.get(channel)
                channels[-1].update(
                    status={"connected": bool(status["connected"]), "detail": status["detail"], "at": status["at"]}
                    if status is not None else None,
                    qr_svg=status["qr_svg"] if status is not None else None)
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
        this answer, the backend keeps its keyed digest (_code_hash). Any earlier unused
        code of the channel stops working."""
        self.steward._owner()
        pepper = self._code_key()
        moment = _now()
        with self.store.transaction(write=True) as conn:
            conn.execute("DELETE FROM state_owner_channel_codes WHERE channel=? AND used_at IS NULL", (channel,))
            while True:
                code = f"{secrets.randbelow(10 ** 6):06d}"
                digest = _code_hash(pepper, channel, code)
                if conn.execute("SELECT 1 FROM state_owner_channel_codes WHERE code_hash=?",
                                (digest,)).fetchone() is None:
                    break
            conn.execute("INSERT INTO state_owner_channel_codes(code_hash,channel,issued_at,expires_at) "
                         "VALUES(?,?,?,?)",
                         (digest, channel, moment.isoformat(), (moment + CODE_TTL).isoformat()))
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
    def _decide(self, matched, decided_by, record, written_at=None):
        """The dashboard's approve / decline path (api.steward_routers) for an exact
        phrase. A refusal leaves the proposal as it was and is answered, not raised:
        the message itself was written. A phrase written more than APPROVAL_MAX_AGE
        before now (`written_at`, a channel's own time) is refused approval_too_old, one
        dated more than APPROVAL_CLOCK_SKEW after now approval_clock_skew.
        An executed decision also names the proposal's `kind` and `target` (its
        project_id) from the stored row, for the channel's one-line answer."""
        steward, proposal_id = self.steward, matched["proposal_id"]
        version, carrier = matched["expected_version"], record if record.get("channel") else None
        out = {"proposal_id": proposal_id, "action": matched["action"]}
        if written_at is not None and _now() - written_at > APPROVAL_MAX_AGE:
            return {**out, "outcome": "refused", "refusal": "approval_too_old"}
        if written_at is not None and written_at - _now() > APPROVAL_CLOCK_SKEW:
            return {**out, "outcome": "refused", "refusal": "approval_clock_skew"}
        try:
            if matched["action"] == "decline":
                declined = steward.decide_decline(proposal_id, version, decided_by, record, carrier)
                return {**out, "outcome": "executed", **_subject(declined), "result": declined}
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
        return {**out, "outcome": "executed", **_subject(row), "result": result}
