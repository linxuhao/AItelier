#!/usr/bin/env python3
"""One-shot, explicit output-destination migration. Dry-run unless --apply.

Only configs declaring source-copy delivery are changed. Plain write-mode can
produce artifacts and is NOT a code signal. Running/pinned graph rows and old
workspaces are deliberately never edited by this file migration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml

from core.output_migration import migrate_document, write_migrated_config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply and not args.backup_dir:
        parser.error("--apply requires --backup-dir (original bytes retained)")
    reports = []
    for path in args.paths:
        original = path.read_bytes()
        data = yaml.safe_load(original)
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a YAML mapping")
        changes = migrate_document(data)
        sha = hashlib.sha256(original).hexdigest()
        if changes and args.apply:
            rendered = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
            # Validate a real graph; addon patches are checked after composition by tests.
            if "steps" in data and "begin" in data:
                from skillflow.graph import PipelineGraph
                PipelineGraph._from_dict(data)
            write_migrated_config(path, original, rendered.encode("utf-8"), args.backup_dir)
        reports.append({"path": str(path), "before_sha256": sha, "changes": changes,
                        "applied": bool(changes and args.apply)})
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
