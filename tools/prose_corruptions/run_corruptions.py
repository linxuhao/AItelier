"""Apply each director-defined corruption to a clean worktree and run the suite.

For every entry in `catalog.py`:

  1. copy the current worktree to a throwaway directory (git-tracked files plus
     the catalog, so the runner is present), asserting a clean starting tree;
  2. assert the entry's `old` bytes occur exactly once, then apply them (or add
     the new module);
  3. run the WHOLE suite in that copy, capturing the BARE exit code (no pipe)
     and the names of the tests that failed;
  4. restore and confirm `git status --porcelain` is empty.

Run it from the repository root:

    python tools/prose_corruptions/run_corruptions.py [--rev <sha>] \
        [--out logs/prose_corruptions.txt]

`--rev` exports that commit into the throwaway copy first, so the same runner
measures a corruption against a different tree (the base commit, for instance).
`--targets` narrows the pytest invocation (the default is the whole suite).
The per-corruption results are written as a table; the bare exit code is what
decides whether the corruption was caught.
"""

import argparse
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = Path(__file__).resolve().parent / "catalog.py"

# The tool paths travel with the runner into every tree it measures: the
# catalog is the fixture data and the runner is the harness.
TOOL_PATHS = ("tools/prose_corruptions/catalog.py",
              "tools/prose_corruptions/run_corruptions.py",
              "tools/prose_corruptions/scan_joined_lines.py")

# Tests this branch adds. They travel ONLY into the live-worktree tree; a run
# against an older revision must measure THAT revision's own suite, or the
# base pole would be measured with this branch's checker and prove nothing.
NEW_TEST_PATHS = ("tests/unit/test_prose_corruption_catalog.py",)

SKIP_DIRS = ("__pycache__", ".git", ".ruff_cache", "node_modules")

# Revisions the corruption fixtures read out of git history. The throwaway
# tree needs them reachable, or every fixture that quotes an old revision
# looks "caught" merely because `git show` failed there.
HISTORY_REVS = ("3b3d6560", "fad833b016a5c54d1a5f6253f4fcdfc0fbeb9722")


def _load_catalog():
    spec = importlib.util.spec_from_file_location("prose_corruption_catalog",
                                                 CATALOG_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True)


def _git_file(rev, rel):
    """The bytes of `rel` as of `rev`, straight out of the repository."""
    out = _git(["show", f"{rev}:{rel}"], REPO_ROOT)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _bare_run(command, cwd):
    """Run `command` and return (exit code, combined output) with no pipe."""
    proc = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _tracked_paths():
    listing = _git(["ls-files"], REPO_ROOT)
    assert listing.returncode == 0, listing.stderr
    return [line for line in listing.stdout.splitlines() if line]


def _link_history(dest):
    """Make the fixture revisions reachable inside the throwaway tree.

    Without them, a fixture that quotes an old revision fails with a `git
    show` error in the copy and would look "caught" for the wrong reason.
    """
    _git(["remote", "add", "origin", str(REPO_ROOT)], dest)
    _git(["fetch", "-q", "origin"], dest)
    for rev in HISTORY_REVS:
        _git(["fetch", "-q", "origin", rev], dest)


def _copy_worktree(dest):
    """Copy the live worktree (tracked files as they are on disk, plus ours)."""
    dest.mkdir(parents=True, exist_ok=True)
    paths = list(dict.fromkeys(_tracked_paths() + list(TOOL_PATHS)
                               + list(NEW_TEST_PATHS)))
    for rel in paths:
        source = REPO_ROOT / rel
        if not source.exists() or any(part in SKIP_DIRS
                                      for part in Path(rel).parts):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, target)
    _git(["init", "-q"], dest)
    _link_history(dest)
    _git(["add", "-A"], dest)
    _git(["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm",
          "worktree"], dest)


