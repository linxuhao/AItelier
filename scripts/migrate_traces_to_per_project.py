#!/usr/bin/env python3
"""Migrate existing skillflow_trace rows into per-project trace.db files.

Run once after enabling per-project trace DBs (trace_db_path=WS_PATH in
the SkillFlow constructor).  Safe to run idempotently — skips projects
that already have a trace.db.

Usage:
    python3 scripts/migrate_traces_to_per_project.py [--delete-source]

    --delete-source   After copying, DELETE the rows from the shared
                      skillflow_trace table (offline only — stop the
                      server first).
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

TRACE_DDL = """
CREATE TABLE IF NOT EXISTS skillflow_trace (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL,
    step_id          TEXT,
    step_instance_id INTEGER,
    seq              INTEGER NOT NULL,
    category         TEXT NOT NULL,
    event            TEXT NOT NULL,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_skillflow_trace_run ON skillflow_trace(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_skillflow_trace_step ON skillflow_trace(step_instance_id);
"""


def default_data_root() -> Path:
    return Path(os.getenv("AITELIER_HOME") or Path.home() / ".AItelier")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--delete-source", action="store_true",
                        help="Delete rows from shared skillflow_trace after copy")
    args = parser.parse_args()

    root = default_data_root()
    shared_db = root / "skillflow.db"
    ws_dir = root / "workspaces"

    if not shared_db.exists():
        print(f"Shared skillflow.db not found at {shared_db} — nothing to migrate.")
        return

    src = sqlite3.connect(str(shared_db))
    src.row_factory = sqlite3.Row

    # Check that the trace table exists
    tables = [r[0] for r in src.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "skillflow_trace" not in tables:
        print("skillflow_trace table not present — nothing to migrate.")
        src.close()
        return

    # Collect project_id → [run_id, ...] mapping
    project_runs: dict[str, list[str]] = {}
    for row in src.execute(
        "SELECT id, project_id FROM skillflow_runs WHERE project_id IS NOT NULL"
    ).fetchall():
        pid = row["project_id"]
        project_runs.setdefault(pid, []).append(row["id"])

    if not project_runs:
        print("No runs with project_id found — nothing to migrate.")
        src.close()
        return

    print(f"Found {len(project_runs)} project(s) with {sum(len(v) for v in project_runs.values())} run(s).")

    total_copied = 0
    total_skipped = 0

    for pid, run_ids in project_runs.items():
        dest_path = ws_dir / pid / "trace.db"
        if dest_path.exists():
            total_skipped += 1
            print(f"  [{pid}] trace.db already exists — skipping")
            continue

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(str(dest_path))
        dest.execute("PRAGMA journal_mode=WAL;")
        for stmt in TRACE_DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                dest.execute(stmt)
        dest.commit()

        copied = 0
        for run_id in run_ids:
            rows = src.execute(
                "SELECT run_id, step_id, step_instance_id, seq, category, "
                "event, payload_json, created_at "
                "FROM skillflow_trace WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
            if not rows:
                continue
            dest.executemany(
                "INSERT INTO skillflow_trace "
                "(run_id, step_id, step_instance_id, seq, category, event, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(r["run_id"], r["step_id"], r["step_instance_id"], r["seq"],
                  r["category"], r["event"], r["payload_json"], r["created_at"])
                 for r in rows],
            )
            copied += len(rows)

        dest.commit()
        dest.close()
        total_copied += copied
        print(f"  [{pid}] {copied} trace row(s) → {dest_path}")

    if args.delete_source:
        print("\nDeleting migrated rows from shared skillflow_trace …")
        for pid, run_ids in project_runs.items():
            dest_path = ws_dir / pid / "trace.db"
            if not dest_path.exists():
                continue
            for run_id in run_ids:
                cur = src.execute(
                    "DELETE FROM skillflow_trace WHERE run_id = ?", (run_id,))
                print(f"  [{pid}] run={run_id}: deleted {cur.rowcount} row(s)")
        src.commit()

    src.close()
    print(f"\nDone: {total_copied} row(s) copied, {total_skipped} project(s) skipped.")


if __name__ == "__main__":
    main()
