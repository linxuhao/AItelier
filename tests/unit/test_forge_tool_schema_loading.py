"""A catalog entry is not proof that a generated tool schema can load."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from skillflow.graph import PipelineGraph
from skillflow.tool_loader import ToolLoader

from aitelier.gate_report import GATE_REPORT_FILE
from aitelier.tools.forge_registry_check.impl import forge_registry_check

ROOT = Path(__file__).resolve().parents[2]
TOOL = "generated_schema_probe"
DESCRIPTION = "Generated check: reject incomplete input"


def _owned_loader(tmp_path, monkeypatch, description, schema=None):
    tools = tmp_path / "owned_tools"
    tool_dir = tools / TOOL
    tool_dir.mkdir(parents=True)
    text = schema if schema is not None else (
        f"name: {TOOL}\ndescription: {description}\nparameters: {{}}\nx-fallible: true\n")
    (tool_dir / "tool.yaml").write_text(text, encoding="utf-8")
    # Even importing this file would fail: the registry gate must read schemas only.
    (tool_dir / "impl.py").write_text(
        'raise AssertionError("generated tool body must never be imported")\n', encoding="utf-8")
    loader = ToolLoader(tools)
    import api.dependencies
    monkeypatch.setattr(api.dependencies, "get_skillflow", lambda: SimpleNamespace(
        _tool_loader=loader, capabilities=lambda: []))
    assert TOOL in loader.list_tools()
    return loader


def _complete_graph(reference="tool", tool=TOOL):
    graph = {
        "name": "schema_probe", "description": "Bounded schema acceptance check",
        "begin": "invoke", "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"},
            {"type": "node_reached", "node": "failed", "result": "failed"}]},
        "steps": [
            {"id": "invoke", "step_type": "tool", "tool_name": tool,
             "tool_error": "route", "transitions": [
                 {"to": "deliver", "match": {"passed": True}},
                 {"to": "failed", "match": {"passed": False}}]},
            {"id": "deliver", "step_type": "agent", "agent_config": "maker",
             "output": {"mode": "content", "fixed": {"result": "result.md"}},
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
            {"id": "failed", "step_type": "gate", "transitions": [{"to": None}]},
        ]}
    roles = {"maker": {"model": "host"}}
    if reference != "tool":
        graph["begin"] = "deliver"
        graph["steps"].pop(0)
        if reference == "context":
            graph["steps"][0]["context"] = [{"source": {"tool": tool}}]
        elif reference == "role":
            roles["maker"]["tools"] = [tool]
        elif reference == "validation":
            graph["steps"][0]["validation"] = [{"tool": tool}]
    PipelineGraph._from_dict(graph)  # complete engine-parsable graph, without a run
    return graph, roles


def _gate(tmp_path, reference="tool", tool=TOOL):
    graph, roles = _complete_graph(reference, tool)
    graph_path, role_path = tmp_path / "graph.yaml", tmp_path / "roles.yaml"
    graph_path.write_text(yaml.safe_dump(graph), encoding="utf-8")
    role_path.write_text(yaml.safe_dump(roles), encoding="utf-8")
    out = tmp_path / "gate"
    result = forge_registry_check(graph_path=str(graph_path), role_table=str(role_path),
                                  out_dir=str(out))
    print(json.dumps({"reference": reference, "tool": tool, "result": result}, sort_keys=True))
    return result, (out / GATE_REPORT_FILE).read_text(encoding="utf-8")


def _repair_target(result):
    graph = yaml.safe_load((ROOT / "configs/pipeline_forge.yaml").read_text())
    step = next(s for s in graph["steps"] if s["id"] == "v_registry")
    assert step["tool_error"] == "route"
    edges = [t for t in step["transitions"]
             if t.get("match", {}).get("failure_class") == result["failure_class"]]
    assert len(edges) == 1
    assert edges[0]["feedback"] is True
    return edges[0]["to"]


@pytest.mark.parametrize("reference", ["tool", "context", "role", "validation"])
def test_real_malformed_referenced_schema_is_refused(tmp_path, monkeypatch, reference):
    loader = _owned_loader(tmp_path, monkeypatch, DESCRIPTION)
    with pytest.raises(yaml.scanner.ScannerError) as exc:
        loader.load_schema(TOOL)
    exact_error = f"{type(exc.value).__name__}: {exc.value}"
    print(json.dumps({"listed": loader.list_tools(), "schema_error": exact_error}))
    result, report = _gate(tmp_path, reference)
    assert result["passed"] is False, "BASELINE BUG: complete graph accepted unloadable schema"
    assert TOOL in result["error"] and exact_error in result["error"]
    assert "tool.yaml" in result["error"] and "rebuild" in result["error"].lower()
    assert exact_error in report
    assert result["failure_class"] == "unknown_tool"
    assert _repair_target(result) == "architect"
    assert all(fn is None for _, fn in loader._cache.values())


@pytest.mark.parametrize("reference", ["tool", "context", "role", "validation"])
def test_quoted_healthy_reference_is_accepted(tmp_path, monkeypatch, reference):
    loader = _owned_loader(tmp_path, monkeypatch, json.dumps(DESCRIPTION))
    assert loader.load_schema(TOOL)["description"] == DESCRIPTION
    result, report = _gate(tmp_path, reference)
    assert result["passed"] is True, result
    assert result["error"] == "" and result["failure_class"] == ""
    assert "PASSED" in report
    assert all(fn is None for _, fn in loader._cache.values())


@pytest.mark.parametrize("reference", ["tool", "context", "role", "validation"])
def test_missing_reference_is_refused(tmp_path, monkeypatch, reference):
    _owned_loader(tmp_path, monkeypatch, json.dumps(DESCRIPTION))
    result, _ = _gate(tmp_path, reference, "missing_generated_tool")
    assert result["passed"] is False
    assert "missing_generated_tool" in result["error"]
    # Invented role grants are fixed in emit_graph; actual broken tool
    # schemas and other missing references take the existing rebuild route.
    target = "emit_graph" if reference == "role" else "architect"
    assert _repair_target(result) == target


@pytest.mark.parametrize("schema", ["null\n", "- invalid\n", "a scalar\n"])
def test_non_mapping_schema_is_refused(tmp_path, monkeypatch, schema):
    _owned_loader(tmp_path, monkeypatch, "", schema=schema)
    result, _ = _gate(tmp_path)
    assert result["passed"] is False
    assert TOOL in result["error"] and "mapping" in result["error"]
    assert _repair_target(result) == "architect"


def test_unreferenced_malformed_tool_does_not_block_healthy_graph(tmp_path, monkeypatch):
    loader = _owned_loader(tmp_path, monkeypatch, DESCRIPTION)
    healthy_dir = tmp_path / "owned_tools" / "healthy_generated_tool"
    healthy_dir.mkdir()
    (healthy_dir / "tool.yaml").write_text(
        'name: healthy_generated_tool\ndescription: "Healthy check: succeeds"\n'
        'parameters: {}\nx-fallible: true\n', encoding="utf-8")
    assert TOOL in loader.list_tools()
    result, _ = _gate(tmp_path, tool="healthy_generated_tool")
    assert result["passed"] is True, result
    assert all(fn is None for _, fn in loader._cache.values())
