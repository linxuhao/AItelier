"""Every ``.py`` file in this repository compiles.

A stale edit tail - a piece of the old line left glued to the end of the new
one - breaks the syntax of a ``.py`` file, so this compile pass is the one check
here that actually sees it. There is no parser for prose (``.md`` / ``.txt``): a
tail like `` policy.`` on a doc line is invisible to any regex and was caught
only by reading the file back, so the delivery note, not a scanner, is the record
of what was written there. ``.ts`` / ``.svelte`` are compiled by the test step's
vitest run, so they are not scanned here either.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Directories that are not this repository's own source: virtualenvs, vendored
# packages, caches.
SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "build",
    "dist",
    "site-packages",
}


def _python_files():
    for path in sorted(ROOT.rglob("*.py")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts):
            continue
        yield path, rel


def test_every_python_file_compiles():
    broken = []
    seen = 0
    for path, rel in _python_files():
        seen += 1
        try:
            compile(path.read_bytes(), str(rel), "exec")
        except SyntaxError as exc:
            broken.append(f"{rel}:{exc.lineno}: {exc.msg}")
    assert seen > 100, f"the walk found only {seen} python files - it is not walking the repo"
    assert not broken, "uncompilable python files:\n" + "\n".join(broken)
