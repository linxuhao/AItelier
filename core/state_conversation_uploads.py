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
    staging total, which the body is then counted against. Uploads past their staging days
    are swept first (`sweep`), so their bytes no longer count."""
    sweep(store, directory)
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


# --- The hosts take the bytes and acknowledge (step 24.2b P5, 7.8.12a "宿主入库") -------------
#
# Each delivery target's host opens the staged bytes with its driver token
# (`GET /api/dsh-drivers/{project_id}/uploads/{upload_id}`, api/dsh_driver_routers.py), stores
# them in its OWN library subdirectory and acknowledges with `ack_conversation_upload`. The
# backend records the subdirectory from the token's driver, never from the host: the steward's
# is its memory user (core.dsh_driver.STEWARD_MEMORY_USER), a director's `d-<driver_id>`. The
# first ack's store and ref (or refusal) are the ones shown; the staged file is removed once
# every target has acknowledged, or after STAGING_DAYS when somebody never came (refused gone).

STAGING_DAYS = timedelta(days=7)
NOT_FOUND = "unknown_upload"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def store_for(conn, driver_id: str) -> str:
    """The library subdirectory a driver's host writes: the steward's memory user, else
    `d-<driver_id>` (read from the driver's registered role, never from a request)."""
    from core.dsh_driver import STEWARD_MEMORY_USER
    row = conn.execute("SELECT capabilities_json FROM drivers WHERE driver_id=?", (driver_id,)).fetchone()
    try:
        capabilities = json.loads(row[0]) if row is not None and row[0] else {}
    except ValueError:
        capabilities = {}
    if isinstance(capabilities, dict) and capabilities.get("role") == "steward":
        return STEWARD_MEMORY_USER
    return "d-" + driver_id


def _is_target(conn, message_id: str, driver_id: str) -> bool:
    return conn.execute("SELECT 1 FROM driver_inbox_deliveries WHERE message_id=? AND target_driver_id=?",
                        (message_id, driver_id)).fetchone() is not None


def _for_target(conn, project_id: str, upload_id, driver_id):
    """The upload row when `driver_id` is a delivery target of the message carrying this
    project's upload; else None. Unknown id, another project, an upload no message carries and
    a driver that is not a target all read the same."""
    if not driver_id or not isinstance(upload_id, str) or not media_types.UPLOAD_ID.fullmatch(upload_id):
        return None
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE upload_id=?", (upload_id,)).fetchone()
    if (row is None or row["project_id"] != project_id or row["message_id"] is None
            or not _is_target(conn, row["message_id"], driver_id)):
        return None
    return row


def _mark_refused(conn, message_id: str, upload_id: str, refused: str) -> None:
    """The message row shows the refusal on that attachment (the panel and the wake write the
    refusal line from it)."""
    stored = conn.execute("SELECT attachments_json FROM driver_inbox_messages WHERE message_id=?",
                          (message_id,)).fetchone()
    if stored is None or stored[0] is None:
        return
    items = json.loads(stored[0])
    for item in items:
        if item.get("id") == upload_id:
            item["refused"] = refused
    conn.execute("UPDATE driver_inbox_messages SET attachments_json=? WHERE message_id=?",
                 (json.dumps(items), message_id))


def sweep(store, directory, at: datetime | None = None) -> list[str]:
    """Uploads older than STAGING_DAYS whose staged file somebody still waits for: an upload no
    message took becomes `expired`; one attached to a message that no target acknowledged
    becomes refused `gone` (the message shows it); a stored or refused one just stops waiting.
    Their staged files are removed after the commit. Run lazily by the upload route, the hosts'
    fetch and the ack, so no background task is needed. Answers the upload_ids swept."""
    cutoff = stamp((at or _now()) - STAGING_DAYS)
    waiting = (f"SELECT upload_id,state,message_id FROM {TABLE} WHERE created_at<? AND "
               "(state IN ('staged','attached') OR (pending_json IS NOT NULL AND pending_json!='[]'))")
    with store.transaction() as conn:
        if conn.execute(waiting + " LIMIT 1", (cutoff,)).fetchone() is None:
            return []
    with store.transaction(write=True) as conn:
        rows = conn.execute(waiting, (cutoff,)).fetchall()
        for row in rows:
            if row["state"] == "staged":
                conn.execute(f"UPDATE {TABLE} SET state='expired', pending_json=NULL WHERE upload_id=?",
                             (row["upload_id"],))
            elif row["state"] == "attached":
                conn.execute(f"UPDATE {TABLE} SET state='refused', refused='gone', pending_json='[]' "
                             "WHERE upload_id=?", (row["upload_id"],))
                _mark_refused(conn, row["message_id"], row["upload_id"], "gone")
            else:
                conn.execute(f"UPDATE {TABLE} SET pending_json='[]' WHERE upload_id=?", (row["upload_id"],))
    for row in rows:
        _drop(os.path.join(os.fspath(directory), row["upload_id"]))
    return [row["upload_id"] for row in rows]


def open_for_target(store, project_id: str, upload_id, driver_id, directory):
    """The hosts' fetch: (open binary handle, mime, size) of a staged upload for a delivery
    target of the message carrying it. LookupError(NOT_FOUND) for anyone else - the same answer
    as an unknown id - and FileNotFoundError("gone") for a target once the file is removed
    (every target acknowledged, or swept). The file is opened before this returns, so a removal
    racing the response cannot cut it short."""
    sweep(store, directory)
    with store.transaction() as conn:
        row = _for_target(conn, project_id, upload_id, driver_id)
    if row is None:
        raise LookupError(NOT_FOUND)
    path = os.path.join(os.fspath(directory), row["upload_id"])
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise FileNotFoundError("gone") from exc
    handle = os.fdopen(fd, "rb")
    return handle, row["mime"], os.fstat(handle.fileno()).st_size


class ConversationUploads:
    """`ack_conversation_upload` for the driver this service authenticated (the one action
    handler of this table, bound per service by core.state_commands._handlers)."""

    def __init__(self, store, driver_id):
        self.store, self.driver_id = store, driver_id

    def ack_conversation_upload(self, project_id, upload_id, ref=None, refused=None):
        """A delivery target's host stored the upload (`ref`, an m_ ref in its own library) or
        refused it (`refused`, the library's code). The store is the token's driver's
        subdirectory (store_for). The first ack decides what is shown (state stored with
        store and ref, or refused - then the message shows the refusal); later acks only stop
        waiting for their target. Once no target is waiting the staged file is removed. A
        repeated ack changes nothing. A driver that is not a target of the message carrying
        this project's upload gets the same unknown_upload as an unknown id."""
        if not self.driver_id:
            raise StateGraphError("driver_identity_required: only a delivery target's host acknowledges an upload")
        from core.media_library import staging_dir
        from core.state_graph import StateNotFound
        directory = staging_dir()
        sweep(self.store, directory)
        with self.store.transaction(write=True) as conn:
            row = _for_target(conn, project_id, upload_id, self.driver_id)
            if row is None:
                raise StateNotFound(NOT_FOUND)
            pending = json.loads(row["pending_json"] or "[]")
            state, store, kept_ref, kept_refused = row["state"], row["store"], row["ref"], row["refused"]
            if self.driver_id in pending:
                pending.remove(self.driver_id)
                if state == "attached":
                    if ref is not None:
                        state, store, kept_ref = "stored", store_for(conn, self.driver_id), ref
                    else:
                        state, kept_refused = "refused", refused
                        _mark_refused(conn, row["message_id"], upload_id, refused)
                conn.execute(f"UPDATE {TABLE} SET state=?, store=?, ref=?, refused=?, pending_json=? "
                             "WHERE upload_id=?",
                             (state, store, kept_ref, kept_refused, json.dumps(pending), upload_id))
        if not pending:
            _drop(os.path.join(os.fspath(directory), upload_id))
        out = {"upload_id": upload_id, "project_id": project_id, "state": state, "waiting": len(pending)}
        if kept_ref is not None:
            out.update(store=store, ref=kept_ref)
        if kept_refused is not None:
            out["refused"] = kept_refused
        return out
