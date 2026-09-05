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
    assert "recorded, NOT gating (reviewed debt, not proof of intent): exit_leak, io_input" in report["summary"]


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
    assert "recorded, NOT gating (reviewed debt, not proof of intent): unclassified" in report["summary"]


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

    gating, debt = gh._split_diagnostics(errs)
    assert sorted(e["native_class"] for e in gating) == ["deferred_call", "image_format"]
    assert all(e["kind"] == "native" for e in gating)
    # non-gating classes are not thrown away either — they come back with the
    # rest, and the CALLER scopes the verdict, so they survive an empty snapshot
    assert [e["native_class"] for e in debt] == ["exit_leak"]
    assert "native_debt" not in probe


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


# ── ownership: what `_parse_errors` actually claims ────────────────────────
#
# `_parse_errors` keeps a diagnostic when the line is a SCRIPT ERROR, a failed
# script load, or an ERROR located at a `push_error` frame. A res:// location on
# a plain ERROR is NOT one of those tests — it only decorates a diagnostic the
# parser already decided to keep. The first version of `_native_errors` skipped
# every res://-located line "because `_parse_errors` owns it", so a plain ERROR
# with a res:// frame fell out of BOTH fields.
#
# No stored report exhibits that shape (1254 report files scanned 2026-09-05,
# zero hits), so this is a latent hole, not a measured loss. It is still a hole:
# the union of the two structured fields has to be lossless by construction, not
# by luck.

def _union(result):
    return result["errors"] + result["native_errors"]


def test_a_plain_engine_error_located_in_user_code_survives_in_the_union(monkeypatch, tmp_path):
    stderr = ("ERROR: Cannot convert argument 2 from Array to Array.\n"
              "   at: _wire_hud (res://scripts/battle/battlefield.gd:41)\n")
    result = _script_report(monkeypatch, tmp_path, stderr)["results"][0]

    kept = [e for e in _union(result) if "Cannot convert argument" in e["msg"]]
    assert len(kept) == 1                                   # once, not zero, not twice
    assert kept[0]["kind"] == "native"
    assert kept[0]["native_class"] == "type_conversion" and kept[0]["blocking"] is True
    assert kept[0]["file"] == "res://scripts/battle/battlefield.gd"
    assert kept[0]["line"] == 41                            # the location is kept too
    assert result["passed"] is False


def test_an_unknown_engine_error_located_in_user_code_survives_as_debt(monkeypatch, tmp_path):
    stderr = ('ERROR: Condition "!is_inside_tree()" is true.\n'
              "   at: _ready (res://scripts/ui/hud.gd:12)\n")
    result = _script_report(monkeypatch, tmp_path, stderr)["results"][0]

    kept = [e for e in _union(result) if "is_inside_tree" in e["msg"]]
    assert len(kept) == 1
    assert kept[0]["native_class"] == "unclassified" and kept[0]["blocking"] is False
    assert kept[0]["file"] == "res://scripts/ui/hud.gd" and kept[0]["line"] == 12
    assert result["passed"] is True                         # unknown is debt, not a verdict


def test_the_lines_parse_errors_really_owns_are_never_counted_twice(monkeypatch, tmp_path):
    """Old counts must not move: a push_error, a failed load and a SCRIPT ERROR
    stay exactly one `errors[]` entry each and produce no native twin."""
    stderr = ("ERROR: deliberate game error\n"
              "   at: push_error (core/variant/variant_utility.cpp:1098)\n"
              'ERROR: Failed to load script "res://scripts/broken.gd"\n'
              "   at: push_error (core/variant/variant_utility.cpp:1098)\n"
              "SCRIPT ERROR: Parse Error: Identifier \"foo\" not declared.\n"
              "          at: GDScript::reload (res://scripts/broken.gd:7)\n")
    result = _script_report(monkeypatch, tmp_path, stderr)["results"][0]

    assert [e["kind"] for e in result["errors"]] == ["push_error", "load", "parse"]
    assert result["native_errors"] == []
    assert len(_union(result)) == 3


# ── evidence retention when the run produced no snapshot ───────────────────
#
# The probe used to hang the non-gating classes off the snapshot dict, so a
# timed-out or malformed run reported "scene did not run" and threw the
# accompanying evidence away with the empty probe. stderr exists whether or not
# a snapshot does; the split now happens in the caller, on the diagnostics.

