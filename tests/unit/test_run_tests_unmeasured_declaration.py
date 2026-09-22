# tests/unit/test_run_tests_unmeasured_declaration.py
#
# "A gate that never ran is not a failing gate" — and, just as important,
# "a gate that failed is not a gate that never ran".
#
# UNMEASURED has exactly two sources and both are the framework watching the
# absence happen: the gate's OWN declaration record, and a run this harness
# killed or could not start. An EXIT CODE is never one of them. `rc=2` is not
# "the engine never ran" — the game repo's own gate reserves 2 for
# `incomplete`, which includes contract files the implementer wrote wrong
# ("no authored scenarios found ... an empty one is not a pass"), and those are
# MEASURED failures that must stay visible with a non-empty `failures[]`.
#
# Every assertion here was checked by mutating the implementation and watching
# it fail; the mutation each test kills is named in its docstring.
import json
from pathlib import Path

import pytest

from aitelier.tools.run_tests import impl as rt

DECLARED = ('AITELIER_REPO_GATE_UNMEASURED='
            '{"state":"blocked","reason":"godot-builder unreachable: '
            'HTTP Error 409: Conflict, gate NOT run"}')


def _case(case_id, detail):
    return "AITELIER_REPO_GATE_CASE=" + json.dumps(
        {"case_id": case_id, "status": "failed", "detail": detail})


def _write_gate(repo, *lines, exit_code=1):
    script = repo / "run_tests.sh"
    body = "#!/bin/sh\n" + "\n".join(
        "printf '%s\\n' " + repr(line) for line in lines) + \
        f"\nexit {exit_code}\n"
    script.write_text(body)
    script.chmod(0o755)


def _repo_with_gate(tmp_path, *lines, exit_code=1, green_pytest=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    if green_pytest:
        (repo / "tests").mkdir()
        (repo / "tests" / "test_ok.py").write_text(
            "def test_ok():\n    assert True\n")
    _write_gate(repo, *lines, exit_code=exit_code)
    return repo


def _report(out):
    return json.loads((Path(out) / "test_report.json").read_text())


# ── an exit code is never an absence ───────────────────────────────────────

@pytest.mark.parametrize("returncode", [0, 1, 2, 3, 124, 137, 255, -1, -9])
def test_no_exit_code_alone_is_ever_unmeasured(returncode):
    """The whole surface of what a bare `rc` may say. Nothing here is a
    declaration, so the only two legal readings are pass (0) and fail."""
    gate = {"returncode": returncode, "output": "=== gate ===\nred prose\n",
            "output_truncated": False}
    outcome = rt._repo_gate_outcome(gate)
    assert outcome != rt.REPO_GATE_UNMEASURED
    if returncode == 0:
        assert outcome == rt.REPO_GATE_MEASURED_PASS
    else:
        assert outcome == rt.REPO_GATE_MEASURED_FAIL


def test_there_is_no_exit_code_that_could_be_wired_back_to_unmeasured():
    """The previous attempt's `REPO_GATE_UNMEASURED_EXIT_CODE` must not come
    back: re-introducing the constant is how `rc=2 == unmeasured` returns."""
    assert not hasattr(rt, "REPO_GATE_UNMEASURED_EXIT_CODE")


def test_a_contract_error_exit_2_is_a_measured_failure_with_failures(tmp_path):
    """The author's own broken contract file, at exit 2, is a RED — the text
    the game gate prints says so itself ("an empty one is not a pass"). It may
    not become an absence with `failures: []` (the forbidden technique: a
    legitimate red leaking past as "nothing was measured")."""
    repo = _repo_with_gate(
        tmp_path,
        "CONTRACT ERROR: no authored scenarios found under tests/play, "
        "an empty one is not a pass.",
        exit_code=2)
    out = tmp_path / "out"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)

    assert report["repo_gate"]["measured"] == rt.REPO_GATE_MEASURED_FAIL
    assert report["repo_gate"]["returncode"] == 2
    assert report.get("repo_gate_unmeasured") is None
    assert report["passed"] is False and result["passed"] is False
    assert report["failures"], "a red gate must name a failure"
    assert "repo_gate:run_tests.sh" in report["failures"][0]


