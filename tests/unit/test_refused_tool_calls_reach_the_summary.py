"""A call refused before execution has to be countable in the run summary.

"The tool ran and failed" leaves a result to read. "The tool never ran" leaves a
hole, and the hole reads downstream as missing evidence. They are different
dispositions and must not share a counter.
"""

from __future__ import annotations

import json

import pytest

from core.run_driver import refused_tool_calls, summarise_run


class _SF:
    """Only the surface summarise_run touches."""

    def __init__(self, trace_rows):
        self._trace_rows = trace_rows

    def get_run(self, run_id):
        return {"id": run_id, "status": "completed", "graph_name": "coding_task",
                "project_id": "", "updated_at": "2026-09-20 23:12:00"}

    def get_steps(self, run_id):
        return [{"step_id": "implement", "status": "completed"}]

    def trace_query(self, run_id, sql, params):
        if "created_at" in sql and "LIMIT" in sql:
            return [{"created_at": "2026-09-20 23:12:00"}]
        if "COUNT(*)" in sql:
            # The engine hands an unknown run the SHARED trace database, which
            # is empty: this is the probe that tells "nothing was refused" from
            # "there is no trace to look in".
            return [{"n": len(self._trace_rows)}]
        category = params[1]
        return [row for row in self._trace_rows
                if row["category"] == category]


def _row(event, category="tool_result", **payload):
    return {"event": event, "category": category,
            "payload_json": json.dumps(payload)}


REFUSAL = ("semantic_search() failed: unrecognised argument(s): project_id. "
           "Accepted parameters: query, limit, globs, fts. "
           "No tool action was performed.")


def test_refusals_are_counted_per_tool():
    sf = _SF([
        _row("semantic_search", error=REFUSAL),
        _row("git_history", error="git_history() failed: unrecognised "
                                  "argument(s): project_id. Accepted "
                                  "parameters: mode, path. "
                                  "No tool action was performed."),
        _row("git_history", error="git_history() failed: unrecognised "
                                  "argument(s): project_id. Accepted "
                                  "parameters: mode, path. "
                                  "No tool action was performed."),
        _row("read", output="fine"),
    ])
    out = refused_tool_calls(sf, "run-1")
    assert out["total"] == 3
    assert out["by_tool"] == {"git_history": 2, "semantic_search": 1}


def test_an_empty_but_readable_trace_reports_the_inability_not_zero():
    """A trace with no rows for this run cannot support "nothing was refused".

    This is the defect the counter exists to kill, one layer down. An unknown
    run resolves to the SHARED trace database, which is empty by design, and a
    run whose per-run ``trace.db`` is gone resolves there too: both answer the
    refusal query with zero rows and no exception. Reported as ``0`` they read
    as "I looked and nothing was refused", which is the "could not count,
    rendered as nothing happened" shape.
    """
    out = refused_tool_calls(_SF([]), "run-with-no-trace")
    assert out["total"] is None
    assert "no rows" in out["unreadable"]
    assert "by_tool" not in out


def test_a_trace_that_holds_rows_can_still_report_a_true_zero():
    """The inability must not swallow the honest zero.

    A run whose trace demonstrably carries its own events, none of them a
    refusal, gets ``0`` — otherwise every clean run would report "I could not
    count" and the signal would be worthless.
    """
    out = refused_tool_calls(_SF([_row("read_file", output="fine")]), "run-1")
    assert out["total"] == 0
    assert out["by_tool"] == {}


def test_a_tool_that_ran_and_failed_is_not_counted_as_refused():
    """The forbidden fold: a real failure must stay out of this counter."""
    sf = _SF([
        _row("run_tests", error="run_tests() failed: RuntimeError: 3 tests failed"),
        _row("read", error="read() failed: FileNotFoundError: no such file"),
    ])
    out = refused_tool_calls(sf, "run-1")
    assert out["total"] == 0
    assert out["by_tool"] == {}


