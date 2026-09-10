"""A repeat read is answered with a recall id instead of being run again.

The sect-curriculum run (2026-09-10) spent 40 of its 48 turns reading —
player_profile.gd five times, progression_gongfa_data.gd eight — and called
recall_observation ZERO times. The way back from a compacted result existed
and the agent never took it, so the host takes it for them.

What these tests guard is the half that is cheap to get wrong and expensive to
discover: WHICH tools may be answered from the index, and whether the id the
loop records is one recall_observation can actually resolve. The loop wiring
itself is proven from a live run's `repeat_call_deduped` trace event, not here.
"""
import json

from core.dpe_pipeline import (
    _REPEATABLE_READ_TOOLS,
    _REPEAT_MIN_CHARS,
    _observation_digest,
    _recall_observation,
    _repeat_call_key,
)

# Every tool whose answer depends on state this step may have changed, or on
# running something. Serving any of these from the index would hand back a
# stale verdict wearing a fresh one's clothes.
MUST_NEVER_BE_DEDUPED = {
    "run_tests", "pytest", "godot_compile", "godot_playtest",
    "godot_playtest_scenario", "godot_vision", "unity_compile",
    "unity_playtest", "repo_apply", "repo_delete", "repo_remove_file",
    "write", "test_write", "restage", "apply_state", "scaffold",
    "register_tool", "git_push_post", "closeout_gate", "run_gate",
}


def test_no_gate_or_writing_tool_may_be_answered_from_the_index():
    overlap = _REPEATABLE_READ_TOOLS & MUST_NEVER_BE_DEDUPED
    assert not overlap, (
        "these would serve a stale answer as a current one: %s" % sorted(overlap))


def test_the_allowlist_is_an_allowlist():
    """A blocklist inherits every tool added after it was written; this must
    stay closed. The failure of a missing entry is a wasted read, which is the
    direction that is safe to be wrong in."""
    assert isinstance(_REPEATABLE_READ_TOOLS, frozenset)
    assert _REPEATABLE_READ_TOOLS, "an empty allowlist disables the mechanism"


def test_same_tool_different_arguments_is_not_a_repeat():
    a = _repeat_call_key("read_file", {"path": "a.gd"})
    b = _repeat_call_key("read_file", {"path": "b.gd"})
    assert a != b


def test_argument_order_does_not_make_a_new_call():
    a = _repeat_call_key("semantic_search", {"query": "x", "limit": 8})
    b = _repeat_call_key("semantic_search", {"limit": 8, "query": "x"})
    assert a == b, "key must not depend on dict ordering"


def test_the_same_tool_name_under_different_tools_is_distinct():
    assert (_repeat_call_key("read_file", {"path": "a.gd"})
            != _repeat_call_key("list_tree", {"path": "a.gd"}))


def test_the_id_the_loop_records_is_one_recall_can_resolve():
    """The loop stores `_observation_digest(result_str)` and tells the agent to
    pass it to recall_observation. If those two ever disagree the note is a
    dead end — which is the exact failure recall_observation was built to end."""
    result_str = json.dumps({"content": "X" * (_REPEAT_MIN_CHARS + 10)})
    digest = _observation_digest(result_str)
    messages = [{"role": "tool", "tool_call_id": "1", "content": result_str}]

    got = _recall_observation(messages, digest, start=0, end=32)

    assert "error" not in got, got
    assert got["sha256"] == digest
    assert got["original_chars"] == len(result_str)


def test_a_short_prefix_of_the_id_still_resolves():
    """The note quotes the full 16-char digest, but an agent that copies only
    part of it must not be sent away empty-handed."""
    result_str = json.dumps({"content": "Y" * 5000})
    digest = _observation_digest(result_str)
    messages = [{"role": "tool", "content": result_str}]

    assert "error" not in _recall_observation(messages, digest[:8])
