"""A truncated completion and a formatting mistake must not share one answer.

Retained corpus: tests/fixtures/jsonmode_truncation_20260921_trace_7132.jsonl
(run 340aa512, step_instance 7132, 2026-09-21; 95 rows).

The chain that killed the round, each link traceable by seq:

  seq 88   apply_patch preflight: "hunks overlap or are out of order; combine
           them" -- read as "send the whole file in one call";
  seq 90   the agent writes that conclusion down ("go in one ordered patch");
  seq 95   it complies: 20,061 characters, ending `*** End Patch\n}}"}]` --
           the top-level object never closes. `_extract_json` returned None,
           the reply was treated as a formatting mistake, and 15 turns and
           27 minutes ended with zero bytes written.

The two causes are textually separable and this file holds the line between
them: (乙) a structure that opened and never closed is TRUNCATED and is refused
with instruction to split; (甲) a reply with no structure at all is a format
mistake and keeps the old instruction.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.dpe_pipeline import (
    JSON_FAILURE_NO_JSON,
    JSON_FAILURE_TRUNCATED,
    MaxRetriesExceeded,
    PipelineEngine,
    _APPLY_PATCH_MAX_CHARS,
    _truncation_feedback,
    classify_json_failure,
    oversized_patch_refusal,
)

CORPUS = (Path(__file__).resolve().parents[1] / "fixtures"
          / "jsonmode_truncation_20260921_trace_7132.jsonl")


def _rows():
    return [json.loads(line)
            for line in CORPUS.read_text(encoding="utf-8").splitlines()]


def _row(seq):
    return next(r for r in _rows() if r["seq"] == seq)


def _payload(seq):
    return json.loads(_row(seq)["payload_json"])


def test_corpus_is_the_retained_incident():
    """The sample the goal names must be present and shaped as documented."""
    assert len(_rows()) == 95
    assert _row(88)["event"] == "apply_patch"
    assert "hunks overlap or are out of order; combine them" in \
        _payload(88)["error"]
    assert "go in one ordered patch" in _payload(90)["text"]
    truncated = _payload(95)["text"]
    # The trace stores the text inside a JSON string, so what the engine
    # receives ends in a literal backslash-n and backslash-quote. Built with
    # chr(92) so this assertion never carries an escape of its own.
    tail = ("*** End Patch" + chr(92) + "n}}" + chr(92) + chr(34) + "}]")



class TestTheTwoCausesAreToldApart:
    """(乙) and (甲) are different failures and must not share a verdict."""

    def test_the_recorded_truncation_is_classified_as_truncated(self):
        truncated = _payload(95)["text"]
        assert classify_json_failure(truncated) == JSON_FAILURE_TRUNCATED
        assert PipelineEngine._detect_truncated_json(truncated) is True

    def test_pure_prose_is_classified_as_no_json(self):
        """The opposite pole: genuinely no JSON, not one opening brace."""
        prose = ("I have finished reviewing the plan. The work is complete; "
                 "no further edits are needed this turn.")
        assert "{" not in prose
        assert classify_json_failure(prose) == JSON_FAILURE_NO_JSON
        assert PipelineEngine._detect_truncated_json(prose) is False

    def test_the_corpus_has_both_poles(self):
        """Both kinds exist in the retained sample, so the split is real."""
        verdicts = [classify_json_failure(p.get("text") or "")
                    for p in (json.loads(r["payload_json"]) for r in _rows()
                              if r["category"] == "response")
                    if (p.get("text") or "").strip()]
        assert JSON_FAILURE_TRUNCATED in verdicts
        assert JSON_FAILURE_NO_JSON in verdicts

    def test_a_complete_object_is_not_truncated(self):
        assert classify_json_failure('{"thoughts": "ok", "actions": []}') == \
            JSON_FAILURE_NO_JSON

    def test_braces_inside_strings_do_not_fake_a_truncation(self):
        """The old counter ran over the whole text, strings included."""
        complete = json.dumps({"actions": [
            {"tool": "apply_patch", "params": {
                "patch": "*** Begin Patch\n*** Update File: a.py\n@@\n-{ 'x': 1 }\n"
                         "+{ 'x': 2 }\n*** End Patch\n"}}]})
        assert json.loads(complete)
        assert classify_json_failure(complete) == JSON_FAILURE_NO_JSON

    def test_a_truncation_inside_a_string_is_still_a_truncation(self):
        """Cut mid-string: the quote never closes, so the reply was cut off."""
        cut = ('{"actions": [{"tool": "apply_patch", "params": '
               '{"patch": "*** Begin Patch')
        assert classify_json_failure(cut) == JSON_FAILURE_TRUNCATED


class TestTruncationIsRefusedNotRepaired:
    """Half a patch applied as though whole is worse than the round failing."""

    def test_no_repair_helper_exists_to_be_called(self):
        assert not hasattr(PipelineEngine, "_repair_truncated_json")

    def test_a_truncated_patch_never_parses_back_into_a_payload(self):
        truncated = _payload(95)["text"]
        assert PipelineEngine._extract_json(truncated, try_multiple=True) is None
        assert PipelineEngine._extract_json(truncated) is None


class TestTheMessageTellsTheAgentToSplit:
    """Not to reformat — it was already sending JSON."""

    def test_the_message_says_truncated_and_to_split(self):
        msg = _truncation_feedback("x" * 20061)
        assert "TRUNCATED" in msg.upper()
        assert "split" in msg.lower()
        assert "20,061" in msg

    def test_the_message_does_not_reuse_the_format_instruction(self):
        """That line would push it to resend the same oversized payload."""
        msg = _truncation_feedback("x" * 500)
        assert "ONLY a JSON object" not in msg
        assert "not valid JSON" not in msg

    def test_the_message_forbids_resending_the_same_payload(self):
        assert "not resend the same" in _truncation_feedback("x" * 10).lower()


class TestOversizedPatchIsRefusedBeforeSubmission:
    """The ceiling the upstream preflight advice cannot push past."""

    def test_a_huge_patch_is_refused_with_a_split_instruction(self):
        refusal = oversized_patch_refusal(
            {"patch": "x" * (_APPLY_PATCH_MAX_CHARS + 1)})
        assert refusal is not None
        assert refusal["oversized"] is True
        assert "split" in refusal["error"].lower()
        assert "hunks" in refusal["error"].lower()

    def test_a_small_patch_passes(self):
        assert oversized_patch_refusal(
            {"patch": "*** Begin Patch\n*** End Patch\n"}) is None

    def test_both_addressing_modes_count_toward_the_ceiling(self):
        half = "x" * (_APPLY_PATCH_MAX_CHARS // 2 + 10)
        refusal = oversized_patch_refusal({"patch": half, "references": half})
        assert refusal is not None and refusal["chars"] > _APPLY_PATCH_MAX_CHARS

    def test_a_non_dict_params_is_not_a_crash(self):
        assert oversized_patch_refusal(None) is None
        assert oversized_patch_refusal([1, 2, 3]) is None


# ── The turn loops, end to end ────────────────────────────────────────────
# The unit poles above say which verdict is correct. These say what the run
# DOES with it: the round is handed back and the agent continues to deliver.

def _action(tool, **params):
    return {"tool": tool, "params": params}


def _response(*actions):
    return json.dumps({"actions": actions})


def _engine(tmp_path, responses):
    """Real JSON turn loop; only the model and the tools are stubbed."""
    e = object.__new__(PipelineEngine)
    answers = list(responses)
    e.calls, e.events, e.traces, e.prompts = [], [], [], []

    def run(prompt):
        e.prompts.append(prompt)
        return answers.pop(0)

    agent = SimpleNamespace(run=run,
                            gateway=SimpleNamespace(litellm_model="stub"))
    e.factory = SimpleNamespace(
        get_agent=lambda _: agent,
        get_max_retries=lambda _: 2,
        get_max_tool_turns=lambda _: 12)
    e.assembler = SimpleNamespace(assemble=lambda *a, **k: str(a[3]))
    e._agent_role = lambda _: "green"
    e._get_project_path = lambda *a: tmp_path
    e._get_code_path = lambda *a: tmp_path
    e._refuse_if_run_cancelled = lambda *a: None
    e._note_feedback = lambda *a: None
    e._repeat_note = lambda *a: ""
    e._unresolved_note = lambda *a: ""
    e._emit = lambda kind, data=None: e.events.append((kind, data))
    e._trace = lambda category, event, payload: e.traces.append(
        (category, event, payload))
    e._max_tool_turns = 0
    e._resolved_context = {}
    e._user_lang = ""
    e._tool_schemas = {"create": {}, "apply_patch": {}, "read": {},
                       "finish_step": {}}
    e._output_target = "code"
    e._output_fixed = {}

    def execute(call):
        e.calls.append(call)
        name, params = call["tool"], call.get("params", {})
        if name == "read":
            return {"content": "baseline"}
        if name == "create":
            path = tmp_path / params["file"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params["content"])
            return {"written": params["file"]}
        if name == "apply_patch":
            # The host boundary's own refusal, as _exec_tool would return it.
            return oversized_patch_refusal(params) or {"applied": True}
        raise AssertionError(name)

    e._exec_tool = execute
    return e


def _run(e):
    return e._run_tool_step(1, "implement", None, "fixture",
                            agent_config_name="stub")


class TestATruncationDoesNotEndTheRound:
    """The round is handed BACK to the agent, which continues and delivers."""

    @staticmethod
    def _truncation():
        return _payload(95)["text"]

    def test_the_round_survives_and_the_agent_delivers(self, tmp_path):
        e = _engine(tmp_path, [
            self._truncation(),
            _response(_action("create", file="final/d.md",
                              content="candidate"))])
        assert _run(e) is True
        assert (tmp_path / "final/d.md").read_text() == "candidate"

    def test_the_agent_gets_another_turn_naming_truncation_and_split(
            self, tmp_path):
        e = _engine(tmp_path, [
            self._truncation(),
            _response(_action("create", file="a.md", content="x"))])
        assert _run(e) is True
        assert len(e.prompts) >= 2, "the round was not handed back"
        # What the agent was actually shown on the turn after the truncation.
        seen = e.prompts[1] + json.dumps([d for _k, d in e.events])
        assert "TRUNCATED" in seen.upper()
        assert "split" in seen.lower()

    def test_the_refusal_is_traced_not_silent(self, tmp_path):
        e = _engine(tmp_path, [
            self._truncation(),
            _response(_action("create", file="a.md", content="x"))])
        assert _run(e) is True
        assert "truncation_refused" in [ev for _c, ev, _p in e.traces]

    def test_nothing_from_the_truncated_payload_is_applied(self, tmp_path):
        """No half patch: the recorded reply's patch is never submitted."""
        e = _engine(tmp_path, [
            self._truncation(),
            _response(_action("create", file="a.md", content="x"))])
        assert _run(e) is True
        assert [c["tool"] for c in e.calls] == ["create"]

    def test_a_write_too_large_for_one_response_is_refused_and_split(
            self, tmp_path):
        """The ceiling the seq-88 advice cannot push past."""
        huge = ("*** Begin Patch\n*** Add File: a.py\n"
                + "x" * (_APPLY_PATCH_MAX_CHARS + 100)
                + "\n*** End Patch\n")
        e = _engine(tmp_path, [
            _response(_action("apply_patch", patch=huge)),
            _response(_action("create", file="a.py", content="value = 1\n"))])
        assert _run(e) is True
        assert (tmp_path / "a.py").read_text() == "value = 1\n"
        seen = json.dumps([d for k, d in e.events])
        assert "split" in seen.lower()

    def test_pure_prose_behaviour_is_unchanged(self, tmp_path):
        """(甲) keeps its own, different instruction — the reformat one."""
        e = _engine(tmp_path, ["I reviewed the plan; no edits are needed."])
        with pytest.raises(MaxRetriesExceeded, match="parse JSON"):
            _run(e)
        errors = [d["error"] for k, d in e.events if k == "parse_error"]
        assert errors and "ONLY a JSON object" in errors[-1]
        assert not any(k == "truncation_detected" for k, _ in e.events)
