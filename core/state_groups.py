"""The steward in the owner's family group (step 23, design-steward 7.7): WeChat first.

The owner binds ONE group per channel with a group code (`G-XXXX-XXXX`, 10 minutes, once,
kind 'group' in state_owner_channel_codes) that the owner's bound account posts in the
group; the steward's HOST relays every @-message the bridge hands over
(`relay_group_message`) and posts the steward's last words back (`post_group_reply`).
What the backend decides here, in code, never the model nor the message text:

- who spoke: `sender_role` is `owner` only when the sender is the channel's CURRENT bound
  account (core.state_channels.require_bound's read), else `member`;
- the bounds (GROUP_LIMITS, owner's numbers of 2026-11-20 and the platform director's
  ruling of 2026-11-20 on Q6: code constants, never read from the environment, pinned by
  tests): 60 accepted questions per group and hour, 20 per member (`sender_ref`, so a
  dashboard rename never resets it; the owner counts toward the 60 only), 120 outbound
  group messages per account (channel) and hour (`group_quota_account`), one quota notice
  per group and hour, a message the channel dates more than 10 minutes ago is dropped,
  rows are kept 30 days;
- the exit check on a reply (live codes of either kind, any non-public project's id or
  title, a host path): the group gets the fixed sentence, the original is kept withheld
  for the owner;
- the turn audience the steward's host records each turn (`set_steward_turn_audience`):
  the family memory (`note_family_memory`, `list_family_memory`) is accepted only on a
  request with the public-read header that names the recorded turn (`turn`), whose
  recorded audience is a bound group;
- the group reminders (step 25 C11, design-steward 7.9.4): `post_group_notice` posts a
  reminder's text, the owner's exact words, taken from the schedule row (never from the
  call) after that row fired for that due time, to the group the row recorded if that binding
  still stands, through the same exit check, at most group_notices_per_hour per group (apart
  from the 60 questions) and within the account's 120 outbound messages an hour. No model
  turn: the host sends it like a reply.

None of these writes a State event and none notifies a subscriber: a group message must
never reach the steward as an owner-audience runtime event (7.7.7). Logs carry channel,
group, sender, time and length, never a body. Every table is private
(core.state_privacy.PRIVATE_STATE_TABLES).
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import unicodedata
from datetime import datetime, timedelta
from types import MappingProxyType

from core.message_body_guard import refuse_credential_body
from core import state_channels
from core.state_channels import (BODY_MAX_BYTES, CHANNELS, CODE_TTL, _code_hash, _require_utf8, _written_at,
                                 digit_windows, require_bound)
from core.state_graph import StateConflict, StateGraphError, StateNotFound
from core.state_privacy import writer_only_read
from core.state_steward import _hash

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_owner_groups (
 channel TEXT NOT NULL CHECK(channel IN ('slack','wechat')),
 group_ref TEXT NOT NULL CHECK(group_ref<>''),
 label TEXT, mention_prefix TEXT NOT NULL, bound_at TEXT NOT NULL, revoked_at TEXT,
 PRIMARY KEY(channel, group_ref));
CREATE UNIQUE INDEX IF NOT EXISTS state_owner_groups_one_live ON state_owner_groups(channel)
 WHERE revoked_at IS NULL;
CREATE TABLE IF NOT EXISTS state_group_messages (
 message_id TEXT PRIMARY KEY,
 channel TEXT NOT NULL, group_ref TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('in','out')),
 kind TEXT NOT NULL,
 accepted INTEGER NOT NULL DEFAULT 0 CHECK(accepted IN (0,1)),
 sender_ref TEXT, sender_role TEXT CHECK(sender_role IS NULL OR sender_role IN ('owner','member')),
 channel_message_ref TEXT, body TEXT, original TEXT, withheld_reason TEXT,
 reply_to TEXT, answered_by TEXT, request_key TEXT, at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS state_group_messages_inbound
 ON state_group_messages(channel, group_ref, channel_message_ref) WHERE direction='in';
CREATE UNIQUE INDEX IF NOT EXISTS state_group_messages_reply_key
 ON state_group_messages(request_key) WHERE direction='out' AND request_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS state_group_messages_window ON state_group_messages(channel, direction, at);
CREATE TABLE IF NOT EXISTS state_group_members (
 channel TEXT NOT NULL, group_ref TEXT NOT NULL, sender_ref TEXT NOT NULL CHECK(sender_ref<>''),
 ordinal INTEGER NOT NULL, label TEXT, named_at TEXT,
 PRIMARY KEY(channel, group_ref, sender_ref));
CREATE TABLE IF NOT EXISTS state_family_memory (
 entry_id TEXT PRIMARY KEY, channel TEXT NOT NULL, group_ref TEXT NOT NULL, text TEXT NOT NULL,
 added_by_role TEXT, added_by_ref TEXT, at TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS state_family_note (
 channel TEXT NOT NULL, group_ref TEXT NOT NULL, text TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(channel, group_ref));
CREATE TABLE IF NOT EXISTS state_steward_turn_audience (
 id INTEGER PRIMARY KEY CHECK(id=1), turn TEXT NOT NULL,
 audience TEXT NOT NULL CHECK(audience IN ('owner','group')),
 channel TEXT, group_ref TEXT, sender_refs_json TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state_pending_groups (
 channel TEXT NOT NULL, room_ref TEXT NOT NULL CHECK(room_ref<>''), mention_prefix TEXT NOT NULL,
 code_hash TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(channel, room_ref));
"""

# The platform director's ruling of 2026-11-20 (design 7.7.10, Q6): the group bounds live in
# CODE, here, in the layer that enforces them; no environment variable overrides them (unlike
# core.state_channels.rate_limits) and tests pin every number. Reopen only when the owner asks
# to tune a limit without a code change. The plugin keeps its own (merge and tool-call bounds,
# integrations/dsh-director/lib/channels.mjs GROUP_LIMITS) and the bridge its send gap and
# backstop (integrations/wechat-bridge/bridge.mjs GROUP_SEND_LIMITS); no key is in two places.
GROUP_LIMITS = MappingProxyType({
    "group_per_hour": 60, "member_per_hour": 20, "account_out_per_hour": 120, "notice_per_hour": 1,
    "fresh_minutes": 10, "retention_days": 30,
    # One reply is one WeChat message (the bridge splits at 1500): quote line + body <= this.
    "reply_chars": 1400,
    # Step 25 C11 (design-steward 7.9.4): the owner's group reminders, counted apart from the 60
    # questions: a group gets at most 60 + 1 + 1 + 20 + 6 = 88 messages an hour, under the
    # bridge's 90 backstop; every one also counts toward account_out_per_hour.
    "group_notices_per_hour": 6,
})
# The family memory (7.7.8a, director 420): bounded, the same 30 days as the group rows.
FAMILY_LIMITS = MappingProxyType({"entries": 100, "entry_chars": 200, "note_chars": 1000, "show_chars": 20000})
# Step 0 item 1 (7.7.11) decides this: True only if the owner's id in a group is NOT the
# account's 1:1 id - then a valid group code posted by anyone records a pending group the
# owner picks on the dashboard (7.7.9 last item) instead of binding on the owner's post.
PICK_PENDING_GROUPS = False

