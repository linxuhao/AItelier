#!/usr/bin/env python3
"""Validate a held State DAG migration in a temporary database; never apply it.

Usage: python scripts/preview_state_migration.py manifest.json --report preview.json
The manifest's source commit and evidence hashes are checked read-only. This CLI
has no production DB target, no apply flag, no workflow launch, and no approval.
"""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.state_migration import read_manifest, dry_run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.resolve() == args.manifest.resolve():
        parser.error("report must not overwrite the source manifest")
    try:
        result = dry_run(read_manifest(args.manifest))
        # Exclusive create: preserve first failures and older rehearsal results.
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except Exception as exc:
        print(f"REFUSED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k:v for k,v in result.items() if k not in {"overview","inputs","assumptions"}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
