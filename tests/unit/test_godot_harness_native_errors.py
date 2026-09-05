"""Native (engine-side) Godot errors: classified, reported, and gated by class.

The defect these tests pin: `godot_harness._parse_errors` keeps only diagnostics
that carry a res:// frame, so a plain engine `ERROR:` line was discarded — and
`run_script` decides pass/fail from the return code and the suite's own FAIL
marker alone. A suite could therefore exit 0, print PASS, and be reported green
over a call the engine refused to make.

Every stderr below is the REAL stderr of a real report, copied out and stripped
of content irrelevant to classification (enemy names, the user:// temp path,
two game-balance push_error lines). Provenance, on linxuhaserver:

  encounter_wave4.txt             ~/.AItelier/director/reports/m1-runtime-wave3/
                                  raw/scripts-wave4-22d9b64-script.json
                                  → res://tests/test_encounter.gd, which that
                                  report called `passed: true`
  encounter_repaired_0c7d857.txt  same folder, hud-0c7d857-script.json, the same
                                  suite after the deferred-call repair
  negative_user_storage.txt       the wave-4 unit_test_runner run, whose
                                  test_user_storage feeds malformed JSON, a
                                  truncated ConfigFile and an impossible copy ON
                                  PURPOSE

The tests drive `run_script` / `_probe_once` / `_playtest_spec` — the report and
its pass decision — not just the regex helper.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

_FIXTURES = Path(__file__).parent / "fixtures" / "godot_native_stderr"


def _stderr(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _script_report(monkeypatch, tmp_path, stderr, rc=0, stdout="PASS test_encounter\n"):
    """Run the script gate over one entry point with `stderr` as Godot's output."""
    (tmp_path / "project.godot").write_text("[application]\n")
    work = tmp_path / "work" / "proj"
    work.mkdir(parents=True)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources", lambda dst, timeout=0: None)
    monkeypatch.setattr(gh, "_run", lambda *a, **k: subprocess.CompletedProcess(
        "godot", rc, stdout, stderr))
    return gh.run_script(str(tmp_path), ["res://tests/test_encounter.gd"], timeout=60)


def _classes(result, blocking=None):
    natives = result["native_errors"]
    if blocking is not None:
        natives = [e for e in natives if e["blocking"] is blocking]
    return sorted({e["native_class"] for e in natives})


# ── the run that passed and should not have ────────────────────────────────
def test_the_deferred_call_that_passed_the_encounter_report_now_fails_it(monkeypatch, tmp_path):
    """rc 0, PASS on stdout, no FAIL marker — and still red, because the engine
    said the game's deferred `_wire_hud` call could not be made."""
    report = _script_report(monkeypatch, tmp_path, _stderr("encounter_wave4.txt"))
    result = report["results"][0]

    assert report["passed"] is False
    assert result["returncode"] == 0 and result["passed"] is False
    deferred = [e for e in result["native_errors"] if e["native_class"] == "deferred_call"]
    assert len(deferred) == 1
    assert "_wire_hud" in deferred[0]["msg"] and deferred[0]["blocking"] is True
    assert deferred[0]["at"] == "_call_function (core/object/message_queue.cpp:222)"
    assert "deferred_call" in result["native_blocking"]
    assert "deferred_call" in report["summary"]


def test_the_atlas_format_error_blocks_too_and_is_reported_once_with_its_count(monkeypatch, tmp_path):
    """The blit_rect format errors were missed the same way. Eight identical
    lines collapse to ONE entry carrying count 8 — deduplicated, not dropped."""
    result = _script_report(monkeypatch, tmp_path,
                            _stderr("encounter_wave4.txt"))["results"][0]

    atlas = [e for e in result["native_errors"] if e["native_class"] == "image_format"]
    assert len(atlas) == 1
    assert atlas[0]["count"] == 8 and atlas[0]["blocking"] is True
    assert atlas[0]["at"] == "blit_rect (core/io/image.cpp:2865)"


def test_the_repaired_run_clears_the_deferred_call_and_still_shows_the_atlas_debt(monkeypatch, tmp_path):
    """hud-0c7d857 fixed the deferred call; the atlas errors are still there.
    The gate must say so instead of reporting the suite green again."""
    result = _script_report(monkeypatch, tmp_path,
                            _stderr("encounter_repaired_0c7d857.txt"))["results"][0]

    assert _classes(result, blocking=True) == ["image_format"]
    assert not any(e["native_class"] == "deferred_call" for e in result["native_errors"])
    assert result["passed"] is False


# ── the negative tests that must stay green ────────────────────────────────
def test_a_suite_that_provokes_engine_errors_on_purpose_still_passes(monkeypatch, tmp_path):
    """test_user_storage's malformed JSON, corrupt ConfigFile and failed copy are
    the test doing its job. They are recorded, with their class, and gate nothing."""
    report = _script_report(monkeypatch, tmp_path, _stderr("negative_user_storage.txt"),
                            stdout="PASS test_user_storage\n")
    result = report["results"][0]

    assert report["passed"] is True and result["passed"] is True
    assert result["native_blocking"] == []
    assert _classes(result) == ["exit_leak", "io_input"]
    io = [e for e in result["native_errors"] if e["native_class"] == "io_input"]
    assert {e["at"].split(" ")[1] for e in io} == {"(core/io/json.cpp:582)",
                                                  "(core/io/config_file.cpp:303)",
                                                  "(core/io/dir_access.cpp:425)"}
    assert all(e["blocking"] is False for e in io)
    assert "Recorded, not gating: exit_leak, io_input" in report["summary"]


