# tests/unit/test_tree_level_accounting_witnesses.py
#
# The mutations independent review re-ran on the r5 CANDIDATE TREE and found
# alive. The in-suite mutants in `test_run_tests_unmeasured_declaration.py`
# mutate a function OBJECT, so they prove the READER is written one way — they
# cannot see the tree being edited, which is the mutation that actually ships.
# Every assertion below reads the REAL file on disk and fails when the tree is
# mutated.
#
# Named mutations, each with the witness that kills it:
#
#   M21   `line.startswith(_REPO_GATE_UNMEASURED_PREFIX)` -> `PREFIX in line`
#         (with the slice following it): one echoed log line is read as a
#         declaration, turning a real red into an absence.
#   M21b  the same `in` on `_REPO_GATE_CASE_PREFIX`: one echoed log line
#         fabricates a prunable known-red identity — a red forgiven by a gate
#         that never ran.
#   N9    delete the protocol contract text. A deletion that leaves every test
#         green means the text had no reader.
#   S1    delete the deferral skip in `core/scheduler.py`'s tick.
#   H2    delete the hold check in `AItelierSkillFlow.advance_run`.
#   EDGE  move the `repo_gate_absent` edge off the `test` step (where
#         `from_file` resolves against the step that owns the file) back onto
#         `test_evidence`, where it raises FileNotFoundError on every
#         evaluation and can never match.
#   ABS   let a declared absence seed the baseline / pass relatively: the ONE
#         field a run must never reach from an absence. Killed end-to-end by
#         `test_run_tests_unmeasured_declaration.py::
#         test_a_declared_absence_never_seeds_a_baseline_and_never_passes_\
#         relative`, which drives the REAL tool four times and reads the
#         baseline file back off disk.
#
# These are SOURCE pins on purpose. A behavioural test cannot distinguish
# `startswith` from `in` at the noise length review used; the tree, read as
# text, can — see the exactly-prefix-length witnesses that already live in
# `test_run_tests_unmeasured_declaration.py`.
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent.parent
_IMPL = _ROOT / "aitelier" / "tools" / "run_tests" / "impl.py"
_SCHEDULER = _ROOT / "core" / "scheduler.py"
_HOST = _ROOT / "core" / "skillflow_host.py"
_CONFIG = _ROOT / "configs" / "coding_impl.yaml"
_PROTOCOL = _ROOT / "docs" / "repo-gate-unmeasured-protocol.md"

_DECL_PREFIX = "_REPO_GATE_UNMEASURED_PREFIX"
_CASE_PREFIX = "_REPO_GATE_CASE_PREFIX"


def test_ABS_the_absence_branch_is_ahead_of_the_seed_in_the_real_source():
    """ABS: the absence must be decided BEFORE the seed, in the tree.

    Asserted against the REAL `_apply_baseline` text rather than a restatement:
    the seed takes `known = set(keys)` from this run's `failures[]`, and an
    absence's only failure entry says the gate produced NO VERDICT — seeding
    off that records a gate that never spoke as this repo's standing
    known-red. `absent` must be defined before the seed and must gate both the
    seed and `passed_relative`.
    """
    source = _source(_IMPL)
    assert "absent = bool(report.get(\"repo_gate_absent\")" in source, (
        "ABS: the absence is no longer read in `_apply_baseline`")
    seed = source.index("known = set(keys)")
    absent = source.index("absent = bool(report.get(\"repo_gate_absent\")")
    assert absent < seed, (
        "ABS: the seed now runs before the absence is read, so an absence can "
        "seed the baseline off its own 'was NOT measured' entry")
    # The relative-pass assignment must consult `absent` too: an absence that
    # HAS a baseline behind it still may not claim the red was already there.
    window = source[source.index("report[\"passed_relative\"] = (state in"
                                 " BASELINE_MEASURED"):][:200]
    assert "absent" in window, (
        "ABS: `passed_relative` no longer consults the absence — an absence "
        "with a baseline behind it would report a relative pass")


