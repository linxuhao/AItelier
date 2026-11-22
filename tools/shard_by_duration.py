"""Split the test files into N shards by measured seconds, not by file count.

Shards cut by file count are as slow as the unlucky shard that drew the heavy
files (2026-11-21: 50.6 min for the slowest of six against 26.5 min for the
fastest). Greedy longest-first packing over measured per-file seconds evens
them out.

  python tools/shard_by_duration.py split -n 6 [--durations FILE] [--out-dir DIR] [PATH ...]
      PATH defaults to every tests/**/test_*.py. A file the durations file does
      not name is charged the median of the files it does name. Without
      --out-dir each shard is printed as one line of space-separated paths;
      with it, DIR/shard<i>.txt is written. A summary goes to stderr.

  python tools/shard_by_duration.py measure LOG[:LIST] [LOG[:LIST] ...] > FILE
      Builds the durations file from pytest logs run with --durations=N. The
      listed rows are summed per file. With :LIST (the file list that shard
      ran), the part of the shard's total that --durations did not list is
      spread evenly over every file of that list, so files too fast to be
      listed still get a share.

The committed tools/test_durations.json was seeded from the six shard logs of
the 2026-11-21 full-suite audit (main 10a5c970c6, loaded host). Re-measure on
an idle host with --durations=0 and regenerate it when the suite shifts.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DURATIONS = REPO / "tools" / "test_durations.json"
ROW = re.compile(r"^\s*([0-9.]+)s\s+(?:call|setup|teardown)\s+([^:\s]+\.py)::")
TOTAL = re.compile(r"\bin ([0-9.]+)s\b")


def measure(specs: list[str]) -> dict[str, float]:
    seconds: dict[str, float] = {}
    for spec in specs:
        log, _, listing = spec.partition(":")
        text = Path(log).read_text(encoding="utf-8", errors="replace")
        listed: dict[str, float] = {}
        for line in text.splitlines():
            row = ROW.match(line)
            if row:
                listed[row.group(2)] = listed.get(row.group(2), 0.0) + float(row.group(1))
        for name, value in listed.items():
            seconds[name] = seconds.get(name, 0.0) + value
        if listing:
            files = Path(listing).read_text(encoding="utf-8").split()
            totals = TOTAL.findall(text)
            if files and totals:
                share = max(float(totals[-1]) - sum(listed.values()), 0.0) / len(files)
                for name in files:
                    seconds[name] = seconds.get(name, 0.0) + share
    return {name: round(value, 1) for name, value in sorted(seconds.items())}


def split(files: list[str], durations: dict[str, float], shards: int):
    default = statistics.median(durations.values()) if durations else 1.0
    cost = {name: durations.get(name, default) for name in files}
    bins: list[tuple[float, list[str]]] = [(0.0, []) for _ in range(shards)]
    for name in sorted(files, key=lambda n: (-cost[n], n)):
        index = min(range(shards), key=lambda i: bins[i][0])
        total, members = bins[index]
        bins[index] = (total + cost[name], members + [name])
    return [(total, sorted(members)) for total, members in bins], default


def _test_files() -> list[str]:
    return sorted(str(p.relative_to(REPO)) for p in (REPO / "tests").rglob("test_*.py"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("split")
    sp.add_argument("-n", "--shards", type=int, required=True)
    sp.add_argument("--durations", type=Path, default=DEFAULT_DURATIONS)
    sp.add_argument("--out-dir", type=Path)
    sp.add_argument("paths", nargs="*")
    mp = sub.add_parser("measure")
    mp.add_argument("logs", nargs="+")
    args = parser.parse_args(argv)

    if args.command == "measure":
        json.dump(measure(args.logs), sys.stdout, indent=1, sort_keys=True)
        sys.stdout.write("\n")
        return 0

    durations = json.loads(args.durations.read_text()) if args.durations.is_file() else {}
    bins, default = split(args.paths or _test_files(), durations, args.shards)
    print(f"# unmeasured files charged the median {default:.1f}s", file=sys.stderr)
    for index, (total, members) in enumerate(bins):
        print(f"# shard{index}: ~{total:.0f}s, {len(members)} files", file=sys.stderr)
        if args.out_dir:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            (args.out_dir / f"shard{index}.txt").write_text("\n".join(members) + "\n")
        else:
            print(" ".join(members))
    return 0


if __name__ == "__main__":
    sys.exit(main())
