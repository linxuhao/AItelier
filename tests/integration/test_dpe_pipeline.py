# tests/integration/test_dpe_pipeline.py
# Pipeline integration tests with mocked agents.
# New flow: Green writes via sf.execute_tool() → Apply → Build → done.
# Commit (Draft→Final) is handled by skillflow draft_commit tool node.
# Validation is handled by skillflow confirm_step().
# Red review is handled by skillflow _review nodes (separate step).

import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from core.dpe_pipeline import PipelineEngine, MaxRetriesExceeded

TS = {"read_file": {}, "list_tree": {}, "write": {}}
TS_NO_TOOLS = {}
TS_CONSTRAINED = {"write_plan": {}, "write_subtask_manifest": {}, "write_subtask_card": {}}
TS_RW = {  # read+write: simulates researcher/architect/PM steps
    "read_file": {"description": "Read a file", "parameters": {"path": {"type": "string"}}},
    "list_tree": {"description": "List directory tree", "parameters": {"path": {"type": "string"}}},
    "web_search": {"description": "Search the web", "parameters": {"query": {"type": "string"}}},
    "write_sota": {
        "description": "Replace step1_sota.md with new content.",
        "parameters": {"content": {"type": "string"}},
    },
    "create_sota": {
        "description": "Create step1_sota.md with initial content.",
        "parameters": {"initialContent": {"type": "string"}},
    },
    "append_sota": {
        "description": "Append content to step1_sota.md.",
        "parameters": {"content": {"type": "string"}},
    },
}


class MockWorkspace:
    def __init__(self, tmp_path: Path = None):
        self.base_path = tmp_path or Path("/tmp/mock_workspace")
        self.projects_base = self.base_path / "projects"
        self.written_drafts = {}
        self.committed = False
        self.applied = False
        self.rollback_calls = []

    def _get_secure_path(self, project_id: str) -> Path:
        return self.base_path / project_id

    def get_code_path(self, project_id: str) -> Path:
        code_path = self.projects_base / project_id
        code_path.mkdir(parents=True, exist_ok=True)
        return code_path

    def write_draft(self, project_id: str, step_id: str, filename: str, content: str,
                    graph_name: str = None):
        self.written_drafts[f"{step_id}/{filename}"] = content

    def get_final_path(self, project_id: str, step_id: str) -> Path:
        return self.base_path / project_id / step_id

    def _get_git_hash(self, project_path: Path) -> str:
        return "pre_apply_fake_hash"

    def rollback(self, project_id: str, commit_hash: str) -> bool:
        self.rollback_calls.append(commit_hash)
        return True


@pytest.fixture
def engine():
    with patch("core.agents.AgentFactory.__init__", return_value=None):
        engine = PipelineEngine()
        engine.factory = MagicMock()
        engine.factory.get_max_retries.return_value = 3
        engine.factory.get_max_tool_turns.return_value = 5
        return engine


def _setup_workspace(tmp_path, project_id="default", step_id="t_impl"):
    project_path = tmp_path / project_id
    project_path.mkdir(parents=True, exist_ok=True)
    draft_dir = project_path / f"{step_id}.tmp"
    draft_dir.mkdir(parents=True, exist_ok=True)
    code_path = tmp_path / "projects" / project_id
    code_path.mkdir(parents=True, exist_ok=True)
    (code_path / "README.md").write_text("# Old Project\nExisting code.\ndef old(): pass\n")
    return project_path


def _make_build_result(passed: bool, summary: str = "") -> dict:
    return {
        "passed": passed,
        "checks": [{"name": "python_compile", "passed": passed, "output": summary or "ok"}],
        "summary": summary or ("Build: OK" if passed else "Build: FAILED"),
    }


def _mock_green(response):
    mg = MagicMock()
    mg.gateway.litellm_model = "mock-model"
    mg.run.return_value = response
    return mg


# ════════════════════════════════════════════════════════════════════════
# Happy path: agent writes files → done.
# Validation, commit, apply, build are skillflow tool nodes.
# ════════════════════════════════════════════════════════════════════════