def test_ABS_the_unmeasured_state_is_not_a_measurement():
    """ABS, second half: `unmeasured` must be in neither set of things a
    reader treats as a measurement, or the absence is back to being a pass --
    the exact failure the criterion names."""
    from aitelier.tools.run_tests import impl as rt

    assert "unmeasured" not in rt.BASELINE_MEASURED, (
        "ABS: `unmeasured` joined BASELINE_MEASURED — an absence is now a "
        "relative pass")
    from aitelier.gate_evidence import _BASELINE_MEASURED as readers
    assert "unmeasured" not in readers, (
        "ABS: the release-state reader treats an absence as measured")



def _source(path):
    return path.read_text(encoding="utf-8")


def _unmeasured_witnesses(source):
    """M21: the declaration channel must test the line START and slice by the
    prefix's real length. Both halves are named separately so a failure says
    which one moved."""
    out = []
    if f"line.startswith({_DECL_PREFIX})" not in source:
        out.append(f"M21: the line start is no longer tested with startswith("
                   f"{_DECL_PREFIX})")
    if f"{_DECL_PREFIX} in line" in source:
        out.append(f"M21: `{_DECL_PREFIX} in line` is present — a mid-line "
                   "echo is a declaration again (a real red becomes an "
                   "absence)")
    if (f"raw = line[line.index({_DECL_PREFIX})" in source
            or f"line.index({_DECL_PREFIX})" in source):
        out.append("M21: the slice seeks the prefix where it was found "
                   "(`line.index(...)`) instead of the line start")
    if f"raw = line[len({_DECL_PREFIX}):]" not in source:
        out.append(f"M21: the slice is not `line[len({_DECL_PREFIX}):]`")
    return out


def _case_witnesses(source):
    """M21b: the same two halves on the per-case channel."""
    out = []
    if f"line.startswith({_CASE_PREFIX})" not in source:
        out.append(f"M21b: the line start is no longer tested with startswith("
                   f"{_CASE_PREFIX})")
    if f"{_CASE_PREFIX} in line" in source:
        out.append(f"M21b: `{_CASE_PREFIX} in line` is present — one echoed "
                   "log line fabricates a prunable known-red identity")
    if f"raw = line[len({_CASE_PREFIX}):]" not in source:
        out.append(f"M21b: the slice is not `line[len({_CASE_PREFIX}):]`")
    return out


def test_M21_the_tree_still_tests_the_line_start_not_a_substring():
    offenders = _unmeasured_witnesses(_source(_IMPL))
    assert offenders == [], "\n".join(offenders)


def test_M21b_the_tree_still_tests_the_case_prefix_at_the_line_start():
    offenders = _case_witnesses(_source(_IMPL))
    assert offenders == [], "\n".join(offenders)


@pytest.mark.parametrize("mutation,replacement", [
    # The two halves of M21, each applied to the REAL source text.
    (f"line.startswith({_DECL_PREFIX})", f"{_DECL_PREFIX} in line"),
    (f"raw = line[len({_DECL_PREFIX}):]",
     f"raw = line[line.index({_DECL_PREFIX}) + len({_DECL_PREFIX}):]"),
])
def test_M21_the_tree_witnesses_go_red_on_the_actual_mutation(mutation,
                                                              replacement):
    """A witness nobody has seen fail is a claim. Apply each half of M21 to
    the REAL source text and require the named offence to appear."""
    source = _source(_IMPL)
    mutated = source.replace(mutation, replacement)
    assert mutated != source, f"the mutation did not apply: {mutation}"
    offenders = _unmeasured_witnesses(mutated)
    assert offenders, "the mutation left the witness silent"


def test_M21b_the_tree_witness_goes_red_on_the_actual_mutation():
    source = _source(_IMPL)
    mutated = source.replace(f"line.startswith({_CASE_PREFIX})",
                             f"{_CASE_PREFIX} in line")
    assert mutated != source, "the mutation did not apply"
    assert _case_witnesses(mutated), "the mutation left the witness silent"



