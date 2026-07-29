"""A failed run must say what actually failed.

skillflow reports loop exhaustion as a bare "Cycle limit exceeded" and often leaves
error_reason unset; the text that tells you what to fix was written to the trace by
the failing gate. The dashboard shows the enriched version.
"""
from unittest.mock import MagicMock

import pytest

from core import scheduler


def _sf_with_trace(rows):
    sf = MagicMock()
    sf.trace_query.return_value = rows
    return sf


@pytest.fixture(autouse=True)
def _clean_cache():
    """The reason cache is module-level and keyed by run id — isolate each test."""
    scheduler._failure_reason_cache.clear()
    yield
    scheduler._failure_reason_cache.clear()


@pytest.fixture
def patched_sf(monkeypatch):
    def _install(rows):
        import api.dependencies as deps
        monkeypatch.setattr(deps, "get_skillflow", lambda: _sf_with_trace(rows))
    return _install


def test_specific_error_is_left_alone(patched_sf):
    patched_sf([])
    assert scheduler._failure_reason({"id": "r", "error_reason": "disk full"}) == "disk full"


def test_cycle_limit_is_enriched_from_the_trace(patched_sf):
    patched_sf([{"step_id": "v_registry",
                 "payload_json": '{"passed": false, "error": "Registry check failed — '
                                 'agent_config \'spec_maker\' not defined in role table"}'}])
    reason = scheduler._failure_reason({"id": "r", "error_reason": "Cycle limit exceeded"})
    assert reason.startswith("Cycle limit exceeded — v_registry:")
    assert "spec_maker" in reason


class TestSkillflowsOwnDetailDoesNotSuppressTheTrace:
    """skillflow >=1.5.30 enriches the reason itself — that must not cost us ours.

    A loop that exhausts `max_loop` now names the `from_file` field of the edges it
    failed to match, and then the edge:

        Cycle limit exceeded — continuity_report.json violations: 字数超限 …
                               (edges: All transitions from 'continuity_check' …)

    The vagueness test was an EXACT match on the whole string, so the moment the
    base stopped being *literally* "Cycle limit exceeded" it counted as specific
    and `_last_trace_error` was never consulted — the same class of loss the
    enrichment was shipped to fix, one layer up. The head of the reason is what
    decides; the two details concatenate.
    """

    def test_an_enriched_cycle_limit_still_reaches_the_trace(self, patched_sf):
        patched_sf([{"step_id": "continuity_check",
                     "payload_json": '{"passed": false, "feedback": "第 3 段与第 1 章设定冲突"}'}])
        base = ("Cycle limit exceeded — continuity_report.json summary: continuity check "
                "finished (edges: All transitions from 'continuity_check' are exhausted)")
        reason = scheduler._failure_reason({"id": "r", "error_reason": base})
        assert reason.startswith(base + " — continuity_check:")
        assert "第 3 段与第 1 章设定冲突" in reason

    def test_the_edge_detail_alone_is_still_vague(self, patched_sf):
        """No `from_file` to read (a flag-routed loop) — skillflow appends only the edge."""
        patched_sf([{"step_id": "v_smoke", "payload_json": '{"error": "graph does not terminate"}'}])
        base = "Cycle limit exceeded (edges: 'a' -> 'b' (max_loop=3 reached))"
        assert scheduler._failure_reason({"id": "r", "error_reason": base}) == \
            f"{base} — v_smoke: graph does not terminate"

    def test_a_bare_cycle_limit_is_unchanged(self, patched_sf):
        """skillflow leaves the base byte-identical when it finds no reason."""
        patched_sf([{"step_id": "v_smoke", "payload_json": '{"error": "boom"}'}])
        assert scheduler._failure_reason({"id": "r", "error_reason": "Cycle limit exceeded"}) == \
            "Cycle limit exceeded — v_smoke: boom"

    def test_the_same_detail_is_not_said_twice(self, patched_sf):
        """Both sides can quote the same routing file; "X — Y — Y" reads as two failures."""
        patched_sf([{"step_id": "continuity_check",
                     "payload_json": '{"passed": false, "feedback": "字数超限: 5662 字（上限 4500）"}'}])
        base = "Cycle limit exceeded — continuity_report.json violations: 字数超限: 5662 字（上限 4500）"
        assert scheduler._failure_reason({"id": "r", "error_reason": base}) == base

    def test_a_real_error_carrying_an_em_dash_is_left_alone(self, patched_sf):
        """The head is what decides — a specific reason stays specific."""
        patched_sf([{"step_id": "v_smoke", "payload_json": '{"error": "never read"}'}])
        base = "Tool step 'apply_state' crashed 3 times — failing (likely a bug in the tool)"
        assert scheduler._failure_reason({"id": "r", "error_reason": base}) == base