def test_pipeline_happy_path(engine):
    import tempfile
    tmp_path = Path(tempfile.mkdtemp())
    mock_workspace = MockWorkspace(tmp_path)
    _setup_workspace(tmp_path)

    engine.factory.get_agent.return_value = _mock_green(json.dumps({
        "thoughts": "Writing the add function.",
        "actions": [{"tool": "write", "params": {"file": "main.py", "content": "def add(a, b): return a + b"}}]
    }))

    result = engine.run_step(task_id=101, step_id="t_impl", workspace=mock_workspace,
                             project_id="default", agent_config_name="task_implementer",
                             tool_schemas=TS)

    assert result is True


# ════════════════════════════════════════════════════════════════════════
# Multi-turn: explore first, then write
# ════════════════════════════════════════════════════════════════════════

def test_pipeline_tool_exploration_then_write(engine):
    import tempfile
    tmp_path = Path(tempfile.mkdtemp())
    mock_workspace = MockWorkspace(tmp_path)
    _setup_workspace(tmp_path)

    mg = MagicMock()
    mg.gateway.litellm_model = "mock-model"
    mg.run.side_effect = [
        json.dumps({
            "thoughts": "Let me explore first",
            "actions": [
                {"tool": "list_tree", "params": {"path": "src", "depth": 2}},
                {"tool": "read_file", "params": {"path": "src/main.py"}}
            ]
        }),
        json.dumps({
            "thoughts": "Now writing the fix",
            "actions": [{"tool": "write", "params": {"file": "src/main.py", "content": "def fixed(): pass"}}]
        })
    ]

    engine.factory.get_agent.return_value = mg

    result = engine.run_step(task_id=103, step_id="t_impl", workspace=mock_workspace,
                             project_id="default", agent_config_name="task_implementer",
                             tool_schemas=TS)

    assert result is True
    assert mg.run.call_count == 2


# ════════════════════════════════════════════════════════════════════════
# Content-mode step (files dict)
# ════════════════════════════════════════════════════════════════════════

def test_pipeline_content_step_actions_format(engine):
    """Content step with 'actions' format (primary path)."""
    import tempfile
    tmp_path = Path(tempfile.mkdtemp())
    mock_workspace = MockWorkspace(tmp_path)
    _setup_workspace(tmp_path, step_id="3")

    engine._exec_tool = MagicMock(return_value={"written": "tasks_manifest.json"})

    engine.factory.get_agent.return_value = _mock_green(json.dumps({
        "thoughts": "Decomposing into tasks",
        "actions": [
            {"tool": "write_tasks_manifest", "params": {"content": '{"total": 2}'}},
            {"tool": "write_task_card", "params": {"content": '{"id": "task_1"}'}},
            # Step 3 accumulates files across turns; an explicit end_step signals
            # the decomposition is complete (otherwise it loops until max turns).
            {"tool": "end_step", "params": {"summary": "decomposition complete"}},
        ]
    }))

    # Step 3 (PM) is constrained to its own write tools.
    result = engine.run_step(task_id=102, step_id="3", workspace=mock_workspace,
                             project_id="default", agent_config_name="pm",
                             tool_schemas={"write_tasks_manifest": {}, "write_task_card": {}})

    assert result is True
    # Each write_* action is dispatched through _exec_tool.
    assert engine._exec_tool.call_count == 2


def test_pipeline_content_step_files_fallback(engine):
    """Content step with legacy 'files' format — fallback converts to write actions."""
    import tempfile
    tmp_path = Path(tempfile.mkdtemp())
    mock_workspace = MockWorkspace(tmp_path)
    _setup_workspace(tmp_path, step_id="3")

    engine._exec_tool = MagicMock(return_value={"written": "some_file.json"})
    # The legacy 'files' shape is a JSON-mode fallback (non-tool-calling models),
    # so exercise the JSON dispatch path rather than native tool-calling.
    engine.factory.is_native.return_value = False

    engine.factory.get_agent.return_value = _mock_green(json.dumps({
        "thoughts": "Decomposing into subtasks",
        "files": {
            "subtasks_manifest.json": '{"total": 2}',
            "subtasks/task_1.json": '{"id": "task_1"}'
        }
    }))

    result = engine.run_step(task_id=103, step_id="3", workspace=mock_workspace,
                             project_id="default", agent_config_name="pm",
                             tool_schemas={"write_tasks_manifest": {}, "write_task_card": {}})

    assert result is True
    # Each file in the legacy dict is converted to a generic write action and
    # dispatched through _exec_tool.
    assert engine._exec_tool.call_count == 2