def test_N9_the_protocol_text_has_a_reader():
    """N9 deleted five lines of contract text and the whole suite stayed green,
    which means nothing read them. This is the reader for the two things the
    text has to keep saying: an absence is DECLARED and never inferred, and a
    gate that produced no verdict may not be charged to the implementer."""
    text = _source(_PROTOCOL)
    assert "AITELIER_REPO_GATE_UNMEASURED=" in text, (
        "N9: the declaration line the protocol is built on is gone")
    assert "no exit code" in text.lower(), (
        "N9: the protocol no longer says UNMEASURED is never an exit code")
    assert "gate did not run: no verdict was measured" in text, (
        "N9: the sentence an expired absence ends with is gone")
    assert "never be charged to the implementer" in text, (
        "N9: the accounting rule the card exists for is gone")
    assert "Cycle limit exceeded" in text, (
        "N9: the failure the accounting rule exists to replace is gone")



def test_the_protocol_table_makes_no_unreproducible_m21_claim():
    """The false table row review vetoed twice. It must stay DELETED, and the
    replacement must not be the same claim in different words: no row may name
    M21/M21b as killed, because the one-token mutation has no observable
    effect at the noise length the row implied."""
    for line in _source(_PROTOCOL).splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        assert not any(c.startswith("M21") for c in cells), (
            f"the false M21 row is back: {line!r}")


def test_the_absent_terminal_checker_wording_is_not_self_contradictory():
    """The mangled sentence review read back out of the candidate (a fragment
    spliced onto itself around ``Round 5 MEASURED``). The text has to read as
    prose, so the shape is asserted: no line may repeat that phrase."""
    for line in _source(_PROTOCOL).splitlines():
        assert line.count("Round 5 MEASURED") <= 1, (
            f"this line says it twice — a write went wrong: {line!r}")


# ── the call sites, read off the tree ──────────────────────────────────────

def test_S1_the_tick_call_site_is_present_in_the_tree():
    """Kills S1 (delete the deferral skip in the tick; 0/33 in review)."""
    text = _source(_SCHEDULER)
    missing = [name for name in (
        'gate_deferral.observe_run(sf, run_id)',
        'if deferral["state"] == "silent":',
        'if deferral["state"] == "expired":',
        "gate_absence_terminal",
    ) if name not in text]
    assert missing == [], f"S1: the tick no longer reads the deferral: {missing}"


def test_H2_the_host_hold_is_present_in_the_tree():
    """Kills H2 (delete the hold check in `advance_run`; 12/12 in review)."""
    text = _source(_HOST)
    assert "hold_blocks_advance" in text, (
        "H2: `advance_run` no longer consults the deferral hold")

def test_EDGE_the_absence_edge_is_a_flag_on_the_step_that_owns_the_report():
    """Kills EDGE (move the edge back to `test_evidence`, or back to
    `from_file`).

    `from_file` is resolved against the evaluating STEP's own output dir, so on
    `test_evidence` — a json_schema validator that writes nothing — the reader
    raised `FileNotFoundError`, the engine recorded `transition file_reader
    failed`, and the edge could never match. Moved even to `test`, a
    `from_file` match still crashes `_flags_match` when the report parses to a
    non-object. So the edge must be a FLAG match, on `test`. Read from the
    config itself, never from a copy of the expectation.
    """
    graph = yaml.safe_load(_source(_CONFIG))
    steps = {s["id"]: s for s in graph["steps"]}
    flag = {"field": "repo_gate_absent", "value": True}

    assert flag in [t.get("match") for t in steps["test"]["transitions"]], (
        "EDGE: `repo_gate_absent` is not matched as a FLAG on the `test` step")
    for step_id, step in steps.items():
        for transition in step.get("transitions") or []:
            match = transition.get("match") or {}
            assert "repo_gate_absent" not in str(match.get("from_file")), (
                f"EDGE: {step_id} reads `repo_gate_absent` through a file — "
                "that path either cannot resolve or crashes the resolver")
    # Order: the absence must be decided BEFORE the written-evidence edge, or
    # `written: test_report.json` wins first and hands it to `test_evidence`.
    tos = [t["to"] for t in steps["test"]["transitions"]]
    assert tos.index("test_gate_absent") < tos.index("test_evidence"), tos
    assert steps["test"]["transitions"][0]["to"] == "test_evidence_missing", (
        "the `_error` edge must come first: a failed invocation may not "
        "consume a prior attempt's report")

