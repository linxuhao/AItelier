"""Every catalogued mutation is a concrete edit against a real file, and the
card forbids counting an anchor that matches zero times as a kill. This test
is the cheap half of that guarantee: it loads the catalog and checks that each
edit's anchor matches EXACTLY once in the delivered tree, applying a file's
edits in order (so a second edit sees the first's result)."""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT / "tools" / "mutation_catalog"))
from mutations import MUTATIONS  # noqa: E402


def _validate(name):
    work = {}
    for edit in MUTATIONS[name]["edits"]:
        rel = edit["file"]
        path = _ROOT / rel
        text = work.get(rel)
        if text is None:
            text = path.read_text(encoding="utf-8")
        hits = text.count(edit["anchor"])
        assert hits == 1, (
            f"{name}: anchor hit {hits} (need 1) in {rel}: "
            f"{edit['anchor'][:70]!r}")
        work[rel] = text.replace(edit["anchor"], edit["replacement"], 1)
    return work


def test_each_mutation_anchor_hits_exactly_once():
    problems = []
    for name in sorted(MUTATIONS):
        try:
            _validate(name)
        except AssertionError as exc:
            problems.append(str(exc))
    assert not problems, "anchor problems:\n" + "\n".join(problems)


def test_the_goal_table_mutations_are_all_present():
    for name in ("N9", "M21", "M21b", "G2", "G2b", "DUPIMPL", "RESTART"):
        assert name in MUTATIONS, name
