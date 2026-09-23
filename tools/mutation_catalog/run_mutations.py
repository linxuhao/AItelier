"""Apply every named mutation to a copy of the real tree, run it at two
scopes, read the BARE exit code, record which tests went red, and restore.

Run from the repository root:

    python tools/mutation_catalog/run_mutations.py            # all mutations
    python tools/mutation_catalog/run_mutations.py M21 DUPIMPL # a subset

Exit status of THIS script is 0 only if every mutation it ran produced a
non-zero full-suite exit code (a kill).  It writes one log per mutation under
logs/mutations/ and a machine-readable table to logs/mutations/summary.json.

Nothing here weakens a gate to make a mutation look killed: an anchor that does
not match exactly once is reported as ANCHOR-ERROR (and is NOT a kill), the
worktree is copied so the tree under test is otherwise the delivered one, and
the bare returncode is read from the subprocess directly — never through a
pipe, so `| tail` cannot mask a non-zero exit.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mutations import MUTATIONS, FULL_SCOPE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = ROOT / "logs" / "mutations"


def _apply_copy(dst: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mut-")) / "tree"
    shutil.copytree(dst, tmp, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", "*.pyc", ".pytest_cache", "logs"))
    return tmp


def _snapshot(tree: Path, edits) -> dict:
    """Return original texts keyed by touched file after validating anchors."""
    originals: dict[str, str] = {}
    for edit in edits:
        path = tree / edit["file"]
        text = path.read_text(encoding="utf-8") if originals.get(edit["file"]) is None \
            else originals[edit["file"]]
        hits = text.count(edit["anchor"])
        if hits != 1:
            raise AnchorError(f"anchor hit {hits} times (need exactly 1): "
                              f"{edit['file']}: {edit['anchor'][:60]!r}")
        originals[edit["file"]] = text.replace(
            edit["anchor"], edit["replacement"], 1)
    return originals


class AnchorError(Exception):
    pass


def _run(tree: Path, targets) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         *targets],
        cwd=str(tree), capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    code = proc.returncode  # BARE exit code: not piped, not tee'd
    (LOG_DIR).mkdir(parents=True, exist_ok=True)
    return code, out


def _red_tests(output: str) -> list[str]:
    ids = []
    for line in output.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            ids.append(line.split(" ")[1])
    return sorted(set(ids))


def run_one(name: str, mutation: dict) -> dict:
    tree = _apply_copy(ROOT)
    record = {"name": name, "edits": [e["file"] for e in mutation["edits"]]}
    try:
        originals = _snapshot(tree, mutation["edits"])
    except AnchorError as exc:
        record["status"] = "anchor-error"
        record["detail"] = str(exc)
        shutil.rmtree(tree.parent, ignore_errors=True)
        return record
    for rel, text in originals.items():
        (tree / rel).write_text(text, encoding="utf-8")
    tcode, tout = _run(tree, mutation.get("targeted", []))
    fcode, fout = _run(tree, FULL_SCOPE)
    (LOG_DIR).mkdir(parents=True, exist_ok=True)
    (LOG_DIR / f"{name}.log").write_text(
        "# targeted (bare rc=%d)\n%s\n\n# full (bare rc=%d)\n%s\n"
        % (tcode, tout, fcode, fout), encoding="utf-8")
    # Verify the tree copy is a git repo copy and status is clean after we
    # simply discard it (we never touch the original tree, so porcelain of the
    # REAL worktree is what matters; assert it stays empty here).
    status = subprocess.run(["git", "status", "--porcelain"],
                            cwd=str(ROOT), capture_output=True, text=True).stdout.strip()
    record.update(
        targeted_rc=tcode, full_rc=fcode,
        red_tests=_red_tests(fout) or _red_tests(tout),
        killed=bool(fcode != 0),
        worktree_dirty=bool(status))
    shutil.rmtree(tree.parent, ignore_errors=True)
    return record


def main(argv) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    names = argv or sorted(MUTATIONS)
    results = []
    for name in names:
        results.append(run_one(name, MUTATIONS[name]))
    (LOG_DIR / "summary.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")
    for r in results:
        line = (f"{r['name']:22s} {r.get('status', 'killed' if r.get('killed') else 'SURVIVED'):9s}"
                f" targeted_rc={r.get('targeted_rc')} full_rc={r.get('full_rc')}"
                f" red={len(r.get('red_tests', []))}")
        print(line)
    failed = [r["name"] for r in results
              if r.get("status") == "anchor-error" or not r.get("killed")]
    if failed:
        print("NOT KILLED / ANCHOR-ERROR: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
