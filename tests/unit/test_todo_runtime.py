"""The todo runtime's whole point: completion is observed, not claimed.

Each test below is a rule from the design, written so that breaking the rule
makes it fail - not so that it describes the implementation back to itself.
"""
from pathlib import Path
import sys

import pytest

from core.todo_runtime import (Binding, Item, Plan, TodoRun, builtin_library)


def _plan(*items: Item) -> Plan:
    return Plan(key="fixture-plan", goal="prove the item is done by observation", items=tuple(items))


def _run(tmp_path: Path, *items: Item, clock=None) -> TodoRun:
    return TodoRun(_plan(*items), builtin_library(), tmp_path, clock=clock)


def test_an_item_is_not_required_to_be_red_before_it_runs(tmp_path):
    """Over-gating check: a predicate already true must not fail the step.

    Requiring redness first would make deleting the file the only way to satisfy
    the contract, which is how a gate teaches a worker to cheat.
    """
    (tmp_path / "app.py").write_text("x = 1\n")
    item = Item(id="touch", title="edit app.py", predicates=(Binding("files_present", {"pattern": "*.py"}),))
    run = _run(tmp_path, item)
    run.begin("touch")
    assert run.observe("touch").outcome == "pass"


def test_a_claim_without_a_change_is_a_failure(tmp_path):
    """The worker saying it is done is not an input to the verdict."""
    target = tmp_path / "app.py"
    target.write_text("x = 1\n")
    item = Item(id="edit", title="edit", predicates=(Binding("file_changed", {"path": "app.py", "contains": "y = 2"}),))
    run = _run(tmp_path, item)
    run.begin("edit")
    assert run.observe("edit").outcome == "fail", "an untouched file must not pass a change predicate"

    target.write_text("x = 1  # touched\n")          # changed, but not the intended change
    assert run.observe("edit").outcome == "fail", "a spurious edit must not satisfy a bare delta"

    target.write_text("x = 1\ny = 2\n")
    assert run.observe("edit").outcome == "pass"


def test_a_missing_tool_is_an_absence_not_a_failure_and_ends_by_naming_it(tmp_path):
    """Could-not-run is neither pass nor fail, and reusing the ledger is the point."""
    now = [0.0]
    item = Item(id="lint", title="lint",
                predicates=(Binding("command_exit", {"argv": ["definitely-not-a-real-binary-xyz"], "timeout": 5}),))
    run = _run(tmp_path, item, clock=lambda: now[0])
    run.begin("lint")

    first = run.observe("lint")
    assert first.outcome == "absent", "a missing binary must not be reported as a failing step"
    key = "fixture-plan/lint"
    assert key in run.ledger.episodes, "the absence must be recorded as an episode"
    assert run.ledger.episodes[key].gate == "command_exit"

    now[0] = 10_000_000.0                              # outlive any ceiling
    assert run.observe("lint").outcome == "absent"
    reason = run.terminal_reason("lint")
    assert "command_exit" in reason, f"an expired absence must name what it waited for: {reason!r}"


def test_the_library_is_portable_and_plans_only_bind(tmp_path):
    library = builtin_library()
    with pytest.raises(ValueError):
        @library.define("files_present")
        def _duplicate(**kwargs): ...  # a plan may not redefine a shared predicate

    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    one = Item(id="py", title="", predicates=(Binding("files_present", {"pattern": "*.py"}),))
    two = Item(id="txt", title="", predicates=(Binding("files_present", {"pattern": "*.txt"}),))
    # One definition, two bindings: this is what keeps the workflow config portable.
    run = _run(tmp_path, one, two)
    for item in (one, two):
        run.begin(item.id)
        assert run.observe(item.id).outcome == "pass"

    broken = Item(id="bad", title="", predicates=(Binding("not_a_predicate", {}),))
    run2 = _run(tmp_path, broken)
    run2.begin("bad")
    with pytest.raises(KeyError):
        run2.observe("bad")


def test_resume_is_by_reference_and_bounded(tmp_path):
    first = Item(id="one", title="", predicates=(Binding("files_present", {"pattern": "*.py"}),))
    second = Item(id="two", title="", predicates=(Binding("files_present", {"pattern": "*.py"}),), needs=("one",))
    run = _run(tmp_path, first, second)
    assert run.next_item().id == "one", "the dependency order must be respected"

    (tmp_path / "a.py").write_text("")
    run.begin("one")
    assert run.observe("one").complete
    assert run.next_item().id == "two"

    brief = run.brief(max_chars=200)
    assert len(brief) <= 200, "the brief must stay bounded; it is re-injected after every compact"
    assert "two" in brief and "one" in run.report()["done"]
    assert "definitely-not-a-real-binary-xyz" not in brief, "the brief carries ids, not the whole catalogue"