# ════════════════════════════════════════════════════════════════════════
# No write → MaxRetriesExceeded
# ════════════════════════════════════════════════════════════════════════

class TestToolStepEmptyWrittenFiles:
    def test_no_write_raises_max_retries(self, engine):
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        mock_workspace = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path, project_id="empty_write_test", step_id="t_impl")

        engine._exec_tool = MagicMock(return_value={"output": "mock result"})

        engine.factory.get_agent.return_value = _mock_green(json.dumps({
            "thoughts": "Let me explore",
            "actions": [
                {"tool": "list_tree", "params": {"path": ".", "depth": 1}},
                {"tool": "read_file", "params": {"path": "README.md"}}
            ]
        }))
        engine.factory.get_max_tool_turns.return_value = 3
        engine.factory.get_max_retries.return_value = 2

        with pytest.raises(MaxRetriesExceeded) as exc_info:
            engine.run_step(task_id=999, step_id="t_impl", workspace=mock_workspace,
                            project_id="empty_write_test", agent_config_name="task_implementer",
                            tool_schemas=TS)

        assert "without producing any write actions" in str(exc_info.value)


# ── JSON extraction tests ─────────────────────────────────────────────

class TestExtractJsonMultipleObjects:
    def test_extract_json_single_object(self, engine):
        result = engine._extract_json('{"thoughts": "hello", "actions": []}', try_multiple=True)
        assert result == {"thoughts": "hello", "actions": []}

    def test_extract_json_prefers_files_key(self, engine):
        text = '{"thoughts": "hello", "actions": []}\n{"files": {"design.md": "# Architecture"}}'
        result = engine._extract_json(text, try_multiple=True)
        assert result["files"] == {"design.md": "# Architecture"}

    def test_extract_json_with_code_fences(self, engine):
        text = '```json\n{"files": {"design.md": "# Arch"}, "thoughts": "hello"}\n```'
        result = engine._extract_json(text, try_multiple=True)
        assert result["files"] == {"design.md": "# Arch"}


class TestPreWriteJsonRepair:
    def test_valid_json_unchanged(self, engine):
        assert engine._ensure_valid_json_content("file.json", '{"key": "value"}') == '{"key": "value"}'

    def test_non_json_file_unchanged(self, engine):
        assert engine._ensure_valid_json_content("file.md", "# heading\nsome text") == "# heading\nsome text"

    def test_repairs_single_quote_escapes(self, engine):
        result = engine._ensure_valid_json_content("manifest.json", r'{"desc": "Type \'exit\' to quit."}')
        assert json.loads(result)["desc"] == "Type 'exit' to quit."

    def test_repairs_trailing_commas(self, engine):
        result = engine._ensure_valid_json_content("data.json", '{"items": [1, 2,]}')
        assert json.loads(result) == {"items": [1, 2]}

    def test_unrepairable_returns_original(self, engine):
        content = "{totally broken!!!"
        assert engine._ensure_valid_json_content("file.json", content) == content


# ════════════════════════════════════════════════════════════════════════
# _make_feedback_example — step-aware examples (no more hardcoded
# task_verify_report.json for non-verifier steps)
# ════════════════════════════════════════════════════════════════════════

class TestMakeFeedbackExample:
    def test_uses_tool_schemas_not_hardcoded(self, engine):
        """Example should reference the step's actual output files."""
        engine._tool_schemas = {
            "write_sota": {
                "description": "Replace step1_sota.md with new content.",
                "parameters": {"content": {"type": "string"}},
            },
        }
        result = engine._make_feedback_example()
        assert "step1_sota.md" in result
        assert "task_verify_report.json" not in result

    def test_fallback_when_no_tools(self, engine):
        """Empty tool_schemas → generic fallback, no crash."""
        engine._tool_schemas = {}
        result = engine._make_feedback_example()
        assert "output.md" in result
        assert isinstance(result, str)

    def test_multiple_files_listed(self, engine):
        """Multiple write tools → multiple files in example."""
        engine._tool_schemas = {
            "write_sota": {
                "description": "Replace step1_sota.md with new content.",
                "parameters": {"content": {"type": "string"}},
            },
            "write_design": {
                "description": "Replace step2_design.md with new content.",
                "parameters": {"content": {"type": "string"}},
            },
        }
        result = engine._make_feedback_example()
        assert "step1_sota.md" in result
        assert "step2_design.md" in result


