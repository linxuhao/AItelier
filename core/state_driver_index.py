"""Index-mode director notebook: short assertions listed, bodies fetched by address.

A notebook that can only grow is a cliff, not a brake. The wuxia-myth permanent
section reached 99,730 of 100,000 characters because the only documented way for
a ruling to expire was "replaced in place by a newer one" and nothing executed
that sentence. Entries therefore have two retirement channels and neither one
deletes a body:

* supersede - a successor exists; the old address keeps a tombstone pointing at it;
* delist    - no successor; the line leaves the index because reading it can no
              longer change any decision. The body stays readable at its address
              and the number of evicted entries is reported, so a short index
              cannot hide how much was thrown away.

Assertions are capped and the cap fails loudly: an assertion is never truncated,
never silently dropped, and never accepted only to fail on read.
"""
from __future__ import annotations

import re
import uuid

from core.state_graph import StateGraphError

# Measured on 2026-09-17 over the 246 nonempty lines of the live `aitelier` and
# `wuxia-myth` notes (revisions 79 / 209): p50=64, p90=91, p95=120 characters,
# then an EMPTY band - no line at all is 151..250 characters long - before the
# narrative tail (251, 295, 383, 392, 565, 665, 702, 843, 1234). 200 is the
# midpoint of that measured gap: it admits every assertion-shaped line that
# exists (96.3% of lines, and 100% of lines below the gap) and refuses every
# narrative-shaped one. Narrative belongs in the body, which is what the address
# is for.
MAX_ASSERTION_CHARS = 200
MAX_ENTRY_BODY_CHARS = 20000
MAX_REASON_CHARS = 500
MAX_LANDED_CHARS = 120
MAX_INDEX_LIMIT = 500

FORCE_VALUES = ("in_force", "informational")
LISTING_VALUES = ("listed", "delisted")

ENTRY_ID = re.compile(r"[0-9a-f]{12}")
ADDRESS = re.compile(r"note://([A-Za-z0-9][A-Za-z0-9._-]{0,127})/([0-9a-f]{12})")

ENTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_driver_note_entries (
    project_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    assertion TEXT NOT NULL,
    body TEXT NOT NULL,
    force TEXT NOT NULL CHECK(force IN ('in_force','informational')),
    landed TEXT NOT NULL,
    listing TEXT NOT NULL CHECK(listing IN ('listed','delisted')),
    superseded_by TEXT,
    supersede_reason TEXT NOT NULL,
    delist_reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    director_identity TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, entry_id),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TRIGGER IF NOT EXISTS state_driver_note_entries_immutable_record
BEFORE UPDATE ON state_driver_note_entries
WHEN OLD.project_id<>NEW.project_id OR OLD.entry_id<>NEW.entry_id
  OR OLD.assertion<>NEW.assertion OR OLD.body<>NEW.body
  OR OLD.created_at<>NEW.created_at
BEGIN SELECT RAISE(ABORT,'driver note entry identity, assertion and body are immutable'); END;
CREATE TRIGGER IF NOT EXISTS state_driver_note_entries_no_delete
BEFORE DELETE ON state_driver_note_entries
BEGIN SELECT RAISE(ABORT,'driver note entries are never deleted; supersede or delist'); END;
"""


def new_entry_id() -> str:
    return uuid.uuid4().hex[:12]


def address_of(project_id: str, entry_id: str) -> str:
    return f"note://{project_id}/{entry_id}"


def assertion_text(value: str) -> str:
    """Cap the assertion loudly. Never truncate, never defer the failure to read."""
    if not isinstance(value, str) or not value.strip():
        raise StateGraphError("assertion must be nonempty text")
    stripped = value.strip()
    if len(stripped) > MAX_ASSERTION_CHARS:
        raise StateGraphError(
            f"assertion is {len(stripped)} characters but the index line cap is "
            f"{MAX_ASSERTION_CHARS}; it is refused, not truncated - put the narrative in "
            "the body and keep the one line a reader cannot violate")
    if "\n" in stripped or "\r" in stripped:
        raise StateGraphError("assertion must be a single line; put the narrative in the body")
    return stripped


def body_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateGraphError("body must be nonempty text")
    if len(value) > MAX_ENTRY_BODY_CHARS:
        raise StateGraphError(
            f"body is {len(value)} characters but the cap is {MAX_ENTRY_BODY_CHARS}; "
            "it is refused, not truncated")
    return value


def reason_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateGraphError(f"{label} must be nonempty text; the reason is kept with the entry")
    stripped = value.strip()
    if len(stripped) > MAX_REASON_CHARS:
        raise StateGraphError(
            f"{label} is {len(stripped)} characters but the cap is {MAX_REASON_CHARS}")
    return stripped


def landed_text(value: str) -> str:
    """A landed marker is a STATUS, never a retirement: landed does not mean expired."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise StateGraphError("landed must be text")
    stripped = value.strip()
    if len(stripped) > MAX_LANDED_CHARS:
        raise StateGraphError(
            f"landed marker is {len(stripped)} characters but the cap is {MAX_LANDED_CHARS}")
    if "\n" in stripped or "\r" in stripped:
        raise StateGraphError("landed marker must be a single line")
    return stripped


def force_value(value: str) -> str:
    if value not in FORCE_VALUES:
        raise StateGraphError("force must be in_force or informational")
    return value


def entry_id_value(value: str) -> str:
    if not isinstance(value, str) or not ENTRY_ID.fullmatch(value):
        raise StateGraphError("entry_id must be 12 lowercase hex characters")
    return value


def referenced_addresses(value: str) -> list[tuple[str, str]]:
    """Every note:// address a piece of notebook text points at."""
    if not isinstance(value, str) or not value:
        return []
    return [(match.group(1), match.group(2)) for match in ADDRESS.finditer(value)]


def index_line(row) -> str:
    """One line a reader cannot violate: the assertion PLUS its status.

    A topic ("travel money") leaves the reader unable to tell they are about to
    break something. An assertion plus status ("never reduce travel money below
    three places", landed in three places, still in force) is enough on its own.
    Landed and in-force are separate fields precisely because both must be
    expressible at once.
    """
    parts = [row["assertion"]]
    if row["superseded_by"]:
        parts.append(f"[superseded -> {address_of(row['project_id'], row['superseded_by'])}]")
    else:
        parts.append("[in force]" if row["force"] == "in_force" else "[informational]")
    if row["landed"]:
        parts.append(f"[landed: {row['landed']}]")
    if row["listing"] == "delisted":
        parts.append("[delisted]")
    return " ".join(parts)


def entry_summary(row) -> dict:
    return {
        "address": address_of(row["project_id"], row["entry_id"]),
        "entry_id": row["entry_id"],
        "assertion": row["assertion"],
        "force": row["force"],
        "landed": row["landed"],
        "listing": row["listing"],
        "lifecycle": "superseded" if row["superseded_by"] else "current",
        "superseded_by": (address_of(row["project_id"], row["superseded_by"])
                          if row["superseded_by"] else None),
        "supersede_reason": row["supersede_reason"] or None,
        "delist_reason": row["delist_reason"] or None,
        "index_line": index_line(row),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "body_chars": len(row["body"]),
    }


def entry_detail(row) -> dict:
    return {**entry_summary(row), "project_id": row["project_id"], "body": row["body"],
            "director_identity": row["director_identity"]}
