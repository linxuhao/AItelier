"""Every source file in this repository parses, and no prose file carries a
spliced, duplicated or truncated fragment.

This card has twice shipped a file whose edit left a piece of the old line
glued to the new one (a stray `` by the API')`` tail; a clause repeated twice in
a row; a truncated sentence).  Nothing was watching the files the reviewer had
not already named, so this module watches all of them instead of a list:

* every ``.py`` file must compile (this is the detector that sees the glued
  tail, because a stale tail is a syntax error);
* no ``.md`` / ``.txt`` line may contain a fragment of at least 24 characters
  that immediately repeats itself while carrying two or more words
  (``These extra**assification). These extra**`` is that shape);
* no ``.ts`` / ``.svelte`` line may graft a group closer onto a statement
  (``expect(x).toHaveBeenCalled();  });`` is that shape).

``test_the_detectors_see_the_known_corruption_shapes`` feeds each detector the
exact corruption shape of the base revision *and* a clean sample, so a detector
that flags everything or nothing fails here.
"""

from __future__ import annotations

import pathlib
import re

import pytest

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

PY_SUFFIXES = {".py"}
PROSE_SUFFIXES = {".md", ".txt"}
WEB_SUFFIXES = {".ts", ".svelte"}

# The delivery note quotes the before/after of the corruption it reports, so it
# repeats fragments on purpose; `logs/` holds captured tool output.  Neither is
# part of the scan surface.
SKIP_TOP_LEVEL = {"final", "logs"}

MIN_FRAGMENT = 24
_WORD = re.compile(r"[A-Za-z]{3,}")
ADJACENT_REPEAT = re.compile(r"(.{%d,}?)\1" % MIN_FRAGMENT)
GRAFTED_CLOSER = re.compile(r";[ \t]+\}\)")


def _walk(suffixes):
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        rel = path.relative_to(ROOT)
        if any(part.startswith(".") or part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.parts[0] in SKIP_TOP_LEVEL:
            continue
        yield path, rel


def repeated_fragment(line):
    """First immediate self-repeat of at least two words, or ``None``.

    Runs of one repeated character (box-drawing rules, indentation) contain no
    words and are not corruption, so they are filtered out here.
    """
    for match in ADJACENT_REPEAT.finditer(line):
        fragment = match.group(1)
        if len(_WORD.findall(fragment)) >= 2:
            return fragment
    return None


def test_every_python_file_compiles():
    broken = []
    seen = 0
    for path, rel in _walk(PY_SUFFIXES):
        seen += 1
        try:
            compile(path.read_bytes(), str(rel), "exec")
        except SyntaxError as exc:
            broken.append(f"{rel}:{exc.lineno}: {exc.msg}")
    assert seen > 100, f"the scan found only {seen} python files - it is not walking the repo"
    assert not broken, "uncompilable python files:\n" + "\n".join(broken)


def test_no_prose_file_repeats_a_fragment():
    hits = []
    seen = 0
    for path, rel in _walk(PROSE_SUFFIXES):
        seen += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if len(line) < 2 * MIN_FRAGMENT:
                continue
            fragment = repeated_fragment(line)
            if fragment:
                hits.append(f"{rel}:{number}: repeated fragment {fragment[:60]!r}")
    assert seen > 20, f"the scan found only {seen} prose files - it is not walking the repo"
    assert not hits, "repeated fragments:\n" + "\n".join(hits)


def test_no_web_source_grafts_a_group_closer_onto_a_statement():
    hits = []
    seen = 0
    for path, rel in _walk(WEB_SUFFIXES):
        seen += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            # An arrow-function one-liner (`x => { ...; })`) legitimately ends
            # that way; the grafted closer has no arrow before it.
            if "=>" in line:
                continue
            if GRAFTED_CLOSER.search(line):
                hits.append(f"{rel}:{number}: {line.strip()[:90]!r}")
    assert seen > 20, f"the scan found only {seen} web sources - it is not walking the repo"
    assert not hits, "grafted group closers:\n" + "\n".join(hits)



def test_the_detectors_see_the_known_corruption_shapes():
    # tests/browser/state_project_smoke.py at base 79d6f5eb: the new string kept
    # the tail of the old one, so the file could not compile.
    spliced = (
        "                checks.append('anonymous reader sees the public graph "
        "and the opened project working notes') by the API')"
    )
    with pytest.raises(SyntaxError):
        compile(spliced, "state_project_smoke.py", "exec")
    with pytest.raises(SyntaxError):
        compile("x = (\n", "truncated.py", "exec")
    # docs/state-project-ui-migration.md:96 before the r2 fix: duplicated clause.
    assert repeated_fragment(
        "...classification). These extra**assification). These extra** ..."
    )
    # web/src/__tests__/views/StateProject.test.ts:77 before this rev: a closer
    # grafted onto the end of the statement.
    assert GRAFTED_CLOSER.search(
        "    expect(api.stateDriverNote).toHaveBeenCalled();  });"
    )
    # Clean samples are not flagged.
    assert repeated_fragment("State project data is private to authorized writers.") is None
    assert repeated_fragment("-" * 40) is None
    assert GRAFTED_CLOSER.search("    expect(api.stateDriverNote).toHaveBeenCalled();") is None
    assert GRAFTED_CLOSER.search("  });") is None
