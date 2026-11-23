"""The dashboard's conversation uploads (step 24.2b, design-steward 7.8.12a "上传 暂存").

One row per file the owner (or a project user) uploaded into a conversation, from the moment
its bytes reach the staging directory (core.media_library.staging_dir) until the hosts have
stored it in their media library or it was refused:

- `kind` and `mime` are decided by the file's first bytes (the shared media-types table),
  never by the client;
- `state` is staged -> attached (a message carries it, `message_id`) -> stored (the first
  host's ack: `store` and `ref` in that writer's subdirectory) | refused (`refused`) |
  expired;
- `pending_json` names the delivery targets that have not acknowledged it yet;
- the per-project hourly count of the upload route is counted from `created_at` here, so a
  backend restart keeps it.

PRIVATE (core.state_privacy): a row names the project, the user and the file's name.
"""
from __future__ import annotations

import errno
import json
import math
import os
import secrets
from datetime import datetime, timedelta, timezone

from core import media_types
from core.state_graph import StateGraphError

TABLE = "state_conversation_uploads"
STATES = ("staged", "attached", "stored", "refused", "expired")
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
 upload_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, user TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('image','voice','file')), mime TEXT NOT NULL,
 size INTEGER NOT NULL CHECK(size >= 0), name TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ({", ".join(repr(s) for s in STATES)})),
 refused TEXT, message_id TEXT, store TEXT, ref TEXT, pending_json TEXT,
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS {TABLE}_by_project ON {TABLE}(project_id, created_at);
"""


def ensure_schema(conn) -> None:
    """Create the table on a trusted connection (the caller commits)."""
    conn.executescript(SCHEMA)


# --- The upload route's rules (step 24.2b P3, 7.8.12a "上传：面板 → 后端暂存") ---------------
#
# api/dsh_driver_routers.py `POST /{project_id}/conversation/uploads` owns the HTTP side (the
# write gate, the project-user check, the custom header, Sec-Fetch-Site); everything below is
# the part that decides: the kind from the first bytes, the per-kind byte cap while the body is
# still arriving, the staging total, the per-project hourly count and the row.

# The panel is one more source with the channels' figure: 20 inbound files an hour, counted per
# project from this table's rows (not in memory, so a backend restart keeps the count). Only
# files that reached staging have a row, so a refused upload is not counted.
RATE_PER_HOUR = media_types.limits()["inbound_per_channel_hour"]
RATE_WINDOW = timedelta(hours=1)
# The staging directory's total; past it an upload is refused staging_full (507).
STAGING_TOTAL_BYTES = 2 * 1024 ** 3
# The longest signature in the shared table ends before byte 16.
HEAD_BYTES = 16


class UploadRefused(Exception):
    """One refusal of the upload route: its HTTP status and its stable code."""

    def __init__(self, status: int, code: str, retry_after: int | None = None):
        super().__init__(code)
        self.status, self.code, self.retry_after = status, code, retry_after


def stamp(at: datetime | None = None) -> str:
    """created_at, always with microseconds so the stored strings order as the instants do."""
    return (at or datetime.now(timezone.utc)).isoformat(timespec="microseconds")


def new_upload_id() -> str:
    return "u_" + secrets.token_hex(8)


def valid_name(name) -> bool:
    """The display name the client gave (`?name=`): 1-255 characters, no control character.
    It names nothing on disk; the staged file is named by its upload_id."""
    return (isinstance(name, str) and 1 <= len(name) <= media_types.NAME_MAX
            and not any(ord(ch) < 32 or ord(ch) == 127 for ch in name))


def byte_limit(kind: str) -> int:
    """The single-file cap of a kind (image 20 MiB, file 25 MiB; voice's 25 MiB is only the
    staging byte bound - its 2-minute limit is measured by the host's ingest)."""
    return media_types.limits()[f"{kind}_bytes"]


def staging_bytes(directory) -> int:
    """What the staging directory holds now (regular files, partials included)."""
    total = 0
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            continue
    return total


def retry_after(conn, project_id: str, at: datetime | None = None) -> int | None:
    """None while the project has had fewer than RATE_PER_HOUR uploads in the last hour; else
    the seconds until one of them leaves the window."""
    at = at or datetime.now(timezone.utc)
    cutoff = stamp(at - RATE_WINDOW)
    count = conn.execute(f"SELECT COUNT(*) FROM {TABLE} WHERE project_id=? AND created_at>?",
                         (project_id, cutoff)).fetchone()[0]
    if count < RATE_PER_HOUR:
        return None
    leaving = conn.execute(f"SELECT created_at FROM {TABLE} WHERE project_id=? AND created_at>? "
                           "ORDER BY created_at LIMIT 1 OFFSET ?",
                           (project_id, cutoff, count - RATE_PER_HOUR)).fetchone()[0]
    left = (datetime.fromisoformat(leaving) + RATE_WINDOW - at).total_seconds()
    return max(1, math.ceil(left))


def admit(store, project_id: str, directory) -> int:
    """Before a byte is read: the project exists (StateNotFound), its hourly count is not used
    up (429 rate_limited) and staging is not already full (507 staging_full). Answers the
    staging total, which the body is then counted against."""
    with store.transaction() as conn:
        store._project(conn, project_id)
        wait = retry_after(conn, project_id)
    if wait is not None:
        raise UploadRefused(429, "rate_limited", wait)
    base = staging_bytes(directory)
    if base >= STAGING_TOTAL_BYTES:
        raise UploadRefused(507, "staging_full")
    return base


def _drop(path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


async def receive(chunks, directory, upload_id: str, base: int):
    """Stream the body into `<directory>/<upload_id>.part` (0600), judging as it arrives:
    the first HEAD_BYTES decide the type by the shared table (415 type, before any file
    exists); the kind's cap (413 too_large) and the staging total (507 staging_full) are
    checked before each chunk is written. Any refusal or failure - the client going away
    included - removes the partial. Answers (type entry, size, partial path)."""
    head, entry, handle, size = b"", None, None, 0
    partial = os.path.join(os.fspath(directory), upload_id + ".part")

    def judge(data):
        nonlocal size
        if size + len(data) > byte_limit(entry["kind"]):
            raise UploadRefused(413, "too_large")
        if base + size + len(data) > STAGING_TOTAL_BYTES:
            raise UploadRefused(507, "staging_full")
        handle.write(data)
        size += len(data)

    def start():
        nonlocal entry, handle
        entry = media_types.sniff(head)
        if entry is None:
            raise UploadRefused(415, "type")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(fd, "wb")
        judge(head)

    try:
        async for chunk in chunks:
            if not chunk:
                continue
            if entry is None:
                head += chunk
                if len(head) >= HEAD_BYTES:
                    start()
            else:
                judge(chunk)
        if entry is None:
            start()
        handle.close()
    except OSError as exc:
        if handle is not None:
            handle.close()
            _drop(partial)
        if exc.errno in (errno.ENOSPC, errno.EDQUOT):  # the staging disk itself is full
            raise UploadRefused(507, "staging_full") from exc
        raise
    except BaseException:
        if handle is not None:
            handle.close()
            _drop(partial)
        raise
    return entry, size, partial


def commit(store, *, project_id: str, user: str, upload_id: str, entry: dict, size: int,
           name: str, partial, directory) -> dict:
    """The row, in one write transaction that counts the hour again (two uploads racing past
    `admit` cannot both be the 20th), and the partial renamed to its upload_id before the
    commit. On any failure the partial (or the renamed file) is removed."""
    final = os.path.join(os.fspath(directory), upload_id)
    try:
        with store.transaction(write=True) as conn:
            store._project(conn, project_id)
            wait = retry_after(conn, project_id)
            if wait is not None:
                raise UploadRefused(429, "rate_limited", wait)
            conn.execute(f"INSERT INTO {TABLE}(upload_id,project_id,user,kind,mime,size,name,state,created_at) "
                         "VALUES(?,?,?,?,?,?,?,'staged',?)",
                         (upload_id, project_id, user, entry["kind"], entry["mime"], size, name, stamp()))
            os.replace(partial, final)
    except BaseException:
        _drop(partial)
        _drop(final)
        raise
    return {"upload_id": upload_id, "kind": entry["kind"], "mime": entry["mime"], "size": size, "name": name}


# --- A message carries uploads (step 24.2b P4, 7.8.12a "消息带附件") ---------------------------


def claim(conn, project_id: str, user: str, upload_ids) -> list[dict]:
    """The message-row attachments of `upload_ids`, checked on the message's own write
    connection: each is an upload of THIS project by THIS user, still `staged` and used by no
    message (a repeat inside the one message is a second use). One that is not refuses the
    whole message invalid_attachment, before anything is written."""
    shown = []
    for upload_id in upload_ids:
        row = conn.execute(f"SELECT project_id,user,kind,mime,size,name,state,message_id FROM {TABLE} "
                           "WHERE upload_id=?", (upload_id,)).fetchone()
        if (row is None or row["project_id"] != project_id or row["user"] != user
                or row["state"] != "staged" or row["message_id"] is not None
                or any(item["id"] == upload_id for item in shown)):
            raise StateGraphError("invalid_attachment: each attachment is an upload of yours in this project "
                                  "that no message carries yet")
        shown.append(media_types.message_attachment(upload_id, row["kind"], row["mime"], row["size"], row["name"]))
    return shown


def attach(conn, upload_ids, message_id: str, targets) -> None:
    """The uploads now belong to `message_id`; every delivery target still has to store them
    (`pending_json`, emptied by the hosts' acks)."""
    pending = json.dumps(sorted(set(targets)))
    for upload_id in upload_ids:
        conn.execute(f"UPDATE {TABLE} SET state='attached', message_id=?, pending_json=? "
                     "WHERE upload_id=? AND state='staged' AND message_id IS NULL",
                     (message_id, pending, upload_id))
