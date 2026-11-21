"""Step 22 (design-steward 7.6): the directors' open questions pushed to the owner's
default chat channel, reminded, and answered there.

`sync_owner_questions` (steward only; its host calls it every minute, never its model)
reads the open questions of every project but the steward's own (the one rule,
core.driver_questions), and - when a default channel is set and it is not the quiet hours
(23:00-08:00 in core.state_steward.default_tz()) - writes at most one push (split into
messages of at most PUSH_CHARS) as the steward's own message in its conversation, with
subject PUSH_SUBJECT, in the SAME transaction that records it in the ledger
`state_owner_question_pushes`: a question gets its code `Q<code_no>` the first time it is
pushed, and the code is never reused. The host mirrors each message to the channel
(lib/channels.mjs); a message written but never mirrored is handed back as `unmirrored`.

`relay_owner_answer` (steward only; its host's `relay_answer` tool, which the model calls
with ONE message id) turns an owner message that came from a chat channel into the
owner's answer to the pushed questions it names: the codes are read by code from the
message (`parse_codes`), the words are copied byte for byte, and each named question gets
one row in the owner's half of its project's conversation, citing it in `answers` - the
same writer as a dashboard answer (core.driver_inbox.write_user_half). Each relay is
recorded in `state_owner_question_answers`. Two answers to one question reach its director
in the order the owner wrote them: one received before an existing answer is not relayed
(`superseded`).

The approval phrase of 7.2 is another mechanism: it decides a proposal and closes no
question. An authorization answer is `Q<code> <words>` (APPROVAL_PHRASE_NOTE).

Both tables are private State tables (core.state_privacy.PRIVATE_STATE_TABLES)."""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from core.driver_questions import all_questions, conversation_rows
from core.state_channels import require_bound
from core.state_graph import ARCHIVED_PROJECT_SQL, StateGraphError
from core.state_privacy import UntrustedDatabase, writer_only_read
from core.state_steward import _hash, default_tz, steward_project

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_owner_question_pushes (
 code_no INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id TEXT NOT NULL, message_id TEXT NOT NULL, segment_id TEXT NOT NULL,
 asker_driver_id TEXT,
 pushed_at TEXT, push_message_id TEXT,
 push_failed INTEGER NOT NULL DEFAULT 0 CHECK(push_failed IN (0,1)),
 reminders_sent INTEGER NOT NULL DEFAULT 0 CHECK(reminders_sent BETWEEN 0 AND 2), last_reminded_at TEXT,
 older INTEGER NOT NULL DEFAULT 0 CHECK(older IN (0,1)),
 closed_at TEXT,
 closed_how TEXT CHECK(closed_how IS NULL OR closed_how IN ('dashboard','wechat','slack','withdrawn','archived')),
 noticed_at TEXT,
 UNIQUE(message_id, segment_id));
CREATE TABLE IF NOT EXISTS state_owner_question_answers (
 source_message_id TEXT NOT NULL,
 code_no INTEGER NOT NULL REFERENCES state_owner_question_pushes(code_no),
 answer_message_id TEXT NOT NULL, at TEXT NOT NULL, received_at TEXT NOT NULL,
 PRIMARY KEY(source_message_id, code_no));
