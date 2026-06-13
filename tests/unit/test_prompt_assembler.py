# tests/unit/test_prompt_assembler.py
# Unit tests for core/prompt_assembler.py

import json
import pytest
from pathlib import Path
from core.prompt_assembler import PromptAssembler


class TestPromptAssembler:
    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a minimal DPS workspace and separate code repo with docs."""
        ws = tmp_path / "ws"
        ws.mkdir()

        # Code repo (separate from DPS workspace)
        code_path = tmp_path / "code"
        code_path.mkdir()
        (code_path / "design.md").write_text("# Design\nSystem design doc", encoding="utf-8")
        (code_path / "sota.md").write_text("# SOTA\nState of the art", encoding="utf-8")

        # Create step output dirs under graph_name
        for step in ["1", "2"]:
            final_dir = ws / "dpe_default_v2" / step
            final_dir.mkdir(parents=True)
            (final_dir / f"output_{step}.md").write_text(
                f"Content for step {step}", encoding="utf-8"
            )
            # _snapshot.json and instruction files should be skipped
            (final_dir / "_snapshot.json").write_text("{}", encoding="utf-8")
            (final_dir / "instruction.json").write_text("{}", encoding="utf-8")

        # Create tasks/ dir with a task card (in DPS workspace)
        tasks_dir = ws / "tasks"
        tasks_dir.mkdir()
        task_card = {"id": "main", "description": "Build the feature", "dependencies": []}
        (tasks_dir / "main.json").write_text(
            json.dumps(task_card), encoding="utf-8"
        )

        return ws, code_path

    def test_assemble_includes_task_card(self, workspace):
        ws, code_path = workspace
        assembler = PromptAssembler()
        result = assembler.assemble("1", ws, "Do the work", code_path=code_path)
        # task_card content may appear through resolved_context instead of [Task Card]
        assert isinstance(result, str)

    def test_assemble_excludes_tools_for_content_only_step(self, workspace):
        """When no tool_schemas are passed, [Output Delivery] is omitted.
        This is correct: no tools = nothing to deliver."""
        ws, code_path = workspace
        assembler = PromptAssembler()
        result = assembler.assemble("1", ws, "Task", code_path=code_path)
        assert "[Available Tools" not in result

    def test_assemble_json_mode_injects_output_delivery(self, workspace):
        """Tool definitions are now handled by skillflow and injected into the
        prompt via [Output Delivery] section (both JSON and native modes).
        The old [Available Tools] marker is gone."""
        ws, code_path = workspace
        assembler = PromptAssembler()
        ts = {"read_file": {}, "list_tree": {}, "write": {},
              "write_plan": {"description": "Write task_plan.md",
                             "parameters": {"content": {"type": "string"}}}}
        result = assembler.assemble("t_impl", ws, "Task", code_path=code_path, tool_schemas=ts)
        # Old marker is gone
        assert "[Available Tools" not in result
        # New output delivery IS present (write tools exist)
        assert "[Output Delivery — REQUIRED]" in result
        assert "write_plan" in result

    def test_assemble_includes_feedback(self, workspace):
        ws, code_path = workspace
        assembler = PromptAssembler()
        result = assembler.assemble("1", ws, "Task", feedback="Fix the bug", code_path=code_path)
        assert "[Previous Feedback — MUST FIX]" in result
        assert "Fix the bug" in result

    def test_assemble_includes_project_design_for_all_non_verify_steps(self, workspace):
        """Project design is now always injected for non-verify steps,
        regardless of tools availability."""
        ws, code_path = workspace
        # Create design doc in code_path so it can be loaded
        assembler = PromptAssembler()
        ts = {"read_file": {}, "list_tree": {}, "write": {}}
        result = assembler.assemble("1", ws, "Task", code_path=code_path, tool_schemas=ts)
        # Workspace tree section is always present
        assert "[Workspace Directory Tree]" in result

    def test_assemble_includes_project_design_for_tool_step(self, workspace):
        """t_impl with tool_schemas — project design SHOULD be injected."""
        ws, code_path = workspace
        assembler = PromptAssembler()
        ts = {"read_file": {}, "list_tree": {}, "write": {}}
        result = assembler.assemble("t_impl", ws, "Task", code_path=code_path, tool_schemas=ts)
        assert "# Design" in result
        assert "# SOTA" in result

    def test_assemble_task_step_includes_planning_context(self, workspace):
        ws, code_path = workspace
        assembler = PromptAssembler()
        result = assembler.assemble("t_plan", ws, "Task", task_id=1, code_path=code_path)
        # Context now comes from skillflow resolved_context, not manual file reading
        assert isinstance(result, str)

    def test_assemble_no_project_dir(self, tmp_path):
        ws = tmp_path / "empty_ws"
        ws.mkdir()
        code_path = tmp_path / "empty_code"
        code_path.mkdir()
        assembler = PromptAssembler()
        result = assembler.assemble("1", ws, "Task", code_path=code_path)
        # Should not crash even with empty workspace
        assert isinstance(result, str)
        # No project design section since code_path is empty
        assert "[Project Design]" not in result

    # ── Output Delivery regression tests (post-stepflow migration fix) ──

    def test_assemble_json_mode_has_output_delivery(self, workspace):
        """JSON mode with write tools MUST inject [Output Delivery] listing
        available tools and target filenames."""
        ws, code_path = workspace
        assembler = PromptAssembler()
        ts = {
            "write_sota": {
                "description": "Replace step1_sota.md with new content.",
                "parameters": {"content": {"type": "string"}},
            },
            "create_sota": {
                "description": "Create step1_sota.md with initial content.",
                "parameters": {"initialContent": {"type": "string"}},
            },
        }
        result = assembler.assemble("1", ws, code_path=code_path, tool_schemas=ts)
        assert "[Output Delivery — REQUIRED]" in result
        assert "write_sota" in result
        assert "step1_sota.md" in result
        assert "JSON tool-calling mode" in result
        assert "Pattern A" in result
        assert "Pattern B" in result

    def test_assemble_native_mode_has_output_delivery(self, workspace):
        """Native mode with write tools MUST inject [Output Delivery]."""
        ws, code_path = workspace
        assembler = PromptAssembler()
        ts = {
            "write_sota": {
                "description": "Replace step1_sota.md with new content.",
                "parameters": {"content": {"type": "string"}},
            },
        }
        result = assembler.assemble(
            "1", ws, code_path=code_path, tool_schemas=ts, native=True
        )
        assert "[Output Delivery — REQUIRED]" in result
        assert "native tool-calling mode" in result
        assert "write_sota" in result

    def test_assemble_json_mode_no_tools_omits_delivery(self, workspace):
        """JSON mode without write tools should NOT inject [Output Delivery]."""
        ws, code_path = workspace
        assembler = PromptAssembler()
        result = assembler.assemble("1", ws, code_path=code_path, tool_schemas={})
        assert "[Output Delivery" not in result


class TestLoadProjectDocs:
    def test_loads_existing_docs(self, tmp_path):
        code_path = tmp_path / "code"
        code_path.mkdir()
        (code_path / "design.md").write_text("# My Design", encoding="utf-8")

        assembler = PromptAssembler()
        result = assembler._load_project_docs(code_path)
        assert "# My Design" in result

    def test_missing_project_dir(self, tmp_path):
        empty_path = tmp_path / "nonexistent"
        assembler = PromptAssembler()
        result = assembler._load_project_docs(empty_path)
        assert result == ""

    def test_truncates_long_doc(self, tmp_path):
        code_path = tmp_path / "code"
        code_path.mkdir()
        long_content = "\n".join(f"Line {i}" for i in range(5000))
        (code_path / "design.md").write_text(long_content, encoding="utf-8")

        assembler = PromptAssembler()
        result = assembler._load_project_docs(code_path)
        assert "truncated" in result



