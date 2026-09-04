"""The native loop traces each appended message ONCE (prompt_delta), so any
turn's prompt is reproducible from the trace without storing history n times."""
import json

from core.dpe_pipeline import PipelineEngine


class _Host:
    def __init__(self):
        self.events = []
    def _trace(self, category, event, payload=None):
        self.events.append((category, event, payload))
    _trace_prompt_deltas = PipelineEngine._trace_prompt_deltas


def test_each_message_is_traced_once_across_turns():
    h = _Host(); h._delta_traced = 0
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    h._trace_prompt_deltas(msgs, 1)
    msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]})
    msgs.append({"role": "tool", "tool_call_id": "c1", "content": "R" * 30000})
    h._trace_prompt_deltas(msgs, 2)
    h._trace_prompt_deltas(msgs, 3)          # nothing appended: nothing traced
    deltas = [p for c, e, p in h.events if e == "prompt_delta"]
    assert [(p["turn"], p["index"], p["role"]) for p in deltas] == [
        (1, 0, "system"), (1, 1, "user"), (2, 2, "assistant"), (2, 3, "tool")]
    assert deltas[3]["content"] == "R" * 30000            # full, not a preview
    assert deltas[3]["tool_call_id"] == "c1"
    assert json.loads(deltas[2]["tool_calls"]) == [{"id": "c1"}]
    assert h._delta_traced == 4


def test_non_string_content_is_serialised_not_dropped():
    h = _Host(); h._delta_traced = 0
    h._trace_prompt_deltas([{"role": "user", "content": [{"type": "text", "text": "x"}]}], 1)
    assert json.loads(h.events[0][2]["content"]) == [{"type": "text", "text": "x"}]


def test_replay_is_the_concatenation_of_deltas():
    # prompt(turn n) == all deltas with turn <= n, in order
    h = _Host(); h._delta_traced = 0
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    h._trace_prompt_deltas(msgs, 1)
    msgs.append({"role": "assistant", "content": "A1"}); h._trace_prompt_deltas(msgs, 2)
    msgs.append({"role": "assistant", "content": "A2"}); h._trace_prompt_deltas(msgs, 3)
    upto2 = [p["content"] for c, e, p in h.events if p.get("turn", 9) <= 2]
    assert upto2 == ["S", "U", "A1"] == [m["content"] for m in msgs[:3]]
