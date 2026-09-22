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
import re
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
from core.output_migration import (STRICT_PATCH_GUIDANCE_EN,
                                   STRICT_PATCH_GUIDANCE_ZH)

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
    # The recorded reply is an apply_patch body cut off mid-file: the top-level
    # object never closes. Assert that shape from the data itself -- the old
    # body computed `truncated` and `tail` and asserted NEITHER (the pass-on-
    # absence the review named). Reading the real ending avoids re-typing an
    # escape that silently did not match.
    tail = truncated.rstrip()[-24:]
    assert "*** End Patch" in tail, "the reply is cut off right after End Patch"
    assert tail.endswith("}]"), \
        "the top-level object never closes: the reply ends in the array close"
    assert classify_json_failure(truncated) == JSON_FAILURE_TRUNCATED, \
        "an opened-but-unclosed object is the (乙) truncation pole, not (甲)"


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

    def test_prose_with_parentheses_is_not_a_truncation(self):
        """The theme of this card: a `(` used to count as an unclosable bracket.

        Every one of these is the (甲) formatting pole -- ordinary prose with a
        round bracket, nothing JSON-like to close. A `(` must never raise the
        depth counter, because a `)` does not lower it in JSON text.
        """
        for text in ("I reviewed the plan (no edits needed).", "(a)"):
            assert classify_json_failure(text) == JSON_FAILURE_NO_JSON, text
            assert PipelineEngine._detect_truncated_json(text) is False, text

    def test_seq_64_is_a_complete_reply_not_a_truncation(self):
        """The corpus line the candidate mis-called TRUNCATED.

        seq 64 is prose with a fully-closed JSON object embedded in it; the
        engine extracted it and its read call ran at seq 66, so the round
        continued. The prose contains a round bracket -- exactly the character
        that used to raise a depth `(` never brought back down -- so this line
        is the canary for the mirror-image bug.
        """
        reply = _payload(64)["text"]
        assert "(" in reply, "seq 64's prose carries a round bracket"
        assert PipelineEngine._extract_json(reply) is not None, \
            "seq 64 is a complete reply: its JSON extracts and runs"
        assert classify_json_failure(reply) == JSON_FAILURE_NO_JSON, \
            "a round bracket must not make a complete reply read as truncated"
        assert PipelineEngine._detect_truncated_json(reply) is False

    def test_the_corpus_carries_only_the_truncation_pole(self):
        """Why the old 'both poles' assertion was vacuous, and what replaces it.

        The retained sample is the (乙) incident: every response line carries
        JSON structure, so a genuine formatting-mistake line with nothing to
        close cannot occur here -- the corpus is the authority for truncation
        and the regression, never for (甲). The (甲) pole is supplied by the
        external literals above, not by editing the corpus. Asserting the
        structural fact is a real check: the old test passed even on a (甲)-
        free corpus because NO_JSON is what a COMPLETE object also returns.
        """
        texts = [(json.loads(r["payload_json"]).get("text") or "")
                 for r in _rows() if r["category"] == "response"]
        texts = [t for t in texts if t.strip()]
        assert texts, "the corpus must carry response rows"
        assert all("{" in t for t in texts), \
            "the corpus has no `{`-free line, so (甲) cannot live here"
        assert any(classify_json_failure(t) == JSON_FAILURE_TRUNCATED
                   for t in texts), "the retained (乙) pole must still be present"

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
    """Not to reformat: it was already sending JSON."""

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

    def test_the_real_seq_85_payload_would_be_refused(self):
        """A length taken from the corpus, not from the constant.

        The other ceiling tests write `_APPLY_PATCH_MAX_CHARS + 1`, so raising
        the constant to 30,000 leaves every one of them green. seq 85's own
        patch body is 16,047 characters: whatever the ceiling is set to, a
        payload of that real size must not be admitted, so this assertion goes
        red the moment the ceiling is raised above 16,047.
        """
        reply = _payload(85)["text"]
        assert len(reply) == 17070, len(reply)
        body = re.search(r'"patch": "(.*?)\*\*\* End Patch"', reply, re.S)
        assert body, "seq 85 carries the patch body"
        patch = json.loads('"%s*** End Patch"' % body.group(1))
        assert len(patch) == 16047, len(patch)
        refusal = oversized_patch_refusal({"patch": patch})
        assert refusal is not None, (
            "a 16,047-character patch was admitted: the ceiling is above the "
            "payload that was actually cut off")



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

    def test_the_agent_prompt_names_truncation_and_split(self, tmp_path):
        """The message must reach the AGENT's own next prompt.

        The previous assertion read `prompts[1] + json.dumps(events)` — a
        cross-channel OR that stays green even if the agent never sees the
        message. This check reads the PROMPT channel ALONE, so it goes red if
        the message reaches SSE but never the agent.
        """
        e = _engine(tmp_path, [
            self._truncation(),
            _response(_action("create", file="a.md", content="x"))])
        assert _run(e) is True
        assert len(e.prompts) >= 2, "the round was not handed back"
        assert "TRUNCATED" in e.prompts[1].upper(), \
            "the agent's next prompt never named the truncation"
        assert "split" in e.prompts[1].lower(), \
            "the agent's next prompt never told it to split"

    def test_the_sse_channel_names_truncation_and_split(self, tmp_path):
        """The same message must reach the SSE event stream, asserted ALONE."""
        e = _engine(tmp_path, [
            self._truncation(),
            _response(_action("create", file="a.md", content="x"))])
        assert _run(e) is True
        sse = json.dumps([d for _k, d in e.events])
        assert "TRUNCATED" in sse.upper()
        assert "split" in sse.lower()

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


