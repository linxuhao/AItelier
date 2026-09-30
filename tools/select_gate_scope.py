#!/usr/bin/env python3
"""Write a static scope plan. This does not run any test or authorize release."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aitelier.scoped_gate import select_scope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path)
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("spec", type=Path, help="complete authored JSON contract")
    parser.add_argument("--measurement", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    spec = json.loads(args.spec.read_text())
    measurement = None
    # An unreadable measurement is absence; the selector records the full
    # fallback. An unreadable contract cannot supply a safe full inventory.
    if args.measurement:
        try:
            measurement = json.loads(args.measurement.read_text())
        except (OSError, ValueError):
            measurement = None
    scope = select_scope(args.repo, args.base, args.head, spec, measurement)
    args.out.write_text(json.dumps(scope, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"coverage": scope["coverage"],
                      "selected": len(scope["selected_scenarios"]),
                      "total": len(scope["all_scenarios"]),
                      "fallback_full": scope["fallback_full"],
                      "selection_wall_sec": scope["selection_wall_sec"],
                      "fallback_reason": scope["selection_basis"].get("fallback_reason")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
