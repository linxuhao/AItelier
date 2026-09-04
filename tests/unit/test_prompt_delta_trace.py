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


# ── resume after a host restart ──────────────────────────────────────────

def _tool(content, cid="c1"):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _deltas_from(msgs):
    """What the trace holds after a live loop traced these messages."""
    h = _Host(); h._delta_traced = 0
    h._trace_prompt_deltas(msgs, 1)
    return [(e, p) for c, e, p in h.events]


def test_rebuild_is_byte_identical_for_complete_turns():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read"}}]},
            _tool(json.dumps({"content": "file body"})),
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "create"}}]},
            _tool(json.dumps({"created": "a.gd"}), "c2")]
    r = PipelineEngine._rebuild_from_deltas(_deltas_from(msgs), max_turns=30)
    assert r["messages"] == msgs
    assert r["turns"] == 2 and r["written_files"] == ["a.gd"]
    assert r["current_max_turns"] == 30 and r["turn_grants"] == 0 and r["dropped_tail"] == 0


def test_a_trailing_incomplete_turn_is_dropped():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            _tool(json.dumps({"created": "a.gd"})),
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2"}, {"id": "c3"}]},
            _tool(json.dumps({"created": "b.gd"}), "c2")]     # c3 never traced: crash mid-turn
    r = PipelineEngine._rebuild_from_deltas(_deltas_from(msgs), max_turns=30)
    assert r["messages"] == msgs[:4]
    assert r["turns"] == 1 and r["written_files"] == ["a.gd"] and r["dropped_tail"] == 2


def test_grants_are_restored_from_the_tool_results():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            _tool(json.dumps({"status": "granted", "turns": 6, "note": "ask_more_turns: +6 turns granted (1/2)."}))]
    r = PipelineEngine._rebuild_from_deltas(_deltas_from(msgs), max_turns=30)
    assert r["current_max_turns"] == 36 and r["turn_grants"] == 1


def test_nothing_to_resume_without_deltas():
    assert PipelineEngine._rebuild_from_deltas([("claimed", {}), ("user_prompt", {"user": "x"})], 30) is None


def test_deltas_from_two_attempts_are_one_conversation():
    # a retry attempt appends its nudge to the SAME list; indices keep growing
    h = _Host(); h._delta_traced = 0
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]}, _tool("{}")]
    h._trace_prompt_deltas(msgs, 1)
    h._delta_attempt = 2
    msgs.append({"role": "user", "content": "[Retry] write now"}); h._trace_prompt_deltas(msgs, 2)
    rows = [(e, p) for c, e, p in h.events]
    assert [p["attempt"] for e, p in rows] == [1, 1, 1, 1, 2]
    r = PipelineEngine._rebuild_from_deltas(rows, 30)
    assert r["messages"] == msgs and r["turns"] == 1
