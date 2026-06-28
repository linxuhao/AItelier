# tests/integration/test_native_parity.py
# Regression tests for native-mode parity with JSON mode:
#   - turn budget resolved by role (agent_config_name), not step_id (Bug H)
#   - no-tool-call reply is salvaged with a write nudge, not ended empty
#   - genuine no-op signal floors with existing files (parity w/ JSON no-op)
#   - pure exploration exhaustion still raises (floor must NOT mask it)
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.dpe_pipeline import PipelineEngine, MaxRetriesExceeded

TS = {"read_file": {}, "list_tree": {}, "write": {}}


class _WS:
    """Minimal workspace double; records draft writes."""
    def __init__(self, tmp: Path):
        self.base_path = tmp
        self.projects_base = tmp / "projects"
        self.written_drafts = {}

    def _get_secure_path(self, project_id):
        return self.base_path / project_id

    def get_code_path(self, project_id):
        p = self.projects_base / project_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write_draft(self, project_id, step_id, filename, content, graph_name=None):
        self.written_drafts[f"{step_id}/{filename}"] = content


def _setup(tmp, project_id="default", step_id="t_impl"):
    (tmp / project_id / f"{step_id}.tmp").mkdir(parents=True, exist_ok=True)
    code = tmp / "projects" / project_id
    code.mkdir(parents=True, exist_ok=True)
    (code / "README.md").write_text("# existing\n")


def _turn(text="", tool_calls=None, reasoning=""):
    return SimpleNamespace(text=text, tool_calls=tool_calls or [],
                           reasoning_content=reasoning)


def _tc(name, args=None, cid="c1"):
    return {"id": cid, "function": {"name": name,
                                    "arguments": json.dumps(args or {})}}


@pytest.fixture
def engine():
    from unittest.mock import patch
    with patch("core.agents.AgentFactory.__init__", return_value=None):
        eng = PipelineEngine()
        eng.factory = MagicMock()
        eng.factory.is_native.return_value = True
        eng.factory.get_fallback_to_json.return_value = False  # stay in native
        eng.factory.get_max_retries.return_value = 1
        eng.factory.get_max_tool_turns.return_value = 4
        nat = eng.factory.get_native_agent.return_value
        nat.gateway.litellm_model = "mock"
        nat.gateway.last_usage = {}
        return eng


def _run(engine, ws, **kw):
    return engine.run_step(task_id=1, step_id="t_impl", workspace=ws,
                           project_id="default",
                           agent_config_name="task_implementer",
                           tool_schemas=TS, **kw)


def test_budget_resolved_by_role_not_step_id(engine):
    """Bug H: the turn budget must be looked up by role, not step_id."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
                            _turn(tool_calls=[_tc("finish_step")])]
    assert _run(engine, ws) is True
    called = [c.args[0] for c in engine.factory.get_max_tool_turns.call_args_list if c.args]
    assert "task_implementer" in called
    assert "t_impl" not in called


def test_no_tool_reply_is_salvaged_then_writes(engine):
    """Feature B: a prose-only reply nudges the agent to write instead of
    ending the step empty (the inst-975 failure mode)."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"written": "main.py"})
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [
        _turn(text="No changes needed."),                                   # nudge
        _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})]),
        _turn(tool_calls=[_tc("finish_step")]),
    ]
    assert _run(engine, ws) is True
    assert nat.turn.call_count == 3  # salvaged, did not stop at turn 1


def test_genuine_noop_floors_with_existing_files(engine):
    """Feature C: when the agent keeps producing no tool call until the budget
    is exhausted, floor with existing files (parity w/ JSON no-op)."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"output": "x"})
    engine.factory.get_max_tool_turns.return_value = 2
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = [_turn(text="no-op"), _turn(text="still no-op")]
    assert _run(engine, ws) is True
    assert any("README.md" in k for k in ws.written_drafts)


def test_exploration_exhaustion_still_raises(engine):
    """Floor must NOT mask a real failure: an agent that keeps exploring
    (tool calls every turn) but never writes still raises."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"output": "read result"})
    engine.factory.get_max_tool_turns.return_value = 2
    nat = engine.factory.get_native_agent.return_value
    nat.turn.return_value = _turn(tool_calls=[_tc("read_file", {"path": "README.md"})])
    with pytest.raises(MaxRetriesExceeded):
        _run(engine, ws)
    assert not ws.written_drafts  # floor did not trigger


def test_retry_continues_conversation_for_cache_reuse(engine):
    """Cache-friendly carryover: a retry CONTINUES the prior attempt's message
    list (so the cached prefix is reused) instead of rebuilding a fresh prompt.
    Verified by checking the retry's first turn already sees attempt-1's
    assistant/tool messages, and the system+initial-user prompt is unchanged."""
    tmp = Path(tempfile.mkdtemp()); _setup(tmp); ws = _WS(tmp)
    engine._exec_tool = MagicMock(return_value={"output": "read result"})
    engine.factory.get_max_tool_turns.return_value = 2
    engine.factory.get_max_retries.return_value = 2  # allow a retry

    snapshots = []  # (roles tuple, first-user-content) per turn

    def rec(messages, tools, tool_choice):
        roles = tuple(m["role"] for m in messages)
        snapshots.append((roles, messages[1]["content"] if len(messages) > 1 else None))
        # attempt 1: explore (read) both turns → no write → retry.
        # attempt 2 (4th turn overall): write, then finish.
        if len(snapshots) <= 2:
            return _turn(tool_calls=[_tc("read_file", {"path": "README.md"})])
        if len(snapshots) == 3:
            return _turn(tool_calls=[_tc("write", {"file": "main.py", "content": "x"})])
        return _turn(tool_calls=[_tc("finish_step")])

    engine._exec_tool = MagicMock(side_effect=lambda a: (
        {"written": "main.py"} if a["tool"] == "write" else {"output": "r"}))
    nat = engine.factory.get_native_agent.return_value
    nat.turn.side_effect = rec
    assert _run(engine, ws) is True

    # The retry's first turn (snapshot index 2) must have MORE messages than
    # attempt-1 turn-1 (continuation, not a 2-message rebuild) and include a
    # 'tool' role from the prior exploration.
    a1_first_roles = snapshots[0][0]
    retry_first_roles = snapshots[2][0]
    assert len(retry_first_roles) > len(a1_first_roles)
    assert "tool" in retry_first_roles
    # The cached prefix (system + initial user prompt) is byte-identical.
    assert snapshots[2][1] == snapshots[0][1]
