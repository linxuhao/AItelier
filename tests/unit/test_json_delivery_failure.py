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

def engine(tmp_path, responses, *, failed_writes=0, max_turns=None, max_retries=3):
    e = object.__new__(PipelineEngine)
    answers = iter(responses)
    e.calls, e.events, e.traces, e.prompts = [], [], [], []
    def run(prompt):
        e.prompts.append(prompt)
        # Once the script is spent, keep returning the malformed reply: a turn
        # the engine consumes (a refused truncation costs a turn, not the whole
        # attempt) must not surface as a StopIteration that masks the outcome.
        return next(answers, INVALID)
    agent = SimpleNamespace(run=run, gateway=SimpleNamespace(litellm_model="stub"))
    e.factory = SimpleNamespace(get_agent=lambda _: agent,
        get_max_retries=lambda _: max_retries,
        get_max_tool_turns=lambda _: len(responses) if max_turns is None else max_turns)
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
    # A truncated turn now costs a TURN rather than the attempt, so script enough
    # of them for the budget to run out on its own — which is the point: the
    # round is handed back and never completes by replaying a write.
    responses = ([response(action("gen_image_asset"))] if prior_media else []) + [INVALID] * 12
    e = engine(tmp_path, responses)
    # INVALID is a structure that opened and never closed, so it is classified as
    # a TRUNCATED completion and refused with "split the work" rather than
    # answered with the formatting instruction. What this test binds is unchanged:
    # no write is replayed, no step reports done, and the round does not complete.
    with pytest.raises(MaxRetriesExceeded):
        run(e)
    assert any(ev == "truncation_refused" for _c, ev, _p in e.traces)
    assert not any(kind == "step_done" for kind, _ in e.events)
    assert any(payload.get("text") == INVALID
               for _c, ev, payload in e.traces if ev == "agent_response")
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

# Recorded reply has an extra closing brace INSIDE actions, not a truncated tail.
EXTRA_BRACE = (Path(__file__).parent / "fixtures/malformed_json_extra_brace_20260926.txt").read_text()
PROSE = "I reviewed the plan (no edits needed)."
CORRECTION = (
    "System Error: Failed to parse JSON. "
    "You MUST respond with ONLY a JSON object like: "
    '{"thoughts": "...", "actions": [{"tool": "write", "params": {"file": "path", "content": "..."}}]}. '
    "Do NOT add any text before or after the JSON."
)

@pytest.mark.parametrize("malformed", [EXTRA_BRACE, PROSE], ids=["reported-extra-brace", "non-json"])
def test_malformed_gets_exact_correction_and_corrected_write(tmp_path, malformed):
    assert PipelineEngine._extract_json(malformed) is None
    assert not PipelineEngine._detect_truncated_json(malformed)
    e = engine(tmp_path, [malformed, successful_writes()])
    assert run(e) is True
    assert len(e.prompts) == 2
    assert e.prompts[1] == CORRECTION
    assert [c["tool"] for c in e.calls] == ["create", "create"]
    assert not any(k == "agent_message" for k, _ in e.events)
    assert [p["text"] for _c, ev, p in e.traces if ev == "agent_response"] == [malformed, successful_writes()]
    assert (tmp_path / "final/delivery.md").read_text() == "candidate"

@pytest.mark.parametrize("prior", ["none", "media", "effect"])
@pytest.mark.parametrize("parse_limit", [2, 3, 4])
def test_consecutive_malformed_bound_retains_reason_without_replay(tmp_path, prior, parse_limit):
    before = [] if prior == "none" else [response(action("gen_image_asset"))]
    e = engine(tmp_path, before + [EXTRA_BRACE] * 8, max_retries=parse_limit)
    if prior == "effect":
        def effect(call):
            e.calls.append(call)
            return {"state_written": "durable-change"}
        e._exec_tool = effect
    with pytest.raises(MaxRetriesExceeded, match="Failed to parse JSON"):
        run(e)
    assert len(e.prompts) == len(before) + parse_limit
    assert len(e.calls) == len(before)
    assert not any(k == "step_done" for k, _ in e.events)
    if prior == "media":
        assert (tmp_path / PNG).read_bytes() == b"retained candidate bytes"

def test_valid_payload_resets_consecutive_parse_failure_count(tmp_path):
    e = engine(tmp_path, [EXTRA_BRACE, PROSE, response(action("read", path="source.txt")),
        PROSE, EXTRA_BRACE, successful_writes()])
    assert run(e) is True
    assert len(e.prompts) == 6
    assert [c["tool"] for c in e.calls] == ["read", "create", "create"]

@pytest.mark.parametrize("prior_media", [False, True])
def test_parse_recovery_stays_inside_existing_turn_budget(tmp_path, prior_media):
    before = [response(action("gen_image_asset"))] if prior_media else []
    e = engine(tmp_path, before + [PROSE, successful_writes()], max_turns=len(before) + 1)
    with pytest.raises(MaxRetriesExceeded, match="Failed to parse JSON"):
        run(e)
    assert len(e.prompts) == len(before) + 1
    assert [c["tool"] for c in e.calls] == (["gen_image_asset"] if prior_media else [])
    assert not (tmp_path / "final/delivery.md").exists()
    assert not any(k == "step_done" for k, _ in e.events)

