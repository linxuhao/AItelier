"""The trace must identify the WIRE view of every tool-result prompt_delta.

prompt_delta retains the full raw observation, so a reader could mistake the
unseen bytes for model input. Each tool delta therefore records whether the
content was projected, the exact wire character count, and the digest / marker
identity, derived from the SAME _compact_marker/_project_native_messages path
the native loop runs before each provider call. The prompt_projection event
names the replaced message indices. No marker bytes, cap, recall behavior,
history order or provider routing changes.
"""
import json

from core.dpe_pipeline import (
    _NATIVE_HISTORY_TOOL_CHARS,
    _compact_marker,
    _observation_digest,
    _observation_storage_digest,
    _project_native_messages,
    PipelineEngine,
)

CAP = _NATIVE_HISTORY_TOOL_CHARS


class _Host:
    def __init__(self):
        self.events = []

    def _trace(self, category, event, payload=None):
        self.events.append((category, event, payload))

    _trace_prompt_deltas = PipelineEngine._trace_prompt_deltas


def _deltas(msgs):
    h = _Host()
    h._delta_traced = 0
    h._trace_prompt_deltas(msgs, 1)
    return [p for c, e, p in h.events if e == "prompt_delta"]


def test_exact_threshold_is_not_projected_one_over_is():
    at_cap = {"role": "tool", "tool_call_id": "c1", "content": "x" * CAP}
    projected, report = _project_native_messages([at_cap])
    assert projected[0]["content"] == at_cap["content"]          # byte-identical
    assert report["replaced_indices"] == []
    assert report["wire_view"] == [{
        "index": 0, "raw_chars": CAP, "wire_chars": CAP,
        "projected": False,
        "sha256": _observation_digest(at_cap["content"]),
        "wire_sha256": _observation_storage_digest(at_cap["content"]),
    }]

    over = {"role": "tool", "tool_call_id": "c2", "content": "y" * (CAP + 1)}
    projected, report = _project_native_messages([over])
    marker = _compact_marker(over["content"], "tool result")
    assert projected[0]["content"] == marker                     # the real marker
    assert report["replaced_indices"] == [0]
    entry = report["wire_view"][0]
    assert entry["projected"] is True
    assert entry["raw_chars"] == CAP + 1
    assert entry["wire_chars"] == len(marker)
    assert entry["wire_sha256"] == _observation_storage_digest(marker)
    assert entry["sha256"] == _observation_digest(over["content"])  # marker identity


def test_delta_payload_records_wire_identity_for_small_and_huge_results():
    small = {"role": "tool", "tool_call_id": "s", "content": json.dumps({"ok": True})}
    huge = {"role": "tool", "tool_call_id": "h", "content": "z" * (CAP + 5000)}
    msgs = [{"role": "user", "content": "U"}, small, huge]
    deltas = _deltas(msgs)

    small_d, huge_d = deltas[1], deltas[2]
    assert small_d["content"] == small["content"]                # raw retained in trace
    assert small_d["wire_projected"] is False
    assert small_d["raw_chars"] == small_d["wire_chars"] == len(small["content"])
    assert small_d["wire_sha256"] == _observation_storage_digest(small["content"])

    marker = _compact_marker(huge["content"], "tool result")
    assert huge_d["content"] == huge["content"]                  # raw retained in trace
    assert huge_d["wire_projected"] is True
    assert huge_d["raw_chars"] == len(huge["content"])
    assert huge_d["wire_chars"] == len(marker)
    assert huge_d["wire_sha256"] == _observation_storage_digest(marker)
    assert huge_d["observation_sha256"] == _observation_digest(huge["content"])

    # Trace wire identity agrees with the actual projector, index by index.
    _, report = _project_native_messages(msgs)
    for i, (delta, entry) in enumerate(zip(deltas[1:], report["wire_view"])):
        assert entry["index"] == i + 1
        assert delta["wire_projected"] == entry["projected"]
        assert delta["wire_chars"] == entry["wire_chars"]
        assert delta["wire_sha256"] == entry["wire_sha256"]


def test_prompt_projection_event_identifies_replaced_indices_across_results():
    # Two oversized results and one small one: only the two are on replaced_indices.
    msgs = [
        {"role": "user", "content": "U"},
        {"role": "tool", "tool_call_id": "a", "content": "a" * (CAP + 1)},
        {"role": "tool", "tool_call_id": "b", "content": json.dumps({"ok": 1})},
        {"role": "tool", "tool_call_id": "c", "content": "c" * (CAP + 2)},
    ]
    projected, report = _project_native_messages(msgs)
    assert report["replaced_indices"] == [1, 3]
    assert projected[2]["content"] == msgs[2]["content"]          # small result untouched
    assert projected[1]["content"] == _compact_marker(msgs[1]["content"], "tool result")
    assert projected[3]["content"] == _compact_marker(msgs[3]["content"], "tool result")
    assert [e["projected"] for e in report["wire_view"]] == [True, False, True]
    assert report["compacted_tool_results"] == 2
    # The prompt_projection event spreads the report, so the indices travel with it.
    event_payload = {"attempt": 1, "turn": 2, **report}
    assert event_payload["replaced_indices"] == [1, 3]
    assert len(event_payload["wire_view"]) == 3


def test_repeat_sends_are_stable_and_history_is_never_mutated():
    msgs = [
        {"role": "user", "content": "U"},
        {"role": "tool", "tool_call_id": "a", "content": "a" * (CAP + 10)},
        {"role": "tool", "tool_call_id": "b", "content": "b"},
    ]
    original = json.loads(json.dumps(msgs))
    first_msgs, first_report = _project_native_messages(msgs)
    second_msgs, second_report = _project_native_messages(msgs)
    assert msgs == original                                       # raw history preserved
    assert first_msgs == second_msgs
    assert first_report == second_report                          # frozen wire identity
    assert first_report["replaced_indices"] == [1]
    # A later send re-derives the same marker the trace recorded at first send.
    assert first_report["wire_view"][0]["wire_sha256"] == (
        _observation_storage_digest(_compact_marker(msgs[1]["content"], "tool result")))


def test_non_tool_and_reasoning_deltas_gain_no_wire_fields():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "assistant", "content": None, "reasoning_content": "r" * 50000,
         "tool_calls": [{"id": "c1"}]},
    ]
    deltas = _deltas(msgs)
    assert all("wire_projected" not in d and "wire_chars" not in d for d in deltas)
    _, report = _project_native_messages(msgs)
    assert report["wire_view"] == [] and report["replaced_indices"] == []
    assert report["compacted_reasoning"] == 0
