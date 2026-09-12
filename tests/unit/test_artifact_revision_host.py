"""Real SkillFlow + host dispatch; only LLM responses are simulated."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from core.dpe_pipeline import PipelineEngine
from core.workspace_manager import WorkspaceManager
from aitelier.tools.tasks_manifest_complete.impl import tasks_manifest_complete


def fixture(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    sf = SkillFlow(str(tmp_path / "state.db"), workspace_base=str(tmp_path / "artifacts"),
                   code_path_resolver=lambda pid, run_id=None: repo,
                   tool_loader=ToolLoader(Path(skillflow.__file__).parent / "tools"))
    sf._tool_loader.register_dynamic_tool("manifest_complete", {"parameters": {}}, tasks_manifest_complete)
    node = StepNode(id="cards", output_mode="content", output_carry_forward=True,
                    output_fixed={"card": "tasks/*.json", "manifest": "tasks_manifest.json"},
                    context=[{"from": "repository", "mode": "tool"}], checkpoint=True,
                    validation=[{"tool": "manifest_complete"}],
                    transitions=[Transition(to="done")])
    sf.register_graph(PipelineGraph(name="revision", begin="cards", steps=[node, StepNode(id="done")]))
    rid = sf.create_run("revision", project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)
    first = sf.claim_next_step(rid)
    candidate = Path(first.inputs["_output_dir"])
    (candidate / "tasks").mkdir()
    for name in ("A", "B", "C"):
        (candidate / "tasks" / f"{name}.json").write_text(json.dumps({"id": name, "value": 1}))
    (candidate / "tasks_manifest.json").write_text('{"execution_order":[["A","B","C"]]}')
    sf.confirm_step(first.token, StepResult())
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "paused"
    sf.reject_checkpoint(rid, "cards", "Change A only.")
    claim = sf.claim_next_step(rid)
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    return sf, rid, claim, repo


def response(name, **params):
    return SimpleNamespace(text="", reasoning_content="", truncated=False,
        tool_calls=[{"id": name, "type": "function", "function": {
            "name": name, "arguments": json.dumps(params)}}])


def host(sf, rid, claim, repo, native, json_run, turn):
    gateway = SimpleNamespace(last_usage={}, litellm_model="mock")
    agent = SimpleNamespace(turn=turn, run=json_run, system_prompt="Revise the candidate.", gateway=gateway)
    e = PipelineEngine()
    e.factory = SimpleNamespace(is_native=lambda _: native, get_native_agent=lambda _: agent,
        get_agent=lambda _: agent, get_max_retries=lambda _: 1,
        get_max_tool_turns=lambda _: 8, get_fallback_to_json=lambda _: True)
    # Render actual schemas/context without requiring unrelated project preambles.
    e.assembler = SimpleNamespace(assemble=lambda *a, **kw: json.dumps({
        "context": kw.get("resolved_context"), "tools": kw.get("tool_schemas")}, ensure_ascii=False))
    e._get_project_path = lambda *a: repo.parent / "artifacts/p"
    e._get_code_path = lambda *a: repo
    e._preamble_steps = lambda _: []
    e._trace_cb = lambda cat, event, payload: sf.trace(rid, cat, event, payload,
        step_id=claim.step_id, step_instance_id=claim.token.step_instance_id)
    ws = WorkspaceManager(str(repo.parent / "artifacts"), projects_base=str(repo.parent / "projects"))
    return e, ws


def execute(e, ws, rid, claim):
    return e.run_step(1, claim.step_id, ws, "p", agent_config_name="fake", run_id=rid,
        step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch,
        tool_schemas=claim.inputs["_tool_schemas"], output_dir=claim.inputs["_output_dir"],
        output_target=claim.inputs["_output_target"], output_fixed=claim.inputs["_output_fixed"],
        config_name=claim.inputs["_config_name"], artifact_dir=claim.inputs["_artifact_dir"],
        carry_forward=claim.inputs["_output_carry_forward"])


@pytest.mark.parametrize("mode", ["native", "json", "fallback"])
def test_one_card_revision_requires_no_sibling_rewrites(tmp_path, monkeypatch, mode):
    sf, rid, claim, repo = fixture(tmp_path, monkeypatch)
    prior = Path(claim.inputs["_artifact_dir"])
    unchanged = {name: (prior / "tasks" / f"{name}.json").read_bytes() for name in ("B", "C")}
    calls = []
    def turn(messages, **kwargs):
        calls.append("native")
        assert "Unchanged artifacts are preserved" in str(messages)
        if mode == "fallback":
            raise RuntimeError("simulated native transport incompatibility")
        if len(calls) == 1:
            return response("edit_card", id="A", old_str='"value": 1', new_str='"value": 2')
        return response("finish_step", summary="A revised")
    def json_run(prompt):
        calls.append("json")
        assert "Unchanged artifacts are preserved" in prompt
        return json.dumps({"actions": [
            {"tool": "edit_card", "params": {"id": "A", "old_str": '"value": 1', "new_str": '"value": 2'}},
            {"tool": "finish_step", "params": {"summary": "A revised"}}]})
    e, ws = host(sf, rid, claim, repo, mode != "json", json_run, turn)
    assert execute(e, ws, rid, claim)
    sf.confirm_step(claim.token, StepResult())
    assert sf._conn.execute("SELECT status FROM skillflow_steps WHERE id=?",
                            (claim.token.step_instance_id,)).fetchone()["status"] == "completed"
    assert json.loads((prior / "tasks/A.json").read_text())["value"] == 2
    for name, contents in unchanged.items():
        assert (prior / "tasks" / f"{name}.json").read_bytes() == contents
    assert len(calls) == (2 if mode in ("native", "fallback") else 1)


def test_native_reclaim_keeps_candidate_when_conversation_cannot_resume(tmp_path, monkeypatch):
    sf, rid, claim, repo = fixture(tmp_path, monkeypatch)
    candidate = Path(claim.inputs["_output_dir"])
    call = lambda name, **params: sf.execute_tool(name, params, run_id=rid, step_id=claim.step_id,
        step_instance_id=claim.token.step_instance_id, claim_epoch=claim.token.claim_epoch)
    assert call("delete_card", id="B").get("deleted")
    assert "error" not in call("write_manifest", content='{"execution_order":[["A","C"]]}')
    assert "error" not in call("edit_card", id="A", old_str='"value": 1', new_str='"value": 7')
    sf.release_claim(claim.token, "simulated host interruption before trace flush")
    claim = sf.claim_next_step(rid)
    e, ws = host(sf, rid, claim, repo, True, lambda _: "", lambda *a, **kw: response("finish_step", summary="retained"))
    e._resume_from_trace = lambda *a: None
    assert execute(e, ws, rid, claim)
    assert not (candidate / "tasks/B.json").exists()
    assert json.loads((candidate / "tasks/A.json").read_text())["value"] == 7
    sf.confirm_step(claim.token, StepResult())
    prior = Path(claim.inputs["_artifact_dir"])
    assert not (prior / "tasks/B.json").exists()
    assert tasks_manifest_complete(workspace_root=str(prior))["passed"]


def test_validation_feedback_names_only_affected_cards(tmp_path):
    (tmp_path / "tasks").mkdir()
    (tmp_path / "tasks/A.json").write_text("{}")
    (tmp_path / "tasks_manifest.json").write_text('{"execution_order":[["A","B"]]}')
    result = tasks_manifest_complete(workspace_root=str(tmp_path))
    assert not result["passed"] and result["missing"] == ["B"]
    assert "candidate" in result["error"]
    for obsolete in ("Re-emit EVERY", "staging", "promotion REPLACES"):
        assert obsolete not in result["error"]
    block = PipelineEngine._validation_error_block(result["error"])
    assert "Repair the reported errors" in block
    assert "not carried over" not in block


@pytest.mark.parametrize("failure", ["transport", "protocol"])
def test_error_after_native_edit_keeps_the_edit_and_siblings(tmp_path, monkeypatch, failure):
    sf, rid, claim, repo = fixture(tmp_path, monkeypatch)
    count = []
    def turn(messages, **kwargs):
        count.append("native")
        if len(count) == 1:
            return response("edit_card", id="A", old_str='"value": 1', new_str='"value": 9')
        if failure == "transport":
            raise RuntimeError("transport failed after successful edit")
        return SimpleNamespace(text="", tool_calls=None)  # malformed native response enters fallback
    def json_run(prompt):
        count.append("json")
        current = Path(claim.inputs["_output_dir"])
        assert json.loads((current / "tasks/A.json").read_text())["value"] == 9
        assert (current / "tasks/B.json").is_file() and (current / "tasks/C.json").is_file()
        return json.dumps({"actions": [{"tool": "finish_step", "params": {"summary": "retained"}}]})
    e, ws = host(sf, rid, claim, repo, True, json_run, turn)
    assert execute(e, ws, rid, claim)
    sf.confirm_step(claim.token, StepResult())
    assert count == (["native", "native", "json"] if failure == "protocol" else ["native", "native"])
    assert json.loads((Path(claim.inputs["_artifact_dir"]) / "tasks/A.json").read_text())["value"] == 9
