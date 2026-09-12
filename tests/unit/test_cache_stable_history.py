import json
from types import SimpleNamespace
from unittest.mock import patch

from core.ai_router import AIGateway, _observe_request_prefix
from core.dpe_pipeline import (
    PipelineEngine, _context_boundary, _context_handoff_messages,
    _observation_digest, _project_native_messages,
)

TOOLS = [{"type": "function", "function": {"name": "read",
          "parameters": {"type": "object", "properties": {}}}}]


def test_large_observation_is_frozen_before_first_outbound_and_never_rewritten():
    large = json.dumps({"content": "x" * 10000})
    base = [
        {"role": "system", "content": "s"},
        {"role": "assistant", "content": None,
         "reasoning_content": "required reasoning",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": large},
    ]
    first, report = _project_native_messages(base, history_char_budget=1000)
    assert report["stable_at_first_send"] is True
    marker = first[-1]["content"]
    assert json.loads(marker)["sha256"] == _observation_digest(large)
    assert first[-2]["reasoning_content"] == "required reasoning"

    extended = base + [{"role": "user", "content": "continue"}]
    second, _ = _project_native_messages(extended, history_char_budget=1000)
    assert second[:len(first)] == first


def test_adapter_fingerprint_names_clean_append_and_rewrite():
    first = _observe_request_prefix(
        [{"role": "system", "content": "s"},
         {"role": "user", "content": "one"}], TOOLS, endpoint="p/model")
    appended = _observe_request_prefix(
        [{"role": "system", "content": "s"},
         {"role": "user", "content": "one"},
         {"role": "assistant", "content": "two"}], TOOLS, first,
        endpoint="p/model")
    assert appended["append_only"] is True
    assert appended["first_changed_position"] == len(first["prefix"])

    rewritten = _observe_request_prefix(
        [{"role": "system", "content": "changed"},
         {"role": "user", "content": "one"}], TOOLS, appended,
        endpoint="p/model")
    assert rewritten["append_only"] is False
    assert rewritten["first_changed_position"] == 1


def test_gateway_observes_the_sanitized_actual_native_request(monkeypatch, tmp_path):
    cfg = tmp_path / "providers.json"
    cfg.write_text(json.dumps({"p": {"base_url": "http://example.invalid/v1",
                                      "max_input_tokens": 1000}}))
    gw = AIGateway("p/model", config_path=str(cfg))
    msg = SimpleNamespace(content="ok", tool_calls=[], reasoning_content="")
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1,
                              prompt_cache_hit_tokens=8,
                              prompt_cache_miss_tokens=2))
    with patch.object(gw, "_complete_prebuilt", return_value=response):
        gw.generate_native([{"role": "user", "content": " "}], tools=TOOLS)
    # Sanitization replaces the blank plain message before hashing.
    expected = _observe_request_prefix(
        [{"role": "user", "content": " "}], TOOLS,
        endpoint=gw.active_model)
    assert gw.last_outbound["fingerprint"] == expected["fingerprint"]


