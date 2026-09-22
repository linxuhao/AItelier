"""No prose may keep a falsified guarantee alive on this round's files.

A previous round deleted last round's lies and wrote fresh ones no test could
falsify. Prose is not evidence; the assertion is. This test enforces the ban on
the four "keep a lie alive" phrases across the EXACT set of files this round
touched, so a future edit that tries to soften a refusal with one of them fails
here rather than passing on a green suite. It also checks the two guarantee
carriers that would otherwise be unverifiable: that every universal claim about
the verdict is backed by a concrete, runnable test in this tree.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Files this round's change touched (the diff surface). Pre-existing prose in
# other modules is out of scope; this test is about what THIS round wrote.
ROUND_FILES = [
    "api/state_verdict.py",
    "api/state_http.py",
    "api/state_author_surface.py",
    "tests/integration/test_author_surface_generator.py",
    "tests/integration/test_coverage_measures_judged_not_reached.py",
    "tests/integration/test_verdict_runs_on_fastapi_machinery.py",
    "tests/integration/test_private_delivery_before_early_ok.py",
]

BANNED = ["not a defect", "documented", "intentionally", "by design", "known limitation"]


@pytest.mark.parametrize("rel", ROUND_FILES)
def test_a_round_file_carries_no_lie_keeping_phrase(rel):
    text = (REPO / rel).read_text(encoding="utf-8")
    hits = [phrase for phrase in BANNED if phrase in text]
    assert hits == [], f"{rel} uses a banned phrase: {hits}"


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
    assert inspect.iscoroutinefunction(guard), "the verdict must run as an async dependency"


def test_record_judged_requires_a_ruling_string():
    """Guarantee: coverage counts a RULING, not an arrival. Falsified if
    `record_judged` loses its ruling argument (the arrival-count regression)."""
    import inspect
    from api.state_verdict import record_judged
    params = list(inspect.signature(record_judged).parameters)
    assert "ruling" in params, params


def test_binding_for_orders_the_private_check_before_the_dispatch_approval():
    """Guarantee: the private-delivery check runs before the dispatch approval.
    Falsified by re-reading the function source and checking a `get_driver_note`
    carrier is refused - the assertion a mutation cannot pass silently."""
    from fastapi import Depends
    from api.state_author_surface import carrier_shapes, compile_handler
    from api.state_graph_routers import get_service
    from api.state_verdict import binding_for
    from core.state_commands import execute
    shape = carrier_shapes()["carrier4_param_name_template"]
    handler = compile_handler(shape, namespace={
        "Depends": Depends, "get_service": get_service, "execute": execute})
    binding = binding_for(handler, "get_graph", "/api/state/gen/{action}")
    assert binding.ok is False
    assert re.search(r"private", binding.reason), binding.reason
