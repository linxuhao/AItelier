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


import ast
import json
import re
import sys
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
from tools.prose_corruptions import catalog as CATALOG


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

    def test_a_bracket_inside_a_string_value_is_not_a_truncation(self):
        """The m4 blind spot: an unpaired bracket inside a JSON string.

        A string VALUE may carry '(' or an unterminated '['; the depth
        counter reads structure, so a complete reply that merely quotes a
        bracket must still classify as no-JSON, never truncated.
        """
        for text in ('{"a": "("}', '{"a": "see (below", "b": [1, 2]}'):
            assert json.loads(text)
            assert classify_json_failure(text) == JSON_FAILURE_NO_JSON, text
            assert PipelineEngine._detect_truncated_json(text) is False, text
        # The mirror pole: a string left open AFTER the braces balance is a
        # truncation that only the in_string half of the verdict can see.
        cut = '{"a": "x"} trailing "'
        assert classify_json_failure(cut) == JSON_FAILURE_TRUNCATED, cut

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
# The corpus is DERIVED, not declared: every templates/*.md file on disk
# (glob, not a list), the agent_configs system prompts, and the code modules
# and constants that inject prose into agent prompts. The corruption bytes
# that measure the checker are supplied by the director and live in ONE place,
# tools/prose_corruptions/catalog.py, one byte string per corruption; that
# catalog is the fixture source for the checks below, so the bytes that measure
# the checker and the bytes the runner applies to a clean worktree are the same
# bytes. Adding an entry there adds it to the parametrized checks below.
#
# The rule each corruption must fire (id -> rule, named in the catalog):
#
#   K1/K2 fix_tests.md sha advice replaced    stale_hunk_remedy,
#                                             reference_advice_missing
#   K3   ZH reference example unannounced     unannounced_reference_example
#   K4/K5 new module prompt constant          unaccounted_prose_constant
#   K6a  r2 EN duplicated two-line run        repeated_run
#   K6b  r2 ZH corruption                     repeated_run
#   K6c  duplicate banner core/dpe_pipeline   adjacent_duplicate_line
#   K6d  r3 ZH trailing backtick              odd_backtick_count
#   K6e  fix_tests.md clipped block           severed_clause
#
# Because the corpus is derived from the filesystem, planting a defect in ANY
# file under templates/ — a new zz_probe.md, task_implementer.md,
# game_designer.md, any of them — turns test_the_agent_facing_prose_is_intact
# red naming that file, and
# test_a_duplicate_line_planted_in_any_surface_fires_by_name proves the
# per-surface attribution mechanically.
REPO_ROOT = Path(__file__).resolve().parents[2]
_LONG_LINE = 25
STRICT_GUIDANCE_BLOCKS = ("GUIDANCE_EN", "GUIDANCE_ZH",
                          "templates/fix_tests.md")

# Modules whose prose reaches agent prompts wholesale. Every multi-line
# string constant in these files is inside the corpus without enumeration,
# because the whole file text is the corpus entry.
PROSE_CODE_FILES = ("core/dpe_pipeline.py", "core/prompt_assembler.py",
                    "core/output_migration.py")

# Multi-line constants elsewhere in core/ that ARE agent-facing prose: the
# meta agent and meta-conversation prompts and the MCP onboarding guide are
# read by an agent, so they are corpus surfaces even though they are not the
# apply_patch guidance blocks. Keyed by the surface name the corpus uses.
PROSE_PROMPT_CONSTANTS = {
    "core/meta_agent.py:SYSTEM_PROMPT":
        ("core/meta_agent.py", "SYSTEM_PROMPT"),
    "core/meta_conversation.py:REVISION_SYSTEM_PROMPT":
        ("core/meta_conversation.py", "REVISION_SYSTEM_PROMPT"),
    "core/meta_conversation.py:_INTENT_SYSTEM_PROMPT":
        ("core/meta_conversation.py", "_INTENT_SYSTEM_PROMPT"),
    "core/meta_conversation.py:_SPEC_HEADER":
        ("core/meta_conversation.py", "_SPEC_HEADER"),
    # These two JSON schemas are concatenated straight into the meta agent's
    # and the intent classifier's system prompt (core/meta_conversation.py:
    # `self.system_prompt + META_JSON_SCHEMA`, `_INTENT_SYSTEM_PROMPT +
    # _INTENT_SCHEMA`), so an agent reads them as prose. They are corpus
    # surfaces, not exemptions — the exemption header claims its entries never
    # reach an agent prompt, which is false for anything spliced into one.
    "core/meta_conversation.py:META_JSON_SCHEMA":
        ("core/meta_conversation.py", "META_JSON_SCHEMA"),
    "core/meta_conversation.py:_INTENT_SCHEMA":
        ("core/meta_conversation.py", "_INTENT_SCHEMA"),
    "core/state_driver_guide.py:STATE_DRIVER_GUIDE":
        ("core/state_driver_guide.py", "STATE_DRIVER_GUIDE"),
}

