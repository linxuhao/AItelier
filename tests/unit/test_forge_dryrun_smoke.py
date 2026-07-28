"""The smoke gate must say WHY it failed, not just where it stopped.

skillflow writes the only actionable text about a failed run into
`skillflow_runs.error_reason` ("No matching transition from 'X' with flags {...}").
The smoke runs with `trace_enabled=False`, so if the tool drops that field the
information is gone for good — and the emitter re-emits blind. That is what
happened to `mcp_server_builder`: every round produced the same content-free
"dry-run smoke failed (status=failed). Trail: [...]".
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

_IMPL = (Path(__file__).resolve().parents[2]
         / "aitelier" / "tools" / "forge_dryrun_smoke" / "impl.py")
_spec = importlib.util.spec_from_file_location("forge_dryrun_smoke_impl", _IMPL)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
forge_dryrun_smoke = _mod.forge_dryrun_smoke


def _write(tmp_path: Path, graph: dict) -> str:
    p = tmp_path / "gen_x.yaml"
    p.write_text(yaml.safe_dump(graph), encoding="utf-8")
    return str(p)


# A graph whose reviewer can only route on a REJECT: the happy path (the stub's
# default `passed: true`) matches nothing, so the run dies mid-graph — exactly the
# shape that produced round 2's content-free smoke failure.
DEAD_END = {
    "name": "gen_dead_end",
    "begin": "make",
    "end_conditions": {"combinator": "or", "conditions": [
        {"type": "node_reached", "node": "done", "result": "completed",
         "require_completed": True}]},
    "steps": [
        {"id": "make", "step_type": "agent", "agent_config": "maker",
         "transitions": [{"to": "check"}]},
        {"id": "check", "step_type": "agent", "agent_config": "checker",
         "transitions": [{"to": "done", "match": {"passed": False}}]},
        {"id": "done", "step_type": "agent", "agent_config": "maker",
         "transitions": []},
    ],
}


def test_failure_message_quotes_skillflows_reason(tmp_path):
    res = forge_dryrun_smoke(graph_path=_write(tmp_path, DEAD_END))
    if res.get("status") in ("import_error", "boot_error"):
        pytest.skip(f"engine unavailable: {res.get('error')}")
    assert res["passed"] is False
    err = res["error"]
    assert "No matching transition" in err, err
    assert "check" in err, err
    # and the raw field is surfaced for programmatic readers too
    assert res["error_reason"] and "No matching transition" in res["error_reason"]


def test_missing_graph_still_reports_cleanly(tmp_path):
    res = forge_dryrun_smoke(graph_path=str(tmp_path / "nope.yaml"))
    assert res == {"passed": False, "status": "no_graph",
                   "error": f"graph not found: {tmp_path / 'nope.yaml'}"}


# ── The stub must speak the tools' OWN contracts ──────────────────────────────
# `forge_palette` tells makers "a tool that can fail needs a failure edge — branch
# on the result". skillflow's native `pytest` returns {"verdict": "passed"|"failed"},
# so a CORRECT graph branches on `verdict` — and the stub, which returned only
# {passed, has_suggestions}, made that unmatchable. The gate failed every graph that
# followed the convention it teaches.

PYTEST_CONTRACT = {
    "name": "gen_contract", "begin": "make",
    "end_conditions": {"combinator": "or", "conditions": [
        {"type": "node_reached", "node": "done", "result": "completed"}]},
    "steps": [
        {"id": "make", "step_type": "agent", "agent_config": "maker",
         "output": {"mode": "write"}, "transitions": [{"to": "check"}]},
        {"id": "check", "step_type": "tool", "tool_name": "pytest",
         "tool_params": {"file": "t.py"},
         "transitions": [{"to": "done", "match": {"verdict": "passed"}},
                         {"to": "make", "match": {"verdict": "failed"},
                          "max_loop": 2}]},
        {"id": "done", "step_type": "gate", "transitions": [{"to": None}]},
    ],
}


def test_a_graph_branching_on_pytests_real_contract_passes(tmp_path):
    res = forge_dryrun_smoke(graph_path=_write(tmp_path, PYTEST_CONTRACT))
    if res.get("status") in ("import_error", "boot_error"):
        pytest.skip(f"engine unavailable: {res.get('error')}")
    assert res["passed"] is True, res["error"]
    assert res["status"] == "completed"


def test_the_adversarial_run_still_terminates(tmp_path):
    res = forge_dryrun_smoke(graph_path=_write(tmp_path, PYTEST_CONTRACT),
                             verdict=False)
    if res.get("status") in ("import_error", "boot_error"):
        pytest.skip("engine unavailable")
    assert res["passed"] is True
    assert res["status"] != "max_steps"


def test_derived_flags_never_override_the_verdict():
    """ADD-only. Otherwise a reviewer whose success edge matches the WRONG value
    would be satisfied by its own mistake and the smoke would stop catching it —
    which is exactly what `test_failure_message_quotes_skillflows_reason` covers."""
    from aitelier.stub_runner import StubStepRunner
    node = {"id": "check", "transitions": [
        {"to": "done", "match": {"passed": False}}]}
    r = StubStepRunner(verdict=True, graph={"steps": [node]})
    assert r._derived_flags(node) == {"passed": False}      # what the branch says
    # …but the runner keeps its own verdict:
    class _S:
        step_id, step_config, inputs = "check", {}, {}
    assert r.run(_S()).flags["passed"] is True


def test_reserved_match_forms_are_not_mistaken_for_flags():
    """`{from_file,…}` is fixture-backed and `{from: checkpoint,…}` routes on
    `_checkpoint_approved`; adopting either would inject nonsense flags like
    `from="checkpoint"` (both forms appear in the shipped novel_* configs)."""
    from aitelier.stub_runner import StubStepRunner
    node = {"id": "g", "transitions": [
        {"to": "a", "match": {"from_file": "v.json", "field": "passed", "value": True}},
        {"to": "b", "match": {"from": "checkpoint", "value": "approved"}},
    ]}
    assert StubStepRunner(verdict=True, graph={"steps": [node]})._derived_flags(node) == {}


def test_the_indirect_field_value_form_is_understood():
    """`{field: X, value: V}` means flags[X] == V — used by gen_mdlink_pipeline."""
    from aitelier.stub_runner import StubStepRunner
    node = {"id": "g", "transitions": [
        {"to": "a", "match": {"field": "status", "value": "clean"}},
        {"to": "b", "match": {"field": "status", "value": "dirty"}},
    ]}
    r = StubStepRunner(verdict=True, graph={"steps": [node]})
    assert r._derived_flags(node) == {"status": "clean"}
    assert StubStepRunner(verdict=False,
                          graph={"steps": [node]})._derived_flags(node) == {"status": "dirty"}


def test_the_step_definition_comes_from_the_graph_not_step_config():
    """`ClaimedStep.step_config` is skillflow's opaque per-step `config:` key, which
    every real graph leaves unset — reading transitions from it silently did nothing
    (so `_write_transition_files` and `_touch_declared_outputs` never ran either)."""
    from aitelier.stub_runner import StubStepRunner
    node = {"id": "check", "transitions": [{"to": "done", "match": {"verdict": "passed"}}]}
    r = StubStepRunner(verdict=True, graph={"steps": [node]})

    class _S:
        step_id, step_config, inputs = "check", {}, {}     # empty, as in production
    assert r.run(_S()).flags["verdict"] == "passed"