def test_an_unreadable_trace_does_not_render_as_zero():
    class _Broken(_SF):
        def trace_query(self, run_id, sql, params):
            if "created_at" in sql:
                return []
            raise RuntimeError("trace.db is locked")

    out = refused_tool_calls(_Broken([]), "run-1")
    assert out["total"] is None
    assert "locked" in out["unreadable"]


def test_the_summary_carries_the_count_beside_the_failure(tmp_path):
    sf = _SF([_row("semantic_search", error=REFUSAL)])

    class _WS:
        def get_final_path(self, *a, **k):
            return tmp_path / "nope"

    class _Registry:
        def get(self, _name):
            return None

    out = summarise_run(sf, _WS(), _Registry(), "run-1")
    assert out["refused_tool_calls"]["total"] == 1
    assert out["refused_tool_calls"]["by_tool"] == {"semantic_search": 1}
    # Separate dispositions: a refusal is not a failure.
    assert out["first_failure"] is None


@pytest.mark.parametrize("marker", ["No tool action was performed"])
def test_the_marker_matches_the_engine_wording(marker):
    """If SkillFlow rewords the refusal, this counter goes silently to zero.

    Pin the sentence against the installed engine's own source so the drift is
    a red test rather than an empty count.
    """
    import inspect as _inspect
    from pathlib import Path

    import skillflow
    core_py = Path(_inspect.getfile(skillflow)).parent / "core.py"
    assert marker in core_py.read_text(encoding="utf-8"), (
        f"{marker!r} is no longer the engine's wording for a pre-execution "
        f"refusal — core/run_driver.py:_REFUSED_MARKER must follow it")


def test_a_terminal_attempt_envelope_carries_the_refusal_count():
    """The director must read it where the attempt ENDS, not in a second call.

    ``_relay_failure_metadata`` computed the whole summary and kept one key,
    so ``refused_tool_calls`` was discarded on the spot and a director closing
    an attempt saw nothing about calls that never ran. Run 0cf3c10b's missing
    commit history was found by a reviewer reasoning backwards, which is
    exactly the work the field exists to spare.
    """
    from core.state_service import StateService

    service = object.__new__(StateService)
    service.sf = _SF([_row("semantic_search", error=REFUSAL)])
    service.ws = object()
    service.registry = object()
    service.runtime_factory = None

    for status in ("candidate", "failed", "superseded"):
        envelope = service._with_refusals(
            {"execution_kind": "skillflow", "status": status, "run_id": "run-1",
             "error": None})
        assert envelope["refused_tool_calls"]["total"] == 1, status
        assert envelope["refused_tool_calls"]["by_tool"] == {"semantic_search": 1}
        # The forbidden fold: it is its own field, never part of a failure count.
        assert "refus" not in str(envelope.get("error") or "")


def test_a_running_attempt_is_left_alone():
    """Mid-run the count is not yet the record; only a terminal attempt is."""
    from core.state_service import StateService

    service = object.__new__(StateService)
    service.sf = _SF([_row("semantic_search", error=REFUSAL)])
    service.ws = object()
    service.registry = object()
    service.runtime_factory = None

    envelope = service._with_refusals(
        {"execution_kind": "skillflow", "status": "running", "run_id": "run-1"})
    assert "refused_tool_calls" not in envelope


def test_a_terminal_envelope_whose_trace_is_gone_says_so():
    """`null`, never `0`: the envelope must not claim a count it could not take."""
    from core.state_service import StateService

    service = object.__new__(StateService)
    service.sf = _SF([])
    service.ws = object()
    service.registry = object()
    service.runtime_factory = None

    envelope = service._with_refusals(
        {"execution_kind": "skillflow", "status": "candidate", "run_id": "run-1"})
    assert envelope["refused_tool_calls"]["total"] is None
    assert envelope["refused_tool_calls"]["unreadable"]
