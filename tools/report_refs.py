#!/usr/bin/env python3
"""Refuse to reclaim a director report bundle that State still points at.

Why this exists: on 2026-11-04, 7 of 15 CANDIDATE nodes in the aitelier DAG had
terminal reports whose cited files were GONE — the bundle had been reclaimed while
the State report still named it. The project already had the rule ("reclaim owns
reports, but check State references first"), and it was still violated, because
nothing enforced it: reclamation is manual, and the check was a thing to remember.

Run this before deleting anything under ~/.AItelier/director/reports/.

    report_refs.py <bundle-dir> [--db PATH] [--json]

Exit codes:
    0  no State reference found - the bundle is not cited by any terminal report
    1  CITED - do not reclaim; the references are printed
    2  usage / database problem (treat as refused: an unreadable check is not a pass)

It checks two things, because either one alone misses half the cases:
  (a) a terminal observation whose report_ref lives INSIDE the bundle;
  (b) any terminal report whose TEXT names a path inside the bundle - this is how
      the 2026-11-04 losses happened: the report was stored elsewhere but cited
      files in a bundle that was later reclaimed.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sqlite3
import sys

DEFAULT_DB = os.path.expanduser("~/.AItelier/aitelier.db")


def _inside(candidate: str, bundle: pathlib.Path) -> bool:
    try:
        pathlib.Path(candidate).resolve().relative_to(bundle.resolve())
        return True
    except (ValueError, OSError):
        return False


def find_refs(bundle: pathlib.Path, db_path: str) -> list[dict]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    hits: list[dict] = []

    rows = con.execute(
        "select attempt_id, observation_id, report_ref from state_external_observations "
        "where report_ref is not null and report_ref != ''"
    ).fetchall()
    for row in rows:
        if _inside(row["report_ref"], bundle):
            hits.append({"kind": "report_stored_here",
                         "attempt_id": row["attempt_id"],
                         "observation_id": row["observation_id"],
                         "path": row["report_ref"]})
            continue
        # (b) the report lives elsewhere but may cite files inside the bundle
        ref = pathlib.Path(row["report_ref"])
        if not ref.is_file():
            continue
        try:
            text = ref.read_text(errors="ignore")
        except OSError:
            continue
        marker = str(bundle)
        if marker in text:
            names = sorted({p for p in text.split() if marker in p})
            hits.append({"kind": "cited_by_report",
                         "attempt_id": row["attempt_id"],
                         "observation_id": row["observation_id"],
                         "report_ref": str(ref),
                         "cites": len(names)})
    con.close()
    return hits


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("bundle")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    bundle = pathlib.Path(args.bundle).expanduser()
    if not bundle.is_dir():
        print(f"refusing: {bundle} is not a directory", file=sys.stderr)
        return 2
    if not pathlib.Path(args.db).is_file():
        print(f"refusing: {args.db} does not exist; an unreadable check is not a pass",
              file=sys.stderr)
        return 2

    try:
        hits = find_refs(bundle, args.db)
    except sqlite3.Error as exc:
        print(f"refusing: cannot read State ({exc}); an unreadable check is not a pass",
              file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"bundle": str(bundle), "cited": bool(hits), "refs": hits}, indent=2))
    elif hits:
        print(f"CITED: {bundle} is still referenced by State - do not reclaim")
        for h in hits:
            if h["kind"] == "report_stored_here":
                print(f"  terminal report stored here: {h['attempt_id']} ({h['observation_id']})")
            else:
                print(f"  cited by {h['attempt_id']} ({h['observation_id']}): {h['cites']} path(s)")
    else:
        print(f"free: no State reference points into {bundle}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
