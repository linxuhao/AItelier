"""Host identity in tool dispatch belongs to the run, never to a caller or a graph literal.

SkillFlow injects ``run_id``/``step_id``/``project_id`` (and the roots) with
``setdefault`` AFTER the caller's params or the graph's literal params, so a
value placed there first would be what an executor-routed tool binds against.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import skillflow as skillflow_package
from skillflow import SkillFlow
from skillflow.exceptions import GraphValidationError, ToolArgumentsUnavailable
from skillflow.graph import PipelineGraph, StepNode, Transition

from core.skillflow_host import AItelierSkillFlow
from core.tool_routes import (AItelierToolLoader, EXECUTOR_TOOLS,
                              graph_identity_violations, spoofed_slot_keys)

ROOT = Path(__file__).resolve().parents[2]
SPOOF = dict.fromkeys(["run_id", "step_id", "project_id", "operation_id",
                       "config_name", "project_root", "workspace_root"], "foreign")


@pytest.mark.parametrize("tool", sorted(EXECUTOR_TOOLS))
def test_agent_ingress_replaces_caller_identity_for_every_executor_tool(monkeypatch, tool):
    host = object.__new__(AItelierSkillFlow)
    monkeypatch.setattr(host, "_tool_accepts_keyword", lambda *a: False)
    captured = {}
    def execute(self, name, params, **kwargs):
        captured.update(params=params, host=kwargs)
        return {}
    monkeypatch.setattr(SkillFlow, "_execute_tool_impl", execute)
    host._execute_tool_impl(tool, {"file": "t.py", "out_dir": "/owned/out", **SPOOF},
                            run_id="owned-run", step_id="s", project_root="/owned")
    assert captured["params"] == {"file": "t.py", "out_dir": "/owned/out"}
    assert captured["host"] == {"run_id": "owned-run", "step_id": "s", "project_root": "/owned"}


def test_agent_ingress_leaves_other_tools_params_alone(monkeypatch):
    host = object.__new__(AItelierSkillFlow)
    monkeypatch.setattr(host, "_tool_accepts_keyword", lambda *a: False)
    captured = {}
    monkeypatch.setattr(SkillFlow, "_execute_tool_impl",
                        lambda self, name, params, **k: captured.update(params=params) or {})
    host._execute_tool_impl("read_file", {"path": "a", "workspace_root": "/w"}, run_id="r")
    assert captured["params"] == {"path": "a", "workspace_root": "/w"}


@pytest.mark.parametrize("tool,params,bad", [
    ("repo_apply", {"run_id": "x", "message": "m"}, ["run_id"]),
    ("notify", {"project_id": "p", "step_id": "s"}, ["step_id", "project_id"]),
    ("anything", {"operation_id": 4}, ["operation_id"]),
    ("run_tests", {"project_root": "/somebody/else"}, ["project_root"]),
    ("pytest", {"workspace_root": "$PROJECT_ROOT/../x"}, ["workspace_root"]),
    ("focused_check", {"project_root": "relative"}, ["project_root"]),
    ("lint", {"workspace_root": "/foreign"}, ["workspace_root"]),
    ("lint", {"project_root": "$PROJECT_ROOT"}, []),
    ("run_tests", {"project_root": "$PROJECT_ROOT", "out_dir": "$STEP_DIR"}, []),
    ("pytest", {"workspace_root": "$CONFIG_DIR/test"}, []),
    ("run_tests", {"project_root": ""}, []),
    ("repo_apply", {"project_root": "/literal/ok/for/non-executor"}, []),
    ("notify", {"payload": {"run_id": "business data", "project_id": "x"}}, []),
])
def test_slot_rule_examines_top_level_dispatch_keys_only(tool, params, bad):
    assert spoofed_slot_keys(tool, params) == bad


def _graph(**step):
    base = dict(id="s", step_type="tool", tool_name="repo_apply",
                transitions=[Transition(to=None)])
    base.update(step)
    return PipelineGraph(name="identity_probe", begin="s", steps=[StepNode(**base)])


@pytest.mark.parametrize("step", [
    {"tool_params": {"run_id": "foreign"}},
    {"step_type": "agent", "tool_name": None, "agent_config": "a",
     "validation": [{"tool": "pytest", "files": ["t.py"], "run_id": "foreign"}]},
    {"step_type": "agent", "tool_name": None, "agent_config": "a",
     "validation": [{"tool": "pytest", "files": ["t.py"], "workspace_root": "/foreign/repo"}]},
    {"step_type": "agent", "tool_name": None, "agent_config": "a",
     "lifecycle": {"on_deliver": {"tool": "repo_apply", "params": {"project_id": "x"}}}},
    {"step_type": "agent", "tool_name": None, "agent_config": "a",
     "lifecycle": {"on_deliver": [{"tool": "repo_apply", "params": {}},
                                  {"tool": "run_tests", "params": {"run_id": "x"}}]}},
    {"step_type": "agent", "tool_name": None, "agent_config": "a",
     "lifecycle": {"after_deliver": [{"tool": "pytest", "files": ["t.py"], "step_id": "x"}]}},
])
def test_register_graph_refuses_identity_in_every_dispatch_slot(step):
    sf = AItelierSkillFlow(":memory:")
    sf.agent_registry.register("a", model="flash", tools=[])
    graph = _graph(**step)
    assert graph_identity_violations(graph)
    with pytest.raises(GraphValidationError):
        sf.register_graph(graph)
    assert "identity_probe" not in sf._graphs


def test_register_graph_keeps_resolver_root_syntax_and_business_payloads():
    sf = AItelierSkillFlow(":memory:")
    sf.register_graph(_graph(tool_name="run_tests",
                             tool_params={"project_root": "$PROJECT_ROOT", "out_dir": "$STEP_DIR",
                                          "payload": {"run_id": "data"}}))
    assert "identity_probe" in sf._graphs


def test_every_bundled_config_and_addon_has_no_identity_in_a_dispatch_slot():
    configs = sorted((ROOT / "configs").rglob("*.yaml"))
    assert configs
    checked = 0
    for path in configs:
        try:
            graph = PipelineGraph.from_yaml(path)
        except Exception:                                        # noqa: BLE001
            continue            # addon overlays are not standalone graphs
        assert graph_identity_violations(graph) == [], path
        checked += 1
    assert checked >= 10


def test_generated_registration_skips_a_spoofing_file_before_the_transaction():
    from core.pipeline_registry import _validated_registration
    yaml_text = ("name: x\nbegin: s\nsteps:\n- id: s\n  step_type: tool\n"
                 "  tool_name: run_tests\n  tool_params: {run_id: foreign}\n")
    with pytest.raises(ValueError, match="run_id"):
        _validated_registration("gen_identity_probe", yaml_text)


def _engine(tmp_path):
    loader = AItelierToolLoader(Path(skillflow_package.__file__).parent / "tools")
    loader.add_tools_dir(ROOT / "aitelier" / "tools")
    return AItelierSkillFlow(str(tmp_path / "sf.db"), tool_loader=loader,
                             workspace_base=str(tmp_path / "ws"),
                             projects_base=str(tmp_path / "projects"))


def test_dispatch_refuses_a_pinned_inline_literal_that_bypassed_registration(tmp_path, monkeypatch):
    """A run pinned to content registered before the rule existed is refused at dispatch."""
    sf = _engine(tmp_path)
    graph = _graph(tool_params={"run_id": "foreign"})
    SkillFlow.register_graph(sf, graph)                      # the pre-rule registration
    node = graph.steps[0]
    monkeypatch.setattr(sf._tool_loader, "load_fn", lambda n: pytest.fail("tool loaded"))
    with pytest.raises(ToolArgumentsUnavailable, match="run_id"):
        sf._execute_tool_inline(node, run_id="r", graph_name="identity_probe")


def test_dispatch_refuses_a_spoofed_hook_without_loading_the_tool(tmp_path, monkeypatch):
    sf = _engine(tmp_path)
    monkeypatch.setattr(sf._tool_loader, "load_fn", lambda n: pytest.fail("tool loaded"))
    out = sf._execute_tool_hook(None, None, "on_deliver",
                                {"tool": "run_tests", "params": {"run_id": "foreign"}})
    assert out["passed"] is False and "run_id" in out["error"]
