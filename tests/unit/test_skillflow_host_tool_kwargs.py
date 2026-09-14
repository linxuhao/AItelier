"""Compatibility of host-owned tool kwargs with SkillFlow 1.5.77."""

from __future__ import annotations

from pathlib import Path

from skillflow.tool_loader import ToolLoader

from core.skillflow_host import AItelierSkillFlow


def _write_tool(root: Path, name: str, implementation: str) -> None:
    tool = root / name
    tool.mkdir()
    (tool / "tool.yaml").write_text(
        f"name: {name}\ndescription: test\nparameters: {{type: object}}\n",
        encoding="utf-8",
    )
    (tool / "impl.py").write_text(implementation, encoding="utf-8")


def _host(loader: ToolLoader) -> AItelierSkillFlow:
    host = object.__new__(AItelierSkillFlow)
    host._tool_loader = loader
    host._workspace = None
    host._step_scoped_names = set()
    host._step_tool_fns = {}
    host._get_project_id = lambda _run_id: "owned-project"
    return host


def test_host_identity_is_only_bound_when_tool_accepts_it(tmp_path: Path) -> None:
    _write_tool(
        tmp_path,
        "native_read",
        "def native_read():\n    return {'ok': True}\n",
    )
    _write_tool(
        tmp_path,
        "forge_tool",
        "def forge_tool(project_id=''):\n    return {'project_id': project_id}\n",
    )
    loader = ToolLoader(tmp_path)
    host = _host(loader)

    native_result = host._execute_tool_impl("native_read", {}, run_id="run-1")
    forge_result = host._execute_tool_impl("forge_tool", {}, run_id="run-1")

    assert native_result == {"ok": True}
    assert forge_result == {"project_id": "owned-project"}