# ── the declaration channel has to survive a REAL gate's output ────────────

def test_a_declaration_survives_output_far_past_the_retention_bound(tmp_path):
    """The channel the whole protocol rests on, on a gate whose output is far
    larger than the 2000 characters `output` retains.

    The record is FIRST and the log continues long past the bound, so the
    retained fragment provably does not contain it: only scanning the whole
    text before truncating can read it. This is the run that used to be
    impossible — before, the declaration was only readable while the gate's
    log was SHORT, and no real gate's log is.
    """
    noise = "=" * 3000
    repo = _repo_with_gate(tmp_path, DECLARED, noise, exit_code=3)
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)
    gate = report["repo_gate"]

    assert gate["output_truncated"] is True
    # The fragment really is a fragment: no record survives in it.
    assert rt._REPO_GATE_UNMEASURED_PREFIX not in gate["output"]
    assert rt._unmeasured_declaration(gate["output"]) is None
    # ...and the whole-text scan read it anyway.
    assert gate["unmeasured_declaration"]["state"] == "blocked"
    assert gate["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["repo_gate_unmeasured"] is True
    assert report["passed"] is False


def test_a_bounded_fragment_is_not_a_record_on_its_own():
    """A gate handed to this reader as a FRAGMENT (a report read back off disk,
    a replayed gate) proves nothing about a line that may have been cut in
    half. Kills M2 (deleting the `output_truncated` branch): the fragment
    below reads as a declaration the moment the branch is gone."""
    gate = {"returncode": 3, "output_truncated": True,
            "output": DECLARED + "\n" + "=" * 2000}
    assert rt._repo_gate_declares_unmeasured(gate) is False
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_MEASURED_FAIL


def test_a_truncated_gate_with_no_record_at_all_is_a_red():
    """Kills M19 (inverting that branch): inverted, EVERY truncated gate —
    the shape of all four recorded witnesses — reads as an absence."""
    gate = {"returncode": 1, "output_truncated": True,
            "output": "plain red prose\n" * 50}
    assert rt._repo_gate_declares_unmeasured(gate) is False
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_MEASURED_FAIL


# ── one line can never turn a red into an absence ──────────────────────────

@pytest.mark.parametrize("state", ["failed", "red", "error"])
def test_a_declaration_that_says_failed_is_not_an_absence(state):
    """Kills M17 (adding "failed"/"red" to the unmeasured state set): the gate
    says it FAILED, which is a measurement, so the red stays visible."""
    body = json.dumps({"state": state, "reason": "a real failure"})
    gate = {"returncode": 1, "output_truncated": False,
            "output": rt._REPO_GATE_UNMEASURED_PREFIX + body}
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_MEASURED_FAIL


def test_the_unmeasured_state_set_holds_no_red_word():
    assert not ({"failed", "red", "error", "incomplete"}
                & set(rt._REPO_GATE_UNMEASURED_STATES))


def test_a_log_echo_of_the_prefix_mid_line_is_not_a_declaration():
    """Kills M21 (`startswith` -> `in`): a gate whose log merely QUOTES the
    protocol line is not declaring anything."""
    line = ("the gate reported: " + rt._REPO_GATE_UNMEASURED_PREFIX
            + json.dumps({"state": "blocked"}))
    gate = {"returncode": 1, "output": line, "output_truncated": False}
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_MEASURED_FAIL


def test_a_log_echo_of_the_case_prefix_mid_line_is_not_a_case_record():
    """The same mutation on the per-case channel, which is what turns an
    echoed log line into a prunable known-red identity."""
    gate = {"returncode": 1, "output_truncated": False,
            "output": "note: " + _case("A", "quoted")}
    cases, identity_error = rt._repo_gate_failure_cases(gate)
    assert cases == []
    assert identity_error


# The two witnesses BELOW are the ones the mutation actually has to fail. A
# mid-line echo only kills `PREFIX in line` when the slice the mutated reader
# takes lands ON the JSON — i.e. when the noise header is EXACTLY as long as
# the prefix. With a short header ("the gate reported: ", 19 characters) the
# mutated slice cuts into the prefix itself, `json.loads` fails, and the test
# stays green under the mutation while reading as if it had caught it. The
# length is asserted here so a future edit to the protocol line cannot
# silently re-disarm these witnesses.

_MIDLINE_NOISE = "x" * len(rt._REPO_GATE_UNMEASURED_PREFIX)
_CASE_MIDLINE_NOISE = "x" * len(rt._REPO_GATE_CASE_PREFIX)


def test_a_mid_line_echo_kills_the_in_operator_on_the_declaration_channel():
    """Kills M21 (`startswith` -> `in`) at the ONE noise length where it is
    observable. This is the witness the previous revision claimed and did not
    have: measured, the mutation turns this test red."""
    assert len(_MIDLINE_NOISE) == len(rt._REPO_GATE_UNMEASURED_PREFIX) > 19
    body = json.dumps({"state": "blocked", "reason": "an echo, not a record"})
    line = _MIDLINE_NOISE + rt._REPO_GATE_UNMEASURED_PREFIX + body
    gate = {"returncode": 1, "output": line, "output_truncated": False}
    assert rt._unmeasured_declaration(line) is None
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_MEASURED_FAIL


def test_a_mid_line_echo_kills_the_in_operator_on_the_case_channel():
    """Kills M21b (the same `in` on `_REPO_GATE_CASE_PREFIX`): a single log
    line that echoes the case prefix must not fabricate a prunable red
    identity."""
    assert len(_CASE_MIDLINE_NOISE) == len(rt._REPO_GATE_CASE_PREFIX) > 0
    gate = {"returncode": 1, "output_truncated": False,
            "output": _CASE_MIDLINE_NOISE + _case("A", "quoted")}
    cases, identity_error = rt._repo_gate_failure_cases(gate)
    assert cases == []
    assert identity_error


def _apply_m21(func):
    """Apply the mutation ITSELF, in-process, and return the mutant callable.

    The card's claim is "M21 is killed". An assertion about the candidate
    cannot show that — it only shows the candidate behaves one way. So each
    witness below re-runs the REAL reader mutated and asserts the reading
    FLIPS. The mutation is reproduced by the suite, not asserted about.

    `_apply_m21` applies both halves: `startswith` → `in` AND the slice
    seeking the prefix where it was found (`line.index(PREFIX) + len(PREFIX)`).
    Together they turn a mid-line echo into a declaration.
    """
    import ast
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(func))
    mutated = source.replace(
        "line.startswith(_REPO_GATE_UNMEASURED_PREFIX)",
        "_REPO_GATE_UNMEASURED_PREFIX in line").replace(
        "raw = line[len(_REPO_GATE_UNMEASURED_PREFIX):]",
        "raw = line[line.index(_REPO_GATE_UNMEASURED_PREFIX) + "
        "len(_REPO_GATE_UNMEASURED_PREFIX):]").replace(
        "line.startswith(_REPO_GATE_CASE_PREFIX)",
        "_REPO_GATE_CASE_PREFIX in line").replace(
        "raw = line[len(_REPO_GATE_CASE_PREFIX):]",
        "raw = line[line.index(_REPO_GATE_CASE_PREFIX) + "
        "len(_REPO_GATE_CASE_PREFIX):]")
    assert mutated != source, "the mutation did not apply — the reader changed"
    namespace = dict(vars(rt))
    exec(compile(ast.parse(mutated), "<m21>", "exec"), namespace)
    return namespace[func.__name__]


