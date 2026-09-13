"""Native-loop artifact recovery with a projected large observation."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader

from core.dpe_pipeline import PipelineEngine
from core.prompt_assembler import PromptAssembler
from core.workspace_manager import WorkspaceManager


def _call(name, **params):
    return SimpleNamespace(
        text="", reasoning_content="", truncated=False,
        tool_calls=[{"id": name, "type": "function", "function": {
            "name": name, "arguments": json.dumps(params, ensure_ascii=False)}}],
    )


def _fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sf = SkillFlow(
        str(tmp_path / "state.db"),
        workspace_base=str(tmp_path / "artifacts"),
        code_path_resolver=lambda _pid, run_id=None: repo,
        tool_loader=ToolLoader(Path(skillflow.__file__).parent / "tools"),
    )
    dump_holder = {"content": ""}
    sf._tool_loader.register_dynamic_tool(
        "artifact_dump", {"parameters": {}},
        lambda **_kwargs: {"content": dump_holder["content"]},
    )
    node = StepNode(
        id="design", output_mode="write", output_carry_forward=True,
        context=[{"from": "repository", "mode": "tool"}],
        config={"extra_tools": ["artifact_dump"]}, checkpoint=True,
        transitions=[Transition(to="done")],
    )
    sf.register_graph(PipelineGraph(
        name="artifact_projection", begin="design",
        steps=[node, StepNode(id="done")],
    ))
    rid = sf.create_run("artifact_projection", project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)
    first = sf.claim_next_step(rid)
    promoted = Path(first.inputs["_artifact_dir"])
    candidate = Path(first.inputs["_output_dir"])
    candidate.mkdir(parents=True, exist_ok=True)
    prefix = "prefix bytes\r\n" * 600
    middle = "MIDDLE-UNIQUE-🙂-value"
    suffix = "suffix bytes\r\n" * 600
    original = (prefix + middle + "\r\n" + suffix).encode("utf-8")
    (candidate / "report.md").write_bytes(original)
    (candidate / "a").mkdir()
    (candidate / "b").mkdir()
    (candidate / "a" / "target.md").write_text("a", encoding="utf-8")
    (candidate / "b" / "target.md").write_text("b", encoding="utf-8")
    sf.confirm_step(first.token, StepResult())
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "paused"
    sf.reject_checkpoint(rid, "design", "recover one exact region")
    claim = sf.claim_next_step(rid)
    return sf, rid, claim, repo, promoted, original, middle, dump_holder


class _ScriptedNativeAgent:
    system_prompt = "Perform the bounded artifact recovery exactly as instructed."

    def __init__(self):
        self.gateway = SimpleNamespace(last_usage={}, litellm_model="test/native")
        self.calls = []
        self.emitted = []

    def turn(self, messages, **_kwargs):
        self.calls.append(messages)
        called = [
            call["function"]["name"]
            for message in messages
            if message.get("role") == "assistant"
            for call in (message.get("tool_calls") or [])
        ]
        if "artifact_dump" not in called:
            self.emitted.append("artifact_dump")
            return _call("artifact_dump")
        marker = next(
            json.loads(message["content"])
            for message in messages
            if message.get("role") == "tool"
            and isinstance(message.get("content"), str)
            and message["content"].startswith("{")
            and json.loads(message["content"]).get("_aitelier_compacted")
        )
        if "recall_observation" not in called:
            self.emitted.append("recall_observation")
            return _call("recall_observation", sha256=marker["sha256"],
                         start=8000, end=10000)
        recalled = next(
            message["content"] for message in reversed(messages)
            if message.get("role") == "tool"
            and "MIDDLE-UNIQUE" in str(message.get("content"))
        )
        if "edit" not in called:
            exact = json.loads(recalled)["content"]
            assert "MIDDLE-UNIQUE-🙂-value" in exact
            self.emitted.append("edit")
            return _call("edit", file="report.md",
                         old_str="MIDDLE-UNIQUE-🙂-value",
                         new_str="MIDDLE-UNIQUE-🌙-value")
        if "read" not in called:
            self.emitted.append("read")
            return _call("read", path="missing/target.md", source="self", raw=True)
        ambiguity = next(
            message["content"] for message in reversed(messages)
            if message.get("role") == "tool" and "candidates" in str(message.get("content"))
        )
        assert {row["path"] for row in json.loads(ambiguity)["candidates"]} == {
            "a/target.md", "b/target.md"
        }
        self.emitted.append("finish_step")
        return _call("finish_step", summary="exact bounded recovery complete")


def test_native_loop_projects_large_artifact_and_recovers_exact_middle(tmp_path, monkeypatch):
    sf, rid, claim, repo, promoted, original, middle, dump_holder = _fixture(tmp_path)
    candidate = Path(claim.inputs["_output_dir"])
    dump = (candidate / "report.md").read_bytes().decode("utf-8")
    dump_holder["content"] = dump
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    agent = _ScriptedNativeAgent()
    engine = PipelineEngine()
    engine.factory = SimpleNamespace(
        is_native=lambda _name: True,
        get_native_agent=lambda _name: agent,
        get_max_retries=lambda _name: 1,
        get_max_tool_turns=lambda _name: 8,
        get_fallback_to_json=lambda _name: False,
    )
    engine.assembler = PromptAssembler()
    engine._get_project_path = lambda *_args: repo.parent / "artifacts" / "p"
    engine._get_code_path = lambda *_args: repo
    engine._preamble_steps = lambda _name: []
    events = []
    engine._trace_cb = lambda category, event, payload: events.append(
        (category, event, payload)
    )
    workspace = WorkspaceManager(
        str(repo.parent / "artifacts"), projects_base=str(repo.parent / "projects")
    )
    assert engine.run_step(
        1, claim.step_id, workspace, "p", agent_config_name="fake", run_id=rid,
        step_instance_id=claim.token.step_instance_id,
        claim_epoch=claim.token.claim_epoch,
        tool_schemas=claim.inputs["_tool_schemas"],
        output_dir=claim.inputs["_output_dir"],
        output_target=claim.inputs["_output_target"],
        output_fixed=claim.inputs["_output_fixed"],
        config_name=claim.inputs["_config_name"],
        artifact_dir=claim.inputs["_artifact_dir"],
        carry_forward=claim.inputs["_output_carry_forward"],
        resolved_context={"assignment": (
            "Recover the unique middle snippet. First call artifact_dump. "
            "When its result is projected, use recall_observation with the "
            "marker sha256 and a bounded start/end slice. Then call one exact edit, "
            "refuse ambiguous read candidates, and finish."
        )},
    )
    sf.confirm_step(claim.token, StepResult())
    final = Path(claim.inputs["_artifact_dir"])
    changed = original.replace(
        middle.encode("utf-8"), "MIDDLE-UNIQUE-🌙-value".encode("utf-8"), 1
    )
    assert (final / "report.md").read_bytes() == changed
    assert (final / "report.md").read_bytes()[:original.index(middle.encode())] == \
        original[:original.index(middle.encode())]
    assert (final / "report.md").read_bytes()[
        original.index(middle.encode()) + len("MIDDLE-UNIQUE-🌙-value".encode()):
    ] == original[original.index(middle.encode()) + len(middle.encode()):]
    assert agent.emitted == [
        "artifact_dump", "recall_observation", "edit", "read", "finish_step"
    ]
    assert any(
        event == "prompt_projection" and payload["original_chars"] > 16 * 1024
        and payload["projected_chars"] < payload["original_chars"]
        for _category, event, payload in events
    )
    assert any(event == "observation_recalled" and payload["returned_chars"]
               and not payload["error"] for _category, event, payload in events)
    assert not any(name in {"write", "create", "write_file"} for message in agent.calls
                   for item in message if item.get("role") == "assistant"
                   for call in (item.get("tool_calls") or [])
                   for name in [call["function"]["name"]])
