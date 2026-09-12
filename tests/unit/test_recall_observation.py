"""recall_observation: the way back from a compacted tool result.

`_project_native_messages` replaces older large tool results with a marker
(sha256 + preview) and keeps the full text in the loop's `messages`. The
marker used to say the result was "retained in the durable tool trace" and
nothing could fetch it — and the `tool_result` trace events it pointed at are
20K-clipped summaries anyway. The agent's only recourse was to re-run the tool,
which for a search, a playtest or a test run is a different result.
"""
import inspect
import json

import pytest

from core.dpe_pipeline import (
    PipelineEngine, _RECALL_MAX_CHARS, _RECALL_MAX_MATCHES,
    _observation_digest, _progress_signature, _project_native_messages,
    _recall_observation,
)


def _tool(content, cid):
    return {"role": "tool", "tool_call_id": cid, "content": content}


def _big(n_lines=2000):
    return "\n".join(f"line {i}: " + ("x" * 30) for i in range(n_lines))


def _history():
    old = _big()
    msgs = [{"role": "system", "content": "S"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            _tool(old, "c1"),
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c2"}]},
            _tool("latest", "c2")]
    return msgs, old


def _compacted_marker(msgs):
    projected, report = _project_native_messages(msgs, history_char_budget=1024)
    assert report["compacted_tool_results"] == 1
    return json.loads(projected[2]["content"])


# ── the marker → recall round trip ─────────────────────────────────────────

def test_the_marker_teaches_the_recall_and_its_id_resolves():
    msgs, old = _history()
    marker = _compacted_marker(msgs)
    assert marker["_aitelier_compacted"] is True
    assert "recall_observation(sha256=\"%s\"" % marker["sha256"] in marker["note"]
    assert "re-run" in marker["note"]

    res = _recall_observation(msgs, marker["sha256"], start=0, end=200)
    assert res["content"] == old[:200]
    assert res["original_chars"] == len(old)
    assert res["sha256"] == marker["sha256"] == _observation_digest(old)


def test_a_slice_is_bounded_and_says_where_to_continue():
    msgs, old = _history()
    sha = _compacted_marker(msgs)["sha256"]
    res = _recall_observation(msgs, sha)                 # no range: from 0
    assert len(res["content"]) == _RECALL_MAX_CHARS < len(old)
    assert res["truncated"] is True and res["next_start"] == _RECALL_MAX_CHARS
    nxt = _recall_observation(msgs, sha, start=res["next_start"])
    assert nxt["content"] == old[_RECALL_MAX_CHARS:2 * _RECALL_MAX_CHARS]
    assert res["content"] + nxt["content"] == old[:2 * _RECALL_MAX_CHARS]


def test_grep_returns_numbered_lines_with_context_and_is_capped():
    msgs, _ = _history()
    sha = _compacted_marker(msgs)["sha256"]
    res = _recall_observation(msgs, sha, grep=r"^line 1500:")
    assert res["matches"] == 1 and res["shown"] == 1
    hit = res["lines"][0]
    assert hit["line"] == 1501                       # 1-based
    assert "1501: line 1500:" in hit["text"]
    assert "1499: " in hit["text"] and "1503: " in hit["text"]   # ±2 context

    many = _recall_observation(msgs, sha, grep=r"^line 1\d\d\d:")
    assert many["matches"] == 1000
    assert many["shown"] == _RECALL_MAX_MATCHES and many["truncated"] is True


def test_a_prefix_of_the_id_is_enough_and_the_full_result_still_wins():
    msgs, old = _history()
    sha = _compacted_marker(msgs)["sha256"]
    assert _recall_observation(msgs, sha[:8], end=5)["content"] == old[:5]


# ── refusals name the cause ────────────────────────────────────────────────

@pytest.mark.parametrize("sha, kw, needle", [
    ("0000000000000000", {}, "no tool result"),
    ("abc", {}, "at least its first 8"),
    (None, {"grep": "["}, None),                       # filled in below
])
def test_refusals(sha, kw, needle):
    msgs, _ = _history()
    if sha is None:
        sha, needle = _compacted_marker(msgs)["sha256"], "not a valid regex"
    res = _recall_observation(msgs, sha, **kw)
    assert needle in res["error"]


def test_a_recall_is_not_progress_for_ask_more_turns():
    """Reading back what was already seen earns no extra turns."""
    assert _progress_signature("recall_observation", {"sha256": "x"},
                               {"content": "y"}) is None


# ── wiring: host-level, never sent to skillflow ────────────────────────────

def test_exec_tool_answers_recall_from_the_loop_messages_without_skillflow(monkeypatch):
    import api.dependencies as deps

    def _no(*a, **k):
        raise AssertionError("recall_observation must not reach skillflow")
    monkeypatch.setattr(deps, "get_skillflow", _no)

    e = object.__new__(PipelineEngine)
    traced = []
    e._trace = lambda c, ev, p=None: traced.append((c, ev, p))
    msgs, old = _history()
    e._native_messages = msgs
    sha = _compacted_marker(msgs)["sha256"]

    res = e._exec_tool({"tool": "recall_observation",
                        "params": {"sha256": sha, "start": 10, "end": 20}})
    assert res["content"] == old[10:20]
    assert [(c, ev) for c, ev, _ in traced] == [("step", "observation_recalled")]
    assert traced[0][2]["returned_chars"] == 10

    # before the loop published its list there is nothing to recall — an
    # error, not an exception
    del e._native_messages
    assert "no tool result" in e._exec_tool(
        {"tool": "recall_observation", "params": {"sha256": sha}})["error"]


def test_the_native_loop_keeps_full_observations_across_context_segments():
    """Recall owns a full-observation store, independent of provider segments."""
    src = inspect.getsource(PipelineEngine._run_native_step)
    assert "self._native_messages.append(tool_message)" in src
    assert 'resume.get("recall_messages")' in src
    assert '"recall_observation" not in self._tool_schemas' in src
