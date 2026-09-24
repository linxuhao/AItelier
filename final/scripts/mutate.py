"""Mutation runs for harness.a-scenario-has-one-reading.

Each mutant deletes one of the new checks in a detached worktree of the
candidate, with an ignition write on the mutated line, then runs the harness
test family in a throwaway container (runsuite.sh), records the bare RC, the
ignition count and the FAILED test ids, and restores the tree with
`git checkout -- .`.
"""
import re
import subprocess
import sys
from pathlib import Path

T = Path("/home/linxuhao/.AItelier/worktrees-scratch/onereading1-tools")
MUT = Path("/home/linxuhao/.AItelier/worktrees-scratch/onereading1-mut")
H = MUT / "docker/godot/godot_harness.py"
FAMILY = sys.argv[1:]


def ign(tag):
    return 'open("%s", "a").write("1\\n")' % (T / "ign" / (tag + ".txt"))


MUTANTS = [
    ("M1_scenario_key_check_deleted",
     "        unknown = sorted(str(k) for k in sc if k not in _SCENARIO_KEYS)\n",
     "        unknown = [] if %s else []\n"),
    ("M2_spec_key_check_deleted",
     "    unknown_spec = sorted(str(k) for k in spec if k not in _SPEC_KEYS)\n",
     "    unknown_spec = [] if %s else []\n"),
    ("M3_order_check_deleted",
     "        if latest is not None and at < latest[1]:\n",
     "        if %s and False:\n"),
    ("M4_probe_spec_errors_not_lifted",
     '                           for m in probe.get("spec_errors") or [])\n',
     "                           for m in ([] if %s else []))\n"),
]

(T / "ign").mkdir(exist_ok=True)
results = []
for tag, old, new in MUTANTS:
    src = H.read_text(encoding="utf-8")
    assert src.count(old) == 1, (tag, src.count(old))
    H.write_text(src.replace(old, new % ign(tag)), encoding="utf-8")
    igf = T / "ign" / (tag + ".txt")
    if igf.exists():
        igf.unlink()
    diff = subprocess.run(["git", "-C", str(MUT), "diff"], capture_output=True, text=True).stdout
    log = T / "logs" / ("mut_%s.txt" % tag)
    rc = subprocess.run([str(T / "runsuite.sh"), str(MUT), str(log),
                         "onereading1-" + tag.split("_")[0].lower()] + FAMILY).returncode
    ignition = len(igf.read_text().splitlines()) if igf.exists() else 0
    text = log.read_text(encoding="utf-8")
    failed = re.findall(r"^FAILED (\S+?)(?: - |$)", text, re.M)
    with log.open("a", encoding="utf-8") as f:
        f.write("MUTANT: %s\nIGNITION: %d\nMUTANT_DIFF:\n%s" % (tag, ignition, diff))
    subprocess.run(["git", "-C", str(MUT), "checkout", "--", "."], check=True)
    clean = subprocess.run(["git", "-C", str(MUT), "status", "--porcelain"],
                           capture_output=True, text=True).stdout == ""
    results.append((tag, rc, ignition, clean, failed))
    print("%s BARE_RC=%d IGNITION=%d CLEAN_AFTER=%s FAILED=%d" % (tag, rc, ignition, clean, len(failed)))
    for t in failed:
        print("    FAILED", t)