# What the group sees instead of a withheld reply: the same words steward.md tells the model
# to use for "no such project" and "cannot say here", so the three read alike (7.7.7).
WITHHELD_TEXT = "这个我在群里答不了。"
# What the owner is told when the exit check holds something back (a reminder or a list item,
# step 25): which kind of thing it found, never the thing itself.
WITHHELD_REASONS = MappingProxyType({"live_code": "a live binding code", "private_project":
                                     "the name or id of a project that is not public",
                                     "host_path": "a path on the host"})
QUOTA_NOTICE = "问得太多了，过一会儿再问。"
BIND_NOTICE = ("管家已加入本群。@ 我提问即可；我在群里只回答公开项目和一般问题，不能替任何人做决定，也不传话；私聊我不会回复。"
               "@ 我的消息会保存 30 天，业主看得到；它们会交给外部模型服务处理，管家自己的会话记录里可能留得更久。"
               "让我记住的事只在本群用，30 天后删。")
OWNER_LABEL = "业主"
# 7.9.4 step 4 (director 489 (b)): a group reminder's row is kind 'notice' with sender_role NULL
# (step 23's CHECK stays owner/member). A reader that names a row's sender reads it as this.
STEWARD_NOTICE_LABEL = "管家通知"
_STEMS = "甲乙丙丁戊己庚辛壬癸"
# Crockford base32 (no I L O U).
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_GROUP_CODE = re.compile(r"G([%s]{8})" % _CROCKFORD)
# The exit check's host paths (7.7.7 item 3), a closed enumeration of the forms the steward's
# context and this host carry: the home forms (`~/` covers ~/.AItelier and ~/.aitelier-secrets),
# the container's /app/ and the system roots. Matched as substrings of the NFKC-folded body,
# so a full-width ／ or ～ is the same path. A web address is not a host path: http(s) URLs are
# taken out before the match, so 'https://apps.apple.com/cn/app/…' or a site's '/home/' page
# passes (a host path dressed as a URL's path is a rewording, like any paraphrase, 7.7.2).
# Ordinary text with slashes ('A/B 测试', 'and/or', '3/4') names none of them and passes.
_PATHS = ("~/", "/home/", "/root/", "/app/", "/tmp/", "/srv/", "/opt/", "/var/", "/etc/", "/run/")
# The URL's characters are a narrow set (no '~', ',', ';', quotes or brackets), so a URL never
# swallows a path written right after it ('https://x.cn，~/.AItelier' is still withheld).
_WEB_URL = re.compile(r"https?://[a-z0-9\-._:/?#@%&=+]*")
# The platform's own name (also the private project `aitelier`'s id) and the steward's own
# project are the exit check's only exemptions (7.7.7 item 2).
_EXEMPT_TERMS = frozenset({"aitelier", "aitelier-home"})


def member_label(ordinal: int) -> str:
    return "成员" + (_STEMS[ordinal - 1] if 1 <= ordinal <= len(_STEMS) else str(ordinal))


def normalise_group_code(text) -> str:
    """NFKC, upper case, without whitespace and '-': 'g-7k2m q9xd' -> 'G7K2MQ9XD'."""
    folded = unicodedata.normalize("NFKC", str(text)).upper()
    return "".join(ch for ch in folded if not ch.isspace() and ch != "-")


def group_code_hash(pepper: str, channel: str, code: str) -> str:
    """A group code's stored digest: the account codes' keyed HMAC with the kind in the
    message (`owner-channel-code:group:{channel}:{code}`), so a group code and an account
    code can never stand for each other. The account form stays as it is (no live account
    code is invalidated by this change); 'group' is never a channel name."""
    return _code_hash(pepper, f"group:{channel}", code)


def _cjk(text: str) -> bool:
    return any(unicodedata.east_asian_width(ch) in ("W", "F") for ch in text)


def _ascii_word(term: str, folded: str) -> bool:
    return re.search(r"(?<![a-z0-9_\-])" + re.escape(term) + r"(?![a-z0-9_\-])", folded) is not None


def private_terms(conn):
    """Every non-public project's id and title (the exit check's names), minus the two
    exemptions."""
    rows = conn.execute(
        "SELECT p.project_id, p.title FROM state_projects p LEFT JOIN state_project_access a "
        "ON a.project_id=p.project_id AND a.visibility='public' WHERE a.project_id IS NULL").fetchall()
    terms = set()
    for project_id, title in rows:
        if project_id == "aitelier-home":
            continue
        for term in (project_id, title):
            if isinstance(term, str):
                folded = unicodedata.normalize("NFKC", term).casefold().strip()
                if folded and folded not in _EXEMPT_TERMS:
                    terms.add(folded)
    return terms


def _now():
    """The channels' clock (core.state_channels._now, which tests replace)."""
    return state_channels._now()


