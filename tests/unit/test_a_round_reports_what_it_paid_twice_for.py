"""The re-read count has to reach the round's own report, not just the engine.

`skillflow.read_accounting` counts what a step paid twice for while the step is
running. That is only half of an observation point: privacy r1's 47 / 12 / 35
existed inside the engine all along and still cost the director an evening of
SQL, because nothing carried it out. These are the two carriers:

* `_budget_failure_report` — the handoff `core/state_service.py` already reads
  when a round dies, so the next round is told WHY the budget went;
* a `read_accounting` trace event on EVERY exit of the native loop, because a
  number that only shows up on a death says nothing about the round that
  nearly died, and nearly is the only warning there is.

The engine that answers may be older than this host: the container tracks the
PyPI wheel while a developer runs an editable checkout. So the missing-counter
path is tested FIRST and on the deployed engine, not assumed.
"""
import sys
import types

import pytest

from core.dpe_pipeline import _budget_failure_report


def report(**kw):
    base = dict(max_turns=100, first_write_turn=None, written_files=[],
                reads_searches=47, tool_failures=0, expansion_requests=[],
                relay=None, turns_used=100)
    base.update(kw)
    return _budget_failure_report(**base)


def test_the_report_always_carries_the_key_even_with_nothing_to_put_in_it():
    """A report whose shape depends on the engine version makes every reader
    of it version-aware. The key is always there; it is empty when unknown."""
    assert report()["read_accounting"] == {}


def test_the_numbers_reach_the_report():
    got = report(read_accounting={
        "reads": 47, "distinct_files": 12, "repaid_reads": 30,
        "repeat_path_reads": 35, "repaid_lines": 1108,
        "worst_file": "core/state_commands.py",
        "by_file": [{"path": "core/state_commands.py", "reads": 12,
                     "repaid_reads": 10, "repaid_lines": 891}]})
    paid = got["read_accounting"]
    assert (paid["reads"], paid["distinct_files"]) == (47, 12)
    assert paid["repaid_reads"] == 30 and paid["repeat_path_reads"] == 35
    assert paid["worst_file"] == "core/state_commands.py"
    # reads_searches on its own reads as diligence; these two live side by side
    assert got["reads_searches"] == 47


class _Engine:
    """Stands in for a skillflow that HAS the counter."""

    def __init__(self, summary):
        self._summary = summary
        self.asked = []

    def summary(self, run_id):
        self.asked.append(run_id)
        return self._summary


@pytest.fixture
def engine_with_counter(monkeypatch):
    counted = {
        "reads": 9, "distinct_files": 2, "repaid_reads": 4,
        "repeat_path_reads": 5, "repaid_lines": 40, "worst_file": "a.py",
        "by_file": [{"path": "a.py", "reads": 7, "repaid_reads": 4,
                     "repaid_lines": 40},
                    {"path": "b.py", "reads": 2, "repaid_reads": 0,
                     "repaid_lines": 0}]}
    engine = _Engine(counted)
    module = types.ModuleType("skillflow.read_accounting")
    module.summary = engine.summary
    monkeypatch.setitem(sys.modules, "skillflow.read_accounting", module)
    import skillflow
    monkeypatch.setattr(skillflow, "read_accounting", module, raising=False)
    return engine


class _Pipeline:
    """The two methods under test, lifted off the real class."""

    from core.dpe_pipeline import PipelineEngine as _real
    _read_accounting = _real._read_accounting
    _report_read_accounting = _real._report_read_accounting

    def __init__(self):
        self._run_id = "run-under-test"
        self.traced = []

    def _trace(self, category, event, payload=None):
        self.traced.append((category, event, payload or {}))


def test_only_the_files_actually_paid_for_twice_are_named(engine_with_counter):
    paid = _Pipeline()._read_accounting()
    assert [entry["path"] for entry in paid["by_file"]] == ["a.py"]
    assert paid["reads"] == 9 and paid["repaid_reads"] == 4
    assert engine_with_counter.asked == ["run-under-test"]


def test_the_count_is_traced(engine_with_counter):
    pipeline = _Pipeline()
    pipeline._report_read_accounting("t_impl")
    events = [p for c, e, p in pipeline.traced if e == "read_accounting"]
    assert len(events) == 1
    assert events[0]["step_id"] == "t_impl"
    assert events[0]["repaid_reads"] == 4 and events[0]["repeat_path_reads"] == 5


def test_a_step_that_read_nothing_traces_nothing(engine_with_counter):
    engine_with_counter._summary = {"reads": 0, "by_file": []}
    pipeline = _Pipeline()
    pipeline._report_read_accounting("t_impl")
    assert [e for c, e, p in pipeline.traced if e == "read_accounting"] == []


def test_an_engine_without_the_counter_costs_a_key_not_a_step(monkeypatch):
    """This is the path the DEPLOYED container takes today: its wheel has no
    read_accounting at all. Reporting must degrade to silence, never to a
    raise inside a finally, which would replace the step\'s real outcome."""
    monkeypatch.setitem(sys.modules, "skillflow.read_accounting", None)
    pipeline = _Pipeline()
    assert pipeline._read_accounting() == {}
    pipeline._report_read_accounting("t_impl")
    assert pipeline.traced == []


def test_an_engine_whose_counter_raises_costs_a_key_not_a_step(monkeypatch):
    module = types.ModuleType("skillflow.read_accounting")

    def explode(run_id):
        raise RuntimeError("ledger is on fire")

    module.summary = explode
    monkeypatch.setitem(sys.modules, "skillflow.read_accounting", module)
    import skillflow
    monkeypatch.setattr(skillflow, "read_accounting", module, raising=False)
    pipeline = _Pipeline()
    assert pipeline._read_accounting() == {}
    pipeline._report_read_accounting("t_impl")
    assert pipeline.traced == []


def test_the_report_runs_on_every_exit_of_the_native_step():
    """Structure, not a string match: the ONE call to the native loop must sit
    in a try whose finally reports. \'On success too\' is the criterion\'s own
    forbidden shape (report only in the rounds that failed), and an except-only
    placement would satisfy every other test here while failing that.
    """
    import ast
    import inspect

    import core.dpe_pipeline as module
    tree = ast.parse(inspect.getsource(module))

    def calls(node, name):
        return any(isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute) and n.func.attr == name
                   for n in ast.walk(node))

    guarded = [t for t in ast.walk(tree)
               if isinstance(t, ast.Try)
               and any(calls(b, "_run_native_step") for b in t.body)
               and t.finalbody
               and any(calls(f, "_report_read_accounting") for f in t.finalbody)]
    assert len(guarded) == 1, "the native loop is not reported on every exit"

    # ...and there is exactly one call to the loop, so that is every exit.
    invocations = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "_run_native_step"]
    assert len(invocations) == 1