def test_m21_is_reproduced_and_this_file_is_what_flips():
    """M21: `line.startswith(PREFIX)` -> `PREFIX in line` on the declaration
    channel. Under it a mid-line echo fires `unmeasured`, so a real red is
    rewritten into an absence. Both halves are measured: the candidate reads
    None, the mutant reads `blocked`."""
    mutant = _apply_m21(rt._unmeasured_declaration)
    body = json.dumps({"state": "blocked", "reason": "an echo, not a record"})
    line = _MIDLINE_NOISE + rt._REPO_GATE_UNMEASURED_PREFIX + body
    assert rt._unmeasured_declaration(line) is None
    assert mutant(line) is not None and mutant(line)["state"] == "blocked"


def test_m21b_is_reproduced_and_this_file_is_what_flips():
    """M21b: the same `in` on `_REPO_GATE_CASE_PREFIX`. Under it one echoed
    log line fabricates a prunable known-red identity — how a red gets
    forgiven by a gate that never ran."""
    mutant = _apply_m21(rt._repo_gate_failure_cases)
    gate = {"returncode": 1, "output_truncated": False,
            "output": _CASE_MIDLINE_NOISE + _case("A", "quoted")}
    cases, identity_error = rt._repo_gate_failure_cases(gate)
    assert cases == [] and identity_error
    m_cases, _ = mutant(gate)
    assert [c["case_id"] for c in m_cases] == ["A"]

