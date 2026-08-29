"""Every loop that holds a claim across an await must handle cancellation.

The behavioural test next door proves the handler works. It cannot prove the
handler is actually attached at each of the three real loops — and three copies
of the same subtle rule is exactly where one gets missed. This walks the source
instead of trusting that it was wired.

The shape it looks for is "holds a claim across an await": a `try` whose body
both awaits `runner.execute(...)` and calls `confirm_step`. That is the block
where a cancellation strands a claim. A bare `await runner.execute(...)` that
neither confirms nor fails (meta_agent's `_run_step`, which hands the coroutine
to a caller that owns the loop) is not that shape and is not required to.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

# SCANNED, not listed. The first version of this file hardcoded two paths and
# `core/scheduler.py` — which was leaking claims at the time — simply was not
# among them, so a test named "every step driver" passed while one driver was
# unfixed. A fixed list can only ever check the drivers someone remembered.
_SCAN_DIRS = ("core", "api", "web_api", "aitelier")


def _sources() -> list[Path]:
    out = []
    for d in _SCAN_DIRS:
        root = _ROOT / d
        if root.is_dir():
            out += [p for p in sorted(root.rglob("*.py"))
                    if "__pycache__" not in p.parts]
    return out


def _calls(node) -> set[str]:
    """Names of everything called: `x.y()` as `y`, and a bare `y()` as `y`.

    Attribute-only was enough while this looked for `confirm_step`; the release
    helper is a module-level function, so a bare-name call, and leaving Name out
    made the check silently unable to see the thing it was added to require.
    """
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def _has_shape(node: ast.Try) -> bool:
    """Confirms a step, with an await in the same `try` body.

    Deliberately NOT "awaits a call literally named `execute`". That version was
    blind to both shapes this codebase would plausibly use for a new driver: a
    helper (`await _execute_and_confirm(...)`) renames the call, and
    `await asyncio.to_thread(runner.execute, claimed)` — the shape CLAUDE.md
    points at, since `runner.execute` blocks the loop — passes `execute` as a
    REFERENCE, which is not an `ast.Call` at all. A leaking driver written
    either way kept the test green.

    Any `try` that confirms a claim and awaits anything is holding a claim
    across a suspension point, which is the whole condition. Over-matching here
    costs a handler on a block that would want one anyway.
    """
    body = ast.Module(body=node.body, type_ignores=[])
    awaits = any(isinstance(n, ast.Await) for n in ast.walk(body))
    # Any callee whose name mentions `confirm`, not just `confirm_step`, so an
    # extracted `await _execute_and_confirm(...)` — where the confirm itself
    # lives in another function and no AST walk of this body can see it —
    # still counts as holding a claim across the await.
    confirms = any("confirm" in n for n in _calls(body))
    return awaits and confirms


def _claim_holding_trys(tree) -> list[ast.Try]:
    """The INNERMOST try with the shape.

    Both real loops sit inside an outer `try` that wraps the whole driver, and
    that outer one matches the shape too — by containing the inner one. Counting
    it as a second site made the inventory read 5 where there are 3, and would
    have demanded a redundant handler on a block that never touches the claim.
    """
    matches = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
               and _has_shape(n)]
    return [n for n in matches
            if not any(isinstance(d, ast.Try) and d is not n and _has_shape(d)
                       for d in ast.walk(ast.Module(body=n.body,
                                                    type_ignores=[])))]


def _cancel_handler(node: ast.Try) -> ast.ExceptHandler | None:
    for h in node.handlers:
        if h.type is None:
            return None           # bare except: catches it, but also swallows
        if any(isinstance(t, ast.Attribute) and t.attr == "CancelledError"
               for t in ast.walk(h.type)):
            return h
    return None


def _releases(node: ast.Try) -> bool:
    """Does the handler actually HAND THE CLAIM BACK?

    Checking only that a `except asyncio.CancelledError` clause exists is the
    wrong property, and this test was written that way first: deleting
    `release_claim_on_cancel(...)` from all three fixed loops left it green,
    while `core/scheduler.py` — whose handler logged and re-raised and released
    nothing — would have passed on the day it was still leaking claims.
    """
    h = _cancel_handler(node)
    if h is None:
        return False
    return "release_claim_on_cancel" in _calls(
        ast.Module(body=h.body, type_ignores=[]))


def _reraises(node: ast.Try) -> bool:
    """A handler that does not re-raise turns a cancellation into a silent
    'the driver finished', which is worse than the leak it was fixing."""
    h = _cancel_handler(node)
    if h is None:
        return False
    return any(isinstance(n, ast.Raise) and n.exc is None
               for n in ast.walk(ast.Module(body=h.body, type_ignores=[])))


def _all_claim_holding_loops() -> list[tuple[str, ast.Try]]:
    out = []
    for path in _sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:                      # not ours to police
            continue
        rel = str(path.relative_to(_ROOT))
        out += [(rel, b) for b in _claim_holding_trys(tree)]
    return out


def test_every_claim_holding_loop_handles_cancellation():
    found = _all_claim_holding_loops()
    assert found, "found no claim-holding loop anywhere — has the shape moved?"
    for rel, b in found:
        assert _releases(b), (
            f"{rel}:{b.lineno} awaits a step and confirms it, but its "
            f"cancellation path does not call release_claim_on_cancel. "
            f"`except Exception` cannot see a CancelledError (it is a "
            f"BaseException), so without an explicit release the step stays "
            f"claimed forever — and the reaper will not take it back from a "
            f"live process. Catching and logging is not enough.")
        assert _reraises(b), (
            f"{rel}:{b.lineno} handles the cancellation but does not re-raise "
            f"it; the caller would be told the driver finished normally.")


def test_the_four_known_loops_are_all_seen():
    """Pins WHICH loops exist, not just how many.

    A new driver in a module nobody thought of is the failure this whole file
    exists to catch, so the scan finding a fifth should stop the build and be
    read — and so should a refactor that quietly removes one.
    """
    found = sorted(f"{rel}:{b.lineno}" for rel, b in _all_claim_holding_loops())
    where = {f.split(":")[0] for f in found}
    assert where == {"core/run_driver.py", "core/meta_agent.py",
                     "core/scheduler.py"}, (
        f"the set of files holding a claim across an await changed: {found}. "
        f"A new one must call release_claim_on_cancel in its CancelledError "
        f"handler; a vanished one should be confirmed intentional.")
    assert len(found) == 4, (
        f"expected 4 claim-holding step loops (run_driver._step, "
        f"meta_agent._run_meta_until_checkpoint, "
        f"meta_agent._run_pipeline_until_checkpoint, "
        f"scheduler._run_skillflow_tick), found {found}")