"""

PUSH_SUBJECT = "待答问题"
RECEIPT_SUBJECT = "转达回执"
# A channel copy of a steward message is cut past 600 characters (lib/channels.mjs
# CHANNEL_LIMITS.mirrorChars): a push is split between two questions to stay under it.
PUSH_CHARS = 600
CONTINUED = "【待你回答】（续）"
WAIT = timedelta(minutes=2)          # a new question waits this long (an asker often asks several)
OLDER = timedelta(hours=24)          # never pushed and open longer: one line in 「较早的问题」, no reminder
NEW_MAX, OLDER_MAX = 15, 20          # per sync
REMINDERS = (timedelta(hours=4), timedelta(hours=24))
REMIND_MERGE = timedelta(hours=2)    # due within this of a due reminder: reminded with it
NOTICE_ALONE = timedelta(minutes=15)  # a closed question's notice waits for a message going anyway, at most this
UNMIRRORED_AFTER, UNMIRRORED_WINDOW = timedelta(minutes=2), timedelta(hours=24)
QUIET_FROM, QUIET_UNTIL = 23, 8      # owner's wall clock, default_tz()
CONTEXT_CHARS, LINE_CHARS = 200, 80
CODES_ONLY_WINDOW = timedelta(minutes=10)
CHANNEL_NAMES = {"wechat": "WeChat", "slack": "Slack"}
# M2 (owner: the cheap option): an authorization answer is `Q<code> <words>`; the 7.2 approval
# phrase decides a proposal and closes no question. The push's second line and every receipt's
# last line say so. Neither names the phrase itself: the mirror moves such a line (VERBATIM).
APPROVAL_PHRASE_PUSH = "授权类问题（合并、重启、花钱）也这样答：「Q{code} 同意」。批准提议的那句口令是另一回事，不关问题，提醒照发。"
APPROVAL_PHRASE_NOTE = "授权类问题也只认「Q编号 你的话」；批准提议的那句口令不关问题。"

# ---------------------------------------------------------------- the numbering rule (7.6.5)
# NOT MEASURED - design 7.6.11 step 0 (before merge, owner present): how a WeChat quote of
# an earlier message appears in the relayed text (the separator line's exact characters, and
# whether the quoted part comes before or after the owner's own words). Until it is measured
# and pasted here, nothing is stripped as a quote; the guessed dashed separator of the
# design's earlier drafts must NOT be put here. Set both from the measurement:
WECHAT_QUOTE_SEPARATOR: str | None = None   # the separator line, exactly as measured
WECHAT_QUOTE_BEFORE_OWN_WORDS: bool | None = None  # True: quote, separator, owner's words

_CODE = r"[Qq](0|[1-9][0-9]*)(?![0-9])"
_CODES = re.compile(rf"{_CODE}(?:[\s、,，/]*{_CODE})*")
_ONE = re.compile(_CODE)


def strip_quote(text, channel):
    """The owner's own words of a WeChat message: the quoted block (as measured, see
    WECHAT_QUOTE_SEPARATOR) removed. Unchanged for Slack, or while the format is unmeasured."""
    if channel != "wechat" or not WECHAT_QUOTE_SEPARATOR:
        return text
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.strip() == WECHAT_QUOTE_SEPARATOR.strip():
            return "\n".join(lines[index + 1:] if WECHAT_QUOTE_BEFORE_OWN_WORDS else lines[:index])
    return text


def _has_words(rest):
    return any(not char.isspace() and not unicodedata.category(char).startswith("P") for char in rest)


def parse_codes(body, channel=None):
    """{codes: [code_no, ...] in order, codes_only: bool} for an owner message.

    Matched on an NFKC copy (the body itself is never changed), after the WeChat quote is
    removed. A line counts when, past its leading whitespace, it starts with a string of codes
    `Q(0|[1-9]\\d*)` (either case; no digit right after a code; codes separated by spaces, 、 ,
    ， or /) followed by words other than punctuation. A line whose codes are followed by ` · `
    is a template line (a pasted or quoted push) and never counts; a code not at the start of
    a line never counts. `codes_only` is a message whose every non-blank line is codes alone:
    its codes are then returned for the message before it (design 7.6.5 step 8)."""
    answered, alone, other = [], [], False
    for raw in strip_quote(unicodedata.normalize("NFKC", body), channel).split("\n"):
        line = raw.lstrip()
        if not line.strip():
            continue
        match = _CODES.match(line)
        rest = line[match.end():] if match else ""
        if match is None or re.match(r"\s*·", rest):
            other = True
            continue
        codes = [int(found.group(1)) for found in _ONE.finditer(match.group(0))]
        if _has_words(rest):
            answered += [code for code in codes if code not in answered]
        else:
            alone += [code for code in codes if code not in alone]
    if answered:
        return {"codes": answered, "codes_only": False}
    if alone and not other:
        return {"codes": alone, "codes_only": True}
    return {"codes": [], "codes_only": False}


# ---------------------------------------------------------------- helpers
def _now() -> datetime:
    """This step's clock (aware UTC); tests replace it."""
    return datetime.now(timezone.utc)


def _at(value) -> datetime:
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_quiet(moment) -> bool:
    """23:00-08:00 on the owner's wall clock (default_tz(), DST by the zone's own rules)."""
    hour = moment.astimezone(ZoneInfo(default_tz())).hour
    return hour >= QUIET_FROM or hour < QUIET_UNTIL


def _first_line(text, limit=LINE_CHARS):
    line = next((part.strip() for part in str(text or "").split("\n") if part.strip()), "")
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _bar(text):
    """Every line of a director's text behind ｜, so no line of it starts with a code."""
    return "\n".join("｜" + line for line in str(text).split("\n"))


def _utc_second(value):
    return _at(value).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local(value):
    return _at(value).astimezone(ZoneInfo(default_tz())).strftime("%m-%d %H:%M")