# The exemption declaration: a multi-line prose constant in core/ that is
# neither in a corpus module nor in PROSE_PROMPT_CONSTANTS must be named here
# as (module path, constant name) with the reason it never reaches an agent
# prompt. The MODULE is part of the key on purpose: a new module that declares
# its own constant called `SYSTEM_PROMPT` is a new agent-facing prompt, not the
# exempted constant of a different file, and a bare name let it through.
PROSE_CONSTANT_EXEMPTIONS = {
    # JSON schemas / DDL / SQL: machine-readable contracts, parsed or executed
    # rather than folded into a prompt as prose.
    ("core/director_messaging.py", "SCHEMA"):
        "SQL DDL for the director notes table",
    ("core/state_attempts.py", "SCHEMA"):
        "SQL DDL schema for the attempt tables",
    ("core/state_attempt_schema.py", "ATTEMPT_TABLE"):
        "SQL DDL for the attempts table",
    ("core/state_attempt_schema.py", "EXTRA_SCHEMA"):
        "JSON schema for structured state output validation",
    ("core/state_design.py", "SCHEMA"):
        "SQL DDL schema for the design tables",
    ("core/state_driver_index.py", "ENTRY_SCHEMA"):
        "JSON schema for state driver index entries",
    ("core/state_driver_notes.py", "SCHEMA"):
        "SQL DDL for the driver notes table",
    ("core/state_graph.py", "SCHEMA"):
        "SQL DDL schema for the state graph tables",
    ("core/state_issues.py", "SCHEMA"):
        "SQL DDL schema for the issue tables",
    ("core/state_metadata.py", "SCHEMA"):
        "SQL DDL schema for the state metadata table",
    ("core/state_run_summary.py", "USAGE_SQL"):
        "SQL query for token usage accounting",
    # Executable plumbing, never rendered into a prompt.
    ("core/tool_guards.py", "probe"):
        "function-local out-of-process importability probe script",
}


def _read(rel):
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _guidance_from_file(text, block):
    """The `STRICT_PATCH_GUIDANCE_<block>` literal inside `text`."""
    match = re.search(
        rf'STRICT_PATCH_GUIDANCE_{block} = """(.*?)"""', text, re.S)
    assert match, f"no STRICT_PATCH_GUIDANCE_{block} in the file text"
    return match.group(1)


def _prose_corpus(reader=None):
    """Every agent-facing prose surface, derived from the filesystem.

    Nothing here is a hand-written list of surfaces: templates/*.md is a
    glob, agent_configs/*.yaml is a glob, and the prompt constants are read
    out of the modules that declare them, so a surface created after this
    test was written is IN the corpus on the next run without touching this
    file.

    `reader` is the source the file-backed surfaces are read from: the live
    worktree by default (resolved at call time, so a test can redirect
    `_read`), so a corruption run's damaged tree goes red in
    `test_the_empty_mutation_leaves_every_surface_clean`; the self-proof
    fixtures pass `_head_read` instead, so their clean pole is HEAD and stays
    intact while the live tree is corrupted.
    """
    import yaml

    if reader is None:
        reader = _read
    # The guidance blocks are corpus surfaces too, and they are read through
    # the same `reader`: a runner that damaged core/output_migration.py must
    # show up as a violation on GUIDANCE_EN / GUIDANCE_ZH, not be masked by
    # the module constant that was imported once at collection time.
    migration = reader("core/output_migration.py")
    corpus = {
        "GUIDANCE_EN": _guidance_from_file(migration, "EN"),
        "GUIDANCE_ZH": _guidance_from_file(migration, "ZH"),
    }
    for md in sorted((REPO_ROOT / "templates").glob("*.md")):
        corpus[f"templates/{md.name}"] = reader(f"templates/{md.name}")
    for rel in PROSE_CODE_FILES:
        corpus[rel] = reader(rel)
    for name, (rel, const_name) in sorted(PROSE_PROMPT_CONSTANTS.items()):
        corpus[name] = _constant_text(rel, const_name)
    for cfg in sorted((REPO_ROOT / "agent_configs").glob("*.yaml")):
        doc = yaml.safe_load(cfg.read_text(encoding="utf-8"))
        for role, prompt in _system_prompts(doc):
            corpus[f"agent_configs/{cfg.name}:{role}"] = prompt
    return corpus


