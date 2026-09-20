"""A test gate must grade the ROUND, not the red the repo already carried.

`gen_dpe_state_game`'s `5_final_test` routes on `test_report.json:passed`, which
is absolute: any pre-existing failure in the repo sends every lap back to
`5_final_test_replan`, so a round burns its whole `max_loop` budget on defects it
never introduced and never gets to 5_review.

`_apply_baseline` adds the relative reading — `new_failures[]` (failures absent
from the known-red baseline) and `passed_relative` — while leaving `passed`
alone, and persists the baseline in the durable `state_dir` the `stateful`
capability injects, so it survives across runs of the config.
"""
import json
from pathlib import Path

from aitelier.tools.run_tests.impl import (BASELINE_FILE, Executed,
                                           _apply_baseline, _baseline_dir,
                                           _failure_key)

RED_A = "FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2"
RED_B = "FAILED tests/test_b.py::test_two - ValueError: nope"
# A complete pytest session that executed both of the tests above. Shrinking
# the baseline needs this: "absent from the failure list" is not "green".
RAN_BOTH = Executed(node_ids={"tests/test_a.py::test_one",
                              "tests/test_b.py::test_two"},
                    pytest_complete=True)


def _report(*failures, passed=False):
    return {"passed": passed, "failures": list(failures)}


def _baseline_path(state, repo):
    """Where run_tests keeps the baseline for one repo under one state_dir."""
    return Path(_baseline_dir(str(state), Path(repo))) / BASELINE_FILE


def test_first_run_seeds_the_baseline_and_owns_nothing(tmp_path):
    r = _report(RED_A, RED_B)
    _apply_baseline(r, str(tmp_path))

    assert r["baseline_seeded"] is True
    assert r["new_failures"] == []
    assert r["passed_relative"] is True
    assert r["passed"] is False           # absolute reading is untouched
    stored = json.loads((tmp_path / BASELINE_FILE).read_text())
    assert sorted(stored["failures"]) == ["tests/test_a.py::test_one",
                                          "tests/test_b.py::test_two"]


def test_only_baseline_failures_pass_relative_while_passed_stays_false(tmp_path):
    _apply_baseline(_report(RED_A, RED_B), str(tmp_path))   # seed

    r = _report(RED_A, RED_B)
    _apply_baseline(r, str(tmp_path))

    assert r.get("baseline_seeded") is None
    assert r["new_failures"] == []
    assert r["passed_relative"] is True
    assert r["passed"] is False


def test_a_genuinely_new_failure_is_reported(tmp_path):
    _apply_baseline(_report(RED_A), str(tmp_path))           # seed: only A

    r = _report(RED_A, RED_B)
    _apply_baseline(r, str(tmp_path))

    assert r["new_failures"] == [RED_B]
    assert r["passed_relative"] is False


def test_the_baseline_persists_across_two_calls(tmp_path):
    _apply_baseline(_report(RED_A), str(tmp_path))
    written = json.loads((tmp_path / BASELINE_FILE).read_text())

    # A second call reads the file back rather than re-seeding: if it re-seeded,
    # B would be forgiven too and the whole gate would be blind.
    r = _report(RED_A, RED_B)
    _apply_baseline(r, str(tmp_path))
    assert r["baseline_failures"] == ["tests/test_a.py::test_one"]
    assert r["new_failures"] == [RED_B]
    assert json.loads((tmp_path / BASELINE_FILE).read_text())["failures"] == \
        written["failures"]


def test_a_fixed_then_rebroken_test_is_new_again(tmp_path):
    _apply_baseline(_report(RED_A, RED_B), str(tmp_path))    # seed: A and B

    green = _report(RED_A)            # B was fixed → pruned from the baseline
    _apply_baseline(green, str(tmp_path), RAN_BOTH)
    # `baseline_failures` reports the set this run was COMPARED against; the
    # pruned set is what is persisted for the next one.
    assert json.loads((tmp_path / BASELINE_FILE).read_text())["failures"] == \
        ["tests/test_a.py::test_one"]

    again = _report(RED_A, RED_B)     # B breaks again → the round owns it
    _apply_baseline(again, str(tmp_path))
    assert again["new_failures"] == [RED_B]
    assert again["passed_relative"] is False


