"""Plant driver-note rows the way retired writers wrote them: SECTION rows of the
retired update_driver_note, and force='informational' ENTRY rows.

The free-text sections were closed on 2026-10-06 (node driver.note-has-no-free-text):
no production code path writes them any more, but live databases still hold them
and driver_note_history / search_driver_note_history must keep reading them.
Tests that need such legacy rows plant them here, on the store's own transaction,
with the same note row, append-only revision row and driver_note_updated event the
retired writer produced. The timestamp comes from ``core.state_driver_notes.now`` so
a test that monkeypatches it still controls the clock.
"""
from __future__ import annotations

import uuid

from core import state_driver_notes


def seed_section(service, project_id: str, section: str, content: str,
                 director_identity: str = "seeder", operation: str = "replace") -> int:
    """Commit one legacy section revision; return its revision number."""
    actor = service.driver_notes.actor
    timestamp = state_driver_notes.now()
    with service.store.transaction(write=True) as conn:
        row = conn.execute("SELECT * FROM state_driver_notes WHERE project_id=?",
                           (project_id,)).fetchone()
        texts = {"permanent": row["permanent_text"] if row else "",
                 "temporary": row["temporary_text"] if row else ""}
        texts[section] = content if operation == "replace" else texts[section] + content
        revision = (row["revision"] if row else 0) + 1
        conn.execute("DELETE FROM state_driver_notes WHERE project_id=?", (project_id,))
        conn.execute(
            "INSERT INTO state_driver_notes(project_id,revision,permanent_text,temporary_text,"
            "updated_by_actor,updated_by_director,updated_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, revision, texts["permanent"], texts["temporary"], actor,
             director_identity, timestamp))
        conn.execute(
            "INSERT INTO state_driver_note_revisions(project_id,revision,permanent_text,"
            "temporary_text,actor,director_identity,operation,section,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, revision, texts["permanent"], texts["temporary"], actor,
             director_identity, operation, section, timestamp))
        service.store._event(conn, project_id, None, "driver_note_updated", {
            "revision": revision, "section": section, "operation": operation,
            "actor": actor, "director_identity": director_identity})
    return revision


def seed_informational_entry(service, project_id: str, assertion: str, body: str,
                             director_identity: str = "seeder") -> str:
    """Plant one ENTRY with force='informational' the way it was written before the
    owner ruling of 2026-10-06 closed that force. Live databases still hold such
    rows; they must stay readable, supersedable and delistable. Returns entry_id."""
    entry_id = uuid.uuid4().hex[:12]
    timestamp = state_driver_notes.now()
    with service.store.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO state_driver_note_entries(project_id,entry_id,assertion,body,force,"
            "landed,listing,superseded_by,supersede_reason,delist_reason,actor,director_identity,"
            "created_at,updated_at) VALUES(?,?,?,?,'informational','','listed',NULL,'','',?,?,?,?)",
            (project_id, entry_id, assertion, body, service.driver_notes.actor,
             director_identity, timestamp, timestamp))
    return entry_id
