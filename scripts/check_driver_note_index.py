#!/usr/bin/env python3
"""Fail if any driver-note address does not resolve to a body.

`design/90_decisions.md` has carried the sentence "leave no pointer that cannot
be resolved" in its file header for months and nothing has ever checked it. That
sentence is prose. This is the check.

    python3 scripts/check_driver_note_index.py <state.sqlite> [project_id ...]

Exit 0: every address in every checked project resolves.
Exit 1: at least one address dangles; each one is printed with its source.
Exit 2: the check could not run (bad arguments, unreadable database).
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    database, projects = argv[1], argv[2:]
    try:
        from core.state_database import StateDatabase
        from core.state_driver_notes import StateDriverNotes
        from core.state_graph import StateGraphStore
        store = StateGraphStore(StateDatabase(database))
        notes = StateDriverNotes(store, actor="scripts/check_driver_note_index.py")
        if not projects:
            projects = [row["project_id"] for row in store.list_projects()]
    except Exception as exc:  # the check itself failing is not a green light
        print(f"check could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    dangling_total = 0
    for project_id in projects:
        report = notes.check_index(project_id)
        dangling_total += len(report["dangling"])
        print(f"{project_id}: ok={report['ok']} addresses_checked={report['addresses_checked']} "
              f"entries={report['entry_count']} listed={report['listed_count']} "
              f"delisted={report['delisted_count']}")
        for item in report["dangling"]:
            print(f"  DANGLING {item['address']} from {item['source']}: {item['reason']}")
    if dangling_total:
        print(f"FAIL: {dangling_total} driver-note address(es) do not resolve", file=sys.stderr)
        return 1
    print("OK: every driver-note address resolves")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