def pack(chunks, limit=PUSH_CHARS):
    """Messages of at most `limit` characters from ordered (separator, text) chunks: split only
    between two chunks (two questions, or two lines of a list). A continuation starts with
    CONTINUED; a chunk longer than `limit` alone (one long question) is a message of its own,
    its first line first - the channel's own cut keeps it."""
    messages, current = [], None
    for sep, chunk in chunks:
        if len(chunk) > limit:
            if current is not None:
                messages.append(current)
            messages.append(chunk)
            current = None
            continue
        if current is None:
            candidate = chunk if not messages else CONTINUED + sep + chunk
            if len(candidate) > limit:
                candidate = chunk
            current = candidate
        elif len(current) + len(sep) + len(chunk) <= limit:
            current += sep + chunk
        else:
            messages.append(current)
            current = CONTINUED + sep + chunk
            if len(current) > limit:
                current = chunk
    if current is not None:
        messages.append(current)
    return messages


class StateOwnerQuestions:
    def __init__(self, store, actor, driver_id=None, steward=None, inbox=None, channels=None):
        self.store, self.actor, self.driver_id = store, actor, driver_id
        self.steward, self.inbox, self.channels = steward, inbox, channels
        self.project_read_trusted = store.project_read_trusted
        if not isinstance(store.db, UntrustedDatabase):
            with store.db.get_connection() as conn:
                conn.executescript(SCHEMA)
                conn.commit()

    # ---------------------------------------------------------------- reads
    @staticmethod
    def _default_channel(conn):
        row = conn.execute("SELECT channel FROM state_owner_channels WHERE is_default=1").fetchone()
        return row[0] if row is not None else None

    @staticmethod
    def _titles(conn):
        return {r[0]: r[1] for r in conn.execute("SELECT project_id,title FROM state_projects")}

    def _scan(self, conn):
        """Every listable question of every project but the steward's, with its status,
        and which projects are archived."""
        home = steward_project()
        live = [r[0] for r in conn.execute(
            f"SELECT p.project_id FROM state_projects p WHERE p.project_id<>? AND {ARCHIVED_PROJECT_SQL} "
            "ORDER BY p.project_id", (home,))]
        archived = {r[0] for r in conn.execute("SELECT project_id FROM state_project_archive WHERE archived=1")}
        found = {}
        for project_id in live:
            for question in all_questions(conversation_rows(conn, project_id)):
                found[(question["message_id"], question["segment_id"])] = {**question, "project_id": project_id}
        return found, archived

    @staticmethod
    def _message(conn, message_id):
        row = conn.execute("SELECT message_id,project_id,sender_driver_id,body,created_at FROM driver_inbox_messages "
                           "WHERE message_id=?", (message_id,)).fetchone()
        return dict(row) if row is not None else None

    def _reader(self, conn):
        if not self.steward.is_owner_session():
            try:
                self.steward._steward(conn)
            except StateGraphError:
                raise StateGraphError("owner_or_steward_required") from None

    @writer_only_read("list_pushed_owner_questions")
    def list_pushed(self):
        """The pushed questions still waiting for the owner (design 7.6.5 step 2, the steward's
        snapshot layer 2): code, project, first line, when pushed, reminders sent."""
        with self.store.transaction() as conn:
            self._reader(conn)
            found, _ = self._scan(conn)
            titles = self._titles(conn)
            rows = conn.execute("SELECT * FROM state_owner_question_pushes WHERE closed_at IS NULL "
                                "AND pushed_at IS NOT NULL ORDER BY code_no").fetchall()
        out = []
        for row in rows:
            question = found.get((row["message_id"], row["segment_id"]))
            if question is None or question["status"] != "open":
                continue
            out.append({"code": f"Q{row['code_no']}", "project_id": row["project_id"],
                        "project_title": titles.get(row["project_id"]), "question": _first_line(question["text"]),
                        "pushed_at": row["pushed_at"], "reminders_sent": row["reminders_sent"]})
        return {"questions": out}

    # ---------------------------------------------------------------- the push
    def sync(self, project_id=None):
        """One look (design 7.6.2-7.6.4): {default_channel, messages: [{message_id, body}],
        unmirrored: [{message_id, body}]}. Writes nothing without a default channel or in the
        quiet hours; otherwise the messages and their ledger rows commit together."""
        moment = _now()
        with self.store.transaction() as conn:
            self.steward._steward(conn, project_id)
            if self._default_channel(conn) is None:
                return {"default_channel": None, "messages": [], "unmirrored": []}
            found, archived = self._scan(conn)
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            default = self._default_channel(conn)
            if default is None:
                return {"default_channel": None, "messages": [], "unmirrored": []}
            if is_quiet(moment):
                return {"default_channel": default, "messages": [], "unmirrored": [], "quiet": True}
            messages = self._push(conn, moment, default, found, archived)
            unmirrored = [{"message_id": r["message_id"], "body": r["body"]} for r in conn.execute(
                "SELECT m.message_id,m.body FROM driver_inbox_messages m WHERE m.project_id=? AND m.audience='user' "
                "AND m.sender_driver_id=? AND m.subject=? AND m.reply_to_message_id IS NULL "
                "AND m.created_at<=? AND m.created_at>=? "
                "AND NOT EXISTS(SELECT 1 FROM state_message_sources s WHERE s.message_id=m.message_id) "
                "ORDER BY m.created_at,m.message_id LIMIT 20",
                (steward_project(), self.driver_id, PUSH_SUBJECT, (moment - UNMIRRORED_AFTER).isoformat(),
                 (moment - UNMIRRORED_WINDOW).isoformat()))]
        if messages:
            self.steward._notify()
        return {"default_channel": default, "messages": messages, "unmirrored": unmirrored}

    def _push(self, conn, moment, default, found, archived):
        stamp = moment.isoformat()
        titles = self._titles(conn)
        ledger = {(r["message_id"], r["segment_id"]): dict(r)
                  for r in conn.execute("SELECT * FROM state_owner_question_pushes")}
        # Closed since: answered, withdrawn or archived (a relay closes its own row).
        for row in ledger.values():
            if row["closed_at"] is not None or row["pushed_at"] is None:  # the 「另有 K 个」 rest: never pushed
                continue
            question = found.get((row["message_id"], row["segment_id"]))
            how = None
            if row["project_id"] in archived:
                how = "archived"
            elif question is not None and question["status"] == "answered":
                how = "dashboard"  # a relayed answer closed its row itself (relay_answer)
            elif question is not None and question["status"] == "withdrawn":
                how = "withdrawn"
            if how is None:
                continue
            row.update(closed_at=stamp, closed_how=how, noticed_at=stamp if how == "archived" else None)
            conn.execute("UPDATE state_owner_question_pushes SET closed_at=?,closed_how=?,noticed_at=? "
                         "WHERE code_no=? AND closed_at IS NULL", (stamp, how, row["noticed_at"], row["code_no"]))
        open_rows = [r for r in ledger.values() if r["closed_at"] is None and r["pushed_at"] is not None]
        # A push the channel did not take: owed in full at the next inbound message from the
        # bound account, or at its reminder (which it then is).
        bound = conn.execute("SELECT sender_ref FROM state_owner_channels WHERE channel=? AND revoked_at IS NULL",
                             (default,)).fetchone()
        owed_now, owed_codes = [], set()
        for row in open_rows:
            failed = conn.execute("SELECT at FROM state_message_sources WHERE message_id=? AND direction='out' "
                                  "AND error IS NOT NULL", (row["push_message_id"],)).fetchone()
            if failed is None:
                continue
            row["push_failed"] = 1
            conn.execute("UPDATE state_owner_question_pushes SET push_failed=1 WHERE code_no=?", (row["code_no"],))
            if bound is not None and bound[0] and conn.execute(
                    "SELECT 1 FROM state_message_sources WHERE direction='in' AND channel=? AND sender_ref=? AND at>?",
                    (default, bound[0], failed[0])).fetchone():
                owed_now.append(row)
                owed_codes.add(row["code_no"])
        # Reminders: at 4 h and 24 h after the push; a due one takes along those due within 2 h.
        reminded = []
        due = []
        for row in open_rows:
            if row["older"] or row["code_no"] in owed_codes:
                continue
            pushed = _at(row["pushed_at"])
            target = sum(moment >= pushed + step for step in REMINDERS)
            if target > row["reminders_sent"]:
                due.append((row, target))
        if due:
            taken = owed_codes | {d[0]["code_no"] for d in due}
            for row in open_rows:
                if row["older"] or row["code_no"] in taken or row["reminders_sent"] >= 2:
                    continue
                if moment + REMIND_MERGE >= _at(row["pushed_at"]) + REMINDERS[row["reminders_sent"]]:
                    due.append((row, row["reminders_sent"] + 1))
            reminded = sorted(due, key=lambda d: d[0]["code_no"])
        # New questions: open for WAIT, never pushed; open longer than OLDER: one line, once.
        fresh, older = [], []
        for key, question in sorted(found.items(), key=lambda kv: (kv[1]["created_at"] or "", kv[0])):
            if question["status"] != "open" or key in ledger:
                continue
            created = _at(question["created_at"])
            if created > moment - WAIT:
                continue
            (older if created <= moment - OLDER else fresh).append(question)
        fresh, older_rest = fresh[:NEW_MAX], older[OLDER_MAX:]
        older = older[:OLDER_MAX]
        # Closed notices: in a message going anyway, or alone NOTICE_ALONE after the first.
        notices = []
        for row in ledger.values():
            if row["closed_at"] is None or row["noticed_at"] is not None or row["closed_how"] == "archived":
                continue
            if row["closed_how"] == default:  # its receipt went to this very channel
                conn.execute("UPDATE state_owner_question_pushes SET noticed_at=? WHERE code_no=?",
                             (stamp, row["code_no"]))
                continue
            notices.append(row)
        going = bool(fresh or older or owed_now or reminded)
        if not going and not any(_at(r["closed_at"]) <= moment - NOTICE_ALONE for r in notices):
            notices = []
        if not going and not notices:
            return []
        # Codes first (the text names them), then the text, then the rows.
        for question in fresh + older:
            asker = self._message(conn, question["message_id"])
            cursor = conn.execute(
                "INSERT INTO state_owner_question_pushes(project_id,message_id,segment_id,asker_driver_id,pushed_at,"
                "older) VALUES(?,?,?,?,?,?)", (question["project_id"], question["message_id"], question["segment_id"],
                                              asker["sender_driver_id"] if asker else None, stamp,
                                              int(question in older)))
            question["code_no"] = cursor.lastrowid
        # The older backlog is pushed once: the rest past OLDER_MAX are only counted 「另有 K 个，
        # 见面板」, and recorded as seen (no pushed_at: no code shown, never pushed or listed).
        for question in older_rest:
            asker = self._message(conn, question["message_id"])
            conn.execute("INSERT INTO state_owner_question_pushes(project_id,message_id,segment_id,asker_driver_id,"
                         "older) VALUES(?,?,?,?,1)", (question["project_id"], question["message_id"],
                                                      question["segment_id"], asker["sender_driver_id"] if asker else None))
        full = [(q["code_no"], q["project_id"], q["message_id"], q["text"]) for q in fresh]
        full += [(r["code_no"], r["project_id"], r["message_id"], self._text(found, r)) for r in owed_now]
        chunks = []
        if full or older:
            # The push's head: how to answer (an older-only push says it too), and M2.
            first = full[0][0] if full else older[0]["code_no"]
            count = f"{len(full)} 个新问题" if full else f"{len(older)} 个较早的问题"
            chunks.append(("", f"【待你回答】{count}。回复时以编号开头，例如「Q{first} 用方案 A」，"
                               "你的原话会转给提问的导演；改主意就再发一条，以后来的为准。也可以在面板回答。\n"
                           + APPROVAL_PHRASE_PUSH.format(code=first)))
            for code, project, message_id, text in full:
                chunks.append(("\n\n", self._block(conn, code, titles.get(project, project), message_id, text)))
        if reminded:
            chunks.append(("\n\n", f"【提醒】还有 {len(reminded)} 个问题等你回答："))
            for row, _ in reminded:
                text = self._text(found, row)
                title = titles.get(row["project_id"], row["project_id"])
                if row["push_failed"]:
                    chunks.append(("\n\n", self._block(conn, row["code_no"], title, row["message_id"], text)))
                else:
                    chunks.append(("\n", f"Q{row['code_no']} · {title}：｜{_first_line(text)}"))
            chunks.append(("\n", f"回复「Q{reminded[0][0]['code_no']} 你的回答」即可，也可以在面板回答。"))
        if older:
            chunks.append(("\n\n", "较早的问题："))
            for question in older:
                chunks.append(("\n", f"Q{question['code_no']} · {titles.get(question['project_id'], question['project_id'])}："
                                     f"｜{_first_line(question['text'])}"))
            if older_rest:
                chunks.append(("\n", f"另有 {len(older_rest)} 个，见面板。"))
        if notices:
            labels = {"dashboard": "已在面板回答", "withdrawn": "已撤回", "wechat": "已在微信回答", "slack": "已在 Slack 回答"}
            groups = {}
            for row in sorted(notices, key=lambda r: r["code_no"]):
                groups.setdefault(row["closed_how"], []).append(
                    f"Q{row['code_no']}（{titles.get(row['project_id'], row['project_id'])}）")
            chunks.append(("\n\n", "".join(f"{labels[how]}：{'、'.join(items)}。" for how, items in groups.items())))
        # The first chunk of a message never starts with a separator.
        chunks[0] = ("", chunks[0][1])
        bodies = pack(chunks)
        written = []
        for body in bodies:
            message_id = str(uuid4())
            conn.execute("INSERT INTO driver_inbox_messages(message_id,thread_id,sender_driver_id,project_id,kind,"
                         "delivery_mode,subject,body,refs_json,reply_to_message_id,created_at,audience) "
                         "VALUES(?,?,?,?,'note','transient',?,?,'{}',NULL,?,'user')",
                         (message_id, message_id, self.driver_id, steward_project(), PUSH_SUBJECT, body, stamp))
            written.append({"message_id": message_id, "body": body})
        where = {}
        for message in written:
            for found_code in re.finditer(r"^Q([0-9]+) · ", message["body"], re.M):
                where.setdefault(int(found_code.group(1)), message["message_id"])
        for code, _, _, _ in full:
            conn.execute("UPDATE state_owner_question_pushes SET push_message_id=?,push_failed=0 WHERE code_no=?",
                         (where.get(code, written[0]["message_id"]), code))
        # A resend owed now that falls on a due reminder is that reminder.
        for row in owed_now:
            target = sum(moment >= _at(row["pushed_at"]) + step for step in REMINDERS)
            if not row["older"] and target > row["reminders_sent"]:
                conn.execute("UPDATE state_owner_question_pushes SET reminders_sent=?,last_reminded_at=? "
                             "WHERE code_no=?", (target, stamp, row["code_no"]))
        for question in older:
            conn.execute("UPDATE state_owner_question_pushes SET push_message_id=? WHERE code_no=?",
                         (where.get(question["code_no"], written[0]["message_id"]), question["code_no"]))
        for row, target in reminded:
            conn.execute("UPDATE state_owner_question_pushes SET reminders_sent=?,last_reminded_at=? WHERE code_no=?",
                         (target, stamp, row["code_no"]))
            if row["push_failed"]:
                conn.execute("UPDATE state_owner_question_pushes SET push_message_id=?,push_failed=0 WHERE code_no=?",
                             (where.get(row["code_no"], written[0]["message_id"]), row["code_no"]))
        for row in notices:
            conn.execute("UPDATE state_owner_question_pushes SET noticed_at=? WHERE code_no=?", (stamp, row["code_no"]))
        return written

    @staticmethod
    def _text(found, row):
        return (found.get((row["message_id"], row["segment_id"])) or {}).get("text") or ""

    def _block(self, conn, code, title, message_id, text):
        """One question in full: the code line, the question, and the background (the
        asking message's body, at most CONTEXT_CHARS)."""
        message = self._message(conn, message_id)
        context = (message or {}).get("body") or ""
        context = context.strip()
        if len(context) > CONTEXT_CHARS:
            context = context[:CONTEXT_CHARS - 1] + "…"
        block = f"Q{code} · {title}\n问：\n{_bar(text)}"
        return block + (f"\n背景：\n{_bar(context)}" if context else "")

    # ---------------------------------------------------------------- the answer
    def relay_answer(self, project_id, request_key, source_message_id):
        """An owner channel message as the answer to the pushed questions it names (design
        7.6.5): {source_message_id, relayed_message_id, channel, results: [...], receipt} with
        one result per code, or {refused: 'no_code', receipt} when the message names none.
        Refused (raised): not_from_channel, channel_sender_not_bound, code_sent_to_bound_account."""
        request_hash = _hash([source_message_id])
        home = steward_project()
        with self.store.transaction() as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "relay_owner_answer", request_key, request_hash)
            if replay is not None:
                return replay
            source = self._source(conn, home, source_message_id)
            require_bound(conn, source["channel"], source["sender_ref"])
            previous = self._previous(conn, home, source) \
                if parse_codes(source["body"], source["channel"])["codes_only"] else None
        # Every owner-text writer refuses a live binding code (raised before any write), also in
        # the words a message of codes alone relays.
        self.channels.refuse_owner_text_code(source["body"])
        if previous is not None:
            self.channels.refuse_owner_text_code(previous["body"])
        moment = _now()
        with self.store.transaction(write=True) as conn:
            self.steward._steward(conn, project_id)
            replay = self.steward._replay(conn, "relay_owner_answer", request_key, request_hash)
            if replay is not None:
                return replay
            source = self._source(conn, home, source_message_id)
            require_bound(conn, source["channel"], source["sender_ref"])
            parsed = parse_codes(source["body"], source["channel"])
            text = source
            if parsed["codes_only"]:
                text = self._previous(conn, home, source)
            if not parsed["codes"] or text is None:
                return {"source_message_id": source_message_id, "refused": "no_code",
                        "receipt": self._candidates(conn)}
            titles = self._titles(conn)
            results, written = [], False
            for code in parsed["codes"]:
                result = self._relay_one(conn, code, source, text, titles, moment)
                written = written or result["outcome"] in ("relayed", "relayed_further")
                results.append(result)
            out = {"source_message_id": source_message_id, "relayed_message_id": text["message_id"],
                   "channel": source["channel"], "results": results,
                   "receipt": "\n".join([r["line"] for r in results] + [APPROVAL_PHRASE_NOTE])}
            self.steward._record(conn, "relay_owner_answer", request_key, request_hash, out)
        if written:
            self.steward._notify()
        return out

    @staticmethod
    def _source(conn, home, message_id):
        """The owner's channel message: the owner's half of the steward's conversation, with
        its inbound source row; else not_from_channel."""
        row = conn.execute(
            "SELECT m.message_id,m.body,m.created_at,s.channel,s.sender_ref,s.at FROM driver_inbox_messages m "
            "JOIN state_message_sources s ON s.message_id=m.message_id AND s.direction='in' "
            "WHERE m.message_id=? AND m.project_id=? AND m.channel='user' AND m.sender_driver_id IS NULL",
            (message_id, home)).fetchone()
        if row is None:
            raise StateGraphError("not_from_channel: source_message_id must name an owner message that came from a "
                                  "chat channel")
        return dict(row)

    def _previous(self, conn, home, source):
        """For a message of codes alone: the same sender's inbound message right before it on
        the same channel, when it names no code, was never relayed, and came at most
        CODES_ONLY_WINDOW earlier; else None."""
        row = conn.execute(
            "SELECT m.message_id,m.body,m.created_at,s.channel,s.sender_ref,s.at FROM state_message_sources s "
            "JOIN driver_inbox_messages m ON m.message_id=s.message_id WHERE s.direction='in' AND s.channel=? "
            "AND s.sender_ref=? AND s.message_id<>? AND s.at<=? AND m.project_id=? "
            "ORDER BY s.at DESC, s.rowid DESC LIMIT 1",
            (source["channel"], source["sender_ref"], source["message_id"], source["at"], home)).fetchone()
        if row is None:
            return None
        row = dict(row)
        if _at(source["at"]) - _at(row["at"]) > CODES_ONLY_WINDOW or parse_codes(row["body"], row["channel"])["codes"]:
            return None
        if conn.execute("SELECT 1 FROM state_owner_question_answers WHERE source_message_id=?",
                        (row["message_id"],)).fetchone():
            return None
        return row

    def _candidates(self, conn):
        """The no_code receipt: the pushed questions still waiting, under PUSH_CHARS."""
        found, _ = self._scan(conn)
        titles = self._titles(conn)
        rows = [r for r in conn.execute("SELECT * FROM state_owner_question_pushes WHERE closed_at IS NULL "
                                        "AND pushed_at IS NOT NULL ORDER BY code_no")
                if found.get((r["message_id"], r["segment_id"]), {}).get("status") == "open"]
        if not rows:
            return "没有认出编号，现在也没有等你回答的已推问题。\n" + APPROVAL_PHRASE_NOTE
        head = "没有认出编号。你是在回答下面哪个问题？"
        tail = f"回「Q{rows[0]['code_no']}」即把上一条原样转过去。\n{APPROVAL_PHRASE_NOTE}"
        lines = [f"Q{r['code_no']} · {titles.get(r['project_id'], r['project_id'])}：｜"
                 f"{_first_line(found[(r['message_id'], r['segment_id'])]['text'])}" for r in rows]
        shown = list(lines)
        while True:
            rest = len(lines) - len(shown)
            body = "\n".join([head, *shown, *([f"另有 {rest} 个，见面板。"] if rest else []), tail])
            if len(body) <= PUSH_CHARS or not shown:
                return body
            shown.pop()

    def _relay_one(self, conn, code, source, text, titles, moment):
        """One code of the message: judged, and written when it is an answer."""
        label = f"Q{code}"
        row = conn.execute("SELECT * FROM state_owner_question_pushes WHERE code_no=?", (code,)).fetchone()
        # The codes this message could see: pushed before the message naming them was received.
        if row is None or row["pushed_at"] is None or _at(source["at"]) < _at(row["pushed_at"]):
            return {"code": label, "outcome": "unknown_code", "line": f"{label} 找不到这个编号"}
        row = dict(row)
        title = titles.get(row["project_id"], row["project_id"])
        question = next((q for q in all_questions(conversation_rows(conn, row["project_id"]))
                         if q["message_id"] == row["message_id"] and q["segment_id"] == row["segment_id"]), None)
        base = {"code": label, "project_id": row["project_id"], "message_id": row["message_id"],
                "segment_id": row["segment_id"]}
        if question is None:
            return {**base, "outcome": "unknown_code", "line": f"{label} 找不到这个编号"}
        first = _first_line(question["text"])
        if question["status"] == "withdrawn":
            reason = _first_line((question.get("withdrawn") or {}).get("reason"), 60)
            newer = conn.execute("SELECT * FROM state_owner_question_pushes WHERE asker_driver_id IS ? AND code_no>? "
                                 "AND closed_at IS NULL AND pushed_at IS NOT NULL ORDER BY code_no DESC LIMIT 1",
                                 (row["asker_driver_id"], code)).fetchone()
            line = f"{label} 已撤回（{reason}），面板上仍可以答"
            if newer is not None:
                found = next((q for q in all_questions(conversation_rows(conn, newer["project_id"]))
                              if q["message_id"] == newer["message_id"] and q["segment_id"] == newer["segment_id"]), None)
                line = f"{label} 已撤回（{reason}）；导演改问了 Q{newer['code_no']}：{_first_line(found['text'] if found else '')}"
            return {**base, "outcome": "question_withdrawn", "line": line}
        if conn.execute("SELECT 1 FROM state_owner_question_answers WHERE source_message_id=? AND code_no=?",
                        (text["message_id"], code)).fetchone():
            return {**base, "outcome": "duplicate", "line": f"{label} 这条已经转过了"}
        # M1: answers reach the director in the order the owner wrote them; a relayed answer
        # dates by when its channel message was received, any other by its own row.
        received = _at(text["at"])
        relayed = {r[0]: r[1] for r in conn.execute(
            "SELECT answer_message_id,received_at FROM state_owner_question_answers WHERE code_no=?", (code,))}
        times = [_at(relayed.get(a["message_id"]) or a["created_at"]) for a in question["answers"]]
        later = [t for t in times if t > received]
        if later:
            return {**base, "outcome": "superseded",
                    "line": f"{label} 没有转：你之后又答过（{_local(max(later).isoformat())}），以后来的为准"}
        further = bool(question["answers"])
        asker = self._message(conn, row["message_id"])["sender_driver_id"]
        member = conn.execute("SELECT 1 FROM project_drivers p JOIN drivers d USING(driver_id) WHERE p.project_id=? "
                              "AND p.driver_id=? AND p.status='member'", (row["project_id"], asker)).fetchone()
        targets = [asker] if member else [r[0] for r in conn.execute(
            "SELECT p.driver_id FROM project_drivers p JOIN drivers d USING(driver_id) WHERE p.project_id=? "
            "AND p.status='member' ORDER BY p.driver_id", (row["project_id"],))]
        if not targets:
            return {**base, "outcome": "no_recipients", "line": f"{label} 没有转：这个项目现在没有导演"}
        subject = (f"{label} = your question {row['segment_id']}: {'a further answer, ' if further else ''}"
                   f"your owner's words, relayed verbatim from {CHANNEL_NAMES[source['channel']]} "
                   f"(received {_utc_second(text['at'])})")
        answers = [{"message_id": row["message_id"], "segment_id": row["segment_id"]}]
        self.inbox._resolve_answers(conn, row["project_id"], answers)
        message_id, _, deliveries = self.inbox.write_user_half(
            conn, row["project_id"], subject, text["body"], reply_to_message_id=row["message_id"],
            target_driver_ids=targets, answers=answers)
        conn.execute("INSERT INTO state_owner_question_answers(source_message_id,code_no,answer_message_id,at,"
                     "received_at) VALUES(?,?,?,?,?)", (text["message_id"], code, message_id, moment.isoformat(),
                                                        text["at"]))
        conn.execute("UPDATE state_owner_question_pushes SET closed_at=?,closed_how=? WHERE code_no=? "
                     "AND closed_at IS NULL", (moment.isoformat(), source["channel"], code))
        return {**base, "outcome": "relayed_further" if further else "relayed", "answer_message_id": message_id,
                "subject": subject, "recipients": [d["target_driver_id"] for d in deliveries],
                "line": f"已转达 {label}{'（补充回答）→' if further else ' →'} {title}：{first}"}