def test_without_a_state_dir_the_fields_still_exist(tmp_path):
    # The `stateful` capability is what injects state_dir. With no baseline to
    # keep, the fields must still be PRESENT — a reader routing on a missing
    # `passed_relative` would read absence as a pass — and `baseline_state`
    # must say they are not a measurement.
    r = _report(RED_A)
    _apply_baseline(r, "")
    assert r["new_failures"] == [RED_A]
    assert r["passed_relative"] is False
    assert r["baseline_failures"] == []
    assert r["baseline_state"] == "unavailable"
    assert not list(Path(tmp_path).iterdir())


def test_failure_keys_drop_what_varies_run_to_run():
    # Same test, different assertion text → same known-red identity.
    assert _failure_key(RED_A) == _failure_key(
        "FAILED tests/test_a.py::test_one - AssertionError: assert 3 == 4")
    # Gate entries embed a returncode and an output tail; keyed on the gate.
    assert _failure_key("repo_gate:run_tests.sh failed (rc=1): 9 parse errors") \
        == "repo_gate:run_tests.sh"
    assert _failure_key("repo_gate:run_tests.sh#compile/autoload failed: old") \
        == "repo_gate:run_tests.sh#compile/autoload"
    assert _failure_key("node:build failed (rc=2): blah") == "node:build"
    assert _failure_key("ERROR tests/test_c.py - ImportError: no module x") \
        == "tests/test_c.py"


def test_an_unreadable_baseline_is_reported_not_silently_reseeded(tmp_path):
    (tmp_path / BASELINE_FILE).write_text("{not json")
    r = _report(RED_A)
    _apply_baseline(r, str(tmp_path))
    assert "unreadable baseline" in r["baseline_error"]
    assert r["new_failures"] == [RED_A]      # nothing is known-red
    assert r["passed_relative"] is False


def _gate_record(case_id, detail, **extra):
    value = {"case_id": case_id, "status": "failed", "detail": detail,
             **extra}
    return "AITELIER_REPO_GATE_CASE=" + json.dumps(value)


def _write_repo_gate(repo, *lines):
    script = repo / "run_tests.sh"
    body = "#!/bin/sh\n" + "\n".join(
        "printf '%s\\n' " + repr(line) for line in lines) + "\nexit 1\n"
    script.write_text(body)
    script.chmod(0o755)


def _run_repo(repo, out, state):
    from aitelier.tools.run_tests.impl import run_tests
    result = run_tests(project_root=str(repo), out_dir=str(out),
                       state_dir=str(state))
    report = json.loads((out / "test_report.json").read_text())
    assert result["passed"] is False and report["passed"] is False
    return report


def test_real_repo_gate_a_then_a_plus_b_exposes_b_and_keeps_detail_stable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    _write_repo_gate(repo, _gate_record("A", "first detail"))
    first = _run_repo(repo, tmp_path / "out-a", state)
    assert first["baseline_seeded"] is True
    assert first["new_failures"] == []

    _write_repo_gate(repo, _gate_record("A", "different human detail"),
                     _gate_record("B", "new case"))
    second = _run_repo(repo, tmp_path / "out-ab", state)
    assert second["passed_relative"] is False
    assert len(second["new_failures"]) == 1
    assert "#B failed: new case" in second["new_failures"][0]
    assert second["baseline_failures"] == ["repo_gate:run_tests.sh#A"]