def test_declared_budget_triggers_one_protocol_clean_recoverable_handoff():
    class Gateway:
        max_output_tokens = 300
        def declared_input_window(self): return 1000
        def estimate_request_tokens(self, messages, tools): return 750

    status = _context_boundary(Gateway(), [{"role": "user", "content": "x"}], TOOLS)
    assert status == {"limit": 1000, "estimated_prompt_tokens": 750,
                      "output_reserve": 300, "handoff": True, "unknown": False}

    failure = json.dumps({"error": "first failure"})
    messages = [{"role": "system", "content": "system"},
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "function": {"name": "edit",
                                                               "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "c1", "content": failure}]
    handoff = _context_handoff_messages(messages, segment=0,
                                        written_files=["a.py", "a.py"])
    assert [m["role"] for m in handoff] == ["system", "user"]
    payload = json.loads(handoff[1]["content"])
    assert payload["written_files"] == ["a.py"]
    assert payload["first_failure"]
    assert payload["observation_ids"] == [_observation_digest(failure)]
    assert "Do not repeat tool side effects" in payload["instruction"]


def test_reclaim_uses_latest_segment_but_retains_old_observations_and_effects():
    def row(segment, index, role, content, **extra):
        payload = {"segment": segment, "index": index, "role": role,
                   "content": content, **extra}
        return ("prompt_delta", payload)

    old_result = json.dumps({"written": "a.py"})
    rows = [
        row(0, 0, "system", "s"),
        row(0, 1, "assistant", "", content_null=True,
            tool_calls=json.dumps([{"id": "c1", "function": {"name": "write"}}])),
        row(0, 2, "tool", old_result, tool_call_id="c1"),
        row(1, 0, "system", "s"),
        row(1, 1, "user", "handoff"),
    ]
    rebuilt = PipelineEngine._rebuild_from_deltas(rows, 10)
    assert [m["role"] for m in rebuilt["messages"]] == ["system", "user"]
    assert rebuilt["written_files"] == ["a.py"]
    assert rebuilt["recall_messages"][0]["content"] == old_result
    assert rebuilt["completed_effect_calls"]
    assert rebuilt["segment"] == 1


def _bare_engine_with_store(tmp_path):
    engine = PipelineEngine.__new__(PipelineEngine)
    engine._observation_store_dir = tmp_path / "observations"
    engine._effect_fence_dir = tmp_path / "effects"
    return engine


def test_oversized_observation_survives_trace_clipping_and_reclaim(tmp_path):
    from core.dpe_pipeline import _recall_observation

    engine = _bare_engine_with_store(tmp_path)
    original = json.dumps({"content": "z" * 300_000})
    digest = engine._persist_native_observation(original)
    # Match SkillFlow's 262144 field cap: the trace copy is no longer the
    # original bytes, while the un-clipped digest reference remains intact.
    head_n, tail_n = 262_144 - 4096, 4096
    dropped = len(original) - head_n - tail_n
    clipped = (original[:head_n]
               + f"\n…[clipped {dropped} chars — head {head_n} + tail "
                 f"{tail_n} of {len(original)} kept]\n"
               + original[-tail_n:])
    rows = [
        ("prompt_delta", {"segment": 0, "index": 0,
                          "role": "system", "content": "s"}),
        ("prompt_delta", {"segment": 0, "index": 1,
                          "role": "user", "content": "task"}),
        ("prompt_delta", {"segment": 0, "index": 2, "role": "tool",
                          "tool_call_id": "c1", "content": clipped,
                          "observation_ref": digest}),
    ]
    resumed = engine._hydrate_resume_observations(
        PipelineEngine._rebuild_from_deltas(rows, 10))
    recalled = _recall_observation(resumed["recall_messages"], digest[:16])
    assert recalled["content"].startswith('{"content": "zz')
    assert recalled["original_chars"] == len(original)
    assert resumed["recall_messages"][0]["content"] == original


def test_repeated_handoff_keeps_primitive_assignment_and_evidence():
    failed = json.dumps({"error": "key failure"})
    first = _context_handoff_messages(
        [{"role": "system", "content": "system"},
         {"role": "user", "content": "primitive task"},
         {"role": "tool", "tool_call_id": "c1", "content": failed}],
        segment=0, written_files=[])
    second = _context_handoff_messages(first, segment=1, written_files=[])
    third = _context_handoff_messages(second, segment=2, written_files=[])
    first_payload = json.loads(first[1]["content"])
    for messages in (second, third):
        payload = json.loads(messages[1]["content"])
        assert payload["original_assignment"] == "primitive task"
        assert payload["observation_ids"] == first_payload["observation_ids"]
        assert payload["first_failure"] == first_payload["first_failure"]


def test_effect_fence_survives_crash_before_tool_result_delta(tmp_path):
    from core.dpe_pipeline import _repeat_call_key

    engine = _bare_engine_with_store(tmp_path)
    params = {"path": "a.py", "content": "x"}
    result = json.dumps({"written": "a.py"})
    call_key = _repeat_call_key("write", params)
    engine._persist_native_effect(call_key, result, ["a.py"], "written")
    # The worker dies after the mutation fence commits, before the tool-result
    # prompt_delta. Reclaim drops the incomplete assistant pair but retains the
    # independently durable effect identity and exact result.
    rows = [
        ("prompt_delta", {"segment": 0, "index": 0,
                          "role": "system", "content": "s"}),
        ("prompt_delta", {"segment": 0, "index": 1,
                          "role": "user", "content": "task"}),
        ("prompt_delta", {"segment": 0, "index": 2,
                          "role": "assistant", "content": "",
                          "content_null": True,
                          "tool_calls": json.dumps([{
                              "id": "c1", "type": "function",
                              "function": {"name": "write",
                                           "arguments": json.dumps(params)}}])}),
    ]
    resumed = engine._hydrate_resume_observations(
        PipelineEngine._rebuild_from_deltas(rows, 10))
    assert [m["role"] for m in resumed["messages"]] == ["system", "user"]
    assert resumed["completed_effect_calls"] == {call_key: result}
    assert resumed["written_files"] == ["a.py"]


def test_first_turn_effect_fence_is_a_valid_resume_even_without_complete_turn():
    import inspect
    source = inspect.getsource(PipelineEngine._run_native_step)
    assert 'and not resume.get("completed_effect_calls")' in source


def test_prompt_delta_records_full_durable_reference_before_trace_clips(tmp_path):
    engine = _bare_engine_with_store(tmp_path)
    engine._delta_traced = 0
    engine._delta_attempt = 1
    engine._context_segment = 0
    events = []
    engine._trace = lambda category, event, payload=None: events.append(
        (category, event, payload))
    original = json.dumps({"content": "q" * 300_000})
    engine._trace_prompt_deltas(
        [{"role": "tool", "tool_call_id": "c1", "content": original}], 1)
    payload = events[0][2]
    ref = payload["observation_ref"]
    assert len(ref) == 64
    assert engine._read_native_observation(ref) == original
