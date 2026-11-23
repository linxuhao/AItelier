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
