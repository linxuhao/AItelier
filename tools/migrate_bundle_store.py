#!/usr/bin/env python3
"""Move retained Git bundle bytes out of ``state_git_artifacts`` rows into the
content-addressed ``state_git_bundle_store`` (raw keyframes + zstd deltas).

Never run automatically. Usage (from the repository root)::

    python tools/migrate_bundle_store.py DB --dry-run [--limit N]   # projection only
    python tools/migrate_bundle_store.py DB [--batch-size N] [--limit N]
    python tools/migrate_bundle_store.py DB --verify --dry-run       # read-only check

For every row (walked by rowid, in batches, one write transaction per batch)
whose ``bundle_bytes`` is non-empty and whose custody is ``state_db_fullpack``
(``--include-legacy`` adds ``legacy_db_plus_cas_path`` rows; their
``retained_ref`` is left untouched): the bytes must hash to ``bundle_sha256``,
are inserted with the same keyframe rule as new candidates
(``core.state_git_artifacts.store_bundle``), are decoded back from the store
and compared byte for byte, and only then is that row's ``bundle_bytes`` set to
X''. The row's ``no_update`` trigger is dropped immediately before that one
UPDATE and recreated from its own stored SQL immediately after, inside the
same transaction, so no other connection ever sees the table without it.
Rows already migrated are skipped, so an interrupted run is resumed by
running it again; a rerun is a no-op. ``unrecognized`` rows, malformed
references and rows whose bytes do not match their digest are never touched
(reported, exit status 1). ``--verify`` reads every row back through the
product reader and checks its digest.

``--dry-run`` opens the database read-only and encodes into a throwaway
temporary store (seeded with the database's existing store rows) to report the
projected stored size; nothing is written to DB.

Space: freed pages stay inside the database file (SQLite reuses them for new
store rows) and the file only shrinks after compaction. With the default
``secure_delete`` build, every freed page is overwritten, so each migrated row
writes about its own size to the WAL; the tool truncates the WAL after every
batch. Keep batches small while the server is running (each batch holds the
write lock for roughly 2-3 s per 80 MB row).

Offline compaction, after the migration (needs free space for the compacted
copy, not for a second full-size copy; put it on a filesystem that has room):

    1. Stop the server and everything else that opens DB.
    2. sqlite3 DB "PRAGMA wal_checkpoint(TRUNCATE);"
    3. sqlite3 DB "VACUUM INTO '/path/with/room/new.db'"
    4. Verify the copy: sqlite3 new.db "PRAGMA integrity_check; PRAGMA foreign_key_check;"
       and python tools/migrate_bundle_store.py /path/with/room/new.db --verify --dry-run
       (every row must restore its exact digest; "to migrate" must be 0).
    5. Swap: mv DB DB.pre-compaction (with any DB-wal / DB-shm), move new.db to DB,
       start the server, check a candidate materializes, then remove the old file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state_attempt_schema import EXTRA_SCHEMA, _statements  # noqa: E402
from core.state_git_artifacts import (  # noqa: E402
    BUNDLE_STORE, git_custody, load_stored_bundle, retained_bundle_bytes, store_bundle)
from core.state_graph import StateConflict  # noqa: E402

ARTIFACTS = "state_git_artifacts"
NO_UPDATE = "state_git_artifacts_no_update"
STORE_DDL = [s for s in _statements(EXTRA_SCHEMA) if BUNDLE_STORE in s]


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _eligible(row, include_legacy):
    """None when the row may move, else the reason it stays as it is."""
    try:
        custody, _, embedded = git_custody(row["retained_ref"])
    except StateConflict:
        return "malformed_reference"
    if custody == "state_db_fullpack":
        return None if embedded == row["bundle_sha256"] else "malformed_reference"
    if custody == "legacy_db_plus_cas_path":
        return None if include_legacy else "legacy_not_requested"
    return "unrecognized"


def _rows(conn, after, size):
    return conn.execute(
        f"SELECT rowid, commit_sha, bundle_sha256, retained_ref, length(bundle_bytes) AS n "
        f"FROM {ARTIFACTS} WHERE rowid>? ORDER BY rowid LIMIT ?", (after, size)).fetchall()


def _raw(conn, rowid):
    return bytes(conn.execute(f"SELECT bundle_bytes FROM {ARTIFACTS} WHERE rowid=?",
                              (rowid,)).fetchone()[0])


def _connect(path, readonly):
    if readonly:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True,
                               isolation_level=None)
    else:
        conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _scratch_store(source):
    """A temporary on-disk store seeded with DB's existing store rows (dry run)."""
    scratch = sqlite3.connect("", isolation_level=None)
    scratch.execute("PRAGMA foreign_keys=ON")
    for statement in STORE_DDL:
        scratch.execute(statement)
    if _has_table(source, BUNDLE_STORE):
        for row in source.execute(f"SELECT * FROM {BUNDLE_STORE} ORDER BY rowid"):
            scratch.execute(f"INSERT INTO {BUNDLE_STORE} VALUES(?,?,?,?,?,?)", tuple(row))
    return scratch


