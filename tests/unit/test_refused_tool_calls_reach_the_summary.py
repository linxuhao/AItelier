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
