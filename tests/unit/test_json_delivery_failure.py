"""Actual JSON turn-loop regressions; no media or model is executed."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from core.dpe_pipeline import PipelineEngine, MaxRetriesExceeded

INVALID = (Path(__file__).parent / "fixtures/media_followup_invalid.json.txt").read_text()
PNG = "assets/candidate.png"

def action(tool, **params):
    return {"tool": tool, "params": params}

def response(*actions):
    return json.dumps({"actions": actions})

def engine(tmp_path, responses, *, failed_writes=0):
    e = object.__new__(PipelineEngine)
    answers = iter(responses)
    e.calls, e.events, e.traces, e.prompts = [], [], [], []
    def run(prompt):
        e.prompts.append(prompt)
        return next(answers)
    agent = SimpleNamespace(run=run, gateway=SimpleNamespace(litellm_model="stub"))
    e.factory = SimpleNamespace(get_agent=lambda _: agent,
        get_max_retries=lambda _: 3, get_max_tool_turns=lambda _: len(responses))
    e.assembler = SimpleNamespace(assemble=lambda *a, **k: str(a[3]))
    e._agent_role = lambda _: "green"
    e._get_project_path = lambda *a: tmp_path
    e._get_code_path = lambda *a: tmp_path
    e._refuse_if_run_cancelled = lambda *a: None
    e._note_feedback = lambda *a: None
    e._repeat_note = lambda *a: ""
    e._unresolved_note = lambda *a: ""
    e._emit = lambda kind, data=None: e.events.append((kind, data))
    e._trace = lambda category, event, payload: e.traces.append((category, event, payload))
    e._max_tool_turns = 0
    e._resolved_context = {}
    e._user_lang = ""
    e._tool_schemas = {name: {} for name in
        ("create", "edit", "read", "gen_image_asset", "finish_step")}
    remaining_failures = failed_writes
    def execute(call):
        nonlocal remaining_failures
        e.calls.append(call)
        name, params = call["tool"], call["params"]
        if name == "gen_image_asset":
            path = tmp_path / PNG
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"retained candidate bytes")
            return {"written": [PNG]}
        if name == "read":
            return {"content": "baseline"}
        if name in ("create", "edit"):
            if remaining_failures:
                remaining_failures -= 1
                return {"error": "retained first write failure"}
            path = tmp_path / params["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params["content"])
            return {"written": params["file"]}
        raise AssertionError(name)
    e._exec_tool = execute
    return e

def run(e):
    return e._run_tool_step(1, "implement", None, "fixture", agent_config_name="stub")

def successful_writes():
    return response(action("create", file="final/manifest.json", content="{}"),
                    action("create", file="final/delivery.md", content="candidate"))

@pytest.mark.parametrize("prior_media", [False, True])
def test_malformed_json_cannot_complete_or_replay_media(tmp_path, prior_media):
    responses = ([response(action("gen_image_asset"))] if prior_media else []) + [INVALID]
    e = engine(tmp_path, responses)
    with pytest.raises(MaxRetriesExceeded, match="parse JSON"):
        run(e)
    assert not any(kind == "step_done" for kind, _ in e.events)
    assert e.traces[-1][2]["text"] == INVALID
    assert sum(c["tool"] == "gen_image_asset" for c in e.calls) == int(prior_media)
    assert not any(c["tool"] == "create" for c in e.calls)
    if prior_media:
        assert (tmp_path / PNG).read_bytes() == b"retained candidate bytes"

def test_valid_followup_writes_after_media_and_read_complete(tmp_path):
    e = engine(tmp_path, [response(action("gen_image_asset")),
        response(action("read", path="source.txt")), successful_writes()])
    assert run(e) is True
    assert (tmp_path / "final/manifest.json").read_text() == "{}"
    assert (tmp_path / "final/delivery.md").read_text() == "candidate"
    assert [c["tool"] for c in e.calls] == ["gen_image_asset", "read", "create", "create"]

@pytest.mark.parametrize("finish_after_failure", [False, True])
def test_prior_media_cannot_mask_all_failed_writes(tmp_path, finish_after_failure):
    responses = [response(action("gen_image_asset")), successful_writes()]
    if finish_after_failure:
        responses.append(response(action("finish_step")))
    e = engine(tmp_path, responses, failed_writes=2)
    with pytest.raises(MaxRetriesExceeded, match="retained first write failure"):
        run(e)
    assert sum(c["tool"] == "gen_image_asset" for c in e.calls) == 1
    assert not any(kind == "step_done" for kind, _ in e.events)
    assert (tmp_path / PNG).read_bytes() == b"retained candidate bytes"

def test_all_failed_writes_can_be_corrected_in_same_attempt(tmp_path):
    e = engine(tmp_path, [response(action("gen_image_asset")),
        successful_writes(), successful_writes()], failed_writes=2)
    assert run(e) is True
    assert len(e.prompts) == 3
    assert "retained first write failure" in e.prompts[-1]
    assert sum(c["tool"] == "gen_image_asset" for c in e.calls) == 1
    assert (tmp_path / "final/delivery.md").exists()

def test_finish_after_successful_media_is_still_supported(tmp_path):
    e = engine(tmp_path, [response(action("gen_image_asset")), response(action("finish_step"))])
    assert run(e) is True
    assert [c["tool"] for c in e.calls] == ["gen_image_asset"]


def test_more_turn_control_after_media_does_not_finish_delivery(tmp_path):
    e = engine(tmp_path, [response(action("gen_image_asset")),
        response(action("ask_more_turns")), successful_writes()])
    assert run(e) is True
    assert len(e.prompts) == 3
    assert (tmp_path / "final/delivery.md").exists()