def _system_prompts(node, trail=""):
    """Every (role, text) pair for a `system_prompt:` key in a config tree."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "system_prompt" and isinstance(value, str):
                found.append((trail.lstrip("/"), value))
            found += _system_prompts(value, f"{trail}/{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found += _system_prompts(value, f"{trail}[{i}]")
    return found


def _constant_text(rel, const_name):
    """The literal text of one module-level string constant in `rel`."""
    for node in ast.walk(ast.parse(_read(rel))):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if const_name not in names:
            continue
        text = _string_constant_text(node.value)
        assert text is not None, f"{rel}:{const_name} is not a string constant"
        return text
    raise AssertionError(f"{rel} declares no constant {const_name}")

def _prose_violations(name, text):
    """Rule violations in one prose surface; empty means intact.

    Every rule below is a claim about what the surface must TEACH and where,
    not a word that happens to occur somewhere in the file: a stale-hunk
    remedy only counts in the sentence that names the stale hunk, a reference
    example only counts when the sentence introducing it announces it, and a
    surface that names `references` must also say what to cite and where the
    citation comes from.
    """
    violations = []
    lines = [line.strip() for line in text.splitlines()]
    long_at = {i for i, line in enumerate(lines) if len(line) >= _LONG_LINE}

    for i in range(1, len(lines)):
        if i in long_at and i - 1 in long_at and lines[i] == lines[i - 1]:
            violations.append(
                f"{PROSE_RULES.get('adjacent', 'adjacent_duplicate_line')}: "
                f"{name}:{i + 1}: {lines[i]!r}")

    if name in STRICT_GUIDANCE_BLOCKS:
        runs = {}
        for i in range(len(lines) - 1):
            if i in long_at and i + 1 in long_at:
                runs.setdefault((lines[i], lines[i + 1]), []).append(i + 1)
        for run, at in runs.items():
            if len(at) > 1:
                violations.append(
                    f"{PROSE_RULES.get('repeated_run', 'repeated_run')}: "
                    f"{name}: lines {at} carry the same two "
                    f"lines: {run[0]!r}")

    if text.count("`") % 2:
        violations.append(
            f"{PROSE_RULES.get('odd_backtick', 'odd_backtick_count')}: "
            f"{name}: {text.count('`')} backticks, so a "
            f"code span closes early")

    if "stale or file." in " ".join(text.split()):
        violations.append(
            f"{PROSE_RULES.get('severed', 'severed_clause')}: "
            f"{name}: 'stale or file.' is a deleted clause")

    violations += _stale_hunk_remedy_violations(name, text)
    violations += _reference_example_violations(name, text)
    violations += _reference_advice_violations(name, text)
    return violations


_REREAD_WORDS = ("reread", "re-read", "重读", "重新读")
_REFERENCE_REMEDY = ("to a reference", "改用引用模式", "引用模式")
_TARGET_WORDS = ("sha", "citation", "range", "window", "那一段", "窗口",
                 "范围")
_ANNOUNCE_ENDINGS = (":", "：", "—", "–")


def _stale_hunk_remedy_violations(name, text):
    """A stale or ambiguous hunk must carry its remedy in the same sentence.

    The remedy is either a re-read of the cited range (naming where the range
    is) or a switch to the reference addressing mode. A sentence that only
    tells the agent to send something smaller leaves it with no way to locate
    the stale spot, which is what the corrupted bytes say.
    """
    out = []
    flat = " ".join(text.split())
    for unit in re.split(r"(?<=[.!?。])\s*", flat):
        hay = unit.lower()
        if "hunk" not in hay:
            continue
        if "stale" not in hay and "ambiguous" not in hay:
            continue
        reread = any(word in hay for word in _REREAD_WORDS)
        # A target must be a WHOLE word, never a substring: `sha` is inside
        # `share`/`shaped` and `range` is inside `arrange`.
        target = any(_has_word(hay, word) for word in _TARGET_WORDS)
        switched = any(word in hay for word in _REFERENCE_REMEDY)
        if not (switched or (reread and target)):
            out.append(
                f"stale_hunk_remedy: {name}: a stale or ambiguous hunk with "
                f"no local remedy: {unit[:80]!r}")
    return out

# The rule each named corruption must fire on each surface, as a DATA TABLE
# the catalog owns: tools/prose_corruptions/catalog.py PROSE_RULES is
# {corruption-id: {slot: rule-name}}, and the violation-emitting rules look
# their name up for the corruption under test (set via set_active_corruption).
# The name is therefore chosen once, in the catalog, as data — never by a
# substring of the prose. The default keys keep each rule's own name for
# whole-corpus checks like test_the_agent_facing_prose_is_intact.
PROSE_RULES = {}

def _reference_example_violations(name, text):
    """A reference example must be announced by the sentence before it."""
    out = []
    for match in re.finditer(r'\{"file"', text):
        span = text[match.start():text.find("`", match.start())]
        if match.start() == 0 or text[match.start() - 1] != "`":
            continue
        if '"sha"' not in span or '"new_text"' not in span:
            continue
        rule = PROSE_RULES.get("unannounced_example",
                               "unannounced_reference_example")
        before = text[:match.start()].rstrip().rstrip("`").rstrip()
        announce = next((line.strip() for line in reversed(before.split("\n"))
                         if line.strip()), "")
        if not announce.endswith(_ANNOUNCE_ENDINGS):
            out.append(
                f"{rule}: {name}: the reference example is introduced by "
                f"{announce[-40:]!r}")
    return out


def _reference_advice_violations(name, text):
    """Naming `references` obliges the SAME sentence to say what to cite.

    The advice is a local conjunction, not a word that occurs somewhere in the
    file: the sentence that names the reference addressing mode must also name
    the thing to quote (`sha`) and where that thing comes from (`citation`).
    An occurrence elsewhere in the surface cannot stand in for it, which is
    the shape that let an unrelated token satisfy the old advice check.
    """
    out = []
    for sentence in _sentences(text):
        if "`references`" not in sentence:
            continue
        absent = [word for word in ("citation", "sha")
                  if not _has_word(sentence, word)]
        if absent:
            out.append(
                f"{PROSE_RULES.get('reference_advice', 'reference_advice_missing')}: "
                f"{name}: the sentence naming "
                f"`references` never states {absent}: {sentence[:70]!r}")
    return out


def _sentences(text):
    """The surface's teaching units: one contiguous paragraph or bullet each.

    A unit ends at a blank line, not at every newline: these surfaces wrap at
    ~78 columns, so a rule that split on newlines would call a wrapped
    sentence two utterances and read the advice as absent.
    """
    for block in re.split(r"\n\s*\n", text):
        flat = re.sub(r"\s+", " ", block).strip()
        if flat:
            yield flat


def _has_word(text, word):
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])",
                     text.lower()) is not None

def test_stale_hunk_remedy_matches_whole_words_not_substrings():
    """A stale-hunk remedy word counts only as a whole word.

    The bare substring `sha` is inside `share` and `shaped`, and `range` is
    inside `arrange`. Each variant below re-reads the range (`reread`) and
    then offers only a word that contains a target as a substring, so the
    substring rule let all three pass with no violation. A word-boundary rule
    names each as `stale_hunk_remedy`. The last line is the genuine remedy,
    a whole word, and must stay clean.
    """
    name = "templates/x.md"
    variants = [
        "If a hunk is stale, reread it and share the change.",
        "When a hunk is ambiguous, reread the whole arrange of files.",
        "If a hunk is stale, reread these diff-shaped lines.",
    ]
    for text in variants:
        hits = _stale_hunk_remedy_violations(name, text)
        assert any("stale_hunk_remedy" in h for h in hits), text
    clean = "If a hunk is stale, reread that range and cite its `sha`."
    assert not _stale_hunk_remedy_violations(name, clean), clean

def test_the_agent_facing_prose_is_intact():
    corpus = _prose_corpus()
    assert corpus, "the corpus must not be empty"
    survivors = {name: _prose_violations(name, text)
                 for name, text in corpus.items()}
    assert not {k: v for k, v in survivors.items() if v}, survivors


def test_the_corpus_is_derived_from_the_filesystem_not_a_list():
    """Coverage is glob-shaped: a new template is in the corpus unseen.

    A checker whose scope is an author's list cannot see a defect outside
    that list. This test asserts the derivation, not a list: the corpus
    carries every *.md under templates/ and the prompt-injecting modules.
    The bytes come from HEAD (`_head_read`), so the derivation is checked
    while a corruption run has the live tree damaged too.
    """
    corpus = _prose_corpus(_head_read)
    templates = sorted(p.name for p in (REPO_ROOT / "templates").glob("*.md"))
    assert len(templates) >= 40, templates
    for name in templates:
        assert f"templates/{name}" in corpus, name
    for rel in PROSE_CODE_FILES:
        assert rel in corpus, rel
    assert "GUIDANCE_EN" in corpus and "GUIDANCE_ZH" in corpus
    assert "STRICT_PATCH_GUIDANCE_ZH" not in corpus


def _string_constant_text(value):
    """The literal text of a string constant, plain or f-string alike.

    `ast.JoinedStr` is an f-string; reading its literal chunks the same way
    as an `ast.Constant`'s value keeps f-string prose inside the corpus
    instead of letting it slip past as "not a constant".
    """
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    if isinstance(value, ast.JoinedStr):
        chunks = []
        for part in value.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                chunks.append(part.value)
            else:
                chunks.append("{}")
        return "".join(chunks)
    return None


def _core_module_paths(root=REPO_ROOT):
    """Every `*.py` module under `root`/core, sorted."""
    return sorted((root / "core").glob("*.py"))


def _prose_constants(root=REPO_ROOT):
    """{(module path, constant name): text} for every prose-sized constant."""
    found = {}
    for py in _core_module_paths(root):
        rel = f"core/{py.name}"
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            text = _string_constant_text(node.value)
            if not names or text is None:
                continue
            if "\n" not in text or len(text) < 120:
                continue
            for const_name in names:
                found[(rel, const_name)] = text
    return found


def _covered_constant_keys():
    """The (module, constant) pairs the corpus already carries by name."""
    return {tuple(key.split(":", 1)) for key in PROSE_PROMPT_CONSTANTS}


def _unaccounted_prose_constants(root=REPO_ROOT):
    """(module, constant) pairs neither in the corpus nor exempt with a reason.

    Exemptions are keyed by (module path, name). A bare name let a NEW module
    declare its own `SYSTEM_PROMPT` and go unseen — the escape a new prompt
    constant needs only to be in a file the table had never heard of.
    """
    missing = []
    for (rel, const_name) in sorted(_prose_constants(root)):
        if rel in PROSE_CODE_FILES:
            continue
        if (rel, const_name) in _covered_constant_keys():
            continue
        reason = PROSE_CONSTANT_EXEMPTIONS.get((rel, const_name))
        if reason is None:
            missing.append(f"{rel}:{const_name}")
        else:
            assert reason.strip(), (rel, const_name)
    return missing


def test_every_prose_constant_in_core_is_accounted_for():
    """Code prose constants are covered, or named in ONE exemption table.

    The corpus covers whole modules (PROSE_CODE_FILES) and the agent-facing
    prompt constants (PROSE_PROMPT_CONSTANTS) wherever they are declared. Any
    other multi-line constant in core/ must appear in PROSE_CONSTANT_EXEMPTIONS
    as (module path, name) with a reason it never reaches an agent prompt. A
    constant in a module the table has never heard of — including one written
    as an f-string — goes red here and names its module.
    """
    missing = _unaccounted_prose_constants()
    assert not missing, (
        "multi-line prose constants in core/ that are neither in the corpus "
        f"nor exempted with a reason: {', '.join(missing)}")

def _git_file(rev, rel):
    import subprocess
    out = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=REPO_ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _head_read(rel):
    """The committed bytes of `rel` as of HEAD, not the working tree.

    The self-proof fixtures below take their clean pole from here. The
    corruption runner damages a throwaway copy's working tree and never
    commits, so HEAD is intact in the pipeline worktree and in the reviewer's
    one-shot container: the fixtures stay green while the live tree is
    damaged, and only the live-tree scan goes red. Tests that must see the
    live tree read it through `_read`, which the tripwire below redirects.
    """
    return _git_file("HEAD", rel)


def _git_guidance(rev, block):
    text = _git_file(rev, "core/output_migration.py")
    match = re.search(
        rf'STRICT_PATCH_GUIDANCE_{block} = """(.*?)"""', text, re.S)
    assert match, f"{rev} carries no STRICT_PATCH_GUIDANCE_{block}"
    return match.group(1)


def _load_corruption_catalog():
    """The corruption bytes and their ids, from the one catalog module."""
    import importlib.util
    path = REPO_ROOT / "tools" / "prose_corruptions" / "catalog.py"
    spec = importlib.util.spec_from_file_location(
        "prose_corruption_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CATALOG = _load_corruption_catalog()


def _corruption_loader(entry):
    """The corrupted SURFACE text: git bytes, or HEAD's file plus edits.

    The clean pole is read from HEAD through `_head_read`, not from the live
    worktree: the corruption runner damages a throwaway copy without
    committing, so a fixture that read the live tree would take the runner's
    own damaged bytes as "clean" and go red on a precondition instead of
    measuring detection.
    """

    def load():
        if entry.get("rev"):
            if entry.get("block"):
                return _git_guidance(entry["rev"], entry["block"])
            return _git_file(entry["rev"], entry["path"])
        return _CATALOG.apply_to_text(_head_read(entry["path"]), entry)

    return load


# tools/prose_corruptions/catalog.py is the only place the corruption bytes
# live: every entry that replaces an existing surface is measured below, by the
# rule that must fire and the surface name it must carry. The entries that
# CREATE a new prompt module have no surface in the corpus;
# tests/unit/test_prose_corruption_catalog.py measures those through the
# accounting check.
def set_active_corruption(label):
    """Point the checker's rule NAMES at one corruption's catalog data.

    The rule a corruption must fire lives in tools/prose_corruptions/catalog.py
    as a data table, never as a word found in the prose; setting the active
    corruption copies that entry's slot names into PROSE_RULES, which every
    violation-emitting rule reads. label=None restores each rule's own default
    name for whole-corpus checks.
    """
    PROSE_RULES.clear()
    if label:
        PROSE_RULES.update(CATALOG.PROSE_RULES.get(label, {}))


_CATALOG_BY_ID = {entry["id"]: entry for entry in _CATALOG.CORRUPTIONS}

GIT_CORRUPTIONS = {
    entry["id"]: (_CATALOG.surface_name(entry), _corruption_loader(entry))
    for entry in _CATALOG.existing_surface_entries()
}

@pytest.mark.parametrize("label", sorted(GIT_CORRUPTIONS))
def test_each_known_corruption_is_caught_by_name(label):
    name, load = GIT_CORRUPTIONS[label]
    set_active_corruption(label)
    try:
        corrupted = load()
        clean = CATALOG.clean_surface_text(_CATALOG_BY_ID[label], _head_read)
        assert corrupted != clean, \
            f"{label}: the fixture is not actually corrupt"
        violations = _prose_violations(name, corrupted)
        assert violations, f"{label}: the checker stayed silent on {name}"
        expected = _CATALOG_BY_ID[label]["rule"]
        assert any(expected in v and name in v for v in violations), \
            (label, expected, violations)
        assert not _prose_violations(name, clean), (name, "clean copy is red")
    finally:
        set_active_corruption(None)


# ── Both languages tell the agent to split, not to reformat ──────────────


@pytest.mark.parametrize("label", ["EN", "ZH"])
def test_both_guidances_carry_the_split_advice(label):
    """EN and ZH both carry the split-don't-reformat advice.

    The word-for-word restoration the previous round attempted silently
    deleted the base ZH split paragraph, leaving EN telling the agent to
    split and ZH silent. Deleting either paragraph goes red here, by name.
    The bytes come from HEAD through `_git_guidance`, so the check is about
    the committed guidance and stays green while a corruption run has the
    live tree damaged.
    """
    guidance = _git_guidance("HEAD", label)
    marker = ("split a large change" if label == "EN"
              else "拆成多次 apply_patch")
    assert marker in guidance, (
        f"{label} lost the split advice: {marker!r} not present")
    if label == "ZH":
        assert "发小一点" in guidance, "ZH lost the resend-smaller advice"


def test_a_duplicate_line_planted_in_any_surface_fires_by_name():
    """Self-proof fixture: plant one line in EVERY surface; each fires.

    A coverage claim without a self-proof fixture is prose. This plants one
    adjacent duplicate line into every corpus surface and demands the
    violation name that surface — so the checker's attribution is proven
    per surface, not asserted. The corpus bytes come from HEAD
    (`_head_read`), so the plant lands on committed prose and the check
    stays green while a corruption run has the live tree damaged.
    """
    corpus = _prose_corpus(_head_read)
    planted = 0
    for name, text in corpus.items():
        lines = text.splitlines()
        idx = next((i for i, l in enumerate(lines)
                    if len(l.strip()) >= _LONG_LINE), None)
        if idx is None:
            continue
        bad = "\n".join(lines[:idx + 1] + [lines[idx]] + lines[idx + 1:])
        violations = _prose_violations(name, bad)
        assert violations, f"{name}: the planted duplicate went undetected"
        assert any(name in v for v in violations), (name, violations)
        planted += 1
    assert planted >= 40, planted


# ── Round 9: the whole surface-driven list under a damaged live tree ──────
# The pole that used to cover two named fixture families now covers EVERY
# test in this file that takes its bytes from a prose surface. The live
# reader is redirected to the runner's damaged bytes: the listed tests must
# stay green (they read HEAD), and the live-tree tripwire must go red naming
# the catalog's rule. K4/K5 create a module with no corpus surface, so for
# them only the green half applies.

def _damaged_reader(entry):
    """A `_read` returning the runner's damaged bytes for `entry`'s file."""
    damaged = _CATALOG.corrupt_file_text(entry, _head_read, _git_file)
    target = _CATALOG.entry_file_path(entry)

    def read(rel):
        return damaged if rel == target else _head_read(rel)

    return read


@pytest.mark.parametrize("entry", _CATALOG.CORRUPTIONS,
                         ids=lambda e: e["id"])
def test_every_surface_driven_test_stays_green_on_a_damaged_live_tree(
        monkeypatch, entry):
    """Pole for the round-9 list: green on the list, red on the tripwire."""
    monkeypatch.setattr(sys.modules[__name__], "_read",
                        _damaged_reader(entry))
    test_the_corpus_is_derived_from_the_filesystem_not_a_list()
    test_a_duplicate_line_planted_in_any_surface_fires_by_name()
    for label in ("EN", "ZH"):
        test_both_guidances_carry_the_split_advice(label)
    if entry["id"] in GIT_CORRUPTIONS:
        test_each_known_corruption_is_caught_by_name(entry["id"])
    surface = _CATALOG.surface_name(entry)
    if surface is None:
        return
    with pytest.raises(AssertionError):
        test_the_agent_facing_prose_is_intact()
    # The tripwire's own message is repr-truncated, so the rule is named
    # from the same damaged bytes it scans: the live corpus surface is red,
    # carrying the catalog's rule and the surface's own name.
    violations = _prose_violations(surface, _prose_corpus()[surface])
    assert any(entry["rule"] in v for v in violations), \
        (entry["id"], entry["rule"], violations)
    assert any(surface in v for v in violations), violations


# ── The apply_patch grant boundary ────────────────────────────────────────
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


def test_apply_patch_with_its_own_grant_reaches_skillflow(monkeypatch):
    engine, seen = _tool_engine(monkeypatch, {"apply_patch": {}, "read": {}})
    result = engine._exec_tool({
        "tool": "apply_patch",
        "params": {"patch": "*** Begin Patch\n*** End Patch\n"},
    })
    assert result == {"ok": True}
    assert seen["name"] == "apply_patch"