def _spec_run(monkeypatch, tmp_path, fixture, *, timeout=False, snapshot=None):
    """Drive `_playtest_spec` for real down to `_run`, controlling the snapshot."""
    state = tmp_path / "probe_state.json"

    def fake_run(args, timeout_=0, extra_env=None, render=False, **kw):
        if snapshot is not None:
            state.write_text(snapshot)
        if timeout:
            raise subprocess.TimeoutExpired("godot", 10, output=b"", stderr=_stderr(fixture).encode())
        return subprocess.CompletedProcess("godot", 0, "", _stderr(fixture))

    monkeypatch.setattr(gh, "_run", lambda args, timeout=0, extra_env=None, render=False:
                        fake_run(args, timeout, extra_env, render))
    spec = {"scenarios": [{"name": "s", "timeline": [{"at": 8, "assert": []}]}]}
    return gh._playtest_spec(tmp_path / "proj", spec, 30, 10)


def test_a_run_with_no_snapshot_fails_and_keeps_every_diagnostic(monkeypatch, tmp_path):
    """Timed out, no probe file: HARD fail, and the evidence still arrives."""
    r = _spec_run(monkeypatch, tmp_path, "encounter_wave4.txt", timeout=True)

    assert r["passed"] is False
    assert r["behavior"]["scenarios"][0]["ran"] is False       # empty probe is NOT "ran"
    assert sorted({e["native_class"] for e in r["errors"]
                   if e["kind"] == "native"}) == ["deferred_call", "image_format"]
    assert [e["native_class"] for e in r["native_debt"]] == ["exit_leak"]
    assert r["native_debt"][0]["scenario"] == "s"
    assert [e["native_class"] for e in
            r["behavior"]["scenarios"][0]["native_debt"]] == ["exit_leak"]


def test_a_malformed_snapshot_fails_and_keeps_every_diagnostic(monkeypatch, tmp_path):
    """Unparseable probe JSON: no asserts, so nothing passes — and the engine's
    own account of the run is still in the report."""
    r = _spec_run(monkeypatch, tmp_path, "encounter_wave4.txt", snapshot="{not json")

    assert r["passed"] is False                                # blocking natives
    assert r["behavior"]["all_passed"] is False                # no asserts evaluated
    assert r["behavior"]["scenarios"][0]["asserts"] == []
    assert any(e["native_class"] == "deferred_call" for e in r["errors"])
    assert [e["native_class"] for e in r["native_debt"]] == ["exit_leak"]


def test_debt_only_output_with_no_snapshot_is_still_recorded_and_still_not_green(monkeypatch, tmp_path):
    """Nothing blocking in stderr, no usable snapshot: the io_input/exit_leak
    evidence is retained and the scenario does not pass on empty assertions."""
    r = _spec_run(monkeypatch, tmp_path, "negative_user_storage.txt", snapshot="{}")

    assert r["behavior"]["scenarios"][0]["passed"] is False
    assert r["behavior"]["all_passed"] is False
    assert sorted({e["native_class"] for e in r["native_debt"]}) == ["exit_leak", "io_input"]
    assert r["errors"] == []                                   # debt never gates


def test_the_legacy_playtest_keeps_the_debt_when_the_probe_is_empty(monkeypatch, tmp_path):
    debt = {"kind": "native", "native_class": "exit_leak", "blocking": False,
            "msg": "1 resources still in use at exit", "at": None,
            "file": None, "line": None, "count": 1}
    monkeypatch.setattr(gh, "_run_probe", lambda *a, **k: ({}, [debt], True))
    r = gh._playtest_legacy(tmp_path / "proj", 30, "ui_accept", 10)

    assert r["passed"] is False and "could not run" in r["summary"]
    assert r["errors"] == []
    assert [e["native_class"] for e in r["native_debt"]] == ["exit_leak"]


def test_the_playtest_summary_names_the_debt_it_is_not_gating_on():
    """The distilled summary is what an agent reads; a report field it never
    prints is evidence nobody sees."""
    from aitelier.tools.godot_compile.impl import _write_playtest_summary
    import tempfile

    pt = {"passed": True, "spec_used": True, "frames": 30, "errors": [],
          "summary": "ran clean",
          "native_debt": [{"kind": "native", "native_class": "exit_leak",
                           "blocking": False, "count": 2,
                           "msg": "1 resources still in use at exit",
                           "at": "clear (core/io/resource.cpp:614)",
                           "file": None, "line": None, "scenario": "s"}],
          "behavior": {"all_passed": True, "scenarios": []}}
    with tempfile.TemporaryDirectory() as d:
        _write_playtest_summary(Path(d), pt)
        md = (Path(d) / "playtest_summary.md").read_text(encoding="utf-8")

    assert "Native engine errors (recorded, not gating)" in md
    assert "exit_leak" in md and "resources still in use at exit" in md
