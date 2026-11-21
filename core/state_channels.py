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
dashboard reads. Neither the detail nor the QR is ever logged. The row holds facts only
(design-steward 7.9.2): since when the channel is down, since when it is up, and whether
the last reading was a real one (`live`). The channel-down alert keeps one ledger row per
outage and kind (`state_channel_alerts`); which alert is due is one pure function of the
facts, the ledger and the time (`due_alerts`), read by the host (`due_channel_alerts`),
claimed (`claim_channel_alert`) and settled (`record_channel_alert`) by its code, never a
model turn; see ALERT_AFTER.

A binding attempt that names its channel message is remembered by that message
(`state_owner_channel_bind_attempts`: who, which message, bound or the refusal), so a
channel that delivers it again - the WeChat plugin replays the bridge's whole ring, at
any age, after a restart - neither counts a refused guess twice nor relays a code that
already bound its sender as an owner message: bind and relay both answer it as a
duplicate. The attempts are kept per channel by count (BIND_ATTEMPTS_KEPT), never aged out.

A live binding code is never persisted in owner-written text: no writer (the relayed
channel message, the owner's dashboard conversation message and the owner's dashboard
message to a driver - core.driver_inbox's `owner_text_guard`, installed here) stores a
body or subject that carries a live code ANYWHERE in it - the check is on the digits the
text contains, in order, with everything between them dropped (after `ascii_digits`, so
the full-width, Arabic-Indic and Devanagari renderings of a code are the code, and a
space, dash, newline or zero-width character between its digits does not hide it), never
on `body.strip() == six digits` - the code is retired and the write is refused
code_sent_to_bound_account (the dashboard writers raise the same code). Without the pepper that keys the codes the check cannot run, so
it refuses channel_secret_missing instead of writing the text unjudged.

An approval relayed by a channel is decided after its message commits, and the decision
is recorded in the request ledger (`pending` until then). A stop between the two leaves
the record pending; a start settles it (`settle_pending_relays`), and the channel's
redelivery then answers the decision (`decided_on_replay`) instead of nothing. A record
whose decision needs the driver runtime is parked for that redelivery, unless the
message's own decision already ran - reading that needs no runtime.

The owner can ask for a new WeChat login QR on the dashboard (`request_login`, owner
session only; design-steward 7.2 "按需二维码"): one pending row in
`state_owner_channel_login_requests` and a State event `owner_channel_login_requested` on
the steward's project. The steward's host runs it in code (never a model turn): it reads
the pending requests (`list_owner_channel_login_requests`, steward only), asks the WeChat
bridge for a QR and writes the answer back (`complete_owner_channel_login_request`). The
QR itself travels only the status path above; a request, its event and its result never
carry it. A request nobody ran within LOGIN_REQUEST_TTL reads as `expired` and is no
longer listed, so a host that comes back later never asks for a QR on an old request.

All six tables are private State tables (core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from core import drivers
from core.driver_inbox import USER_SUBJECT
from core.message_body_guard import refuse_credential_body
from core.state_graph import StateConflict, StateGraphError, StateNotFound, canonical, now
from core.state_privacy import UntrustedDatabase, writer_only_read
from core.state_schema_migration import add_column_if_missing
from core.state_steward import (ARCHIVE_KINDS, ProposalConflict, _hash, _proposal, default_tz, impact_is_empty,
                                steward_project, stop_impact)

log = logging.getLogger(__name__)

CHANNELS = ("slack", "wechat")
SCHEMA = """
CREATE TABLE IF NOT EXISTS state_channel_alerts (
 channel TEXT NOT NULL CHECK(channel IN ('slack','wechat')),
 kind TEXT NOT NULL CHECK(kind IN ('down','up')),
 down_since TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('claimed','sent','failed')),
 attempt INTEGER NOT NULL CHECK(attempt IN (1,2)),
 via TEXT NOT NULL CHECK(via IN ('slack','wechat')),
 claimed_at TEXT NOT NULL, finished_at TEXT, error TEXT,
 PRIMARY KEY(channel, kind, down_since));
-- No other index: both readers (the due function's facts and the owner's channel list) hand
-- the whole ledger to the pure due function, and the ledger stays small - at most one row per
-- kind per outage, a down row only for an outage alerted about, at most one delivered down
-- alert per channel in ALERT_DOWN_EVERY - so a scan of it costs less than keeping an index.
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
 at TEXT NOT NULL, error TEXT, channel_at TEXT);
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
CREATE TABLE IF NOT EXISTS state_owner_channel_login_requests (
 request_id TEXT PRIMARY KEY,
 channel TEXT NOT NULL CHECK(channel IN ('wechat')),
 requested_at TEXT NOT NULL, requested_by TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','done')),
 result_json TEXT, completed_at TEXT);
CREATE INDEX IF NOT EXISTS state_owner_channel_login_requests_channel
 ON state_owner_channel_login_requests(channel,requested_at);
"""

BODY_MAX_BYTES = 4096
CODE_TTL = timedelta(minutes=10)
# Wrong binding attempts on a channel while its code is live; the last one retires the
# code (code_retired until the owner issues a new one): a stranger gets at most this
# many guesses at a code, whoever relays them.
BIND_FAILURES_MAX = 5
# How many binding attempts per channel are kept to answer their redelivery as a
# duplicate: counted, never aged out, because a WeChat plugin restart replays the bridge's
# whole ring (integrations/wechat-bridge RING_SIZE, 500 messages of any age), so every
# binding message still in it is among the channel's newest 1000 attempts. Older ones are
# deleted by the channel's next binding attempt, so the table does not grow for good.
BIND_ATTEMPTS_KEPT = 1000
# An approval phrase written (on the channel) longer ago than this decides nothing
# (approval_too_old): a channel that delivers late - a bridge that logs in again and
# drains what was written while it was down - must not execute an old decision.
APPROVAL_MAX_AGE = timedelta(minutes=10)
# ... and one dated more than this AHEAD of now decides nothing either
# (approval_clock_skew): without a bound on that side a skewed or wrong channel time
# (now + 365 days) would pass the age check forever and quietly switch it off.
APPROVAL_CLOCK_SKEW = timedelta(minutes=2)
# Only for storing an inbound message's `channel_at` (its channel's send time, shown to
# the steward and the dashboard): one dated more than this AHEAD of the backend's time
# is a fast channel clock, and is stored as NULL. Not the approval bound above, on
# purpose: retuning when an approval expires must not change what is stored.
CHANNEL_AT_MAX_AHEAD = timedelta(minutes=2)
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
# The owner's requests for a new login QR (request_login): only WeChat logs in by QR.
# A pending request older than LOGIN_REQUEST_TTL is `expired` - not run any more; each
# channel keeps its newest LOGIN_REQUESTS_KEPT rows.
LOGIN_CHANNELS = ("wechat",)
LOGIN_REQUEST_TTL = timedelta(minutes=2)
LOGIN_REQUESTS_KEPT = 20
_LOGIN_REFUSAL = re.compile(r"[a-z_]{1,40}")
# _own_decision: the proposal is executing under this very message's decision.
_RUNNING = object()
# The State files whose pending relay records this process has already settled at a
# start: one scan per start and file, not one per State service (a service is built per
# request). A file whose decision is still running stays out of it, so a later service
# of the same start tries again.
_START_SETTLED: set[str] = set()
# The channel-down alert (design-steward 7.9.2, C12), decided in code, never a model turn.
# A channel bound to the owner that has been truly down ALERT_AFTER is told to the owner
# through ANOTHER bound, live, connected channel (the default first); a delivered down alert
# is followed by one recovery notice once the channel is truly back. Each outage (its
# down_since) has at most one ledger row per kind, and at most ALERT_ATTEMPTS sends: the
# second ALERT_RETRY_AFTER after the first claim, when that one failed or was not reported
# within ALERT_UNREPORTED (the host most likely died). No down alert within ALERT_DOWN_EVERY
# of the last delivered one. Nothing is due in the quiet hours (local QUIET_FROM:00 to
# QUIET_UNTIL:00 in the owner's zone, core.state_steward.default_tz, as in design 7.6.4).
ALERT_KINDS = ("down", "up")
ALERT_AFTER = timedelta(minutes=10)
ALERT_RETRY_AFTER = timedelta(minutes=30)
ALERT_UNREPORTED = timedelta(minutes=10)
ALERT_ATTEMPTS = 2
ALERT_DOWN_EVERY = timedelta(hours=6)
ALERT_DETAIL_MAX = 80
ALERT_ERROR_MAX = 300
QUIET_FROM, QUIET_UNTIL = 23, 8
# What a channel plugin or the host says before (or instead of) a real reading: the WeChat
# plugin's start, Slack's start, and a plugin taken back. A report of one of these marks the
# row not live and starts no outage clock; the two starts keep `connected` as the last real
# reading left it, while a plugin taken back (STATUS_STOPPED) is not connected - the dashboard
# shows it disconnected, with its new-QR button - though it is no outage.
STATUS_STOPPED = "channel plugin stopped"
STATUS_PLACEHOLDERS = frozenset({"bridge not reached yet", "connecting", STATUS_STOPPED})
# The status row's fact columns (added to existing databases): down_since (this outage's
# start), up_since (the last time it truly came back) - neither is ever cleared - and live
# (whether the last reading was a real one).
_STATUS_COLUMNS = (("down_since", "TEXT"), ("up_since", "TEXT"),
                   ("live", "INTEGER NOT NULL DEFAULT 1 CHECK(live IN (0,1))"))


def alert_limits() -> dict:
    """The due function's rules as the owner's channel list carries them (GET
    /api/steward/channels): the dashboard's alert line reads them from there, never a copy."""
    def minutes(delta):
        return int(delta.total_seconds() // 60)
    return {"after_minutes": minutes(ALERT_AFTER), "retry_after_minutes": minutes(ALERT_RETRY_AFTER),
            "unreported_minutes": minutes(ALERT_UNREPORTED), "attempts": ALERT_ATTEMPTS,
            "down_every_minutes": minutes(ALERT_DOWN_EVERY), "quiet_from_hour": QUIET_FROM,
            "quiet_until_hour": QUIET_UNTIL}


def rate_limits() -> tuple[int, int]:
    """Accepted relays per channel per minute and per hour (env overrides allowed)."""
    return (int(os.environ.get("AITELIER_CHANNEL_RATE_PER_MINUTE") or 6),
            int(os.environ.get("AITELIER_CHANNEL_RATE_PER_HOUR") or 60))


def _now() -> datetime:
    """The channels' clock (aware UTC); tests replace it."""
    return datetime.now(timezone.utc)


def quiet_until(moment: datetime):
    """When the quiet hours that hold `moment` end (aware UTC), or None outside them."""
    local = moment.astimezone(ZoneInfo(default_tz()))
    if QUIET_UNTIL <= local.hour < QUIET_FROM:
        return None
    day = local.date() + timedelta(days=1) if local.hour >= QUIET_FROM else local.date()
    return datetime(day.year, day.month, day.day, QUIET_UNTIL, tzinfo=local.tzinfo).astimezone(timezone.utc)


def _at(stamp):
    return datetime.fromisoformat(stamp)


def _was_up(prev):
    """Whether the channel's last real reading said connected. `connected` is 1 only from a
    real reading; behind a placeholder that wrote 0 (STATUS_STOPPED) the clocks say it: the
    later of up_since and down_since."""
    if prev is None:
        return False
    if prev["connected"] or prev["live"]:
        return bool(prev["connected"])
    up, down = prev["up_since"], prev["down_since"]
    return up is not None and (down is None or _at(up) > _at(down))


def _settled_failed(row, moment):
    """An attempt that failed, or was claimed and not reported within ALERT_UNREPORTED."""
    return row["state"] == "failed" or (row["state"] == "claimed" and moment >= _at(row["claimed_at"]) + ALERT_UNREPORTED)


def _told_at(row, moment):
    """When this ledger row told the owner: a sent one when it was reported; a recovery
    notice given up after its last attempt counts as told too (or no later outage would ever
    be alerted again), at its report or ALERT_UNREPORTED after its claim. None otherwise."""
    if row["state"] == "sent":
        return _at(row["finished_at"])
    if row["kind"] == "up" and row["attempt"] >= ALERT_ATTEMPTS and _settled_failed(row, moment):
        return _at(row["finished_at"]) if row["finished_at"] else _at(row["claimed_at"]) + ALERT_UNREPORTED
    return None


def _last_told(rows, moment):
    """(row, when) of the newest ledger row that told the owner, or None."""
    told = [(at, row) for row in rows if (at := _told_at(row, moment)) is not None]
    return max(told, key=lambda pair: pair[0])[::-1] if told else None


def _next_attempt(row, moment):
    """The attempt a claim of this ledger row's alert would be, or None: 1 with no row; 2
    once the first failed (or went unreported) and ALERT_RETRY_AFTER has passed since its claim."""
    if row is None:
        return 1
    if row["attempt"] < ALERT_ATTEMPTS and _settled_failed(row, moment) \
            and moment >= _at(row["claimed_at"]) + ALERT_RETRY_AFTER:
        return row["attempt"] + 1
    return None


def _usable(status, binding):
    """A channel the alert may be about: bound to the owner, its last reading a real one, and
    not one that says it is not configured (WeChat's `not configured`, Slack's `not configured: ...`)."""
    return (binding is not None and binding["sender_ref"] is not None and binding["revoked_at"] is None
            and status is not None and status["live"] and not (status["detail"] or "").startswith("not configured"))


def _via(channel, statuses, bindings, exclude=()):
    """Another channel bound to the owner, live and connected, the default first; or None.
    Never one in `exclude` (the channels the host cannot send through now)."""
    others = [c for c in CHANNELS if c != channel and c not in exclude and bindings.get(c) is not None
              and bindings[c]["sender_ref"] is not None and bindings[c]["revoked_at"] is None
              and statuses.get(c) is not None and statuses[c]["live"] and statuses[c]["connected"]]
    others.sort(key=lambda c: (not bindings[c]["is_default"], c))
    return others[0] if others else None


def due_alerts(statuses, bindings, ledger, moment, exclude=()):
    """The channel alerts due at `moment`: [{channel, kind, down_since, attempt, via}]. A
    pure function of the status rows and bindings (by channel), the alert ledger's rows and
    the time - nothing about an alert lives anywhere else (design-steward 7.9.2). `exclude`
    (a claim's) only narrows the channels a `via` is chosen from."""
    if quiet_until(moment) is not None:
        return []
    due = []
    for channel in CHANNELS:
        status, binding = statuses.get(channel), bindings.get(channel)
        if not _usable(status, binding):
            continue
        via = _via(channel, statuses, bindings, exclude)
        if via is None:
            continue
        rows = [r for r in ledger if r["channel"] == channel]
        own = {(r["kind"], r["down_since"]): r for r in rows}
        last = _last_told(rows, moment)
        if not status["connected"]:
            since = status["down_since"]
            if since is None or moment < _at(since) + ALERT_AFTER:
                continue
            if any(r["kind"] == "down" and r["state"] == "sent" and moment - _at(r["finished_at"]) < ALERT_DOWN_EVERY
                   for r in rows):
                continue  # one down alert in ALERT_DOWN_EVERY, even for a new outage after a recovery notice
            if last is not None and last[0]["kind"] == "down":
                continue  # the owner was told this channel is down and not yet that it came back
            kind = "down"
        else:
            if last is None or last[0]["kind"] != "down":
                continue  # no down alert went out that a recovery notice would answer
            kind, since = "up", last[0]["down_since"]
        attempt = _next_attempt(own.get((kind, since)), moment)
        if attempt is not None:
            due.append({"channel": channel, "kind": kind, "down_since": since, "attempt": attempt, "via": via})
    return due


def _alert_facts(conn):
    """What due_alerts reads: status rows and bindings by channel, and the ledger."""
    return ({r["channel"]: r for r in conn.execute("SELECT * FROM state_owner_channel_status")},
            {r["channel"]: r for r in conn.execute("SELECT * FROM state_owner_channels")},
            conn.execute("SELECT * FROM state_channel_alerts").fetchall())


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


def ascii_digits(text):
    """`text` with every Unicode digit of every script written as an ASCII digit: NFKC
    folds the compatibility forms ('７' -> '7', '⁷' -> '7') but leaves the DECIMAL blocks
    of the other scripts alone - '٧١١٨٣٣' (Arabic-Indic) and '७११८३३' (Devanagari)
    normalise to themselves - and `unicodedata.decimal` is what turns those into 7, 1, 1,
    8, 3, 3. Measured, so this is not a guess: NFKC of the Arabic-Indic and Devanagari
    renderings equals itself, while `unicodedata.decimal` gives 7 for both."""
    out = []
    for char in unicodedata.normalize("NFKC", text):
        if "0" <= char <= "9":
            out.append(char)
            continue
        try:
            out.append(str(unicodedata.decimal(char)))
            continue
        except (TypeError, ValueError):
            pass
        try:
            out.append(str(unicodedata.digit(char)))  # a digit value without a decimal one ('⁷')
        except (TypeError, ValueError):
            out.append(char)
    return "".join(out)


def digit_windows(body):
    """The distinct 6-ASCII-digit strings `body` carries, wherever they sit.

    A binding code is six ASCII digits the owner may paste inside anything ('binding
    code: 711833', 'please use 711833 to bind slack', '711833 thanks'). The leak this
    guards is the code in the text, not the text's shape, so the check is on the text's
    digits taken IN ORDER with everything between them dropped - every 6-window of that
    one digit string - and never on `body.strip()`. A separator does not hide a code from
    a reader of the stored bytes ('711 833', '7-1-1-8-3-3', a newline, a TAB, U+3000, a
    zero-width joiner or space, keycaps '7\ufe0f\u20e3', '(7)(1)..' that NFKC makes of
    U+2474..), so it does not hide it from this check either; a subject and body joined
    by a newline are one text.

    The body's digits are folded to ASCII first (`ascii_digits`), because the leak is what
    a READER of the stored bytes can recover: '７１１８３３' (full-width) is one
    `unicodedata.normalize('NFKC', cell)` away from '711833', and '٧١١٨٣٣' (Arabic-Indic),
    '७११८३३' (Devanagari) or '⁷¹¹⁸³³' are one `unicodedata` decimal/digit lookup away -
    every rendering is the same code and is caught here. Folding only ADDS windows a body
    carries (a body with no digit in any script still yields none, and a folded digit is
    exactly the leak), so no shape that used to be refused is let through."""
    digits = "".join(char for char in ascii_digits(body) if "0" <= char <= "9")
    return {digits[start:start + 6] for start in range(len(digits) - 5)}


def live_codes(conn, moment):
    """The binding codes that can still bind (unused, unretired, unexpired), as
    {code_hash: channel}. Read without the pepper: only the digests are stored."""
    return {row["code_hash"]: row["channel"] for row in conn.execute(
        "SELECT code_hash,channel,expires_at FROM state_owner_channel_codes "
        "WHERE kind='account' AND used_at IS NULL AND retired_at IS NULL")
        if datetime.fromisoformat(row["expires_at"]) > moment}


def match_live_code(live, pepper, windows):
    """The row of `live` one of `windows` names, looked up by its keyed digest, or None."""
    for channel in CHANNELS:
        for code in windows:
            digest = _code_hash(pepper, channel, code)
            if live.get(digest) == channel:
                return {"code_hash": digest, "channel": channel}
    return None


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
                # Additive (message time): every source row written before it has no
                # channel time, which is what NULL means.
                add_column_if_missing(conn, "state_message_sources", "channel_at", "TEXT")
                added = {column: add_column_if_missing(conn, "state_owner_channel_status", column, definition)
                         for column, definition in _STATUS_COLUMNS}
                # Once each, when its column is added: a placeholder row waits for a real reading;
                # a real outage starts now.
                if added["live"]:
                    marks = ",".join("?" * len(STATUS_PLACEHOLDERS))
                    conn.execute(f"UPDATE state_owner_channel_status SET live=0 WHERE connected=0 AND detail IN ({marks})",
                                 sorted(STATUS_PLACEHOLDERS))
                if added["down_since"]:
                    conn.execute("UPDATE state_owner_channel_status SET down_since=? WHERE connected=0 AND live=1",
                                 (_now().isoformat(),))
                # Step 23 (design-steward 7.7.9): a code is an ACCOUNT code (6 digits, 1:1 chat)
                # or a GROUP code (core.state_groups); a group code may carry the group's name.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(state_owner_channel_codes)")}
                if "kind" not in columns:
                    conn.execute("ALTER TABLE state_owner_channel_codes ADD COLUMN kind TEXT NOT NULL DEFAULT 'account' "
                                 "CHECK(kind IN ('account','group'))")
                if "label" not in columns:
                    conn.execute("ALTER TABLE state_owner_channel_codes ADD COLUMN label TEXT")
                conn.commit()
            if inbox is not None:  # the inbox's owner writers scan their text first
                inbox.owner_text_guard = self.owner_text_guard
            # Only the steward's own service repairs the steward's channel ledger: a
            # director's, an owner's or the recovery actor's starts nothing here.
            if self.driver_id is not None and self.steward.is_steward():
                self._settle_at_start()

    # ---------------------------------------------------------------- a start's repair
    def _settle_at_start(self):
        """A start settles the relay records a stop left `pending`: once per process, State
        file and steward - the steward's first service of a start does it, not its every
        request. It never blocks the service it is built for: a failure is logged and the
        next service tries again."""
        path = self.store.db.db_path
        if path in _START_SETTLED:
            return
        _START_SETTLED.add(path)
        try:
            result = self.settle_pending_relays()
        except Exception:  # noqa: BLE001 - a startup repair never fails a request
            log.exception("owner channel relay records were not settled at startup")
            _START_SETTLED.discard(path)
            return
        if result["deferred"]:
            # A proposal whose decision is still executing here: recover() settles the
            # proposal at this start, and the next service then settles its ledger. Only
            # that case re-opens the scan - a record left for a driver request (`parked`)
            # is settled by the channel's own redelivery, so re-scanning for it on every
            # request would buy nothing and cost the ledger read each time.
            _START_SETTLED.discard(path)

    def settle_pending_relays(self):
        """Decide every approval a relayed message left undecided when the process went
        away: the ledger record is `pending` from the message's commit until the decision
        is recorded, and nothing else ever revisited it, so a stop there executed the
        approval at the next start (recover settles the proposal) while the ledger kept
        approval: null for good - the decision the channel then never learned.

        Runs once per start (see _settle_at_start) and is safe to re-run: the decision
        checks the proposal's status and version like any other, buys nothing twice, and
        a record already settled is left alone. A proposal that only a REQUEST can run
        (start/stop_driver, no driver runtime here) is parked - the channel's own
        redelivery decides it inside such a request - but one whose decision this very
        message already took is answered here, because reading the proposal needs no
        runtime (_own_decision).

        Returns {"settled": how many records now carry their answer,
                 "deferred": how many were left pending because their decision is running,
                 "parked": how many need a driver request to decide them}."""
        with self.store.transaction() as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT actor,operation,request_key,result_json FROM driver_inbox_requests "
                "WHERE operation IN ('relay_owner_message','relay_owner_message:ref') "
                "AND result_json LIKE '%\"pending\"%'")]
        settled = deferred = parked = 0
        for row in rows:
            try:
                first = json.loads(row["result_json"])
            except ValueError:
                continue
            if not isinstance(first, dict) or not isinstance(first.get("pending"), dict):
                continue
            with self.store.transaction() as conn:
                source = conn.execute("SELECT channel,sender_ref,channel_message_ref FROM state_message_sources "
                                      "WHERE message_id=?", (first.get("message_id"),)).fetchone()
            if source is None or source["channel"] not in CHANNELS or not source["sender_ref"] \
                    or not source["channel_message_ref"]:
                continue
            try:
                with self.store.transaction() as conn:
                    kind = self.steward._get(conn, first["pending"].get("proposal_id"))["kind"]
                if kind in DRIVER_KINDS and self.steward.driver_runner is None:
                    # A driver kind without the runtime can only be PARKED: deciding it here
                    # would answer driver_runtime_unavailable and then watch recover() run
                    # the very approval it refused. Exception: a decision this message
                    # already took can be read from the row and needs nothing else.
                    carrier = {"channel": source["channel"], "sender_ref": source["sender_ref"],
                               "channel_message_ref": source["channel_message_ref"],
                               "phrase": first["pending"].get("phrase")}
                    if self._own_decision(first["pending"], carrier) in (None, _RUNNING):
                        parked += 1
                        continue
                answer = self._settle(first, source["channel"], source["sender_ref"],
                                      source["channel_message_ref"], actor=row["actor"])
            except (StateGraphError, StateNotFound, StateConflict):
                continue
            if answer is None:
                deferred += 1
            else:
                settled += 1
        if settled:
            log.warning("owner channel relay records settled at the start: %d", settled)
        return {"settled": settled, "deferred": deferred, "parked": parked}

    # ---------------------------------------------------------------- the steward's host
    def relay_owner_message(self, project_id, request_key, channel, sender_ref, channel_message_ref, body,
                            channel_at=None, precheck=False):
        """One owner message from a channel into the steward's conversation, then the
        chat approval check. Idempotent per request_key and per channel message: either
        replay answers the first result with duplicate: true - except an approval whose
        answer the ledger does not hold yet (the process stopped, or the ledger write
        failed, between the message and the decision): the redelivery decides it, or
        finds it decided by this message, and answers {duplicate: true, decided_on_replay:
        true, approval} so the channel sends its line - and a delivery that arrives WHILE
        the first one is still executing the decision is answered {duplicate: true,
        approval: null, approval_pending: true}: retryable, never a finished answer, so
        the channel asks again instead of counting the line as sent. A channel message is the
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
        if precheck:
            # The channel client's one question before it writes the owner's own words into
            # its durable owed store (lib/channels.mjs): may this text be kept? Nothing is
            # relayed, recorded or decided - see _store_text_verdict.
            return self._store_text_verdict(channel, sender_ref, body)
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
            # Retryable, not terminal: this answer says nothing about the decision, which
            # is still running, so the channel must ask again (a plain duplicate would
            # read as "answered" and the line would never be sent).
            return {"message_id": first["message_id"], "duplicate": True, "approval": None,
                    "approval_pending": True}
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
        binding code the message carries anywhere in it is not written:
        {refused: code_sent_to_bound_account} (see below)."""
        moment = _now()
        try:
            require_bound(conn, channel, sender_ref)
        except StateGraphError:
            self._log_refusal("channel_sender_not_bound", channel, sender_ref, size)
            raise
        # A LIVE binding code the bound owner's message carries - ANYWHERE in it, not only
        # as the whole body - is never written: the code is retired and the relay refused
        # code_sent_to_bound_account. The attempt is kept by its channel message like a
        # binding attempt, so a redelivery is a duplicate that writes nothing (the digits,
        # retired now, would otherwise land as a message). The pepper is read only here,
        # past the steward and binding checks; a text carrying no six-digit run reads no
        # secret and can carry no code.
        live = self.code_in_text(conn, body, moment)
        if live is not None:
            conn.execute("UPDATE state_owner_channel_codes SET retired_at=? WHERE code_hash=? "
                         "AND used_at IS NULL AND retired_at IS NULL",
                         (moment.isoformat(), live["code_hash"]))
            conn.execute("INSERT INTO state_owner_channel_bind_attempts(channel,sender_ref,channel_message_ref,"
                         "outcome,at) VALUES(?,?,?,?,?)",
                         (channel, sender_ref, channel_message_ref, "code_sent_to_bound_account", moment.isoformat()))
            log.warning("owner channel code retired: sent as a message from the bound account channel=%s "
                        "code_channel=%s sender_ref=%s at=%s", channel, live["channel"], sender_ref,
                        moment.isoformat())
            return {"refused": "code_sent_to_bound_account"}
        # The minute's and the hour's share, checked AFTER the binding and code checks on
        # purpose: `rate_limited` is a TRANSIENT answer, and a channel client holds a
        # transiently refused message for a retry (integrations/dsh-director/lib/
        # channels.mjs keeps it in a durable owed store). A body carrying a live code must
        # therefore never be answered that way - the only text a client may ever persist is
        # text the backend has not judged yet - and a stranger never learns that their
        # digits are a live code (the binding check runs first).
        per_minute, per_hour = rate_limits()
        for window, limit in ((timedelta(minutes=1), per_minute), (timedelta(hours=1), per_hour)):
            accepted = conn.execute("SELECT COUNT(*) FROM state_message_sources WHERE channel=? "
                                    "AND direction='in' AND at>?",
                                    (channel, (moment - window).isoformat())).fetchone()[0]
            if accepted >= limit:
                self._log_refusal("rate_limited", channel, sender_ref, size)
                raise StateGraphError(f"rate_limited: {channel} took {accepted} messages in the last "
                                      f"{int(window.total_seconds())} s; at most {limit}")
        self.store._project(conn, home)
        message_id, _, _ = self.inbox.write_user_half(conn, home, USER_SUBJECT, body)
        channel_at = None
        if written_at is not None and written_at - moment > CHANNEL_AT_MAX_AHEAD:
            log.warning("channel_at not stored: %s ahead of the backend's time channel=%s",
                        written_at - moment, channel)
        elif written_at is not None:
            channel_at = written_at.astimezone(timezone.utc).isoformat()
        conn.execute("INSERT INTO state_message_sources(message_id,direction,channel,sender_ref,"
                     "channel_message_ref,at,channel_at) VALUES(?,'in',?,?,?,?,?)",
                     (message_id, channel, sender_ref, channel_message_ref, moment.isoformat(), channel_at))
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

    def _settle(self, first, channel, sender_ref, channel_message_ref, actor=None):
        """Decide the approval a relayed message carries and put the answer in its two
        ledger records: {message_id, duplicate: false, approval}. Re-entry is safe - the
        decision checks the proposal's status and version in its own write transaction -
        and a proposal this very message already decided (the stop came after the
        decision, or the ledger write failed) is answered as that decision, not as
        proposal_not_open. None while the first delivery is still running it. A failing
        ledger write is raised: the decision stands in the proposal, the channel retries
        on the failure, and that redelivery records the answer and sends the line.
        `actor` is whose ledger rows these are (the steward driver's, by default this
        service's): a start settles rows the stopped process wrote under its own actor."""
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
                         (json.dumps(result), actor or self.actor, json.dumps(first)))
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

    def _store_text_verdict(self, channel, sender_ref, body):
        """{store_text}: may a channel client keep `body` in its durable owed store?

        The client writes the owner's own words down BEFORE it relays them, so a message
        whose answer times out (a start/stop decision runs longer than the client's bound)
        is asked again after a restart instead of its line vanishing. It must never write a
        live binding code down: the code is the one secret the owner sends.

        Only the host can answer both at once - it is the only side that knows which code is
        live. A local rule is either too wide or too narrow, measured both ways against this
        tree: a run of six digits refuses an approval whose proposal id carries one
        (core/state_steward.py:538 mints 20 hex chars; ~38% of ids carry such a run, and their
        line was then lost entirely), while `\\p{Nd}{6}` misses the 99 code points that
        unicodedata folds to a digit (superscripts, circled, dingbat, Ethiopic...), so those
        renderings were written down verbatim and then refused as the live code by
        _write_relay. So the client asks, and this is the same rule the relay applies.

        The binding check runs FIRST, exactly as in _write_relay: a stranger is answered
        store_text false without learning whether their digits were a live code.

        Reads only. The client asks before it has decided to keep anything, so a refusal here
        costs nothing and writes nothing.
        """
        with self.store.transaction() as conn:
            self._reader(conn)
            try:
                require_bound(conn, channel, sender_ref)
            except StateGraphError:
                return {"store_text": False}
            return {"store_text": self.code_in_text(conn, body, _now()) is None}

    def code_in_text(self, conn, body, moment):
        """The live binding code `body` carries ANYWHERE in it (its stored row, as
        {code_hash, channel}), or None. The one rule both owner-message writers apply:
        a code's plaintext is never stored, so the check is on the digit runs of a body
        (digit_windows), never on the body's shape, and the code is looked up by its
        keyed digest like a binding attempt (match_live_code).

        Reads no secret for a body with no six-digit run (it can carry no code), and
        reads no secret when no code is live either - the live rows are read first, so a
        flood of ordinary text carrying six digits costs no pepper read. With a live code
        and no pepper the text cannot be checked and is refused channel_secret_missing -
        fail closed: a copy of the State file must never be able to read the code out of
        stored text."""
        windows = digit_windows(body)
        if not windows:
            return None
        live = live_codes(conn, moment)
        if not live:
            return None
        pepper = self._pepper_value()
        if pepper is None:
            raise StateGraphError(f"channel_secret_missing: a live binding code cannot be checked without the "
                                  f"{drivers.PEPPER_SECRET_NAME} secret file (at least 32 characters); the text "
                                  "was not stored")
        return match_live_code(live, pepper, windows)

    def owner_text_guard(self, subject, body):
        """core.driver_inbox's hook (DriverInbox.owner_text_guard, installed in __init__):
        both of its owner writers - the dashboard conversation message
        (send_user_conversation_message) and the owner's dashboard message to a driver
        (send_driver_message) - call it with the subject and the body before anything is
        replayed or written. They are scanned as ONE text, joined by a newline, so a code
        split between the heading and the message is the same leak as a code in either."""
        self.refuse_owner_text_code(f"{subject}\n{body}" if subject else body)

    def refuse_owner_text_code(self, body):
        """The inbox writers' half of the same rule (owner_text_guard): a live binding code
        an owner writes on the dashboard (any project's conversation, or a message to any
        driver; any position in the text) is retired and the message is refused
        code_sent_to_bound_account - it is never written, and neither the message nor its
        request ledger row keeps the digits. Called before the inbox writes anything, like
        the relay's own check, and on every owner-written text field that lands in the row
        (the subject with the body): a code in the heading is the same leak as a code in
        the message."""
        windows = digit_windows(body)
        if not windows:
            return
        moment = _now()
        with self.store.transaction() as conn:
            live = live_codes(conn, moment)
        if not live:
            return
        pepper = self._pepper_value()
        if pepper is None:
            raise StateGraphError(f"channel_secret_missing: a live binding code cannot be checked without the "
                                  f"{drivers.PEPPER_SECRET_NAME} secret file (at least 32 characters); the message "
                                  "was not written")
        matched = match_live_code(live, pepper, windows)
        if matched is None:
            return
        with self.store.transaction(write=True) as conn:
            conn.execute("UPDATE state_owner_channel_codes SET retired_at=? WHERE code_hash=? "
                         "AND used_at IS NULL AND retired_at IS NULL", (moment.isoformat(), matched["code_hash"]))
        log.warning("owner channel code retired: written in an owner message on the dashboard code_channel=%s "
                    "actor=%s at=%s", matched["channel"], self.actor, moment.isoformat())
        raise StateGraphError("code_sent_to_bound_account: a live binding code was written in the owner's "
                              "message; it is retired - issue a new one on the dashboard")

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
            conn.execute("DELETE FROM state_owner_channel_bind_attempts WHERE channel=? AND rowid NOT IN "
                         "(SELECT rowid FROM state_owner_channel_bind_attempts WHERE channel=? "
                         "ORDER BY rowid DESC LIMIT ?)", (channel, channel, BIND_ATTEMPTS_KEPT))
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
            live = conn.execute("SELECT * FROM state_owner_channel_codes WHERE channel=? AND kind='account' "
                                "AND used_at IS NULL", (channel,)).fetchone()
            if live is not None and datetime.fromisoformat(live["expires_at"]) <= moment:
                live = None
            well_formed = len(code) == 6 and code.isascii() and code.isdigit()
            row = conn.execute("SELECT * FROM state_owner_channel_codes WHERE code_hash=? AND kind='account'",
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
            # Facts only (design-steward 7.9.2). A placeholder says nothing about the channel:
            # the row is marked not live and keeps both clocks (its QR, from a reading the
            # placeholder replaces, goes as it always did); a start keeps connected, a plugin
            # taken back is not connected (STATUS_STOPPED). A real reading starts the outage's
            # clock when the channel goes down, and up_since when it comes back.
            prev = conn.execute("SELECT * FROM state_owner_channel_status WHERE channel=?", (channel,)).fetchone()
            if not connected and detail in STATUS_PLACEHOLDERS:
                # qr_svg is cleared on purpose: a QR from before the placeholder (say, of a plugin
                # now stopped) can no longer be scanned, so the dashboard must not keep showing it.
                connected_now = "0" if detail == STATUS_STOPPED else "connected"
                conn.execute("INSERT INTO state_owner_channel_status(channel,connected,detail,qr_svg,at,live) "
                             "VALUES(?,0,?,NULL,?,0) ON CONFLICT(channel) DO UPDATE SET live=0,detail=excluded.detail,"
                             f"qr_svg=NULL,at=excluded.at,connected={connected_now}", (channel, detail, stamp))
            else:
                was_up = _was_up(prev)
                down_since, up_since = (prev["down_since"], prev["up_since"]) if prev is not None else (None, None)
                if connected and not was_up:
                    up_since = stamp
                if not connected and (was_up or down_since is None):
                    down_since = stamp
                conn.execute("INSERT INTO state_owner_channel_status(channel,connected,detail,qr_svg,at,live,down_since,"
                             "up_since) VALUES(?,?,?,?,?,1,?,?) "
                             "ON CONFLICT(channel) DO UPDATE SET connected=excluded.connected,detail=excluded.detail,"
                             "qr_svg=excluded.qr_svg,at=excluded.at,live=1,down_since=excluded.down_since,"
                             "up_since=excluded.up_since",
                             (channel, int(bool(connected)), detail, None if connected else qr_svg, stamp, down_since,
                              up_since))
            return self.steward._record(conn, "report_channel_status", request_key, request_hash,
                                        {"recorded": True, "channel": channel, "connected": bool(connected),
                                         "at": stamp})

    # ---------------------------------------------------------------- channel-down alert (host, in code)
    @writer_only_read("due_channel_alerts")
    def due_channel_alerts(self):
        """The steward's host (or the owner): the channel alerts due now, [{channel, kind,
        down_since, attempt}]. A read: nothing is written, not even a request record."""
        moment = _now()
        with self.store.transaction() as conn:
            self._reader(conn)
            due = due_alerts(*_alert_facts(conn), moment)
        return {"due": [{k: d[k] for k in ("channel", "kind", "down_since", "attempt")} for d in due]}

    def claim_channel_alert(self, project_id, request_key, channel, kind, down_since, attempt, exclude=()):
        """The steward's host, in code: may it send this alert now? Judged again here, in one
        transaction, by due_alerts: due with this very outage and attempt, through a `via`
        not in `exclude` (the channels the host cannot send through now: not registered there,
        or their current reading not accepted here) - the ledger row is claimed and the answer
        is {granted: true, via, to, minutes, detail}, the same for the same request_key;
        otherwise {granted: false} and nothing is written, not even the request record, so the
        same key may be granted once it is due. The request's digest is [channel, kind,
        down_since, attempt] only: a granted claim asked again with another `exclude` answers
        its first grant (that row is already claimed through that via)."""
        if channel not in CHANNELS:
            raise StateGraphError(f"invalid_channel: channel must be one of {', '.join(CHANNELS)}")
        if kind not in ALERT_KINDS or attempt not in range(1, ALERT_ATTEMPTS + 1):
            raise StateGraphError("invalid_request: kind is down or up, attempt 1 or 2")
        exclude = tuple(exclude or ())
        if any(c not in CHANNELS for c in exclude):
            raise StateGraphError(f"invalid_channel: exclude names only {', '.join(CHANNELS)}")
        request_hash = _hash([channel, kind, down_since, attempt])
        moment = _now()
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "claim_channel_alert", request_key, request_hash)
            if replay is not None:
                return replay
            statuses, bindings, ledger = _alert_facts(conn)
            due = [d for d in due_alerts(statuses, bindings, ledger, moment, exclude)
                   if (d["channel"], d["kind"], d["down_since"], d["attempt"]) == (channel, kind, down_since, attempt)]
            if not due:
                return {"granted": False}
            via, status = due[0]["via"], statuses[channel]
            conn.execute("INSERT INTO state_channel_alerts(channel,kind,down_since,state,attempt,via,claimed_at) "
                         "VALUES(?,?,?,'claimed',?,?,?) ON CONFLICT(channel,kind,down_since) DO UPDATE SET "
                         "state='claimed',attempt=excluded.attempt,via=excluded.via,claimed_at=excluded.claimed_at,"
                         "finished_at=NULL,error=NULL", (channel, kind, down_since, attempt, via, moment.isoformat()))
            until = moment if kind == "down" or status["up_since"] is None else _at(status["up_since"])
            return self.steward._record(conn, "claim_channel_alert", request_key, request_hash, {
                "granted": True, "via": via, "to": bindings[via]["sender_ref"],
                "minutes": max(0, int((until - _at(down_since)).total_seconds() // 60)),
                "detail": (status["detail"] or "")[:ALERT_DETAIL_MAX] or None})

    def record_channel_alert(self, project_id, request_key, channel, kind, down_since, attempt, ok, error=None):
        """The steward's host: how the alert it claimed went - `ok`, or the channel's `error`.
        It settles only the ledger row of that outage while it is still this attempt's claim;
        a late or replayed result changes nothing else ({recorded: false})."""
        if channel not in CHANNELS:
            raise StateGraphError(f"invalid_channel: channel must be one of {', '.join(CHANNELS)}")
        if kind not in ALERT_KINDS or (ok is True) == bool(error):
            raise StateGraphError("invalid_request: kind is down or up, and either ok or an error")
        error = error[:ALERT_ERROR_MAX] if error else None
        request_hash = _hash([channel, kind, down_since, attempt, ok, error])
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "record_channel_alert", request_key, request_hash)
            if replay is not None:
                return replay
            changed = conn.execute("UPDATE state_channel_alerts SET state=?,finished_at=?,error=? WHERE channel=? "
                                   "AND kind=? AND down_since=? AND attempt=? AND state='claimed'",
                                   ("sent" if ok else "failed", _now().isoformat(), error, channel, kind, down_since,
                                    attempt)).rowcount
            return self.steward._record(conn, "record_channel_alert", request_key, request_hash,
                                        {"recorded": changed == 1, "channel": channel, "kind": kind})

    # ---------------------------------------------------------------- new login QR (owner -> host)
    @staticmethod
    def _login_view(row, moment):
        status = row["status"]
        if status == "pending" and datetime.fromisoformat(row["requested_at"]) + LOGIN_REQUEST_TTL <= moment:
            status = "expired"
        return {"request_id": row["request_id"], "channel": row["channel"], "status": status,
                "requested_at": row["requested_at"], "completed_at": row["completed_at"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None}

    def request_login(self, channel):
        """The owner asks for a new login QR on `channel`: one pending request the steward's
        host runs (the dashboard's button). A pending request of the channel that has not
        expired is answered again instead of a second one."""
        self.steward._owner()
        if channel not in LOGIN_CHANNELS:
            raise StateGraphError(f"invalid_channel: a login QR exists only for {', '.join(LOGIN_CHANNELS)}")
        moment, home = _now(), steward_project()
        with self.store.transaction(write=True) as conn:
            for row in conn.execute("SELECT * FROM state_owner_channel_login_requests WHERE channel=? AND status='pending' "
                                    "ORDER BY requested_at DESC", (channel,)).fetchall():
                view = self._login_view(row, moment)
                if view["status"] == "pending":
                    return view
            request_id = "qrreq-" + secrets.token_hex(10)
            conn.execute("INSERT INTO state_owner_channel_login_requests(request_id,channel,requested_at,requested_by,"
                         "status) VALUES(?,?,?,?,'pending')", (request_id, channel, moment.isoformat(), self.actor))
            conn.execute("DELETE FROM state_owner_channel_login_requests WHERE channel=? AND request_id NOT IN "
                         "(SELECT request_id FROM state_owner_channel_login_requests WHERE channel=? "
                         "ORDER BY requested_at DESC, request_id DESC LIMIT ?)", (channel, channel, LOGIN_REQUESTS_KEPT))
            # The steward's runtime wakes on this event and runs the request in code.
            if conn.execute("SELECT 1 FROM state_projects WHERE project_id=?", (home,)).fetchone():
                self.store._event(conn, home, None, "owner_channel_login_requested",
                                  {"request_id": request_id, "channel": channel})
            row = conn.execute("SELECT * FROM state_owner_channel_login_requests WHERE request_id=?",
                               (request_id,)).fetchone()
        self.steward._notify()
        return self._login_view(row, moment)

    @writer_only_read("list_owner_channel_login_requests")
    def list_login_requests(self):
        """The steward's host: the requests still to run (pending, not expired), oldest first."""
        moment = _now()
        with self.store.transaction() as conn:
            self.steward._steward(conn)
            rows = conn.execute("SELECT * FROM state_owner_channel_login_requests WHERE status='pending' "
                                "ORDER BY requested_at, request_id").fetchall()
        return {"requests": [{"request_id": r["request_id"], "channel": r["channel"], "requested_at": r["requested_at"]}
                             for r in rows if self._login_view(r, moment)["status"] == "pending"]}

    def complete_login_request(self, project_id, request_key, request_id, result):
        """pending -> done with what the channel answered: {requested: true} or {refused,
        retry_after_s?} and nothing else (never a QR). A request run after its TTL is still
        completed: it did run."""
        if not isinstance(result, dict):
            raise StateGraphError("invalid_result: result must be {requested: true} or {refused, retry_after_s?}")
        if result != {"requested": True}:
            retry = result.get("retry_after_s")
            if (set(result) - {"refused", "retry_after_s"} or not isinstance(result.get("refused"), str)
                    or not _LOGIN_REFUSAL.fullmatch(result["refused"])
                    or (retry is not None and (isinstance(retry, bool) or not isinstance(retry, int)
                                               or not 0 < retry <= 86400))):
                raise StateGraphError("invalid_result: result must be {requested: true} or {refused, retry_after_s?}")
        request_hash = _hash([request_id, result])
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "complete_owner_channel_login_request", request_key, request_hash)
            if replay is not None:
                return replay
            row = conn.execute("SELECT status FROM state_owner_channel_login_requests WHERE request_id=?",
                               (request_id,)).fetchone()
            if row is None:
                raise StateNotFound("unknown_login_request")
            if row["status"] != "pending":
                raise StateConflict("login_request_not_pending")
            conn.execute("UPDATE state_owner_channel_login_requests SET status='done',result_json=?,completed_at=? "
                         "WHERE request_id=?", (canonical(result), _now().isoformat(), request_id))
            recorded = self.steward._record(conn, "complete_owner_channel_login_request", request_key, request_hash,
                                            {"request_id": request_id, "status": "done"})
        self.steward._notify()
        return recorded

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
            ledger = conn.execute("SELECT * FROM state_channel_alerts").fetchall() if with_status else []
            # Each channel's newest request for a new login QR (the dashboard's feedback).
            logins = {r["channel"]: r for r in conn.execute(
                "SELECT * FROM state_owner_channel_login_requests AS r WHERE requested_at=(SELECT MAX(requested_at) "
                "FROM state_owner_channel_login_requests WHERE channel=r.channel)")} if with_status else {}
        moment = _now()
        due = {d["channel"]: d for d in due_alerts(statuses, rows, ledger, moment)} if with_status else {}
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
                    status=self._status_view(status, [r for r in ledger if r["channel"] == channel],
                                             due.get(channel), moment) if status is not None else None,
                    qr_svg=status["qr_svg"] if status is not None else None,
                    login_request=self._login_view(logins[channel], moment) if channel in logins else None)
        if not with_status:
            return {"channels": channels, "default_channel": default}
        quiet = quiet_until(moment)
        return {"channels": channels, "default_channel": default, "now": moment.isoformat(),
                "quiet_now": quiet is not None, "quiet_until": quiet.isoformat() if quiet else None,
                "owner_tz": default_tz(), "alert_limits": alert_limits()}

    @staticmethod
    def _status_view(status, rows, due, moment):
        """A status row for the dashboard: its facts, and the projections of the alert ledger
        the Channels card reads (design-steward 7.9.2) - this outage's down alert, the recovery
        notice of the last delivered one, what the owner was last told, and what is due now."""
        def ledger_row(row):
            return None if row is None else {k: row[k] for k in (
                "kind", "down_since", "state", "attempt", "via", "claimed_at", "finished_at", "error")}
        own = {(r["kind"], r["down_since"]): r for r in rows}
        sent = sorted((r for r in rows if r["kind"] == "down" and r["state"] == "sent"), key=lambda r: r["finished_at"])
        last = _last_told(rows, moment)
        return {"connected": bool(status["connected"]), "detail": status["detail"], "at": status["at"],
                "live": bool(status["live"]), "down_since": status["down_since"], "up_since": status["up_since"],
                "alert_down": ledger_row(own.get(("down", status["down_since"]))) if status["down_since"] else None,
                "alert_up": ledger_row(own.get(("up", sent[-1]["down_since"]))) if sent else None,
                "last_told": {"kind": last[0]["kind"], "down_since": last[0]["down_since"], "at": last[1].isoformat()}
                if last else None,
                "last_down_sent_at": sent[-1]["finished_at"] if sent else None,
                "alert_due": {"kind": due["kind"], "attempt": due["attempt"]} if due else None}

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
                                              "error": r["error"], "channel_at": r["channel_at"]} for r in rows}}

    # ---------------------------------------------------------------- the owner (dashboard)
    def issue_code(self, channel):
        """A fresh 6-digit code for binding `channel`; the plaintext exists only in
        this answer, the backend keeps its keyed digest (_code_hash). Any earlier unused
        code of the channel stops working."""
        self.steward._owner()
        pepper = self._code_key()
        moment = _now()
        with self.store.transaction(write=True) as conn:
            conn.execute("DELETE FROM state_owner_channel_codes WHERE channel=? AND kind='account' AND used_at IS NULL",
                         (channel,))
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
        to the steward but never approves - core.state_steward.is_owner_session).

        This writer is held to the same rule as the relay: a live binding code the text
        carries anywhere is retired and the message refused code_sent_to_bound_account,
        before the inbox (and its request ledger) writes anything - the inbox's own
        owner_text_guard does it, with the subject and body as one text (the subject is the
        same text field on the same stored row)."""
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