# ── a timeout is the framework's own observation ───────────────────────────


def test_a_declared_absence_is_unmeasured_whatever_the_exit_code_was():
    gate = {"returncode": 0, "output_truncated": False, "output": DECLARED}
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_UNMEASURED


def test_a_killed_gate_is_unmeasured_and_its_returncode_is_not_why(tmp_path):
    gate = rt._run_node_cmd(Path(str(tmp_path)), ["bash", "-c", "sleep 3"],
                            timeout=1)
    assert gate["returncode"] == -1
    assert gate["timed_out"] is True
    # Same value the bare exit-code scan above reads as a MEASURED failure —
    # the marker, not the number, is what makes this an absence.
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_UNMEASURED


def test_a_runner_that_cannot_start_is_unmeasured():
    gate = rt._run_node_cmd(Path("/nonexistent-dir-aitelier"),
                            ["bash", "-c", "true"], timeout=5)
    assert gate["runner_error"] is True
    assert rt._repo_gate_outcome(gate) == rt.REPO_GATE_UNMEASURED


# ── the measurement word has readers ───────────────────────────────────────

def test_every_repo_gate_run_carries_its_measurement(tmp_path):
    """Kills M4 (deleting `result["measured"]`): the fold below reads it, so
    the field is not a dead key — it is the only thing that decides whether a
    gate's cases may be pruned or its red forgiven."""
    repo = _repo_with_gate(tmp_path, "plain red prose", exit_code=1)
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    gate = _report(out)["repo_gate"]
    assert "measured" in gate
    assert gate["measured"] == rt.REPO_GATE_MEASURED_FAIL


# ── an unmeasured gate is re-acquired, not spent ───────────────────────────

def _scripted_gate(monkeypatch, sequence):
    calls = []

    def fake(repo, *args, **kwargs):
        calls.append(repo)
        return dict(sequence[min(len(calls) - 1, len(sequence) - 1)])

    monkeypatch.setattr(rt, "_run_repo_gate", fake)
    return calls


