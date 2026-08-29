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
_FILES = ["core/run_driver.py", "core/meta_agent.py"]


def _calls(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _has_shape(node: ast.Try) -> bool:
    body = ast.Module(body=node.body, type_ignores=[])
    awaits_execute = any(isinstance(n, ast.Await) and "execute" in _calls(n)
                         for n in ast.walk(body))
    return awaits_execute and "confirm_step" in _calls(body)


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


def _handles_cancellation(node: ast.Try) -> bool:
    for h in node.handlers:
        if h.type is None:
            return False          # bare except: catches it, but also swallows
        for t in ast.walk(h.type):
            if isinstance(t, ast.Attribute) and t.attr == "CancelledError":
                return True
    return False


def _reraises(node: ast.Try) -> bool:
    """A handler that does not re-raise turns a cancellation into a silent
    'the driver finished', which is worse than the leak it was fixing."""
    for h in node.handlers:
        if h.type is None:
            continue
        if not any(isinstance(t, ast.Attribute) and t.attr == "CancelledError"
                   for t in ast.walk(h.type)):
            continue
        return any(isinstance(n, ast.Raise) and n.exc is None
                   for n in ast.walk(ast.Module(body=h.body, type_ignores=[])))
    return False


@pytest.mark.parametrize("rel", _FILES)
def test_every_claim_holding_loop_handles_cancellation(rel):
    tree = ast.parse((_ROOT / rel).read_text(encoding="utf-8"))
    blocks = _claim_holding_trys(tree)
    assert blocks, f"{rel}: found no claim-holding loop — has the shape moved?"
    for b in blocks:
        assert _handles_cancellation(b), (
            f"{rel}:{b.lineno} awaits a step and confirms it, but does not "
            f"handle asyncio.CancelledError. `except Exception` cannot see it "
            f"(it is a BaseException), so a cancelled driver leaves the step "
            f"claimed forever — the reaper will not take it back from a live "
            f"process.")
        assert _reraises(b), (
            f"{rel}:{b.lineno} handles the cancellation but does not re-raise "
            f"it; the caller would be told the driver finished normally.")


def test_the_three_known_loops_are_all_seen():
    """Pins the count. A refactor that collapses or moves one of these should
    fail here and be re-read, not silently reduce what is checked."""
    total = sum(len(_claim_holding_trys(
        ast.parse((_ROOT / rel).read_text(encoding="utf-8")))) for rel in _FILES)
    assert total == 3, (
        f"expected the 3 claim-holding step loops (run_driver._step, "
        f"meta_agent._run_meta_until_checkpoint, "
        f"meta_agent._run_pipeline_until_checkpoint), found {total}")