def test_the_exit_leaks_stay_observable_instead_of_reading_as_absent(monkeypatch, tmp_path):
    """Resource / RID leaks at exit are known debt: non-gating, never silent."""
    result = _script_report(monkeypatch, tmp_path, _stderr("negative_user_storage.txt"),
                            stdout="PASS test_user_storage\n")["results"][0]

    leaks = [e for e in result["native_errors"] if e["native_class"] == "exit_leak"]
    assert any("14 resources still in use at exit" in e["msg"] for e in leaks)
    assert sum(1 for e in leaks if "RID allocations" in e["msg"]) == 3
    assert all(e["blocking"] is False for e in leaks)


def test_an_engine_error_nobody_classified_stays_visible_as_unclassified_debt(monkeypatch, tmp_path):
    """The blanket drop is gone, but an unknown line is not promoted to a
    failure either: it is debt, and debt has to be readable to be paid."""
    noise = ('ERROR: Condition "!tasks.has(p_task)" is true. Returning: canceled\n'
             "   at: task_step (editor/progress_dialog.cpp:217)\n")
    report = _script_report(monkeypatch, tmp_path, noise, stdout="PASS x\n")
    result = report["results"][0]

    assert gh._parse_errors(noise) == []          # the old contract is untouched
    assert _classes(result) == ["unclassified"]
    assert result["passed"] is True
    assert "Recorded, not gating: unclassified" in report["summary"]


def test_a_push_error_stays_a_push_error_and_is_never_counted_twice(monkeypatch, tmp_path):
    """`errors[]` keeps exactly the population it always had."""
    stderr = ("ERROR: deliberate game error\n"
              "   at: push_error (core/variant/variant_utility.cpp:1098)\n")
    result = _script_report(monkeypatch, tmp_path, stderr)["results"][0]

    assert [e["kind"] for e in result["errors"]] == ["push_error"]
    assert result["native_errors"] == []


# ── evidence handling ──────────────────────────────────────────────────────
def test_classification_reads_the_unabridged_stream_and_keeps_the_truncation_flags(monkeypatch, tmp_path):
    """The display excerpt is capped at 4000 chars; the classifier is not. A
    deferred call buried mid-log must still gate the run."""
    deferred = ("ERROR: Error calling deferred method: "
                "'Node2D(battlefield.gd)::_wire_hud': Cannot convert argument 2 "
                "from Array to Array.\n"
                "   at: _call_function (core/object/message_queue.cpp:222)\n")
    stderr = "engine noise\n" * 400 + deferred + "engine noise\n" * 400
    result = _script_report(monkeypatch, tmp_path, stderr)["results"][0]

    assert result["stderr_truncated"] is True
    assert "[middle truncated]" in result["stderr"]
    assert "_wire_hud" not in result["stderr"]     # dropped from the excerpt...
    assert _classes(result, blocking=True) == ["deferred_call"]   # ...not from the verdict
    assert result["passed"] is False


# ── play-test consistency ──────────────────────────────────────────────────
def test_the_playtest_probe_gates_on_the_same_classes(monkeypatch, tmp_path):
    state = tmp_path / "probe_state.json"

    def fake_run(args, timeout=0, extra_env=None, render=False):
        state.write_text(json.dumps({"frames": 5, "asserts": [], "nodes": {}}))
        return subprocess.CompletedProcess("godot", 0, "", _stderr("encounter_wave4.txt"))

    monkeypatch.setattr(gh, "_run", fake_run)
    probe, errs, timed_out = gh._probe_once(["--path", str(tmp_path)], {}, state, 60, False)

    assert sorted(e["native_class"] for e in errs) == ["deferred_call", "image_format"]
    assert all(e["kind"] == "native" for e in errs)
    # non-gating classes are not thrown away either — they ride in the report
    assert [e["native_class"] for e in probe["native_debt"]] == ["exit_leak"]


def test_a_blocking_native_error_hard_fails_a_scenario_playtest(monkeypatch, tmp_path):
    native = {"kind": "native", "native_class": "deferred_call", "blocking": True,
              "msg": "Error calling deferred method: '...::_wire_hud'",
              "at": "_call_function (core/object/message_queue.cpp:222)",
              "file": None, "line": None, "count": 1}
    monkeypatch.setattr(gh, "_run_probe",
                        lambda *a, **k: ({"frames": 5, "asserts": [], "nodes": {},
                                          "native_debt": []}, [native], False))
    spec = {"scenarios": [{"name": "s", "timeline": [{"at": 8, "assert": []}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)

    assert r["passed"] is False
    assert any(e["native_class"] == "deferred_call" for e in r["errors"])