# ════════════════════════════════════════════════════════════════════════
# Bare-key normalizer — {"filename.md": "content"} → files dict
# (the exact pattern that was failing in production)
# ════════════════════════════════════════════════════════════════════════

class TestBareKeyNormalizer:
    """Tests for the output normalizer that converts LLM bare-key responses
    like {"step1_sota.md": "# Title"} into standard {"files": {...}} shape."""

    def test_bare_key_e2e_written_via_files(self, engine):
        """LLM outputs {"step1_sota.md": "# Report"} — normalizer wraps in
        files dict, then files handler writes via write_draft."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        mock_workspace = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path, step_id="1")

        engine.factory.get_agent.return_value = _mock_green(json.dumps({
            "step1_sota.md": "# SOTA Technical Research Report\n\n## Edge Cases\n- Edge case 1",
        }))

        result = engine.run_step(task_id=0, step_id="1", workspace=mock_workspace,
                                 project_id="default", agent_config_name="researcher",
                                 tool_schemas=TS_RW)

        assert result is True
        # File was written via write_draft (not exec_tool)
        assert "1/step1_sota.md" in mock_workspace.written_drafts
        content = mock_workspace.written_drafts["1/step1_sota.md"]
        assert "SOTA Technical Research Report" in content

    def test_bare_key_with_thoughts(self, engine):
        """Payload has thoughts + bare filename keys."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        mock_workspace = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path, step_id="1")

        engine.factory.get_agent.return_value = _mock_green(json.dumps({
            "thoughts": "Research complete.",
            "step1_sota.md": "# SOTA Report",
        }))

        result = engine.run_step(task_id=0, step_id="1", workspace=mock_workspace,
                                 project_id="default", agent_config_name="researcher",
                                 tool_schemas=TS_RW)

        assert result is True
        assert "1/step1_sota.md" in mock_workspace.written_drafts


# ════════════════════════════════════════════════════════════════════════
# Unknown write tool → helpful feedback (not silent ignore)
# ════════════════════════════════════════════════════════════════════════

class TestUnknownWriteToolFeedback:
    """When the LLM invents a tool name (e.g. 'write_file' instead of
    'write_sota'), the step must NOT silently ignore it. It must feed back
    the correct tool names so the LLM can recover."""

    def test_unknown_write_triggers_retry_not_silent(self, engine):
        """LLM uses 'write_file' — should get feedback listing allowed tools,
        then retry (not silently proceed with zero writes)."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp())
        mock_workspace = MockWorkspace(tmp_path)
        _setup_workspace(tmp_path, step_id="1")

        engine.factory.get_max_tool_turns.return_value = 5
        engine.factory.get_max_retries.return_value = 2

        # Mock _exec_tool: return "written" for write tools, "output" for reads
        def _mock_exec(action):
            tool = action.get("tool", "")
            if tool.startswith("write"):
                return {"written": "step1_sota.md"}
            return {"output": "search result"}
        engine._exec_tool = MagicMock(side_effect=_mock_exec)

        # Agent: first turn uses wrong tool, second turn corrects
        mg = MagicMock()
        mg.gateway.litellm_model = "mock-model"
        mg.run.side_effect = [
            # Turn 1: uses "write_file" (invented) — should get feedback
            json.dumps({
                "thoughts": "Writing the SOTA report",
                "actions": [
                    {"tool": "write_file",
                     "params": {"path": "step1_sota.md",
                                "content": "# SOTA Report"}}
                ],
            }),
            # Turn 2: after feedback, uses correct tool
            json.dumps({
                "thoughts": "Using correct tool now",
                "actions": [
                    {"tool": "write_sota",
                     "params": {"content": "# SOTA Report"}}
                ],
            }),
        ]
        engine.factory.get_agent.return_value = mg

        result = engine.run_step(task_id=0, step_id="1", workspace=mock_workspace,
                                 project_id="default", agent_config_name="researcher",
                                 tool_schemas=TS_RW)

        assert result is True
        # exec_tool should have been called for write_sota (turn 2)
        write_calls = [
            c for c in engine._exec_tool.call_args_list
            if c[0][0].get("tool", "").startswith("write")
        ]
        assert len(write_calls) >= 1
        assert write_calls[0][0][0]["tool"] == "write_sota"