def _copy_tree(dest, rev=None):
    dest.mkdir(parents=True, exist_ok=True)
    if rev is None:
        _copy_worktree(dest)
        return
    archive = subprocess.run(["git", "archive", "--format=tar", rev],
                             cwd=REPO_ROOT, capture_output=True)
    assert archive.returncode == 0, archive.stderr
    subprocess.run(["tar", "-xf", "-"], cwd=dest, input=archive.stdout,
                   check=True)
    _git(["init", "-q"], dest)
    _link_history(dest)
    _git(["add", "-A"], dest)
    _git(["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", rev],
         dest)
    for rel in TOOL_PATHS:
        source = REPO_ROOT / rel
        if not source.exists():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, target)
    _git(["add", "-A"], dest)
    _git(["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm",
          "runner"], dest)


def _apply(entry, worktree, catalog):
    if entry["kind"] == "add":
        target = worktree / entry["path"]
        target.write_text(entry["content"], encoding="utf-8")
        return
    if entry["kind"] == "git":
        if entry.get("block"):
            target = worktree / "core/output_migration.py"
            text = target.read_text(encoding="utf-8")
            block = catalog._guidance(_git_file(entry["rev"],
                                                "core/output_migration.py"),
                                      entry["block"])
            target.write_text(
                re.sub(rf'(?s)(STRICT_PATCH_GUIDANCE_{entry["block"]} = '
                       r'""")(.*?)(""")', lambda m: m.group(1) + block
                       + m.group(3), text), encoding="utf-8")
            return
        target = worktree / entry["path"]
        content = _git_file(entry["rev"], entry["path"])
        target.write_text(content, encoding="utf-8")
        return
    target = worktree / entry["path"]
    text = target.read_text(encoding="utf-8")
    target.write_text(catalog.apply_edits(text, entry), encoding="utf-8")


def _failed_tests(output):
    names = [line.split(" - ")[0].strip()
             for line in output.splitlines()
             if line.startswith(("FAILED ", "ERROR "))]
    return names


def run_all(rev=None, log=None, targets=()):
    catalog = _load_catalog()
    rows = []
    source_before = _git(["status", "--porcelain"], REPO_ROOT).stdout
    for entry in catalog.CORRUPTIONS:
        with tempfile.TemporaryDirectory(prefix="prose-corruption-") as tmp:
            worktree = Path(tmp) / "tree"
            _copy_tree(worktree, rev)
            _apply(entry, worktree, catalog)
            changed = _git(["status", "--porcelain"], worktree)
            command = [sys.executable, "-m", "pytest", "-q",
                       "-p", "no:cacheprovider", *targets]
            code, output = _bare_run(
                command,
                worktree)
            row = {"id": entry["id"], "what": entry["what"],
                   "rule": entry["rule"], "changed": changed.stdout.strip()
                   .replace("\n", " | "), "rc": code,
                   "failed": _failed_tests(output)}
            rows.append(row)
            if log:
                log.append(f"=== {entry['id']} {entry['what']} "
                           f"(rev={rev or 'HEAD'}) ===")
                log.append(f"corruption rule: {entry['rule']}")
                log.append(f"changed paths: {row['changed']}")
                log.append("suite command: " + " ".join(command))
                log.append(f"bare exit code: {code}")
                log.append("failed tests: "
                           + (", ".join(row["failed"]) or "(none)"))
                log.append("")
    after = _git(["status", "--porcelain"], REPO_ROOT).stdout
    assert after == source_before, (
        "a corruption run must leave the source worktree exactly as it found "
        f"it:\nbefore:\n{source_before}\nafter:\n{after}")
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rev", default=None)
    parser.add_argument("--out", default="logs/prose_corruptions.txt")
    parser.add_argument("--targets", nargs="*", default=[])
    args = parser.parse_args()

    header = [f"prose corruption runner, {datetime.now(timezone.utc).isoformat()}",
              f"source repository: {REPO_ROOT}",
              f"tree under test: {args.rev or 'HEAD'}"]
    log = list(header)
    rows = run_all(rev=args.rev, log=log, targets=tuple(args.targets))

    table = ["id | rule | bare RC | failed tests"]
    for row in rows:
        table.append(f"{row['id']} | {row['rule']} | {row['rc']} | "
                     + ("; ".join(row["failed"]) or "-"))
    log.extend(table)

    out = REPO_ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(log) + "\n", encoding="utf-8")
    print("\n".join(table))


if __name__ == "__main__":
    main()
