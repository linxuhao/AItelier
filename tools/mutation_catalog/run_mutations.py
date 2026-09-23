"""Apply every named mutation to a detached git worktree, run it at two
scopes, read the BARE exit code, record which tests went red, and restore.

Run from the repository root:

    python tools/mutation_catalog/run_mutations.py            # all mutations
    python tools/mutation_catalog/run_mutations.py M21 DUPIMPL # a subset

Protocol:
1. Creates a real `git worktree add --detach` so the copy has .git.
2. Runs an EMPTY CONTROL (no mutation applied) at full scope.
   If the control is not fully green (bare RC != 0), the runner refuses to
   produce any kill claims and exits with RC 2 + explanation.
3. For each mutation: validates anchors BEFORE applying, applies, runs at
   two scopes, records killers.
4. A kill counts only if a BEHAVIOURAL test (not an anchor-pin test) turned
   red: the killer list minus the control's red list minus anchor-pin tests
   must be non-empty.
5. Removes the worktree and verifies `git status --porcelain` is clean.

Exit status of THIS script is 0 only if every mutation produced a behavioral
kill. Writes one log per mutation under logs/mutations/ as `.txt` (never
`.log`) and a machine-readable summary.json.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutations import MUTATIONS, FULL_SCOPE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "logs" / "mutations"

# Anchor-pin tests: these verify catalog structure only, not product
# behaviour. A mutation "killed" only by these does not count as a kill.
_ANCHOR_PIN_TESTS = frozenset({
    "tests/unit/test_mutation_catalog_anchors.py::test_the_catalog_parses_and_each_entry_has_required_keys",
    "tests/unit/test_mutation_catalog_anchors.py::test_the_goal_table_mutations_are_all_present",
})


class AnchorError(Exception):
    pass


def _make_worktree(base: Path) -> Path:
    """Create a detached git worktree at a temp path."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="mut-wt-"))
    wt_path = tmp_dir / "tree"
    proc = subprocess.run(
        ["git", "worktree", "add", "--detach", str(wt_path)],
        cwd=str(base), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {proc.stderr}")
    return wt_path


def _remove_worktree(base: Path, wt_path: Path) -> None:
    """Remove the worktree and its temp parent."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(wt_path)],
        cwd=str(base), capture_output=True, text=True)
    parent = wt_path.parent
    if parent.exists():
        shutil.rmtree(parent, ignore_errors=True)


def _validate_anchors(tree: Path, edits) -> None:
    """Check every anchor hits exactly once (sequential); raise AnchorError."""
    texts: dict[str, str] = {}
    for edit in edits:
        rel = edit["file"]
        if rel not in texts:
            texts[rel] = (tree / rel).read_text(encoding="utf-8")
        hits = texts[rel].count(edit["anchor"])
        if hits != 1:
            raise AnchorError(
                f"anchor hit {hits} times (need exactly 1): "
                f"{rel}: {edit['anchor'][:60]!r}")
        texts[rel] = texts[rel].replace(edit["anchor"], edit["replacement"], 1)


def _apply_edits(tree: Path, edits) -> None:
    """Apply edits sequentially per file; writes modified files to tree."""
    files: dict[str, str] = {}
    for edit in edits:
        rel = edit["file"]
        if rel not in files:
            files[rel] = (tree / rel).read_text(encoding="utf-8")
        files[rel] = files[rel].replace(edit["anchor"], edit["replacement"], 1)
    for rel, text in files.items():
        (tree / rel).write_text(text, encoding="utf-8")


def _run_pytest(tree: Path, targets) -> tuple[int, str]:
    """Run pytest at the given targets. Returns (bare_rc, output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *targets],
        cwd=str(tree), capture_output=True, text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    out = proc.stdout + proc.stderr
    return proc.returncode, out


def _red_test_ids(output: str) -> list[str]:
    ids = []
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            ids.append(line.split(" ")[1])
    return sorted(set(ids))


def run_one(name: str, mutation: dict, control_reds: frozenset) -> dict:
    """Apply one mutation in a worktree, run, collect results, restore."""
    wt = _make_worktree(ROOT)
    try:
        _validate_anchors(wt, mutation["edits"])
    except AnchorError as exc:
        _remove_worktree(ROOT, wt)
        return {"name": name, "status": "anchor-error", "detail": str(exc)}
    _apply_edits(wt, mutation["edits"])
    tcode, tout = _run_pytest(wt, mutation.get("targeted", []))
    fcode, fout = _run_pytest(wt, FULL_SCOPE)
    reds = frozenset(_red_test_ids(fout)) | frozenset(_red_test_ids(tout))
    killers = reds - control_reds - _ANCHOR_PIN_TESTS
    killed = bool(killers)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"{name}.txt").write_text(
        f"# {name}\n"
        f"# targeted (bare rc={tcode})\n{tout}\n\n"
        f"# full (bare rc={fcode})\n{fout}\n\n"
        f"# killers={sorted(killers)}\n"
        f"# killed={killed}\n", encoding="utf-8")
    _remove_worktree(ROOT, wt)
    return {
        "name": name, "status": "killed" if killed else "SURVIVED",
        "targeted_rc": tcode, "full_rc": fcode,
        "killers": sorted(killers),
        "all_reds": sorted(reds),
        "killed": killed}


def main(argv) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    names = argv or sorted(MUTATIONS)

    # Phase 1: empty control
    print("Running empty control (no mutation applied)...")
    wt = _make_worktree(ROOT)
    ccode, cout = _run_pytest(wt, FULL_SCOPE)
    control_reds = frozenset(_red_test_ids(cout))
    (LOG_DIR / "CONTROL.txt").write_text(
        f"# empty control (bare rc={ccode})\n{cout}\n"
        f"# control reds={sorted(control_reds)}\n", encoding="utf-8")
    _remove_worktree(ROOT, wt)
    if ccode != 0:
        print(f"CONTROL NOT GREEN (rc={ccode}). Refusing to report kills.")
        print(f"Control reds: {sorted(control_reds)}")
        return 2
    print(f"Control green (rc={ccode}). Proceeding.")

    # Phase 2: each mutation
    results = []
    for name in names:
        print(f"  {name}...", end=" ", flush=True)
        r = run_one(name, MUTATIONS[name], control_reds)
        results.append(r)
        print(r.get("status", "unknown"))

    # Phase 3: verify clean
    status = subprocess.run(["git", "status", "--porcelain"],
                            cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    if status:
        print(f"WARNING: git status not clean: {status}")

    (LOG_DIR / "summary.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    for r in results:
        line = (f"{r['name']:22s} {r.get('status', 'unknown'):9s}"
                f" targeted_rc={r.get('targeted_rc')} full_rc={r.get('full_rc')}"
                f" killers={len(r.get('killers', []))}")
        print(line)
    failed = [r["name"] for r in results
              if r.get("status") == "anchor-error" or not r.get("killed")]
    if failed:
        print("NOT KILLED / ANCHOR-ERROR: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
