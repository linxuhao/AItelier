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