def test_an_unmeasured_gate_is_re_run_until_it_yields_a_verdict(tmp_path,
                                                               monkeypatch):
    """A gate that did not run decided NOTHING: the tool re-acquires the
    verdict instead of handing the loop an absence to spend as a cycle."""
    monkeypatch.setattr(rt, "REPO_GATE_UNMEASURED_ATTEMPTS", 3)
    monkeypatch.setattr(rt, "REPO_GATE_RETRY_DELAY_SECONDS", 0)
    repo = _repo_with_gate(tmp_path, "anything", exit_code=0)
    calls = _scripted_gate(monkeypatch, [
        {"passed": False, "returncode": 3, "output": DECLARED,
         "output_truncated": False, "script": "run_tests.sh",
         "measured": rt.REPO_GATE_UNMEASURED},
        {"passed": True, "returncode": 0, "output": "gate OK",
         "output_truncated": False, "script": "run_tests.sh",
         "measured": rt.REPO_GATE_MEASURED_PASS},
    ])
    out = tmp_path / "out"
    result = rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)

    assert len(calls) == 2
    assert report["repo_gate"]["attempts"] == 2
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_MEASURED_PASS
    assert report.get("repo_gate_unmeasured") is None
    assert report["passed"] is True and result["passed"] is True


def test_re_acquisition_is_bounded_and_the_last_verdict_is_the_reported_one(
        tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "REPO_GATE_UNMEASURED_ATTEMPTS", 3)
    monkeypatch.setattr(rt, "REPO_GATE_RETRY_DELAY_SECONDS", 0)
    repo = _repo_with_gate(tmp_path, "anything", exit_code=0)
    calls = _scripted_gate(monkeypatch, [
        {"passed": False, "returncode": 3, "output": DECLARED,
         "output_truncated": False, "script": "run_tests.sh",
         "measured": rt.REPO_GATE_UNMEASURED},
    ])
    out = tmp_path / "out"
    rt.run_tests(project_root=str(repo), out_dir=str(out))
    report = _report(out)

    assert len(calls) == rt.REPO_GATE_UNMEASURED_ATTEMPTS
    assert report["repo_gate"]["attempts"] == rt.REPO_GATE_UNMEASURED_ATTEMPTS
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["repo_gate_unmeasured"] is True
    assert report["passed"] is False
    assert report["failures"]


# ── an unmeasured gate may not prune (M8); a measured-pass may (M18) ───────

def _baseline_file(state, repo):
    return Path(rt._baseline_dir(str(state), repo)) / rt.BASELINE_FILE


def test_an_unmeasured_repo_gate_may_not_prune_its_known_red(tmp_path,
                                                             monkeypatch):
    """Kills M8 (`repo_gate_cases: None` -> `set()` on the unmeasured branch).
    `Executed.ran("repo_gate:...")` reads that field, so `set()` claims the
    gate RAN — and a known-red case is dropped by a gate that said, in its own
    words, that it never ran."""
    monkeypatch.setattr(rt, "REPO_GATE_UNMEASURED_ATTEMPTS", 1)
    repo = _repo_with_gate(tmp_path, _case("A", "broken"), exit_code=1)
    state = tmp_path / "state"
    rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / "out-1"),
                 state_dir=str(state))
    baseline = _baseline_file(state, repo)
    assert json.loads(baseline.read_text())["failures"] == [
        "repo_gate:run_tests.sh#A"]

    _write_gate(repo, DECLARED, exit_code=3)
    out = tmp_path / "out-2"
    rt.run_tests(project_root=str(repo), out_dir=str(out),
                 state_dir=str(state))
    report = _report(out)

    assert report["repo_gate"]["measured"] == rt.REPO_GATE_UNMEASURED
    assert json.loads(baseline.read_text())["failures"] == [
        "repo_gate:run_tests.sh#A"]
    assert "repo_gate:run_tests.sh#A" in report["baseline_kept_unproven"]


def test_a_repo_gate_that_measured_green_may_prune_its_known_red(tmp_path):
    """Kills M18 (the mirror: `set()` -> `None` on the measured_pass branch).
    The gate DID run and reported nothing failed, so its known-red case is
    measured green and has to leave the baseline."""
    repo = _repo_with_gate(tmp_path, _case("A", "broken"), exit_code=1)
    state = tmp_path / "state"
    rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / "out-1"),
                 state_dir=str(state))
    baseline = _baseline_file(state, repo)
    assert json.loads(baseline.read_text())["failures"] == [
        "repo_gate:run_tests.sh#A"]

    _write_gate(repo, "gate OK", exit_code=0)
    out = tmp_path / "out-2"
    rt.run_tests(project_root=str(repo), out_dir=str(out),
                 state_dir=str(state))
    report = _report(out)

    assert report["repo_gate"]["measured"] == rt.REPO_GATE_MEASURED_PASS
    assert json.loads(baseline.read_text())["failures"] == []
    assert report.get("baseline_kept_unproven") is None


