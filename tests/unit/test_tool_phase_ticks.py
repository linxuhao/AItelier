"""Liveness ticks around inline TOOL-node execution.

Tool nodes (5_test, the Godot gates, git_push_post, …) are executed inline by
``sf.advance_run`` — they never reach the engine, so the engine's tool-phase
ticks never fire and the liveness line went blank for the whole execution
(measured 5m41s for the playtest gate). ``_advance_recording_crashes`` now
peeks ``current_node`` before advancing — advance_run executes at most ONE
inline tool per call, so the peek attributes the coming execution exactly —
and emits a ``tool`` / ``tool_done`` tick pair on the ephemeral SSE channel.
"""

import pytest

from core import scheduler


class _Node:
    def __init__(self, tool_name):
        self.tool_name = tool_name


class _Resolver:
    def __init__(self, tool_steps):
        self._tools = tool_steps  # step_id -> tool_name

    def is_tool(self, step_id):
        return step_id in self._tools

    def get_node(self, step_id):
        return _Node(self._tools[step_id])


class _SF:
    def __init__(self, current_node, status="running", tool_steps=None,
                 delegated=(), advance_raises=None):
        self._run = {"status": status, "current_node": current_node}
        self._resolver = _Resolver(tool_steps or {})
        self._delegated = set(delegated)
        self._advance_raises = advance_raises
        self.advance_calls = 0

    def get_run(self, run_id):
        return dict(self._run)

    def _get_resolver_for_run(self, run_id):
        return self._resolver

    def _should_delegate_tool(self, tool_name):
        return tool_name in self._delegated

    def advance_run(self, run_id):
        self.advance_calls += 1
        if self._advance_raises is not None:
            raise self._advance_raises
        return "next_node"


@pytest.fixture
def ticks(monkeypatch):
    """Capture what _push_phase_tick puts on the SSE channel."""
    import api.sse_manager as sm
    seen = []
    monkeypatch.setattr(sm, "push_global_event", seen.append)
    return seen


def test_inline_tool_gets_a_tick_pair(ticks):
    sf = _SF("5_test", tool_steps={"5_test": "godot_playtest"})
    assert scheduler._advance_recording_crashes(sf, "r1", "p1") == "next_node"

    assert [t["phase"] for t in ticks] == ["tool", "tool_done"]
    for t in ticks:
        assert t["type"] == "llm_progress"
        assert t["project_id"] == "p1"
        assert t["run_id"] == "r1"
        assert t["step_id"] == "5_test"
        assert t["tool"] == "godot_playtest"
        assert "_ts" in t


def test_agent_node_emits_nothing(ticks):
    sf = _SF("t_impl", tool_steps={})
    scheduler._advance_recording_crashes(sf, "r1", "p1")
    assert ticks == []


def test_delegated_native_tool_emits_nothing(ticks):
    # advance_run's fast-path skips delegated tools — a tick would wrap nothing
    sf = _SF("w1", tool_steps={"w1": "write"}, delegated={"write"})
    scheduler._advance_recording_crashes(sf, "r1", "p1")
    assert ticks == []


def test_non_running_run_emits_nothing(ticks):
    sf = _SF("5_test", status="paused", tool_steps={"5_test": "run_tests"})
    scheduler._advance_recording_crashes(sf, "r1", "p1")
    assert ticks == []


def test_crashed_tool_still_clears_the_line(ticks, monkeypatch):
    # tool_done must fire on the crash path too, or the SPA shows a phantom
    # "running tool" for 10 minutes after the run already failed
    monkeypatch.setattr(scheduler, "_record_tick_error",
                        lambda *a, **k: None)
    sf = _SF("5_test", tool_steps={"5_test": "run_tests"},
             advance_raises=RuntimeError("tool crashed"))
    with pytest.raises(RuntimeError):
        scheduler._advance_recording_crashes(sf, "r1", "p1")
    assert [t["phase"] for t in ticks] == ["tool", "tool_done"]


def test_peek_failure_means_no_tick_never_a_broken_advance(ticks):
    class _Broken(_SF):
        def get_run(self, run_id):
            raise RuntimeError("db locked")

    sf = _Broken("5_test", tool_steps={"5_test": "run_tests"})
    assert scheduler._advance_recording_crashes(sf, "r1", "p1") == "next_node"
    assert sf.advance_calls == 1
    assert ticks == []


def test_broken_sse_push_cannot_break_the_advance(monkeypatch):
    import api.sse_manager as sm

    def _boom(_):
        raise RuntimeError("loop is gone")

    monkeypatch.setattr(sm, "push_global_event", _boom)
    sf = _SF("5_test", tool_steps={"5_test": "run_tests"})
    assert scheduler._advance_recording_crashes(sf, "r1", "p1") == "next_node"