class StateGroups:
    def __init__(self, store, actor, driver_id=None, steward=None, channels=None, public_view=False):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        self.steward, self.channels = steward, channels
        self.project_read_trusted = store.project_read_trusted
        self.public_view = public_view
        from core.state_privacy import UntrustedDatabase
        if not isinstance(store.db, UntrustedDatabase):
            with store.db.get_connection() as conn:
                conn.executescript(SCHEMA)
                conn.commit()

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _log(event, channel, group_ref, sender_ref, size=None):
        log.warning("owner group %s channel=%s group_ref=%s sender_ref=%s at=%s%s", event, channel, group_ref,
                    sender_ref, _now().isoformat(), "" if size is None else f" bytes={size}")

    @staticmethod
    def _live_group(conn, channel, group_ref=None):
        row = conn.execute("SELECT * FROM state_owner_groups WHERE channel=? AND revoked_at IS NULL",
                           (channel,)).fetchone()
        if row is None or (group_ref is not None and row["group_ref"] != group_ref):
            return None
        return row

    @staticmethod
    def _member(conn, channel, group_ref, sender_ref, role):
        """The sender's stable name in this group: 业主, the owner's dashboard label, or
        成员甲.. by first appearance (the row is made on first sight)."""
        if role == "owner":
            return OWNER_LABEL
        row = conn.execute("SELECT ordinal,label FROM state_group_members WHERE channel=? AND group_ref=? "
                           "AND sender_ref=?", (channel, group_ref, sender_ref)).fetchone()
        if row is None:
            ordinal = conn.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM state_group_members WHERE channel=? "
                                   "AND group_ref=?", (channel, group_ref)).fetchone()[0]
            conn.execute("INSERT INTO state_group_members(channel,group_ref,sender_ref,ordinal) VALUES(?,?,?,?)",
                         (channel, group_ref, sender_ref, ordinal))
            return member_label(ordinal)
        return row["label"] or member_label(row["ordinal"])

    @staticmethod
    def _label_of(conn, channel, group_ref, sender_ref, role):
        if role == "owner":
            return OWNER_LABEL
        if role != "member":  # NULL: no person (the steward's rows); never read as a member
            return None
        row = conn.execute("SELECT ordinal,label FROM state_group_members WHERE channel=? AND group_ref=? "
                           "AND sender_ref=?", (channel, group_ref, sender_ref)).fetchone()
        return (row["label"] or member_label(row["ordinal"])) if row else None

    @staticmethod
    def _retain(conn, moment):
        cutoff = (moment - timedelta(days=GROUP_LIMITS["retention_days"])).isoformat()
        conn.execute("DELETE FROM state_group_messages WHERE at<?", (cutoff,))
        conn.execute("DELETE FROM state_family_memory WHERE at<?", (cutoff,))

    @staticmethod
    def _count(conn, sql, args):
        return conn.execute(sql, args).fetchone()[0]

    def _out_row(self, conn, channel, group_ref, kind, body, moment, *, reply_to=None, original=None,
                 reason=None, request_key=None):
        """Every message sent into a group is first one stored outbound row: what the
        account's hourly outbound cap counts (7.7.8, 7.7.10)."""
        message_id = "gmsg-" + secrets.token_hex(10)
        conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,body,"
                     "original,withheld_reason,reply_to,request_key,at) VALUES(?,?,?,'out',?,1,?,?,?,?,?,?)",
                     (message_id, channel, group_ref, kind, body, original, reason, reply_to, request_key,
                      moment.isoformat()))
        return message_id

    def _pepper(self):
        return self.channels._code_key()

    def _live_group_codes(self, conn, moment):
        return {row["code_hash"]: row["channel"] for row in conn.execute(
            "SELECT code_hash,channel,expires_at FROM state_owner_channel_codes WHERE kind='group' "
            "AND used_at IS NULL AND retired_at IS NULL") if datetime.fromisoformat(row["expires_at"]) > moment}

    # ---------------------------------------------------------------- the owner (dashboard)
    def issue_group_code(self, channel, label=None):
        """A fresh group code for `channel` (owner session only): the channel must have a
        bound account and no bound group (v1: one group per channel). Any earlier unused
        group code of the channel stops working; account codes are untouched."""
        self.steward._owner()
        if channel not in CHANNELS:
            raise StateGraphError("invalid_channel")
        if label is not None:
            label = unicodedata.normalize("NFKC", label).strip()[:40] or None
        pepper = self._pepper()
        moment = _now()
        with self.store.transaction(write=True) as conn:
            account = conn.execute("SELECT sender_ref FROM state_owner_channels WHERE channel=? AND revoked_at IS NULL",
                                   (channel,)).fetchone()
            if account is None or account[0] is None:
                raise StateGraphError("channel_not_bound: bind your own account on this channel first")
            if self._live_group(conn, channel) is not None:
                raise StateConflict("group_already_bound: unbind the bound group first")
            conn.execute("DELETE FROM state_owner_channel_codes WHERE channel=? AND kind='group' AND used_at IS NULL",
                         (channel,))
            code = "G" + "".join(secrets.choice(_CROCKFORD) for _ in range(8))
            conn.execute("INSERT INTO state_owner_channel_codes(code_hash,channel,issued_at,expires_at,kind,label) "
                         "VALUES(?,?,?,?,'group',?)",
                         (group_code_hash(pepper, channel, code), channel, moment.isoformat(),
                          (moment + CODE_TTL).isoformat(), label))
        return {"code": f"G-{code[1:5]}-{code[5:]}", "expires_at": (moment + CODE_TTL).isoformat()}

    def unbind_owner_group(self, channel):
        """Unbind the channel's group and delete everything kept about it; the family lists
        stay, without who of this group added or checked an item (7.9.5)."""
        self.steward._owner()
        with self.store.transaction(write=True) as conn:
            row = self._live_group(conn, channel)
            if row is not None:
                group = row["group_ref"]
                conn.execute("UPDATE state_owner_groups SET revoked_at=? WHERE channel=? AND group_ref=?",
                             (_now().isoformat(), channel, group))
                # 7.9.5 (director 489 (a)): the family lists stay; who of this group added or
                # checked an item is forgotten (its members, and the owner's account here).
                from core.state_lists import forget_group_senders
                forget_group_senders(conn, [r[0] for r in conn.execute(
                    "SELECT sender_ref FROM state_group_members WHERE channel=? AND group_ref=? UNION "
                    "SELECT sender_ref FROM state_owner_channels WHERE channel=?", (channel, group, channel))])
                for table in ("state_group_messages", "state_group_members", "state_family_memory",
                              "state_family_note"):
                    conn.execute(f"DELETE FROM {table} WHERE channel=? AND group_ref=?", (channel, group))
                # Step 25 C11 (7.9.4): the reminders made for this binding stop; a new binding
                # (even of the same group: a new bound_at) never revives them.
                stopped = [r[0] for r in conn.execute(
                    "SELECT schedule_id FROM state_steward_schedules WHERE target='family_group' AND target_channel=? "
                    "AND target_group_ref=? AND target_bound_at=? AND deleted_at IS NULL AND enabled=1",
                    (channel, group, row["bound_at"]))]
                for schedule_id in stopped:
                    conn.execute("UPDATE state_steward_schedules SET version=version+1,enabled=0,next_run_at=NULL,"
                                 "updated_by='owner',updated_at=? WHERE schedule_id=?", (_now().isoformat(), schedule_id))
                    self.steward._schedule_changed(conn, schedule_id)
                disabled = len(stopped)
        if row is not None and disabled:
            self.steward._notify()
        return {"unbound": row is not None, "channel": channel,
                **({"reminders_disabled": disabled} if row is not None else {})}

    def clear_group_messages(self, channel):
        self.steward._owner()
        with self.store.transaction(write=True) as conn:
            row = self._live_group(conn, channel)
            if row is None:
                raise StateNotFound("group_not_bound")
            deleted = conn.execute("DELETE FROM state_group_messages WHERE channel=? AND group_ref=?",
                                   (channel, row["group_ref"])).rowcount
        return {"cleared": deleted}

    def name_group_member(self, channel, sender_ref, label):
        """The owner names a member the bridge reported (7.7.4): only the label changes, so
        the quota window and the family memory keep their key (`sender_ref`)."""
        self.steward._owner()
        label = unicodedata.normalize("NFKC", str(label)).strip()
        if not label or len(label) > 20:
            raise StateGraphError("invalid_label: 1-20 characters")
        with self.store.transaction(write=True) as conn:
            row = self._live_group(conn, channel)
            if row is None:
                raise StateNotFound("group_not_bound")
            changed = conn.execute("UPDATE state_group_members SET label=?, named_at=? WHERE channel=? AND group_ref=? "
                                   "AND sender_ref=?", (label, _now().isoformat(), channel, row["group_ref"],
                                                       sender_ref)).rowcount
            if not changed:
                raise StateNotFound("unknown_member: this group never saw that sender")
        return {"named": True, "sender_ref": sender_ref, "label": label}

    def list_pending_groups(self):
        self.steward._owner()
        cutoff = (_now() - CODE_TTL).isoformat()
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT channel,room_ref,mention_prefix,at FROM state_pending_groups WHERE at>? "
                                "ORDER BY at", (cutoff,)).fetchall()
        return {"pending": [{"channel": r["channel"], "room_ref_tail": r["room_ref"][-6:], "room_ref": r["room_ref"],
                             "mention_prefix": r["mention_prefix"], "at": r["at"]} for r in rows]}

    def pick_pending_group(self, channel, room_ref):
        """The owner binds a group the bridge reported with a valid group code in the last 10
        minutes (only on the PICK_PENDING_GROUPS path). Never chooses between two."""
        self.steward._owner()
        moment = _now()
        cutoff = (moment - CODE_TTL).isoformat()
        with self.store.transaction(write=True) as conn:
            rows = conn.execute("SELECT * FROM state_pending_groups WHERE channel=? AND at>?",
                                (channel, cutoff)).fetchall()
            if len(rows) > 1:
                raise StateConflict("several_pending_groups: issue a new group code and post it again")
            if not rows or rows[0]["room_ref"] != room_ref:
                raise StateNotFound("unknown_pending_group")
            if self._live_group(conn, channel) is not None:
                raise StateConflict("group_already_bound")
            code = conn.execute("SELECT label,used_at,retired_at,expires_at FROM state_owner_channel_codes "
                                "WHERE code_hash=?", (rows[0]["code_hash"],)).fetchone()
            if code is None or code["used_at"] or code["retired_at"] or datetime.fromisoformat(code["expires_at"]) <= moment:
                raise StateConflict("code_expired")
            self._bind(conn, channel, room_ref, code["label"], rows[0]["mention_prefix"], rows[0]["code_hash"], moment)
            conn.execute("DELETE FROM state_pending_groups WHERE channel=?", (channel,))
            notice = self._out_row(conn, channel, room_ref, "bind_notice", BIND_NOTICE, moment)
        return {"bound": True, "channel": channel, "group_ref": room_ref, "notice": BIND_NOTICE,
                "notice_message_id": notice}

    def set_family_note(self, channel, text):
        self.steward._owner()
        text = unicodedata.normalize("NFKC", str(text)).strip()
        if len(text) > FAMILY_LIMITS["note_chars"]:
            raise StateGraphError(f"note_too_long: at most {FAMILY_LIMITS['note_chars']} characters")
        with self.store.transaction(write=True) as conn:
            row = self._live_group(conn, channel)
            if row is None:
                raise StateNotFound("group_not_bound")
            if text:
                conn.execute("INSERT INTO state_family_note(channel,group_ref,text,updated_at) VALUES(?,?,?,?) "
                             "ON CONFLICT(channel,group_ref) DO UPDATE SET text=excluded.text,"
                             "updated_at=excluded.updated_at", (channel, row["group_ref"], text, _now().isoformat()))
            else:
                conn.execute("DELETE FROM state_family_note WHERE channel=? AND group_ref=?",
                             (channel, row["group_ref"]))
        return {"note": text}

    def delete_family_memory(self, channel, entry_id=None):
        """One entry (entry_id) or all of the group's (None)."""
        self.steward._owner()
        with self.store.transaction(write=True) as conn:
            row = self._live_group(conn, channel)
            if row is None:
                raise StateNotFound("group_not_bound")
            args = [_now().isoformat(), channel, row["group_ref"]]
            sql = "UPDATE state_family_memory SET deleted_at=? WHERE channel=? AND group_ref=? AND deleted_at IS NULL"
            if entry_id is not None:
                sql += " AND entry_id=?"
                args.append(entry_id)
            deleted = conn.execute(sql, args).rowcount
        return {"deleted": deleted}

    def owner_view(self, channel=None):
        """The dashboard's groups: each bound group with its members, message count and last
        time; with `channel`, also its records (withheld originals included), the family note
        and the family memory entries."""
        self.steward._owner()
        with self.store.transaction() as conn:
            groups = []
            for row in conn.execute("SELECT * FROM state_owner_groups WHERE revoked_at IS NULL ORDER BY channel"):
                key = (row["channel"], row["group_ref"])
                stats = conn.execute("SELECT COUNT(*), MAX(at) FROM state_group_messages WHERE channel=? AND group_ref=?",
                                     key).fetchone()
                members = [{"sender_ref": m["sender_ref"], "label": m["label"] or member_label(m["ordinal"]),
                            "named": m["label"] is not None} for m in conn.execute(
                    "SELECT * FROM state_group_members WHERE channel=? AND group_ref=? ORDER BY ordinal", key)]
                group = {"channel": row["channel"], "group_ref": row["group_ref"], "label": row["label"],
                         "mention_prefix": row["mention_prefix"], "bound_at": row["bound_at"],
                         "messages_30d": stats[0], "last_message_at": stats[1], "members": members}
                if channel == row["channel"]:
                    group["messages"] = [dict(m) for m in conn.execute(
                        "SELECT message_id,direction,kind,accepted,sender_ref,sender_role,body,original,withheld_reason,"
                        "reply_to,answered_by,at FROM state_group_messages WHERE channel=? AND group_ref=? "
                        "ORDER BY at DESC, rowid DESC LIMIT 500", key)]
                    for m in group["messages"]:
                        m["sender_label"] = (self._label_of(conn, *key, m["sender_ref"], m["sender_role"])
                                             if m["direction"] == "in" else
                                             STEWARD_NOTICE_LABEL if m["kind"] == "notice" else None)
                    note = conn.execute("SELECT text,updated_at FROM state_family_note WHERE channel=? AND group_ref=?",
                                        key).fetchone()
                    group["family_note"] = dict(note) if note else None
                    group["family_memory"] = self._entries(conn, *key)
                groups.append(group)
        return {"groups": groups}

    def _entries(self, conn, channel, group_ref):
        return [{"entry_id": e["entry_id"], "text": e["text"], "at": e["at"], "added_by_role": e["added_by_role"],
                 "added_by_label": self._label_of(conn, channel, group_ref, e["added_by_ref"], e["added_by_role"])}
                for e in conn.execute("SELECT * FROM state_family_memory WHERE channel=? AND group_ref=? AND "
                                      "deleted_at IS NULL ORDER BY at DESC, rowid DESC", (channel, group_ref))]

    # ---------------------------------------------------------------- the steward's host
    def _bind(self, conn, channel, group_ref, label, prefix, code_hash, moment):
        conn.execute("UPDATE state_owner_channel_codes SET used_at=? WHERE code_hash=?", (moment.isoformat(), code_hash))
        conn.execute("INSERT INTO state_owner_groups(channel,group_ref,label,mention_prefix,bound_at,revoked_at) "
                     "VALUES(?,?,?,?,?,NULL) ON CONFLICT(channel,group_ref) DO UPDATE SET label=excluded.label,"
                     "mention_prefix=excluded.mention_prefix,bound_at=excluded.bound_at,revoked_at=NULL",
                     (channel, group_ref, label, prefix, moment.isoformat()))

    def bind_owner_group(self, project_id, request_key, channel, group_ref, sender_ref, code, mention_prefix,
                         channel_message_ref=None):
        """Bind the group a group code was posted in (the bridge passes only `@prefix code`
        messages from groups not yet bound). Only the channel's bound account binds, only
        with an unused, unexpired group code of that channel, and only while the channel
        has no group. A refused attempt counts against nothing and is answered with nothing
        in the group (the host sends a line only for `bound`)."""
        prefix = unicodedata.normalize("NFKC", str(mention_prefix)).strip()
        if not prefix:
            raise StateGraphError("invalid_request: mention_prefix must be the @ text before the code")
        normalised = normalise_group_code(code)
        request_hash = _hash([channel, group_ref, sender_ref, normalised, prefix])
        moment = _now()
        refusal = None
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "bind_owner_group", request_key, request_hash)
            if replay is not None:
                return {**replay, "duplicate": True}
            match = _GROUP_CODE.fullmatch(normalised)
            digest = group_code_hash(self._pepper(), channel, normalised) if match else None
            row = conn.execute("SELECT * FROM state_owner_channel_codes WHERE code_hash=? AND kind='group'",
                               (digest,)).fetchone() if digest else None
            if row is None or row["channel"] != channel:
                refusal = "invalid_code"
            elif row["used_at"] is not None:
                refusal = "code_used"
            elif row["retired_at"] is not None or datetime.fromisoformat(row["expires_at"]) <= moment:
                refusal = "code_expired"
            elif self._live_group(conn, channel) is not None:
                refusal = "group_already_bound"
            else:
                try:
                    require_bound(conn, channel, sender_ref)
                except StateGraphError:
                    refusal = "channel_sender_not_bound"
                    if PICK_PENDING_GROUPS:
                        conn.execute("INSERT INTO state_pending_groups(channel,room_ref,mention_prefix,code_hash,at) "
                                     "VALUES(?,?,?,?,?) ON CONFLICT(channel,room_ref) DO UPDATE SET "
                                     "mention_prefix=excluded.mention_prefix,code_hash=excluded.code_hash,at=excluded.at",
                                     (channel, group_ref, prefix, digest, moment.isoformat()))
                        refusal = "pending_owner_pick"
            if refusal is None:
                self._bind(conn, channel, group_ref, row["label"], prefix, digest, moment)
                notice = self._out_row(conn, channel, group_ref, "bind_notice", BIND_NOTICE, moment)
                result = {"bound": True, "channel": channel, "group_ref": group_ref, "label": row["label"],
                          "mention_prefix": prefix, "notice": BIND_NOTICE, "notice_message_id": notice}
                out = self.steward._record(conn, "bind_owner_group", request_key, request_hash, result)
        if refusal is not None:
            self._log("bind refused " + refusal, channel, group_ref, sender_ref)
            raise StateGraphError(refusal)
        self._log("bound", channel, group_ref, sender_ref)
        return out

    def relay_group_message(self, project_id, request_key, channel, group_ref, sender_ref, channel_message_ref, body,
                            channel_at=None):
        """One @-message from the bound group: dated by the channel within the last
        fresh_minutes, deduplicated by its channel ref, stamped owner/member by code, held to
        the group's, the member's and the account's hourly bounds, stored. Answers
        {message_id, sender_role, sender_label, duplicate} - or a refusal: group_not_bound,
        undated, stale, code_in_group (a live ACCOUNT code it carries is retired, nothing of
        the body is kept, nothing is answered), group_quota / member_quota (with notice and
        notice_message_id when the hour's one notice is due), group_quota_account."""
        if channel not in CHANNELS:
            raise StateGraphError("invalid_channel")
        _require_utf8("body", body, "invalid_body")
        if not isinstance(body, str) or not body.strip():
            raise StateGraphError("empty_body: body must be nonempty text")
        size = len(body.encode("utf-8"))
        if size > BODY_MAX_BYTES:
            raise StateGraphError(f"body_too_long: {size} bytes; at most {BODY_MAX_BYTES} bytes")
        refuse_credential_body(body)
        written = _written_at(channel_at)
        moment = _now()
        answer = None
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            if self._live_group(conn, channel, group_ref) is None:
                answer = {"refused": "group_not_bound"}
            elif written is None:
                answer = {"refused": "undated"}
            elif moment - written > timedelta(minutes=GROUP_LIMITS["fresh_minutes"]):
                answer = {"refused": "stale"}
            if answer is not None:
                self._log("message dropped " + answer["refused"], channel, group_ref, sender_ref, size)
                return answer
            seen = conn.execute("SELECT message_id,accepted,sender_role FROM state_group_messages WHERE direction='in' "
                                "AND channel=? AND group_ref=? AND channel_message_ref=?",
                                (channel, group_ref, channel_message_ref)).fetchone()
            if seen is not None:
                return {"duplicate": True, "message_id": seen["message_id"], "accepted": bool(seen["accepted"])}
            message_id = "gmsg-" + secrets.token_hex(10)

            def keep(kind, accepted=0, role=None, text=None):
                conn.execute("INSERT INTO state_group_messages(message_id,channel,group_ref,direction,kind,accepted,"
                             "sender_ref,sender_role,channel_message_ref,body,at) VALUES(?,?,?,'in',?,?,?,?,?,?,?)",
                             (message_id, channel, group_ref, kind, accepted, sender_ref, role, channel_message_ref,
                              text, moment.isoformat()))
            # A live ACCOUNT code in a group message: whoever has it for 10 minutes becomes the
            # owner, so it is retired and the body is never kept (7.7.9). Group codes travel the
            # bind path only.
            live = self.channels.code_in_text(conn, body, moment) if digit_windows(body) else None
            if live is not None:
                conn.execute("UPDATE state_owner_channel_codes SET retired_at=? WHERE code_hash=? AND used_at IS NULL "
                             "AND retired_at IS NULL", (moment.isoformat(), live["code_hash"]))
                keep("code_retired")
                self._log("account code retired: posted in the group", channel, group_ref, sender_ref)
                return {"refused": "code_in_group"}
            account = conn.execute("SELECT sender_ref FROM state_owner_channels WHERE channel=? AND revoked_at IS NULL",
                                   (channel,)).fetchone()
            role = "owner" if account is not None and account[0] == sender_ref else "member"
            label = self._member(conn, channel, group_ref, sender_ref, role)
            hour = (moment - timedelta(hours=1)).isoformat()
            sent = self._count(conn, "SELECT COUNT(*) FROM state_group_messages WHERE channel=? AND direction='out' "
                               "AND at>?", (channel, hour))
            if sent >= GROUP_LIMITS["account_out_per_hour"]:
                keep("over_quota", role=role)
                self._log("group_quota_account", channel, group_ref, sender_ref, size)
                return {"refused": "group_quota_account", "sender_role": role}
            asked = self._count(conn, "SELECT COUNT(*) FROM state_group_messages WHERE channel=? AND group_ref=? AND "
                                "direction='in' AND accepted=1 AND at>?", (channel, group_ref, hour))
            mine = self._count(conn, "SELECT COUNT(*) FROM state_group_messages WHERE channel=? AND group_ref=? AND "
                               "direction='in' AND accepted=1 AND sender_ref=? AND at>?",
                               (channel, group_ref, sender_ref, hour))
            over = ("group_quota" if asked >= GROUP_LIMITS["group_per_hour"]
                    else "member_quota" if role == "member" and mine >= GROUP_LIMITS["member_per_hour"] else None)
            if over:
                keep("over_quota", role=role)
                self._log(over, channel, group_ref, sender_ref, size)
                notices = self._count(conn, "SELECT COUNT(*) FROM state_group_messages WHERE channel=? AND group_ref=? "
                                      "AND direction='out' AND kind='quota_notice' AND at>?", (channel, group_ref, hour))
                result = {"refused": over, "sender_role": role}
                if notices < GROUP_LIMITS["notice_per_hour"]:
                    result.update(notice=QUOTA_NOTICE, notice_message_id=self._out_row(
                        conn, channel, group_ref, "quota_notice", QUOTA_NOTICE, moment))
                return result
            keep("question", accepted=1, role=role, text=body)
            self._retain(conn, moment)
        self._log("message accepted", channel, group_ref, sender_ref, size)
        return {"message_id": message_id, "sender_role": role, "sender_label": label, "duplicate": False}

    def exit_check(self, conn, text, moment):
        """The reason a reply may not go to the group, or None (7.7.7): a live code of
        either kind (the code is retired: it is no longer secret), any non-public
        project's id or title, a host path."""
        live = self.channels.code_in_text(conn, text, moment) if digit_windows(text) else None
        if live is not None:
            conn.execute("UPDATE state_owner_channel_codes SET retired_at=? WHERE code_hash=?",
                         (moment.isoformat(), live["code_hash"]))
            return "live_code"
        squeezed = normalise_group_code(text)
        candidates = {m.group(0) for m in (_GROUP_CODE.match(squeezed, i) for i in range(len(squeezed))) if m}
        if candidates:
            groups = self._live_group_codes(conn, moment)
            if groups:
                pepper = self._pepper()
                for code in candidates:
                    for channel in CHANNELS:
                        digest = group_code_hash(pepper, channel, code)
                        if groups.get(digest) == channel:
                            conn.execute("UPDATE state_owner_channel_codes SET retired_at=? WHERE code_hash=?",
                                         (moment.isoformat(), digest))
                            return "live_code"
        folded = unicodedata.normalize("NFKC", text).casefold()
        for term in private_terms(conn):
            if (folded.find(term) >= 0 and len(term) >= 2) if _cjk(term) else _ascii_word(term, folded):
                return "private_project"
        bare = _WEB_URL.sub(" ", folded)
        if any(path in bare for path in _PATHS):
            return "host_path"
        return None

    def post_group_reply(self, project_id, request_key, question_id, body, also_answers=None):
        """The steward's last words of a group turn, posted by its HOST to the group the
        question came from (never a group the caller names). The exit check runs here, in
        this transaction; a withheld reply keeps its original for the owner and the group
        gets WITHHELD_TEXT. Formatted as one WeChat message: 「first 20 characters of the
        question」 on the first line, at most reply_chars in all, cut with '…'. Answered
        once per request_key: the replay returns the first answer."""
        _require_utf8("body", body, "invalid_body")
        if not isinstance(body, str) or not body.strip():
            raise StateGraphError("empty_body")
        others = [i for i in (also_answers or []) if isinstance(i, str) and i != question_id][:10]
        request_hash = _hash([question_id, body, others])
        moment = _now()
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "post_group_reply", request_key, request_hash)
            if replay is not None:
                return {**replay, "duplicate": True}
            question = conn.execute("SELECT * FROM state_group_messages WHERE message_id=? AND direction='in' "
                                    "AND accepted=1", (question_id,)).fetchone()
            if question is None:
                raise StateNotFound("unknown_question")
            channel, group = question["channel"], question["group_ref"]
            if self._live_group(conn, channel, group) is None:
                raise StateConflict("group_not_bound")
            reason = self.exit_check(conn, body, moment)
            quote = "「" + "".join(question["body"].split())[:20] + "」"
            text = (WITHHELD_TEXT if reason else body.strip())
            room = GROUP_LIMITS["reply_chars"] - len(quote) - 1
            text = quote + "\n" + (text if len(text) <= room else text[:room - 1] + "…")
            message_id = self._out_row(conn, channel, group, "reply", text, moment, reply_to=question_id,
                                       original=body if reason else None, reason=reason, request_key=request_key)
            for other in [question_id, *others]:
                conn.execute("UPDATE state_group_messages SET answered_by=? WHERE message_id=? AND channel=? AND "
                             "group_ref=? AND direction='in' AND answered_by IS NULL", (message_id, other, channel, group))
            result = {"message_id": message_id, "channel": channel, "group_ref": group, "text": text,
                      "withheld": bool(reason)}
            self._retain(conn, moment)
            out = self.steward._record(conn, "post_group_reply", request_key, request_hash, result)
        if reason:
            self._log("reply withheld " + reason, channel, group, None, len(body.encode("utf-8")))
        return out

    def post_group_notice(self, project_id, request_key, schedule_id, due_at):
        """A group reminder that fired, posted by the steward's HOST (step 25 C11, 7.9.4). The
        text and the group come from the schedule row, never from the call. In ONE transaction:
        the row fired for exactly this due time (`last_due_at`), is a family_group reminder
        and is not deleted (else not_fired; a once row is disabled by its fire, so `enabled` is
        not read); the binding it recorded (channel, group_ref, bound_at) still stands (else
        group_unbound, and the row is disabled); the exit check passes (else withheld, with the
        kind); the group has had fewer than group_notices_per_hour reminders this hour (else
        group_notice_budget) and the account fewer than account_out_per_hour outbound messages
        (else group_quota_account). Then one outbound row (kind 'notice'); no State event. A
        refusal posts nothing and is answered as {refused, reason?, title}: the host tells the
        owner, never retries. Answered once per request_key (the replay says duplicate)."""
        try:
            due = datetime.fromisoformat(due_at)
        except (TypeError, ValueError):
            raise StateGraphError("invalid_request: due_at must be an ISO 8601 time") from None
        if due.tzinfo is None:
            raise StateGraphError("invalid_request: due_at must carry its UTC offset")
        request_hash = _hash([schedule_id, due_at])
        moment = _now()
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "post_group_notice", request_key, request_hash)
            if replay is not None:
                return {**replay, "duplicate": True}
            row = conn.execute("SELECT * FROM state_steward_schedules WHERE schedule_id=?", (schedule_id,)).fetchone()
            refused, detail = None, None
            if (row is None or row["deleted_at"] is not None or row["target"] != "family_group"
                    or not row["last_due_at"] or datetime.fromisoformat(row["last_due_at"]) != due):
                refused = "not_fired"
            else:
                channel, group = row["target_channel"], row["target_group_ref"]
                live = conn.execute("SELECT 1 FROM state_owner_groups WHERE channel=? AND group_ref=? AND bound_at=? "
                                    "AND revoked_at IS NULL", (channel, group, row["target_bound_at"])).fetchone()
                hour = (moment - timedelta(hours=1)).isoformat()
                if live is None:
                    refused = "group_unbound"
                    conn.execute("UPDATE state_steward_schedules SET version=version+1,enabled=0,next_run_at=NULL,"
                                 "updated_by='steward',updated_at=? WHERE schedule_id=?", (moment.isoformat(), schedule_id))
                    self.steward._schedule_changed(conn, schedule_id)
                elif (detail := self.exit_check(conn, row["instruction"], moment)) is not None:
                    refused = "withheld"
                elif self._count(conn, "SELECT COUNT(*) FROM state_group_messages WHERE channel=? AND group_ref=? AND "
                                 "direction='out' AND kind='notice' AND at>?",
                                 (channel, group, hour)) >= GROUP_LIMITS["group_notices_per_hour"]:
                    refused = "group_notice_budget"
                elif self._count(conn, "SELECT COUNT(*) FROM state_group_messages WHERE channel=? AND direction='out' "
                                 "AND at>?", (channel, hour)) >= GROUP_LIMITS["account_out_per_hour"]:
                    refused = "group_quota_account"
            title = row["title"] if row is not None else None
            if refused is not None:
                result = {"refused": refused, "title": title, **({"reason": detail} if detail else {})}
            else:
                body = row["instruction"].strip()
                message_id = self._out_row(conn, channel, group, "notice", body, moment, request_key=request_key)
                self._retain(conn, moment)
                result = {"posted": True, "message_id": message_id, "channel": channel, "group_ref": group,
                          "text": body, "title": title}
            out = self.steward._record(conn, "post_group_notice", request_key, request_hash, result)
        if refused == "group_unbound":
            self.steward._notify()
        if refused is not None:
            self._log("notice refused " + refused, row["target_channel"] if row is not None else None,
                      row["target_group_ref"] if row is not None else None, None)
        else:
            self._log("notice posted", channel, group, None, len(body.encode("utf-8")))
        return out

    def set_steward_turn_audience(self, project_id, turn, audience, group=None, sender_refs=None):
        """The steward's host records whose turn this is, before the turn's first step runs
        (7.7.3 item 5). One row, overwritten; no event."""
        if audience not in ("owner", "group"):
            raise StateGraphError("invalid_request: audience is owner or group")
        if audience == "group" and not (isinstance(group, dict) and group.get("channel") in CHANNELS
                                        and isinstance(group.get("group_ref"), str) and group["group_ref"]):
            raise StateGraphError("invalid_request: a group audience names {channel, group_ref}")
        refs = [r for r in (sender_refs or []) if isinstance(r, str) and r][:10]
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            conn.execute("INSERT INTO state_steward_turn_audience(id,turn,audience,channel,group_ref,sender_refs_json,at) "
                         "VALUES(1,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET turn=excluded.turn,"
                         "audience=excluded.audience,channel=excluded.channel,group_ref=excluded.group_ref,"
                         "sender_refs_json=excluded.sender_refs_json,at=excluded.at",
                         (str(turn), audience, group["channel"] if audience == "group" else None,
                          group["group_ref"] if audience == "group" else None, json.dumps(refs), _now().isoformat()))
        return {"recorded": True, "turn": str(turn), "audience": audience}

    def group_turn(self, conn, turn, code):
        """The audience record of the group turn a public-view request (a group turn's tool)
        names, or refused with `code`: the request names the turn the host recorded last
        (`turn`, the plugin's open turn) and that turn's recorded audience is a group bound now
        (7.7.8a; the family lists the same, 7.9.5). The record is one overwritten row; naming
        its turn means a stale row can serve no turn but the one it was written for (director
        477 N1): the next turn is recorded before its first step runs, so a request of that turn
        never meets the previous turn's row as its own."""
        self.steward._steward(conn)
        row = conn.execute("SELECT * FROM state_steward_turn_audience WHERE id=1").fetchone()
        if row is None or row["audience"] != "group" or self._live_group(conn, row["channel"], row["group_ref"]) is None:
            raise StateGraphError(f"{code}: this turn is not a bound group's")
        if not isinstance(turn, str) or row["turn"] != turn:
            raise StateGraphError(f"{code}: this request is not the recorded turn's")
        return row

    def turn_asker(self, conn, turn_row):
        """Who asked in this group turn, from the audience record (never from the call):
        (role, sender_ref, label) of the one sender of the turn's questions, or - a turn that
        merged several askers' questions - ('member', None, None)."""
        refs = set(json.loads(turn_row["sender_refs_json"]))
        sender = next(iter(refs)) if len(refs) == 1 else None
        account = conn.execute("SELECT sender_ref FROM state_owner_channels WHERE channel=? AND revoked_at IS NULL",
                               (turn_row["channel"],)).fetchone()
        role = "owner" if sender is not None and account is not None and account[0] == sender else "member"
        return role, sender, self._label_of(conn, turn_row["channel"], turn_row["group_ref"], sender, role)

    def _family_group(self, conn, turn):
        """The family memory's group: only from a group turn (the public-read header and the
        recorded turn, group_turn)."""
        if not self.public_view:
            raise StateGraphError("family_memory_group_turn_only: family memory is read and written in group "
                                  "turns only")
        return self.group_turn(conn, turn, "family_memory_group_turn_only")

    def note_family_memory(self, project_id, request_key, text, turn):
        text = unicodedata.normalize("NFKC", str(text)).strip()
        if not text:
            raise StateGraphError("empty_text")
        if len(text) > FAMILY_LIMITS["entry_chars"]:
            raise StateGraphError(f"entry_too_long: at most {FAMILY_LIMITS['entry_chars']} characters")
        moment = _now()
        with self.store.transaction(write=True) as conn:
            turn = self._family_group(conn, turn)
            channel, group = turn["channel"], turn["group_ref"]
            request_hash = _hash([channel, group, text])
            replay = self.steward._replay(conn, "note_family_memory", request_key, request_hash)
            if replay is not None:
                return replay
            self._retain(conn, moment)
            entries = self._count(conn, "SELECT COUNT(*) FROM state_family_memory WHERE channel=? AND group_ref=? AND "
                                  "deleted_at IS NULL", (channel, group))
            if entries >= FAMILY_LIMITS["entries"]:
                raise StateConflict("family_memory_full: ask your owner to clear some on the dashboard")
            # "Who added it" is the turn's asker; a turn that merged several askers' questions
            # cannot tell which of them asked to remember, so it names nobody.
            role, sender, _label = self.turn_asker(conn, turn)
            entry_id = "fam-" + secrets.token_hex(8)
            conn.execute("INSERT INTO state_family_memory(entry_id,channel,group_ref,text,added_by_role,added_by_ref,at) "
                         "VALUES(?,?,?,?,?,?,?)", (entry_id, channel, group, text, role, sender, moment.isoformat()))
            result = {"noted": True, "entry_id": entry_id,
                      "added_by": self._label_of(conn, channel, group, sender, role)}
            return self.steward._record(conn, "note_family_memory", request_key, request_hash, result)

    @writer_only_read("list_family_memory")
    def list_family_memory(self, turn, project_id=None):
        with self.store.transaction() as conn:
            turn = self._family_group(conn, turn)
            key = (turn["channel"], turn["group_ref"])
            note = conn.execute("SELECT text FROM state_family_note WHERE channel=? AND group_ref=?", key).fetchone()
            entries, size = [], 0
            for entry in self._entries(conn, *key):
                size += len(entry["text"])
                if size > FAMILY_LIMITS["show_chars"]:
                    break
                entries.append({"text": entry["text"], "added_by": entry["added_by_label"], "at": entry["at"]})
        return {"family_note": note[0] if note else None, "entries": entries}

    # ---------------------------------------------------------------- the host's reads
    @writer_only_read("list_owner_groups")
    def list_owner_groups(self):
        """The steward's host: each channel's bound account and live group with its @
        prefix (the bridge's whitelist)."""
        with self.store.transaction() as conn:
            self.steward._steward(conn)
            accounts = {r["channel"]: r["sender_ref"] for r in conn.execute(
                "SELECT channel,sender_ref FROM state_owner_channels WHERE revoked_at IS NULL AND sender_ref IS NOT NULL")}
            groups = [{"channel": r["channel"], "group_ref": r["group_ref"], "label": r["label"],
                       "mention_prefix": r["mention_prefix"]}
                      for r in conn.execute("SELECT * FROM state_owner_groups WHERE revoked_at IS NULL")]
        return {"owner_refs": accounts, "groups": groups}

    @writer_only_read("list_group_recent")
    def list_group_recent(self, channel, group_ref, limit=6):
        """The steward's host: the group's last answered questions and the replies that went
        out (a withheld reply as the sentence the group saw), oldest first - a group wake's
        `recent`, which is data."""
        with self.store.transaction() as conn:
            self.steward._steward(conn)
            rows = conn.execute(
                "SELECT q.at, q.sender_ref, q.sender_role, q.body AS q, r.body AS a FROM state_group_messages q "
                "JOIN state_group_messages r ON r.message_id=q.answered_by WHERE q.channel=? AND q.group_ref=? "
                "AND q.direction='in' ORDER BY q.at DESC, q.rowid DESC LIMIT ?",
                (channel, group_ref, max(1, min(int(limit), 20)))).fetchall()
            return {"recent": [{"at": r["at"], "sender": self._label_of(conn, channel, group_ref, r["sender_ref"],
                                                                         r["sender_role"]),
                                "q": r["q"], "a": r["a"]} for r in reversed(rows)]}

