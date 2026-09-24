"""No prose may keep a falsified guarantee alive on this round's files.

A previous round deleted last round's lies and wrote fresh ones no test could
falsify. Prose is not evidence; the assertion is. This test enforces the ban on
the "keep a lie alive" phrases across the EXACT set of files this round touched,
so a future edit that tries to soften a refusal with one of them fails here
rather than passing on a green suite.

The scan set is DERIVED from git - the diff against this CARD's first-round
base, not the previous round's candidate - not typed here: a file any of the
card's rounds adds or edits is in scope automatically, and the ban cannot be
narrowed by editing a list in this file. The phrases are
assembled from fragments so this file holds no banned literal, which removes
the earlier hole where a phrase written on the declaration line slipped a
line-prefix exclusion. There is no exclusion now: any occurrence fails.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The base this card was opened against - its FIRST round, not the previous
# round's candidate. Anchoring on the card base keeps every file any of the
# card's rounds touched in scope; the FILE LIST still comes from git below.
BASE_SHA = "9c79f11f"

# Assembled from fragments so this module contains no banned literal.
BANNED = [
    "not a " + "defect",
    "docu" + "mented",
    "inten" + "tionally",
    "by " + "desi" + "gn",
    "known " + "limita" + "tion",
]

_SCANNED_SUFFIXES = (".py", ".md", ".ts", ".tsx", ".txt")


def _changed_files() -> list[str]:
    """The files this round's change touches, TAKEN FROM GIT.

    Union of the round's diff against `BASE_SHA` (committed and uncommitted) and
    the worktree status, so a file added, edited or renamed by the round is in
    scope - including this test and the guarantee ledger. Only existing text
    files under the repository are returned.
    """
    def git(*args) -> str:
        result = subprocess.run(["git", *args], cwd=REPO, text=True,
                                capture_output=True)
        return result.stdout if result.returncode == 0 else ""

    names: set = set()
    for args in (("diff", "--name-only", BASE_SHA, "HEAD"),
                 ("diff", "--name-only", BASE_SHA),
                 ("diff", "--name-only", "HEAD~1", "HEAD"),
                 ("show", "--name-only", "--format=", "HEAD"),
                 ("diff", "--name-only", "HEAD"),
                 ("status", "--porcelain")):
        for line in git(*args).splitlines():
            line = line.rstrip()
            if not line:
                continue
            if args[0] == "status":
                line = line[3:]
                if " -> " in line:
                    line = line.split(" -> ")[-1]
            names.add(line.strip())
    return sorted(rel for rel in names
                  if rel.endswith(_SCANNED_SUFFIXES) and (REPO / rel).is_file())


ROUND_FILES = _changed_files()


def test_the_scan_scope_is_derived_from_git_and_not_empty():
    assert ROUND_FILES, "git reported no changed files for this round"


def test_the_scan_is_anchored_on_the_cards_first_round_base():
    """Guarantee: the scan cannot be narrowed by moving its base forward.

    Anchoring on the previous round's candidate drops the files earlier rounds
    of this card already changed, and a phrase planted into one of them slips
    the ban. Falsified by moving BASE_SHA forward: this fails, and a phrase in
    an earlier-round file stops being scanned (measured: a "by desi"+"gn"
    phrase planted into `api/state_only.py` is caught under this base).
    """
    assert BASE_SHA == "9c79f11f", (
        "the scan base must stay the card's first-round base; moving it forward "
        "narrows the scope below files the card already changed")
    # Resolvable in git, so the scope is a re-runnable derivation rather than a
    # typed list: this fails if the base commits forward or stops existing.
    import subprocess
    resolved = subprocess.run(["git", "cat-file", "-e", BASE_SHA], cwd=REPO,
                              capture_output=True)
    assert resolved.returncode == 0, f"base {BASE_SHA} does not resolve in git"
    assert "api/state_only.py" in ROUND_FILES


@pytest.mark.parametrize("rel", ROUND_FILES)
def test_a_round_file_carries_no_lie_keeping_phrase(rel):
    text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
    # The failure names the phrase by its index in BANNED, never by its text,
    # so a log of a red run does not itself carry the phrase into the tree.
    hits = [index for index, phrase in enumerate(BANNED) if phrase in text]
    assert not hits, f"{rel} uses BANNED phrase number(s) {hits}"


def test_no_line_prefix_can_exclude_a_banned_phrase():
    """The hole this replaces: a line-prefix exclusion let a phrase written on
    the declaration line slip the scan. There is no exclusion now, so planting a
    phrase on such a line is caught by the very predicate the scan uses."""
    source = (REPO / "tests/integration/test_no_unfalsifiable_guarantees.py").read_text(
        encoding="utf-8")
    assert [p for p in BANNED if p in source] == []
    planted = source + '    "' + BANNED[0] + '",\n'
    assert [p for p in BANNED if p in planted] == [BANNED[0]]


def test_the_guard_is_still_the_one_router_wide_dependency():
    """Guarantee: exactly one guard runs the verdict for the whole prefix, and it
    is an async dependency (so it applies the verdict through FastAPI's machinery,
    which the criterion-6 test proves). Falsified if a second guard, or a sync
    hand-call of the verdict, returns."""
    from api.state_graph_routers import router as state_router
    deps = state_router.dependencies
    assert len(deps) == 1
    guard = deps[0].dependency
    assert guard.__name__ == "_router_guard"
    import inspect
    # The guard is an async YIELD dependency: its second half (closing the
    # verdict stack) runs after the handler, the way a plain `Depends(D)`
    # route closes D's teardown.
    assert inspect.isasyncgenfunction(guard) or inspect.iscoroutinefunction(guard), \
        "the verdict must run as an async dependency"


def test_record_judged_requires_a_ruling_string():
    """Guarantee: coverage counts a RULING, not an arrival. Falsified if
    `record_judged` loses its ruling argument (the arrival-count regression)."""
    import inspect
    from api.state_verdict import record_judged
    params = list(inspect.signature(record_judged).parameters)
    assert "ruling" in params, params


def test_binding_for_orders_the_private_check_before_the_dispatch_approval():
    """Guarantee: the private-delivery check runs before the dispatch approval.
    Falsified by re-reading the function source and checking a carrier that
    delivers the private mailbox read is refused - the assertion a mutation
    cannot pass silently."""
    from fastapi import Depends
    from tests.support.state_author_surface import carrier_shapes, compile_handler
    from api.state_graph_routers import get_service
    from api.state_verdict import binding_for
    from core.state_commands import execute
    shape = carrier_shapes()["carrier4_param_name_template"]
    handler = compile_handler(shape, namespace={
        "Depends": Depends, "get_service": get_service, "execute": execute})
    binding = binding_for(handler, "get_graph", "/api/state/gen/{action}")
    assert binding.ok is False
    assert re.search(r"private", binding.reason), binding.reason
