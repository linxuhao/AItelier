"""The ONE definition of an open question: a `question` segment no reply cites.

Both surfaces that show questions read this rule, so the number on the project
list is the number the conversation's questions box shows. A question is open
when no row's `answers` carries its `(message_id, segment_id)` pair.

Three decisions the rule makes, and neither surface may take them separately:

* a reply that only sets `reply_to_message_id` cites nothing, so it answers
  nothing;
* a non-question segment is never counted;
* a question segment without an id is skipped, because a citation names an id
  and a question no reply could name would sit open forever.

Order is the thread's own `(created_at, message_id)`, oldest first, so the
count is stable and a caller that wants the list gets it deterministic.
"""
from __future__ import annotations

import json

# The row scope of ONE project's conversation: the driver half (`audience`) and
# the user half (`channel`). Declared here because the questions read and every
# conversation read must agree on which rows are one thread; core/driver_inbox
# imports it rather than spelling it a second time.
CONVERSATION = "(audience='user' OR channel='user')"
_ROWS = ("SELECT message_id,created_at,body,segments_json,answers_json "
         "FROM driver_inbox_messages WHERE project_id=? AND " + CONVERSATION)


def _decoded(row):
    """One stored row in the shape the rule reads: decoded lists, never JSON."""
    return {"message_id": row["message_id"], "created_at": row["created_at"], "body": row["body"],
            "segments": json.loads(row["segments_json"]) if row["segments_json"] else [],
            "answers": json.loads(row["answers_json"]) if row["answers_json"] else []}


def conversation_rows(conn, project_id):
    """The rows of one project's conversation, decoded for `open_questions`."""
    return [_decoded(row) for row in conn.execute(_ROWS, (project_id,))]


def all_questions(rows):
    """Every listable question with the replies that cite it, in thread order.

    Each entry is `{message_id, created_at, segment_id, text, status, answers}`:
    `status` is `answered` when at least one row's `answers` cites the exact
    `(message_id, segment_id)` pair and `open` otherwise, and `answers` lists the
    citing rows `{message_id, created_at, body}` in thread order (two replies may
    cite the same question). `open_questions` is this list's open part, so the
    badge, the questions box and `list_user_questions` read one rule.
    """
    citing = {}
    for row in rows:
        for ref in row.get("answers") or []:
            if isinstance(ref, dict):
                citing.setdefault((ref.get("message_id"), ref.get("segment_id")), []).append(
                    {"message_id": row.get("message_id"), "created_at": row.get("created_at"),
                     "body": row.get("body")})
    found = []
    for row in rows:
        for segment in row.get("segments") or []:
            if not isinstance(segment, dict) or segment.get("kind") != "question":
                continue
            segment_id = segment.get("id")
            if not isinstance(segment_id, str) or not segment_id:
                continue
            answers = sorted(citing.get((row.get("message_id"), segment_id), []),
                             key=lambda a: (a["created_at"] or "", a["message_id"] or ""))
            found.append({"message_id": row.get("message_id"),
                          "created_at": row.get("created_at"),
                          "segment_id": segment_id, "text": segment.get("text"),
                          "status": "answered" if answers else "open", "answers": answers})
    found.sort(key=lambda question: (question["created_at"] or "", question["message_id"] or ""))
    return found


def open_questions(rows):
    """The questions no row's `answers` cites, in thread order (oldest first)."""
    return [{key: question[key] for key in ("message_id", "created_at", "segment_id", "text")}
            for question in all_questions(rows) if question["status"] == "open"]


def open_question_count(rows):
    """How many open questions `rows` carry (0 for none)."""
    return len(open_questions(rows))