# ── a declared absence may NEVER become a relative pass ───────────────────

def test_a_declared_absence_never_seeds_a_baseline_and_never_passes_relative(
        tmp_path):
    """The card's first criterion, end to end through the REAL tool.

    Four CONSECUTIVE declarations of absence, on a repo that has never had a
    baseline taken. Every pass must report `passed_relative: false`, and the
    baseline file must never be written: the seed is taken from this run's
    `failures[]`, whose only entry here says the gate produced NO VERDICT, so
    writing it would record a gate that never spoke as this repo's standing
    known-red — and the NEXT real red from that gate would be forgiven by it.

    The r5 candidate did the opposite on this exact shape: review measured
    `passed_relative: true`, `baseline_state: seeded`, and
    `run_tests_baseline.json` written, on every pole.
    """
    repo = _repo_with_gate(tmp_path, DECLARED, exit_code=3)
    state = tmp_path / "state"
    baseline = _baseline_file(state, repo)

    for attempt in range(1, 5):
        out = tmp_path / f"out-{attempt}"
        result = rt.run_tests(project_root=str(repo), out_dir=str(out),
                              state_dir=str(state))
        report = _report(out)
        where = f"pass {attempt}"
        assert report["repo_gate"]["measured"] == rt.REPO_GATE_UNMEASURED, where
        assert result["repo_gate_absent"] is True, where
        assert report["passed_relative"] is False, where
        assert result["passed_relative"] is False, where
        assert report["baseline_state"] == "unmeasured", (
            f"{where}: an absence with no baseline behind it is not a "
            f"measurement — got {report['baseline_state']!r}")
        assert not baseline.is_file(), (
            f"{where}: an absence seeded a baseline from its own absence")


def test_a_real_red_still_enters_the_baseline_and_the_absence_after_it_does_not_pass(
        tmp_path):
    """The other pole of the same property, and the reason it is not a
    blanket: a gate that MEASURED red must still seed the baseline.

    Without this pole the rule above could be satisfied by never seeding at
    all, which would make every pre-existing red look like this round's.
    """
    repo = _repo_with_gate(tmp_path, _case("A", "broken"), exit_code=1)
    state = tmp_path / "state"
    out = tmp_path / "out-red"
    rt.run_tests(project_root=str(repo), out_dir=str(out), state_dir=str(state))
    report = _report(out)
    baseline = _baseline_file(state, repo)

    assert report["repo_gate"]["measured"] == rt.REPO_GATE_MEASURED_FAIL
    assert report["baseline_state"] == "seeded"
    assert report["passed_relative"] is True
    assert json.loads(baseline.read_text())["failures"] == [
        "repo_gate:run_tests.sh#A"]
    before = baseline.read_bytes()

    # ...and now the SAME gate declares an absence. The known red it seeded has
    # not been measured away, so it must survive, and this pass may not pass
    # relatively off it.
    _write_gate(repo, DECLARED, exit_code=3)
    out = tmp_path / "out-absent"
    rt.run_tests(project_root=str(repo), out_dir=str(out), state_dir=str(state))
    report = _report(out)
    assert report["repo_gate"]["measured"] == rt.REPO_GATE_UNMEASURED
    assert report["passed_relative"] is False
    assert baseline.read_bytes() == before, (
        "an absence changed a baseline it did not measure")
    assert "repo_gate:run_tests.sh#A" in report["baseline_kept_unproven"]