def test_missing_error_reason_falls_back_to_the_trace(patched_sf):
    patched_sf([{"step_id": "v_smoke", "payload_json": '{"error": "graph does not terminate"}'}])
    assert scheduler._failure_reason({"id": "r"}) == "v_smoke: graph does not terminate"


def test_a_failed_verdict_without_an_error_still_reports(patched_sf):
    patched_sf([{"step_id": "review", "payload_json": '{"passed": false, "feedback": "gap in step 3"}'}])
    assert scheduler._failure_reason({"id": "r"}) == "review: gap in step 3"


def test_empty_error_strings_are_skipped(patched_sf):
    """A passing gate records error:"" — that must not become the reason."""
    patched_sf([{"step_id": "v_lint", "payload_json": '{"error": "", "passed": true}'},
                {"step_id": "v_registry", "payload_json": '{"error": "real problem"}'}])
    assert scheduler._failure_reason({"id": "r"}) == "v_registry: real problem"


def test_no_trace_at_all_degrades_quietly(patched_sf):
    patched_sf([])
    assert scheduler._failure_reason({"id": "r"}) == "unknown"


def test_a_broken_trace_db_does_not_raise(monkeypatch):
    import api.dependencies as deps
    sf = MagicMock()
    sf.trace_query.side_effect = RuntimeError("db gone")
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    assert scheduler._failure_reason({"id": "r", "error_reason": "Cycle limit exceeded"}) \
        == "Cycle limit exceeded"


def test_the_reason_is_computed_once_per_run(monkeypatch):
    """A failed run is terminal, but the status sync runs on every poll tick for it —
    without a cache that is a full trace scan forever."""
    scheduler._failure_reason_cache.clear()
    calls = []

    def _sf():
        sf = MagicMock()
        def _q(*a, **k):
            calls.append(1)
            return [{"step_id": "v_registry", "payload_json": '{"error": "boom"}'}]
        sf.trace_query.side_effect = _q
        return sf

    import api.dependencies as deps
    monkeypatch.setattr(deps, "get_skillflow", _sf)
    run = {"id": "run-42", "error_reason": "Cycle limit exceeded"}
    first = scheduler._failure_reason(run)
    for _ in range(20):
        assert scheduler._failure_reason(run) == first
    assert len(calls) == 1
    assert "boom" in first


def test_a_specific_reason_never_touches_the_trace(monkeypatch):
    scheduler._failure_reason_cache.clear()
    import api.dependencies as deps
    sf = MagicMock()
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    assert scheduler._failure_reason({"id": "r", "error_reason": "disk full"}) == "disk full"
    sf.trace_query.assert_not_called()


def test_the_cache_is_bounded(monkeypatch):
    scheduler._failure_reason_cache.clear()
    import api.dependencies as deps
    sf = MagicMock()
    sf.trace_query.return_value = [{"step_id": "s", "payload_json": '{"error": "x"}'}]
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    for i in range(scheduler._FAILURE_CACHE_MAX + 10):
        scheduler._failure_reason({"id": f"run-{i}"})
    assert len(scheduler._failure_reason_cache) <= scheduler._FAILURE_CACHE_MAX


# ── A run that never got created at all ───────────────────────────────────────
# `_failure_reason` can only enrich a run that EXISTS. When create_run itself
# raises there is no run row, no trace and no 'failed' status to enrich, so the
# project sat at 'planning' forever while every tick re-raised into APScheduler.
# The tick has to write the terminal status itself.

def test_create_run_failure_becomes_a_visible_terminal_status(monkeypatch):
    import asyncio

    monkeypatch.setattr(scheduler, "get_skillflow", lambda: MagicMock())
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run",
                        lambda pid: (_ for _ in ()).throw(
                            Exception("UNIQUE constraint failed: "
                                      "skillflow_edge_counts.run_id")))
    written = {}
    monkeypatch.setattr(scheduler.db, "update_project",
                        lambda pid, **kw: written.update(pid=pid, **kw))

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        scheduler._run_skillflow_tick("p1", None))

    assert written["pid"] == "p1"
    assert written["status"].startswith("failed:could not start run — ")
    assert "skillflow_edge_counts" in written["status"]
    assert len(written["status"]) <= 160


def test_a_db_write_failure_does_not_mask_the_original_error(monkeypatch):
    """Best-effort status write: the tick must still return, not raise."""
    import asyncio

    monkeypatch.setattr(scheduler, "get_skillflow", lambda: MagicMock())
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run",
                        lambda pid: (_ for _ in ()).throw(Exception("boom")))
    monkeypatch.setattr(scheduler.db, "update_project",
                        lambda pid, **kw: (_ for _ in ()).throw(Exception("db down")))

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        scheduler._run_skillflow_tick("p1", None))
