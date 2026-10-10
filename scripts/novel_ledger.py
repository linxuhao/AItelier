#!/usr/bin/env python3
"""novel ledger CLI.

  novel_ledger.py migrate <novel_repo> [--dry-run] [--force] [--stale-after N] [--mode auto|replay|strip]
      split in-bible history into novel/ledger/ + compact current-state bible
      (does NOT commit; review `git diff` in the novel repo first)
  novel_ledger.py query <novel_repo> [--kind K] [--name N] [--chapter C]
                        [--from A] [--to B] [--field F] [--type T] [--limit L]
  novel_ledger.py context-size <novel_repo>
      assemble the state_probe bundle into a temp dir and print its size
"""

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aitelier import novel_state as ns  # noqa: E402
from aitelier.novel_ledger_migrate import migrate  # noqa: E402
from aitelier.tools.state_probe.impl import state_probe  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("migrate")
    m.add_argument("repo")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--force", action="store_true")
    m.add_argument("--mode", choices=["auto", "replay", "strip"], default="auto",
                   help="auto: replay when the novel-genesis tag exists, else strip")
    m.add_argument("--stale-after", type=int, default=None,
                   help="also drop card state keys not updated in the last N chapters")
    q = sub.add_parser("query")
    q.add_argument("repo")
    q.add_argument("--kind")
    q.add_argument("--name")
    q.add_argument("--chapter", type=int)
    q.add_argument("--from", dest="chapter_from", type=int)
    q.add_argument("--to", dest="chapter_to", type=int)
    q.add_argument("--field")
    q.add_argument("--type", dest="entry_type")
    q.add_argument("--limit", type=int)
    c = sub.add_parser("context-size")
    c.add_argument("repo")
    a = ap.parse_args(argv)
    repo = str(Path(a.repo).resolve())

    if a.cmd == "migrate":
        out = migrate(repo, dry_run=a.dry_run, force=a.force,
                      stale_after=a.stale_after, mode=a.mode)
    elif a.cmd == "query":
        out = ns.query_ledger(repo, kind=a.kind, name=a.name, chapter=a.chapter,
                              chapter_from=a.chapter_from, chapter_to=a.chapter_to,
                              field=a.field, entry_type=a.entry_type, limit=a.limit)
    else:
        with tempfile.TemporaryDirectory() as td:
            state_probe(project_root=repo, out_dir=td)
            text = (Path(td) / "novel_context.md").read_text(encoding="utf-8")
        out = {"chars": len(text), "bytes": len(text.encode("utf-8")),
               "non_ws_chars": ns.char_count(text)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