def test_malformed_partial_write_is_not_applied(tmp_path):
    malformed = response(action("create", file="partial.txt", content="must not land"))[:-2] + "}}]}"
    assert PipelineEngine._extract_json(malformed) is None
    assert not PipelineEngine._detect_truncated_json(malformed)
    e = engine(tmp_path, [malformed, successful_writes()])
    assert run(e) is True
    assert not (tmp_path / "partial.txt").exists()
    assert [c["params"]["file"] for c in e.calls] == ["final/manifest.json", "final/delivery.md"]

@pytest.mark.parametrize("after", ["finish", "bound", "budget"])
def test_parse_recovery_cannot_mask_unresolved_write_failure(tmp_path, after):
    tail = ([PROSE, response(action("finish_step"))] if after == "finish"
            else [PROSE] * 3 if after == "bound" else [PROSE])
    e = engine(tmp_path, [response(action("gen_image_asset")), successful_writes()] + tail,
               failed_writes=2)
    with pytest.raises(MaxRetriesExceeded, match="retained first write failure"):
        run(e)
    assert sum(c["tool"] == "gen_image_asset" for c in e.calls) == 1
    assert not any(k == "step_done" for k, _ in e.events)

def test_truncated_then_malformed_then_corrected_preserves_split_feedback(tmp_path):
    e = engine(tmp_path, [INVALID, EXTRA_BRACE, successful_writes()])
    assert run(e) is True
    assert "TRUNCATED" in e.prompts[1].upper()
    assert "split" in e.prompts[1].lower()
    assert e.prompts[2] == CORRECTION
    assert [c["tool"] for c in e.calls] == ["create", "create"]


def _engine_with_prior_effect(tmp_path, responses, *, prior):
    before = ([response(action("state_change"))] if prior == "effect" else
              [response(action("gen_image_asset"))] if prior == "media" else [])
    e = engine(tmp_path, before + responses, failed_writes=1)
    e._tool_schemas["state_change"] = {}
    execute = e._exec_tool
    def with_owned_effect(call):
        if call["tool"] == "state_change":
            e.calls.append(call)
            (tmp_path / "state.effect").write_bytes(b"durable-once")
            return {"state_written": "owned durable mock"}
        return execute(call)
    e._exec_tool = with_owned_effect
    return e


def _failed_create():
    return response(action("create", file="blocked.txt", content="never lands"))


def _assert_prior_effect_retained(e, tmp_path, prior):
    assert sum(c["tool"] == "state_change" for c in e.calls) == int(prior == "effect")
    assert sum(c["tool"] == "gen_image_asset" for c in e.calls) == int(prior == "media")
    if prior == "effect":
        assert (tmp_path / "state.effect").read_bytes() == b"durable-once"
    elif prior == "media":
        assert (tmp_path / PNG).read_bytes() == b"retained candidate bytes"


@pytest.mark.parametrize("prior", ["effect", "none", "media"])
@pytest.mark.parametrize("control", ["ask_more_turns", "finish_step", "end_step"])
def test_every_completion_control_respects_pending_write_failure(tmp_path, prior, control):
    e = _engine_with_prior_effect(tmp_path,
        [_failed_create(), EXTRA_BRACE, response(action(control, turns=2))], prior=prior)
    error = None
    result = None
    try:
        result = run(e)
    except MaxRetriesExceeded as exc:
        error = str(exc)
    print(json.dumps({"prior": prior, "control": control, "result": result,
        "error": error, "calls": e.calls, "prompts": e.prompts,
        "events": e.events, "traces": e.traces}, sort_keys=True))
    assert result is not True, "unresolved write was masked by completion control"
    assert error and "retained first write failure" in error
    assert not any(k == "step_done" for k, _ in e.events)
    assert not (tmp_path / "blocked.txt").exists()
    assert not any(c["tool"] == "read" for c in e.calls)  # malformed read refused
    _assert_prior_effect_retained(e, tmp_path, prior)


@pytest.mark.parametrize("prior", ["effect", "none", "media"])
def test_more_turn_control_preserves_pending_failure_until_successful_repair(tmp_path, prior):
    e = _engine_with_prior_effect(tmp_path, [_failed_create(), EXTRA_BRACE,
        response(action("ask_more_turns", turns=2)), successful_writes()], prior=prior)
    assert run(e) is True
    assert len(e.prompts) == 4 + int(prior != "none")
    assert "retained first write failure" in e.prompts[-1]
    assert (tmp_path / "final/delivery.md").read_text() == "candidate"
    assert not (tmp_path / "blocked.txt").exists()
    assert [c["tool"] for c in e.calls].count("create") == 3
    _assert_prior_effect_retained(e, tmp_path, prior)
