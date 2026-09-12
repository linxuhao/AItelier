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

from aitelier.tools.run_tests.impl import (BASELINE_FILE, _apply_baseline,
                                           _failure_key)

RED_A = "FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2"
RED_B = "FAILED tests/test_b.py::test_two - ValueError: nope"


def _report(*failures, passed=False):
    return {"passed": passed, "failures": list(failures)}


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
    _apply_baseline(green, str(tmp_path))
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
    # `passed_relative` would read absence as a pass.
    r = _report(RED_A)
    _apply_baseline(r, "")
    assert r["new_failures"] == [RED_A]
    assert r["passed_relative"] is False
    assert r["baseline_failures"] == []
    assert not list(Path(tmp_path).iterdir())


def test_failure_keys_drop_what_varies_run_to_run():
    # Same test, different assertion text → same known-red identity.
    assert _failure_key(RED_A) == _failure_key(
        "FAILED tests/test_a.py::test_one - AssertionError: assert 3 == 4")
    # Gate entries embed a returncode and an output tail; keyed on the gate.
    assert _failure_key("repo_gate:run_tests.sh failed (rc=1): 9 parse errors") \
        == "repo_gate:run_tests.sh"
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
