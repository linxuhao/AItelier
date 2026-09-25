"""Failure identities of REAL retained wuxia gate reports, read by the tree on
PYTHONPATH (`_report_dir_failure_cases`).

usage: python identity_probe.py <game copy with tools/godot_gate.py> <gate dir name>...

Part 1: every retained red gate (manifest exit_code 1) under
~/.AItelier/gate-reports/wuxia-godot-gate-*, each copied alone into a fresh
ticket (large stage reports symlinked). Prints findings vs identities per gate,
then, for every failing assertion, the identities it received across runs,
grouped by the finding text before " -> actual " (scenario / name: expr).

Part 2: injection. For each chosen real report the game gate's OWN
`verify_playtest` (tools/godot_gate.py of the game copy) writes the findings
twice: from the real report, and from a copy whose every failing row carries a
different observed value (and whose summary is changed). Both finding lists
are named by the tree under test and compared line by line. Exit 0 only when
every compared pair of identity lists is equal and every finding has one.
"""
from __future__ import annotations

import collections
import copy
import glob
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from aitelier.tools.run_tests import impl as rt

print("impl:", rt.__file__)
game = Path(sys.argv[1])
spec_ = importlib.util.spec_from_file_location("godot_gate_copy",
                                               game / "tools" / "godot_gate.py")
gg = importlib.util.module_from_spec(spec_)
spec_.loader.exec_module(gg)
print("gate:", gg.__file__)
root = Path.home() / ".AItelier" / "gate-reports"
BIG = ("playtest.json", "script.json", "python.json", "compile.json")


def ticket_for(src: Path, dst_root: Path, *, files: dict | None = None) -> Path:
    ticket = dst_root / "rt-probe"
    ticket.mkdir(parents=True)
    dst = ticket / src.name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*BIG))
    for big in BIG:
        if (src / big).exists() and not (dst / big).exists():
            os.symlink(src / big, dst / big)
    for name, value in (files or {}).items():
        target = dst / name
        if target.is_symlink():
            target.unlink()
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    return ticket


# ── part 1 ────────────────────────────────────────────────────────────────
by_assert = collections.defaultdict(set)
runs_of = collections.defaultdict(set)
gates = total_findings = total_ids = errors = short = 0
for d in sorted(glob.glob(str(root / "wuxia-godot-gate-*"))):
    try:
        manifest = json.load(open(os.path.join(d, "manifest.json")))
    except Exception:
        continue
    if manifest.get("exit_code") != 1:
        continue
    findings = []
    for f in sorted(glob.glob(d + "/*-findings.json")):
        findings += json.load(open(f))
    with tempfile.TemporaryDirectory() as t:
        res = rt._report_dir_failure_cases(ticket_for(Path(d), Path(t)),
                                           Path(manifest["repo"]))
    recs, err = res if res is not None else (None, "None (no report for repo)")
    gates += 1
    total_findings += len(findings)
    total_ids += len({r["case_id"] for r in recs or []})
    errors += 1 if err else 0
    short += 1 if len(recs or []) != len(findings) else 0
    print(f"{os.path.basename(d)} findings={len(findings)} "
          f"records={len(recs or [])} distinct_ids={len({r['case_id'] for r in recs or []})} "
          f"err={err!r}")
    for r in recs or []:
        if " -> actual " in r["detail"]:
            key = r["detail"].split(" -> actual ")[0].strip()
            by_assert[key].add(r["case_id"])
            runs_of[key].add(os.path.basename(d))
print(f"PART1 red gates={gates} findings={total_findings} distinct_ids={total_ids} "
      f"identity_errors={errors} gates_with_records!=findings={short}")
print("PART1 failing assertions (scenario / name: expr) and the ids they got across runs:")
for key, ids in sorted(by_assert.items()):
    print(f"  {len(ids)} id(s) over {len(runs_of[key])} red gate(s)  {key[:100]}")
    for i in sorted(ids):
        print(f"      {i}")


# ── part 2 ────────────────────────────────────────────────────────────────
def spec_from(report: dict) -> dict:
    """An authored contract with the report's own shape: each scenario authors
    as many assertions as came back, and a scenario whose replay came back
    opts in to repeatability. Replays are added by the gate's own
    `with_repeatability_runs`, as the gate does before it posts the spec."""
    rows = (report.get("behavior") or {}).get("scenarios") or []
    names = [r.get("name") for r in rows]
    scen = []
    for r in rows:
        n = r.get("name")
        if n.endswith(gg.REPLAY_SUFFIX):
            continue
        scen.append({"name": n, "timeline": [{"assert": [None] * len(r.get("asserts") or [])}],
                     "repeatability": (n + gg.REPLAY_SUFFIX) in names})
    return gg.with_repeatability_runs({"scenarios": scen})


def changed(value):
    """An injective change: equal values stay equal, different ones stay different."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1000
    if isinstance(value, str):
        return value + "_injected"
    return {"injected": value}


def inject(report: dict) -> dict:
    """Every failing row, and both rows of every repeatability mismatch, get a
    different `observed`; the summary changes too. The set of failures is
    the same, the text of each assertion finding is not."""
    out = copy.deepcopy(report)
    rows = (out.get("behavior") or {}).get("scenarios") or []
    by_name = {sc.get("name"): sc for sc in rows}
    targets = set()
    for sc in rows:
        for i, row in enumerate(sc.get("asserts") or []):
            if row.get("passed") is not True or row.get("error"):
                targets.add((sc.get("name"), i))
        replay = by_name.get(str(sc.get("name")) + gg.REPLAY_SUFFIX)
        if replay:
            for i, (a, b) in enumerate(zip(sc.get("asserts") or [], replay.get("asserts") or [])):
                if a != b:
                    targets.update({(sc.get("name"), i), (replay.get("name"), i)})
    for name, i in targets:
        row = by_name[name]["asserts"][i]
        row["observed"] = changed(row.get("observed"))
    out["summary"] = str(out.get("summary", "")) + " (injected)"
    return out


def load_whole(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


all_equal = True
for name in sys.argv[2:]:
    src = root / name
    manifest = json.load(open(src / "manifest.json"))
    real = load_whole(src / "playtest.json")
    spec = spec_from(real)
    injected = inject(real)
    lists = []
    for label, report in (("real", real), ("injected", injected)):
        findings = gg.verify_playtest(report, spec)
        with tempfile.TemporaryDirectory() as t:
            ticket = ticket_for(src, Path(t), files={
                "playtest.json": report, "playtest-findings.json": findings})
            recs, err = rt._report_dir_failure_cases(ticket, Path(manifest["repo"]))
        assert err is None, err
        assert len(recs) == len(findings)
        lists.append((findings, [r["case_id"] for r in recs]))
    (f1, ids1), (f2, ids2) = lists
    n_changed = sum(1 for a, b in zip(f1, f2) if a != b)
    same = ids1 == ids2
    all_equal &= same and len(ids1) == len(f1) == len(f2)
    print(f"PART2 {name}: findings real={len(f1)} injected={len(f2)} "
          f"finding texts changed by the injection={n_changed} identity lists equal={same}")
    for i, (a, b) in enumerate(zip(ids1, ids2)):
        print(f"  {i:3d} {'==' if a == b else '!='} {a}  |  {b}")
    for i, (a, b) in enumerate(zip(f1, f2)):
        if a != b:
            print(f"      text {i:3d} real:     {a[:150]}")
            print(f"      text {i:3d} injected: {b[:150]}")
print("PART2 all identity lists equal:", all_equal)
sys.exit(0 if all_equal else 1)