def test_paren_prose_behaviour_is_unchanged_end_to_end(tmp_path):
    """Extreme (2), restored: a paren-bearing reply is (甲), not a cut.

    The candidate told such a reply it had been 'TRUNCATED ... after 67
    characters' — nothing truncated it; it merely contained a '('. The base
    behaviour (raise MaxRetriesExceeded with the reformat instruction) must
    hold even when the prose carries a round bracket. At least one prose
    test in this file MUST use a parenthesis, or the mirror-image regression
    is invisible to the suite.
    """
    prose = ("I reviewed the plan (no edits needed); nothing further "
             "is required this turn.")
    assert "(" in prose and len(prose) < _APPLY_PATCH_MAX_CHARS
    e = _engine(tmp_path, [prose])
    with pytest.raises(MaxRetriesExceeded, match="parse JSON"):
        _run(e)
    errors = [d["error"] for k, d in e.events if k == "parse_error"]
    assert errors and "ONLY a JSON object" in errors[-1]
    assert not any(k == "truncation_detected" for k, _ in e.events)
    assert not [p for p in e.prompts[1:] if "TRUNCATED" in p.upper()]


# ── The prose sent to the agent is a deliverable ──────────────────────────
# The corpus below is every surface that carries agent-facing prose in a code
# step. The checker is not asserted to cover "this class"; it is measured
# against the five corruptions already in git, one at a time. Each listed rule
# has been observed to fire on those exact bytes and to name its own file:
#
#   fixture (git bytes)                        rule that fires
#   duplicate banner, core/dpe_pipeline.py     adjacent_duplicate_line
#   r2 EN duplicated two-line run, 3b3d6560    repeated_run (GUIDANCE_EN)
#   r2 ZH corruption, 3b3d6560                 repeated_run (GUIDANCE_ZH)
#   r3 ZH trailing backtick, fad833b0          odd_backtick_count
#   fix_tests.md clipped block, 3b3d6560       severed_clause
#
# test below is what keeps it honest.
REPO_ROOT = Path(__file__).resolve().parents[2]
_LONG_LINE = 25
STRICT_GUIDANCE_BLOCKS = ("GUIDANCE_EN", "GUIDANCE_ZH",
                          "templates/fix_tests.md")


def _read(rel):
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _prose_corpus():
    """Every agent-facing prose surface, keyed by the path that carries it."""
    corpus = {
        "GUIDANCE_EN": STRICT_PATCH_GUIDANCE_EN,
        "GUIDANCE_ZH": STRICT_PATCH_GUIDANCE_ZH,
    }
    for rel in ("templates/fix_tests.md", "templates/coding_impl.md",
                "core/dpe_pipeline.py", "core/prompt_assembler.py",
                "core/output_migration.py"):
        corpus[rel] = _read(rel)
    return corpus


def _prose_violations(name, text):
    """Rule violations in one prose surface; empty means intact."""
    violations = []
    lines = [line.strip() for line in text.splitlines()]
    long_at = {i for i, line in enumerate(lines) if len(line) >= _LONG_LINE}

    for i in range(1, len(lines)):
        if i in long_at and i - 1 in long_at and lines[i] == lines[i - 1]:
            violations.append(
                f"adjacent_duplicate_line: {name}:{i + 1}: {lines[i]!r}")

    if name in STRICT_GUIDANCE_BLOCKS:
        runs = {}
        for i in range(len(lines) - 1):
            if i in long_at and i + 1 in long_at:
                runs.setdefault((lines[i], lines[i + 1]), []).append(i + 1)
        for run, at in runs.items():
            if len(at) > 1:
                violations.append(
                    f"repeated_run: {name}: lines {at} carry the same two "
                    f"lines: {run[0]!r}")

    if text.count("`") % 2:
        violations.append(
            f"odd_backtick_count: {name}: {text.count('`')} backticks, so a "
            f"code span closes early")

    if "stale or file." in " ".join(text.split()):
        violations.append(
            f"severed_clause: {name}: 'stale or file.' is a deleted clause")
    return violations


def test_the_agent_facing_prose_is_intact():
    corpus = _prose_corpus()
    assert corpus, "the corpus must not be empty"
    survivors = {name: _prose_violations(name, text)
                 for name, text in corpus.items()}
    assert not {k: v for k, v in survivors.items() if v}, survivors


