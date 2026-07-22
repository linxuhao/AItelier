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


def _fanout(agg_scope=None):
    """A graph: make → loop → [verify] → make, then aggregate reads verify."""
    agg_src = {"step": "verify"}
    if agg_scope:
        agg_src["scope"] = agg_scope
    return {
        "name": "g", "description": "x", "begin": "make",
        "end_conditions": {"combinator": "or", "conditions": [
            {"type": "node_reached", "node": "done", "result": "completed"}]},
        "steps": [
            {"id": "make", "step_type": "agent", "agent_config": "m",
             "transitions": [{"to": "loop"}]},
            {"id": "loop", "step_type": "loop",
             "loop": {"source": {"step": "make", "file": "m.json", "field": "execution_order"},
                      "item_as": "item", "max_iterations": 5},
             "transitions": [{"to": "verify", "max_loop": 5}, {"to": "aggregate"}]},
            {"id": "verify", "step_type": "agent", "agent_config": "v",
             "transitions": [{"to": "loop", "max_loop": 5}]},
            {"id": "aggregate", "step_type": "agent", "agent_config": "a",
             "context": [{"source": agg_src}],
             "transitions": [{"to": "done"}]},
            {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
        ],
    }


def test_aggregator_without_scope_is_fine_engine_defaults_to_all(tmp_path):
    # skillflow >=1.5.24 routes by position: an out-of-loop reader gets ALL
    # items by default, so a missing scope is no longer a defect.
    res = forge_registry_check(graph_path=_write(tmp_path, _fanout(agg_scope=None)))
    assert res["passed"] is True


def test_aggregator_with_scope_all_passes(tmp_path):
    res = forge_registry_check(graph_path=_write(tmp_path, _fanout(agg_scope="all")))
    assert res["passed"] is True


def test_explicit_scope_task_on_out_of_loop_reader_is_flagged(tmp_path):
    # The engine silently overrides an outside reader's scope:task to all-items;
    # a declaration that lies about behavior is a violation.
    res = forge_registry_check(graph_path=_write(tmp_path, _fanout(agg_scope="task")))
    assert res["passed"] is False
    assert any("scope: task" in v or "scope: all" in v for v in res["violations"])


def test_invalid_scope_value_is_flagged(tmp_path):
    res = forge_registry_check(graph_path=_write(tmp_path, _fanout(agg_scope="al")))
    assert res["passed"] is False
    assert any("invalid scope" in v for v in res["violations"])


def test_in_loop_reader_of_body_producer_is_not_flagged(tmp_path):
    # verify (in the body) reading make (pre-loop) or a sibling body step with
    # default scope:task is correct — only OUT-of-loop readers need scope:all.
    g = _fanout(agg_scope="all")
    # add an in-loop reader: verify reads a sibling body producer without scope:all
    g["steps"].insert(3, {"id": "verify2", "step_type": "agent", "agent_config": "v2",
                          "context": [{"source": {"step": "verify"}}],
                          "transitions": [{"to": "loop", "max_loop": 5}]})
    # reroute loop→verify2→verify→loop so both are in the body
    g["steps"][1]["transitions"][0]["to"] = "verify2"
    g["steps"][3]["transitions"] = [{"to": "verify"}]  # verify2 → verify
    res = forge_registry_check(graph_path=_write(tmp_path, g))
    # verify2 (in body) reading verify (in body) must NOT be flagged
    assert not any("verify2" in v and "scope: all" in v for v in res["violations"])


def test_giveup_edge_target_is_not_body_reach_back_semantics(tmp_path):
    """The gate hole from the 1.5.23 review: a post-loop aggregator ALSO reachable
    from a body step via a give-up edge must NOT be classified in-body (reach-back
    topology, now taken from skillflow's own loop_body_map). Consequently an
    explicit scope:task there is a lying annotation and gets flagged."""
    g = _fanout(agg_scope="task")
    # add a give-up edge: verify --(passed:false, budget spent)--> aggregate
    verify = next(s for s in g["steps"] if s["id"] == "verify")
    verify["transitions"] = [
        {"to": "loop", "match": {"from_file": "v.json", "field": "p", "value": True},
         "max_loop": 5},
        {"to": "aggregate", "match": {"from_file": "v.json", "field": "p", "value": False},
         "max_loop": 3},
    ]
    res = forge_registry_check(graph_path=_write(tmp_path, g))
    # aggregate is OUT of body despite the drain edge → the scope:task lie fires
    assert any("scope: task" in v and "aggregate" in v for v in res["violations"]), \
        res["violations"]
    # and with the annotation omitted, the same topology passes (engine default)
    g2 = _fanout(agg_scope=None)
    v2 = next(s for s in g2["steps"] if s["id"] == "verify")
    v2["transitions"] = verify["transitions"]
    res2 = forge_registry_check(graph_path=_write(tmp_path, g2))
    assert res2["passed"] is True, res2["violations"]