def migrate(path, *, dry_run=False, batch_size=4, limit=None, include_legacy=False,
            verify=False, log=print):
    conn = _connect(path, readonly=dry_run)
    if not _has_table(conn, ARTIFACTS):
        raise SystemExit(f"{path}: no {ARTIFACTS} table")
    trigger = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                           (NO_UPDATE,)).fetchone()
    if trigger is None:
        raise SystemExit(f"{path}: {NO_UPDATE} trigger is missing; refusing to touch the table")
    trigger_sql = trigger[0]
    store = _scratch_store(conn) if dry_run else conn
    if not dry_run:
        conn.execute("BEGIN IMMEDIATE")
        for statement in STORE_DDL:
            conn.execute(statement)
        conn.execute("COMMIT")
    wal = conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    summary = {"database": str(path), "dry_run": dry_run, "rows_scanned": 0, "migrated": 0,
               "keyframes": 0, "deltas": 0, "already_migrated": 0, "raw_bytes_moved": 0,
               "stored_bytes_added": 0, "skipped": {}, "failed": []}
    after, budget = 0, limit
    while budget is None or budget > 0:
        batch = _rows(conn, after, batch_size)
        if not batch:
            break
        after = batch[-1]["rowid"]
        if not dry_run:
            conn.execute("BEGIN IMMEDIATE")
        try:
            for row in batch:
                summary["rows_scanned"] += 1
                if not row["n"]:
                    summary["already_migrated"] += 1
                    continue
                reason = _eligible(row, include_legacy)
                if reason is not None:
                    summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
                    continue
                if budget is not None:
                    if budget <= 0:
                        continue
                    budget -= 1
                raw = _raw(conn, row["rowid"])
                digest = row["bundle_sha256"]
                if hashlib.sha256(raw).hexdigest() != digest:
                    summary["failed"].append({"rowid": row["rowid"], "commit_sha": row["commit_sha"],
                                              "reason": "bytes do not match bundle_sha256"})
                    continue
                before = store.execute(f"SELECT 1 FROM {BUNDLE_STORE} WHERE bundle_sha256=?",
                                       (digest,)).fetchone()
                codec, stored = store_bundle(store, digest, raw)
                if load_stored_bundle(store, digest) != raw:
                    raise StateConflict(f"store does not restore rowid {row['rowid']} exactly")
                if before is None:
                    summary["keyframes" if codec == "raw" else "deltas"] += 1
                    summary["stored_bytes_added"] += stored
                summary["raw_bytes_moved"] += len(raw)
                summary["migrated"] += 1
                if dry_run:
                    continue
                conn.execute(f"DROP TRIGGER {NO_UPDATE}")
                changed = conn.execute(
                    f"UPDATE {ARTIFACTS} SET bundle_bytes=X'' WHERE rowid=? AND commit_sha=? "
                    "AND bundle_sha256=? AND retained_ref=?",
                    (row["rowid"], row["commit_sha"], digest, row["retained_ref"])).rowcount
                conn.execute(trigger_sql)
                if changed != 1:
                    raise StateConflict(f"rowid {row['rowid']} changed during migration")
                again = dict(conn.execute(f"SELECT * FROM {ARTIFACTS} WHERE rowid=?",
                                          (row["rowid"],)).fetchone())
                if retained_bundle_bytes(conn, again) != raw:
                    raise StateConflict(f"rowid {row['rowid']} does not read back exactly")
            if not dry_run:
                if conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                                (NO_UPDATE,)).fetchone()[0] != trigger_sql:
                    raise StateConflict(f"{NO_UPDATE} was not restored exactly")
                conn.execute("COMMIT")
        except BaseException:
            if not dry_run:
                conn.execute("ROLLBACK")
            raise
        if wal and not dry_run:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log(f"... rowid<={after}: migrated {summary['migrated']} "
            f"({summary['raw_bytes_moved']} -> {summary['stored_bytes_added']} bytes)", file=sys.stderr)
    if verify:
        summary["verified"], summary["verify_failed"] = _verify(conn)
    summary["to_migrate_remaining"] = sum(
        1 for r in conn.execute(f"SELECT rowid, bundle_sha256, retained_ref, length(bundle_bytes) AS n "
                                f"FROM {ARTIFACTS}") if r["n"] and _eligible(r, include_legacy) is None)
    conn.close()
    return summary


def _verify(conn):
    """Every row restores bytes with its own digest through the product reader."""
    ok, failed = 0, []
    conn.execute("BEGIN")
    try:
        for (rowid,) in conn.execute(f"SELECT rowid FROM {ARTIFACTS} ORDER BY rowid").fetchall():
            row = dict(conn.execute(f"SELECT * FROM {ARTIFACTS} WHERE rowid=?", (rowid,)).fetchone())
            try:
                data = retained_bundle_bytes(conn, row)
                reason = None if hashlib.sha256(data).hexdigest() == row["bundle_sha256"] \
                    else "bytes do not match bundle_sha256"
            except StateConflict as exc:
                reason = str(exc)
            if reason is None:
                ok += 1
            else:
                failed.append({"rowid": rowid, "commit_sha": row["commit_sha"], "reason": reason})
    finally:
        conn.execute("ROLLBACK")
    return ok, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db", help="path of the State SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="write nothing; report the projection")
    parser.add_argument("--batch-size", type=int, default=4, help="rows per write transaction (default 4)")
    parser.add_argument("--limit", type=int, help="migrate at most N rows this run")
    parser.add_argument("--include-legacy", action="store_true",
                        help="also move legacy_db_plus_cas_path rows' bytes (reference unchanged)")
    parser.add_argument("--verify", action="store_true",
                        help="afterwards, read every row back and check its digest")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or (args.limit is not None and args.limit < 0):
        parser.error("--batch-size must be >= 1 and --limit >= 0")
    summary = migrate(args.db, dry_run=args.dry_run, batch_size=args.batch_size, limit=args.limit,
                      include_legacy=args.include_legacy, verify=args.verify)
    moved, stored = summary["raw_bytes_moved"], summary["stored_bytes_added"]
    summary["ratio"] = round(stored / moved, 5) if moved else None
    print(json.dumps(summary, indent=2))
    return 1 if summary["failed"] or summary.get("verify_failed") else 0


if __name__ == "__main__":
    sys.exit(main())
