#!/usr/bin/env python3
"""Release retained Git bundle bytes that no LIVE consumer can still ask for.

Never run automatically. Usage (from the repository root)::

    python tools/release_bundle_store.py DB                       # projection only
    python tools/release_bundle_store.py DB --dry-run [--limit N] # same, explicit
    python tools/release_bundle_store.py DB --apply --actor NAME [--window-days N]
                                                 [--batch-size N] [--limit N]

NO ARGUMENT IS A DRY RUN. Without ``--apply`` this tool writes NOTHING: it walks
the SAME batch loop the release would take, and reports the digests the rule
selects, the keyframes a surviving delta pins, the number of batches the pass
takes, and the row count and byte totals of the WHOLE pass - every batch, not the
first payload-capped one. ``--apply`` is the only way to write, and it needs
``--actor`` because every release row names who decided it.

What a release IS (owner ruling, 2026-11-20; measured in
``evidence/bundle-store-release-20261120/``): the bytes have no live consumer any
more - no acceptance receipt, no in-flight attempt, no unretired protected
reference, no open worktree base, no base request that can still take effect.
It is NOT "nothing names the commit": every attempt, observation, evidence,
history link and event keeps naming the commit after the release, and the
unchanged ``state_git_artifacts`` row keeps its commit, tree, digest and
reference. What leaves is the payload; what stays is the identity and one
append-only ``state_git_bundle_releases`` row per digest saying when, by whom,
under which rule and how many bytes - so the ledger can subtract them and a
reader can tell "released" from "missing" (``state_git_bundle_custody``).

Space: a delete moves bytes into free pages; SQLite cannot return them to the
filesystem while ``PRAGMA auto_vacuum`` is 0, and ``secure_delete`` is 0, so a
released payload is NOT zeroed. The file only shrinks after an offline
compaction, exactly as ``tools/migrate_bundle_store.py`` documents:

    1. python tools/release_bundle_store.py DB            (projection; writes nothing)
    2. python tools/release_bundle_store.py DB --apply --actor NAME
    3. stop the server and everything else that opens DB
    4. sqlite3 DB "PRAGMA wal_checkpoint(TRUNCATE);"
    5. sqlite3 DB "VACUUM INTO '/path/with/room/new.db'"
    6. Verify the copy: sqlite3 new.db "PRAGMA integrity_check; PRAGMA foreign_key_check;"
       and python tools/migrate_bundle_store.py /path/with/room/new.db --verify --dry-run
    7. Swap: mv DB DB.pre-release (with any DB-wal / DB-shm), move new.db to DB,
       start the server, materialize one surviving candidate, check one released
       commit is REFUSED as released (not as corrupt), then remove the old file.

Cost, measured on the 2026-11-20 snapshot (2,767 rows / 12.39 GiB): the
store-wide counter each batch reads costs about 39 s, so keep ``--batch-size``
modest while the server is stopped. One batch is one write transaction and the
WAL is truncated after it.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state_git_artifacts import (  # noqa: E402
    BUNDLE_STORE, CUSTODY_VIEW, MAX_BUNDLE_BYTES, RELEASE_RULE, RELEASE_TABLE,
    release_bundles, release_plan, store_totals)

ARTIFACTS = "state_git_artifacts"
NO_DELETE = "state_git_bundle_store_no_delete"
REQUIRED = (ARTIFACTS, BUNDLE_STORE, RELEASE_TABLE)


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


def _has(conn, kind, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type=? AND name=?",
                        (kind, name)).fetchone() is not None


def _require_schema(conn, path):
    """Refuse on a database the release cannot be correct on.

    A database that predates the release ledger still carries the OLD
    unconditional ``state_git_bundle_store_no_delete`` guard. Every DELETE would
    be refused, and widening that guard here would be the tool editing its own
    permission; the in-place upgrade belongs to
    ``core/state_attempt_schema.initialize`` (any server start runs it), so the
    tool names it instead.
    """
    for table in REQUIRED:
        if not _has(conn, "table", table):
            raise SystemExit(f"{path}: no {table} table; start the server once so the "
                             "State schema upgrade runs, then retry")
    guard = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                         (NO_DELETE,)).fetchone()
    if guard is None or RELEASE_TABLE not in (guard[0] or ""):
        raise SystemExit(f"{path}: {NO_DELETE} is not the narrowed guard (no {RELEASE_TABLE} "
                         "predicate); start the server once so core/state_attempt_schema "
                         "upgrades it in place, then retry")
    if not _has(conn, "view", CUSTODY_VIEW):
        raise SystemExit(f"{path}: no {CUSTODY_VIEW} view; start the server once so the "
                         "State schema upgrade runs, then retry")


def _counted_batches_leave_the_store(conn):
    """Let the projection's next batch see the store the apply loop would see.

    An apply pass is not one batch: it loops until the rule's set is exhausted,
    and batch ``k+1`` reads a store that batch ``k``'s DELETE already changed. A
    projection may not write, so the digests it has counted so far are hidden
    from the next plan by a TEMP view of the same name - SQLite resolves an
    unqualified table name to TEMP before main, so ``release_plan`` (the one
    deciding statement, unmodified) judges the remaining store and the loop
    below is the apply loop too. Nothing is deleted and the file is untouched.
    """
    conn.execute("CREATE TEMP TABLE release_projection_taken(bundle_sha256 TEXT PRIMARY KEY)")
    conn.execute(f"CREATE TEMP VIEW {BUNDLE_STORE} AS SELECT * FROM main.{BUNDLE_STORE} "
                 "WHERE bundle_sha256 NOT IN "
                 "(SELECT bundle_sha256 FROM temp.release_projection_taken)")


def _pinned_rows(pinned):
    """The pinned keyframes as the summary reports them (one shape, both modes)."""
    return [{"bundle_sha256": row["bundle_sha256"],
             "commit_sha": row["commit_sha"],
             "payload_bytes": int(row["payload_bytes"]),
             "raw_size": int(row["raw_size"])} for row in pinned]


def release(path, *, actor=None, apply=False, window_days=0, batch_size=32, limit=None,
            max_payload_bytes=MAX_BUNDLE_BYTES, log=print):
    """Project (``apply=False``) or perform the release, one batch per transaction.

    ONE batch loop serves both modes, so the projection describes the pass it
    would actually take instead of the first batch the payload cap allows:
    ``summary["batches"]`` is how many batches that pass is, and
    ``summary["rows"]`` / ``payload_bytes`` / ``raw_size`` are its cumulative
    totals - the same numbers ``--apply`` then reports. ``eligible_rows`` /
    ``eligible_payload_bytes`` are NOT those totals: they stay the rule's
    whole-store reach, read before any row is taken.
    """
    if apply and not actor:
        raise SystemExit("--apply needs --actor NAME: every release row names who decided it")
    conn = _connect(path, readonly=not apply)
    try:
        _require_schema(conn, path)
        summary = {"database": str(path), "dry_run": not apply, "rule": RELEASE_RULE,
                   "actor": actor, "window_days": window_days,
                   "released_rows": 0, "released_payload_bytes": 0, "released_raw_size": 0,
                   "batches": 0, "rows": 0, "payload_bytes": 0, "raw_size": 0,
                   "digests": [], "pinned": [],
                   "store_rows_before": None, "store_payload_before": None,
                   "store_rows_after": None, "store_payload_after": None}
        if not apply:
            # What the RULE allows over the whole store, which is what
            # eligible_rows/eligible_payload_bytes have always meant. limit=0
            # selects nothing, so this is the reach read before any batch.
            reach = release_plan(conn, window_days=window_days, limit=0,
                                 max_payload_bytes=max_payload_bytes)
            summary.update(eligible_rows=reach["eligible_rows"],
                           eligible_payload_bytes=reach["eligible_payload_bytes"],
                           deltas=0, keyframes=0, pinned=_pinned_rows(reach["pinned"]))
            _counted_batches_leave_the_store(conn)
        wal = apply and conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        remaining = limit
        last = None
        while remaining is None or remaining > 0:
            size = batch_size if remaining is None else min(batch_size, remaining)
            if apply:
                result = release_bundles(conn, actor=actor, admin=True, window_days=window_days,
                                         limit=size, max_payload_bytes=max_payload_bytes)
                taken, payload, raw = (result["released_rows"], result["released_payload_bytes"],
                                       result["released_raw_size"])
            else:
                # The same decision release_bundles makes, without its writes.
                result = release_plan(conn, window_days=window_days, limit=size,
                                      max_payload_bytes=max_payload_bytes)
                taken, payload, raw = result["rows"], result["payload_bytes"], result["raw_size"]
            summary["pinned"] = _pinned_rows(result["pinned"])
            if not taken:
                break
            last = result
            if apply:
                summary["store_rows_before"] = result["rows_before"]
                summary["store_payload_before"] = result["payload_before"]
                summary["store_rows_after"] = result["rows_after"]
                summary["store_payload_after"] = result["payload_after"]
                summary["released_at"] = result["released_at"]
                summary["released_rows"] += taken
                summary["released_payload_bytes"] += payload
                summary["released_raw_size"] += raw
            else:
                summary["deltas"] += result["deltas"]
                summary["keyframes"] += result["keyframes"]
                conn.executemany("INSERT INTO temp.release_projection_taken(bundle_sha256) "
                                 "VALUES(?)", [(digest,) for digest in result["digests"]])
            summary["batches"] += 1
            summary["rows"] += taken
            summary["payload_bytes"] += payload
            summary["raw_size"] += raw
            summary["digests"].extend(result["digests"])
            if remaining is not None:
                remaining -= taken
            if apply:
                log(f"... batch {summary['batches']}: released {summary['released_rows']} "
                    f"({summary['released_payload_bytes']} bytes)", file=sys.stderr)
            else:
                log(f"... batch {summary['batches']}: would release {summary['rows']} "
                    f"({summary['payload_bytes']} bytes)", file=sys.stderr)
            if wal:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if not apply:
            # The PASS, not batch one: an operator announcing this number must be
            # announcing what --apply will take.
            log(f"... projection: {summary['batches']} batch(es) cover {summary['rows']} rows / "
                f"{summary['payload_bytes']} payload bytes / {summary['raw_size']} raw bytes",
                file=sys.stderr)
            return summary
        summary["rows"] = summary["released_rows"]
        summary["payload_bytes"] = summary["released_payload_bytes"]
        summary["raw_size"] = summary["released_raw_size"]
        if last is None:
            # Nothing was releasable: say what the store holds instead of
            # repeating the rule's zero, so an operator can tell "nothing to do"
            # from "the rule matched nothing I expected".
            summary["store_rows_before"], summary["store_payload_before"] = store_totals(conn)
        return summary
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("db", help="path of the State SQLite database")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="write nothing; report the projection (the default)")
    mode.add_argument("--apply", action="store_true", help="release the selected bundles")
    parser.add_argument("--actor", help="who decided the release (required with --apply)")
    parser.add_argument("--window-days", type=int, default=0,
                        help="only release candidates older than N days (0: no window)")
    parser.add_argument("--batch-size", type=int, default=32,
                        help="rows per write transaction (default 32)")
    parser.add_argument("--limit", type=int, help="release at most N rows this run")
    parser.add_argument("--max-payload-mib", type=int, default=MAX_BUNDLE_BYTES >> 20,
                        help="bound one transaction by stored bytes (default 256)")
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.max_payload_mib < 1 or args.window_days < 0 \
            or (args.limit is not None and args.limit < 0):
        parser.error("--batch-size/--max-payload-mib must be >= 1, --window-days/--limit >= 0")
    if args.apply and not args.actor:
        parser.error("--apply needs --actor NAME")
    summary = release(args.db, actor=args.actor, apply=args.apply, window_days=args.window_days,
                      batch_size=args.batch_size, limit=args.limit,
                      max_payload_bytes=args.max_payload_mib << 20)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
