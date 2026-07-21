"""Unit tests for the forge_registry_check convention linters — the gate that
turns behaviorally-wrong-but-structurally-valid generated graphs into a gate
failure (which the emit feedback loop then self-repairs)."""

import importlib.util
from pathlib import Path

import pytest

# Load the tool impl directly (it lives in a tool dir, not an importable package).
_IMPL = Path(__file__).resolve().parents[2] / "aitelier/tools/forge_registry_check/impl.py"
_spec = importlib.util.spec_from_file_location("forge_registry_check_impl", _IMPL)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
forge_registry_check = _mod.forge_registry_check


@pytest.fixture(autouse=True)
def _stub_live_tools(monkeypatch):
    # The reviewer-reads-maker check is pure graph analysis; stub the live
    # registry so the test needs no running app.
    monkeypatch.setattr(_mod, "_live_tools", lambda: {"web_search", "write"})


def _write(tmp_path, graph):
    import yaml
    p = tmp_path / "g.yaml"
    p.write_text(yaml.safe_dump(graph), encoding="utf-8")
    return str(p)


def _maker_reviewer(reviewer_reads_maker: bool):
    reviewer_ctx = [{"source": {"config": "g", "output": "task.md"}}]
    if reviewer_reads_maker:
        reviewer_ctx.append({"source": {"step": "draft"}})
    return {
        "name": "g", "description": "x", "begin": "draft",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": [
            {"id": "draft", "step_type": "agent", "agent_config": "d",
             "transitions": [{"to": "review"}]},
            {"id": "review", "step_type": "agent", "agent_config": "r",
             "context": reviewer_ctx,
             "transitions": [
                 {"to": "done", "match": {"from_file": "v.json", "field": "passed", "value": True}},
                 {"to": "draft", "match": {"from_file": "v.json", "field": "passed", "value": False},
                  "max_loop": 3}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }


def test_reviewer_that_ignores_its_maker_is_flagged(tmp_path):
    res = forge_registry_check(graph_path=_write(tmp_path, _maker_reviewer(False)))
    assert res["passed"] is False
    assert any("judges blind" in v for v in res["violations"])
    # the actionable `error` (fed back to the emitter) names the fix
    assert "step: draft" in res["error"]


def test_reviewer_that_reads_its_maker_passes(tmp_path):
    res = forge_registry_check(graph_path=_write(tmp_path, _maker_reviewer(True)))
    assert res["passed"] is True
    assert res["error"] == ""


def test_error_field_summarizes_violations_for_feedback(tmp_path):
    # A hallucinated tool → the error field must carry the reason (the tool-gate
    # feedback path injects only tool_result["error"]).
    bad = _maker_reviewer(True)
    bad["steps"].insert(1, {"id": "fetch", "step_type": "tool",
                            "tool_name": "totally_not_a_real_tool",
                            "transitions": [{"to": "review"}]})
    res = forge_registry_check(graph_path=_write(tmp_path, bad))
    assert res["passed"] is False
    assert "totally_not_a_real_tool" in res["error"]