def test_the_checker_reads_every_file_that_carries_agent_prose():
    """A scope narrower than the prose is a checker that cannot see a defect."""
    corpus = _prose_corpus()
    assert "core/dpe_pipeline.py" in corpus, \
        "the truncation classifier's own prose lives in dpe_pipeline.py"
    assert "core/prompt_assembler.py" in corpus
    assert "core/output_migration.py" in corpus
    assert "templates/fix_tests.md" in corpus
    assert set(STRICT_GUIDANCE_BLOCKS) <= set(corpus)
    for name in corpus:
        assert name == "GUIDANCE_EN" or name == "GUIDANCE_ZH" \
            or (REPO_ROOT / name).is_file(), name
    assert "STRICT_PATCH_GUIDANCE_ZH" not in corpus


def _git_file(rev, rel):
    import subprocess
    out = subprocess.run(["git", "show", f"{rev}:{rel}"],
                         cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _git_guidance(rev, block):
    text = _git_file(rev, "core/output_migration.py")
    match = re.search(
        rf'STRICT_PATCH_GUIDANCE_{block} = """(.*?)"""', text, re.S)
    assert match, f"{rev} carries no STRICT_PATCH_GUIDANCE_{block}"
    return match.group(1)


R2 = "3b3d6560"          # r2 candidate: EN pair-repeat, ZH corruption, banner
R3 = "fad833b016a5c54d1a5f6253f4fcdfc0fbeb9722"   # r3 candidate: ZH backtick

GIT_CORRUPTIONS = {
    "r2 EN duplicated two-line run": (
        "GUIDANCE_EN", lambda: _git_guidance(R2, "EN")),
    "r2 ZH corruption": (
        "GUIDANCE_ZH", lambda: _git_guidance(R2, "ZH")),
    "duplicate banner in core/dpe_pipeline.py": (
        "core/dpe_pipeline.py",
        lambda: _git_file(R2, "core/dpe_pipeline.py")),
    "r3 ZH trailing backtick": (
        "GUIDANCE_ZH", lambda: _git_guidance(R3, "ZH")),
    "fix_tests.md clipped block": (
        "templates/fix_tests.md",
        lambda: _git_file(R2, "templates/fix_tests.md")),
}
@pytest.mark.parametrize("label", sorted(GIT_CORRUPTIONS))
def test_each_known_corruption_is_caught_by_name(label):
    name, load = GIT_CORRUPTIONS[label]
    corrupted = load()
    clean = _prose_corpus()[name]
    assert corrupted != clean, f"{label}: the fixture is not actually corrupt"
    violations = _prose_violations(name, corrupted)
    assert violations, f"{label}: the checker stayed silent on {name}"
    assert any(name in v for v in violations), violations
    assert not _prose_violations(name, clean), (name, "clean copy is red")


# ── The apply_patch grant boundary ───────────────────────────────────────
# `_exec_tool` refuses apply_patch unless the step's OWN schema carries it.
# Without a reader, deleting that clause changes no test result, so a step
# whose schema never granted apply_patch would reach the globally registered
# tool. The two tests below are that reader.


def _tool_engine(monkeypatch, schemas):
    import api.dependencies as dependencies
    from unittest.mock import patch as _patch

    seen = {}

    class FakeSkillFlow:
        def execute_tool(self, name, params, **host):
            seen["name"] = name
            seen["params"] = params
            return {"ok": True}

    monkeypatch.setattr(dependencies, "get_skillflow",
                        lambda: FakeSkillFlow())
    with _patch("core.agents.AgentFactory.__init__", return_value=None):
        engine = PipelineEngine()
    if schemas is not None:
        engine._tool_schemas = schemas
    engine._output_target = "code"
    engine._output_fixed = {}
    engine._write_scope = None
    engine._run_id = "run"
    engine._current_step = "implement"
    engine._step_instance_id = 1
    engine._trace = lambda *a, **k: None
    engine._emit = lambda *a, **k: None
    return engine, seen


def test_apply_patch_needs_a_schema_boundary_that_carries_it(monkeypatch):
    """The `or "apply_patch" not in schemas` half of the refusal.

    With no `_tool_schemas` attribute at all, the earlier `tool_name not in
    schemas` guard has no boundary to enforce, so THIS clause is the only
    thing between such an engine and the globally registered apply_patch tool.
    Delete the clause and the call below reaches SkillFlow.
    """
    engine, seen = _tool_engine(monkeypatch, None)
    result = engine._exec_tool({
        "tool": "apply_patch",
        "params": {"patch": "*** Begin Patch\n*** End Patch\n"},
    })
    assert "apply_patch requires a granted generic code-output step" in \
        result["error"], result
    assert seen == {}, "the un-granted call reached SkillFlow"
    assert seen == {}, "the un-granted call reached SkillFlow"


def test_apply_patch_with_its_own_grant_reaches_skillflow(monkeypatch):
    engine, seen = _tool_engine(monkeypatch, {"apply_patch": {}, "read": {}})
    result = engine._exec_tool({
        "tool": "apply_patch",
        "params": {"patch": "*** Begin Patch\n*** End Patch\n"},
    })
    assert result == {"ok": True}
    assert seen["name"] == "apply_patch"