def test_real_repo_gate_fixed_then_rebroken_case_becomes_new(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    _write_repo_gate(repo, _gate_record("A", "a"), _gate_record("B", "b"))
    _run_repo(repo, tmp_path / "out-seed", state)
    _write_repo_gate(repo, _gate_record("A", "a remains"))
    _run_repo(repo, tmp_path / "out-fixed", state)
    assert json.loads(_baseline_path(state, repo).read_text())["failures"] == [
        "repo_gate:run_tests.sh#A"]
    _write_repo_gate(repo, _gate_record("A", "a"), _gate_record("B", "back"))
    again = _run_repo(repo, tmp_path / "out-again", state)
    assert len(again["new_failures"]) == 1
    assert "#B failed: back" in again["new_failures"][0]


def test_repo_gate_without_state_dir_stays_strict(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_repo_gate(repo, _gate_record("A", "a"))
    from aitelier.tools.run_tests.impl import run_tests
    out = tmp_path / "out"
    result = run_tests(project_root=str(repo), out_dir=str(out), state_dir="")
    report = json.loads((out / "test_report.json").read_text())
    assert result["passed_relative"] is False
    assert report["new_failures"] == [
        "repo_gate:run_tests.sh#A failed: a"]


def test_unreliable_repo_gate_identity_never_seeds_or_changes_baseline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = tmp_path / "state"
    _write_repo_gate(repo, _gate_record("A", "known"))
    _run_repo(repo, tmp_path / "out-seed", state)
    before = _baseline_path(state, repo).read_bytes()

    variants = {
        "missing": ["ordinary failure prose"],
        "duplicate": [_gate_record("A", "one"), _gate_record("A", "two")],
        "malformed": ["AITELIER_REPO_GATE_CASE={not json"],
        "ambiguous": [_gate_record("A", "detail", id="B")],
        "truncated": ["x" * 2100, _gate_record("A", "tail")],
    }
    for name, lines in variants.items():
        _write_repo_gate(repo, *lines)
        report = _run_repo(repo, tmp_path / ("out-" + name), state)
        assert report["passed_relative"] is False, name
        assert report["new_failures"], name
        assert "failure_identity_error" in report["repo_gate"], name
        assert _baseline_path(state, repo).read_bytes() == before, name


# ── no-state-dir is not an empty baseline ───────────────────────────────────
# Measured 2026-09-20: `state_dir` was unset on every run in the deployment —
# no config declared `capability: stateful` — so `_apply_baseline` took the
# `path is None` branch every time, `known` stayed empty, and the report came
# out with `baseline_failures: []`. That is byte-identical to the report of a
# repo that genuinely carries no standing red, and the loop read it that way.

def test_an_unavailable_baseline_is_distinguishable_from_an_empty_one(tmp_path):
    seeded = _report()                       # a genuinely clean repo
    _apply_baseline(seeded, str(tmp_path))
    absent = _report()
    _apply_baseline(absent, "")

    assert seeded["baseline_failures"] == absent["baseline_failures"] == []
    # ...and the ONE field that separates them:
    assert seeded["baseline_state"] == "seeded"
    assert absent["baseline_state"] == "unavailable"


def test_an_unavailable_baseline_cannot_produce_a_relative_pass(tmp_path):
    """A red run whose failures nobody could key used to come out forgiven.

    pytest returncode 2 (usage/internal error) is red with no FAILED lines at
    all. With no baseline, `new_failures` was then `[]` and `passed_relative`
    was True — a claim that the repo already carried this red, made against
    nothing. `gate_evidence.report_state` reads that as `known_failure`, which
    in game_harness routes 5_compile straight on to 5_vision.
    """
    r = {"passed": False, "returncode": 2, "failures": []}
    _apply_baseline(r, "")
    assert r["passed_relative"] is False
    assert r["baseline_state"] == "unavailable"

    # The same report WITH a baseline behind it may still be forgiven.
    ok = {"passed": False, "returncode": 2, "failures": []}
    _apply_baseline(ok, str(tmp_path))
    assert ok["passed_relative"] is True and ok["baseline_state"] == "seeded"


def test_an_unreadable_baseline_is_not_reported_as_an_empty_one(tmp_path):
    (tmp_path / BASELINE_FILE).write_text("{not json")
    r = _report()
    _apply_baseline(r, str(tmp_path))
    assert r["baseline_state"] == "unreadable"
    assert r["passed_relative"] is False


# ── a test that did not run is not a test that passed ───────────────────────

def test_a_known_red_that_never_ran_stays_known_red(tmp_path):
    """The prune rule used to be `keep = known & {keys of this run's failures}`.

    A known-red test that was not collected at all this run — renamed, deleted,
    behind a collection error, or in a session pytest never finished — is
    absent from the failure list for the same reason a FIXED one is, so it was
    dropped. Next run it fails again and the round is blamed for it.
    """
    _apply_baseline(_report(RED_A, RED_B), str(tmp_path))     # seed A and B

    # This run executed only A (B's module was never collected).
    only_a_ran = Executed(node_ids={"tests/test_a.py::test_one"},
                          pytest_complete=True)
    r = _report(RED_A)
    _apply_baseline(r, str(tmp_path), only_a_ran)

    kept = json.loads((tmp_path / BASELINE_FILE).read_text())["failures"]
    assert "tests/test_b.py::test_two" in kept
    assert r["baseline_kept_unproven"] == ["tests/test_b.py::test_two"]

    # ...so when B fails again it is still known-red, not the round's fault.
    again = _report(RED_A, RED_B)
    _apply_baseline(again, str(tmp_path), only_a_ran)
    assert again["new_failures"] == []
    assert again["passed_relative"] is True


def test_a_known_red_that_ran_green_is_still_pruned(tmp_path):
    """The other polarity: proven execution DOES license the shrink."""
    _apply_baseline(_report(RED_A, RED_B), str(tmp_path))     # seed A and B
    r = _report(RED_A)
    _apply_baseline(r, str(tmp_path), RAN_BOTH)

    assert json.loads((tmp_path / BASELINE_FILE).read_text())["failures"] == [
        "tests/test_a.py::test_one"]
    assert "baseline_kept_unproven" not in r


def test_no_execution_evidence_at_all_prunes_nothing(tmp_path):
    """A timeout or a crashed runner leaves `Executed` empty. Nothing shrinks."""
    _apply_baseline(_report(RED_A, RED_B), str(tmp_path))
    r = _report()                       # zero failures reported — nothing ran
    _apply_baseline(r, str(tmp_path), Executed())
    assert json.loads((tmp_path / BASELINE_FILE).read_text())["failures"] == [
        "tests/test_a.py::test_one", "tests/test_b.py::test_two"]


def test_a_repo_gate_that_did_not_run_keeps_its_known_red(tmp_path):
    _apply_baseline(_report("repo_gate:run_tests.sh#A failed: x"), str(tmp_path))
    r = _report()
    _apply_baseline(r, str(tmp_path), Executed(pytest_complete=True))
    assert json.loads((tmp_path / BASELINE_FILE).read_text())["failures"] == [
        "repo_gate:run_tests.sh#A"]
    # The gate ran and did not report the case → it passed → prune.
    r2 = _report()
    _apply_baseline(r2, str(tmp_path),
                    Executed(pytest_complete=True, repo_gate_cases=set()))
    assert json.loads((tmp_path / BASELINE_FILE).read_text())["failures"] == []


def test_junit_gives_the_node_ids_pytest_executed(tmp_path):
    from aitelier.tools.run_tests.impl import _junit_node_ids
    (tmp_path / "j.xml").write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_a" name="test_one" file="tests/test_a.py"/>'
        '<testcase classname="tests.test_b.TestK" name="test_m[x]" '
        'file="tests/test_b.py"/>'
        '</testsuite></testsuites>', encoding="utf-8")
    assert _junit_node_ids(tmp_path / "j.xml") == {
        "tests/test_a.py::test_one", "tests/test_b.py::TestK::test_m[x]"}


def test_an_unparseable_junit_file_licenses_no_prune(tmp_path):
    from aitelier.tools.run_tests.impl import _junit_node_ids
    (tmp_path / "j.xml").write_text("<not xml", encoding="utf-8")
    assert _junit_node_ids(tmp_path / "j.xml") == set()


# ── one state_dir, many repositories ────────────────────────────────────────

def test_two_repositories_under_one_config_do_not_share_a_baseline(tmp_path):
    """`stateful` keys state_dir by CONFIG; coding_impl runs against any repo."""
    a = _baseline_dir(str(tmp_path), Path("/srv/projects/alpha"))
    b = _baseline_dir(str(tmp_path), Path("/srv/projects/beta"))
    assert a and b and a != b
    assert str(tmp_path) in a and str(tmp_path) in b       # still relative to it
    assert _baseline_dir("", Path("/srv/projects/alpha")) == ""
