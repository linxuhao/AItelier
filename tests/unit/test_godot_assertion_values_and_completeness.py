"""Assertion values and measurement completeness in the Godot playtest probe.

Godot-free: the probe report is faked the same way tests/unit/test_godot_harness.py
fakes it, plus source-level checks against the embedded GDScript. Pinned down:

  * A String observation is a VALUE, not assertion truth (`_truthy`, no
    bool(String)); truth is decided ONLY for the supported kinds (Boolean,
    non-empty String, number, null).
  * Any OTHER observation type is an INCOMPLETE measurement, never a vacuous
    pass through bool(value).
  * `_jsonable(null)` is JSON null, not the four-character string "null".
  * A read gates the RAW observable type BEFORE converting: an unsupported
    value (Color/Dictionary/Array/Object) is a tagged read failure, never
    str(v). `_jsonable` keeps its str() catch-all for the public dumps, so a
    baseline must not be converted first.
  * `_read_attr` is TAGGED: a legitimately captured null and a FAILED read
    (parse/execute error, unsupported type) are different facts. A failed
    frame-0 read never becomes a baseline; a failed current read is an explicit
    incomplete measurement, never a comparison.
  * The observed attribute value is retained for BOTH polarities; a read that
    did not happen is reported as an observed read error.
  * An omitted/crashed scheduled assertion, premature frame termination, an
    unsupported/parse/eval/truncated observation, or a missing delta baseline is
    an INCOMPLETE measurement and can never produce a successful (or
    advisory-green) result. A merely FALSE comparison was measured and stays
    advisory.

Native fixtures: the real-engine normal-route assertion cases remain tracked
in tests/unit/test_godot_harness.py under `@requires_godot` (good_project
fixture) -- test_real_playtest_spec_evaluates_assertions,
test_real_playtest_spec_dict_assert_form, test_real_playtest_dumps_state and
test_real_playtest_catches_runtime_error. They exercise the live Expression
evaluator, both assertion polarities and runtime-error hard-failing against a
real Godot binary; this file deliberately does not duplicate them. The four old
native cases are NOT new-case proof for String/null/error observations: a
tracked native fixture/spec for those belongs to the root-controlled native
slot, which the source worker never invokes.
"""

import importlib.util
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

_PROBE_SRC = (gh.PROBE_GD if hasattr(gh, "PROBE_GD")
              else _HARNESS.read_text(encoding="utf-8"))


def _fake_run_probe(monkeypatch, probe, errs=(), timed_out=False):
    def fake(dst, state_path, frames, timeout, extra, scene="", capture_at=None,
             timing=None, render=True, **kwargs):
        if timing is not None:
            timing["game_usec"] = int(probe.get("frames", 0) * 1_000_000 / 60)
        return probe, list(errs), timed_out

    monkeypatch.setattr(gh, "_run_probe", fake)


def _spec_one_assert():
    return {"scenarios": [{"name": "s", "timeline": [
        {"at": 8, "assert": [{"node": "Bird", "expr": "velocity.y < 0"}]}]}]}


# ── completeness: premature termination / missing assertion rows ──────────
def test_premature_frame_termination_is_a_hard_failure(monkeypatch, tmp_path):
    # The probe reports every scheduled assertion, but the game quit before the
    # frame budget was spent. The measurement is incomplete: it must not read
    # as a shorter successful run.
    _fake_run_probe(monkeypatch, {"frames": 4, "complete": False,
                                  "asserts": [{"name": "a", "passed": True,
                                               "actual": True}],
                                  "nodes": {}})
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    assert r["passed"] is False
    assert any("incomplete measurement" in e for e in r["spec_errors"])
    row = r["behavior"]["scenarios"][0] if "scenarios" in r["behavior"] else None
    assert row is not None and row["complete"] is False


def test_omitted_scheduled_assertion_is_a_hard_failure(monkeypatch, tmp_path):
    # Two assertions scheduled, one row reported (e.g. the frame holding the
    # second one never ran, or its evaluation crashed). A report that lost an
    # assertion is not a passing measurement.
    _fake_run_probe(monkeypatch, {"frames": 50,
                                  "asserts": [{"name": "a", "passed": True,
                                               "actual": True}],
                                  "nodes": {}})
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 8, "assert": [{"node": "Bird", "expr": "velocity.y < 0"},
                             {"node": "Bird", "expr": "alive"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is False
    assert any("incomplete measurement" in e for e in r["spec_errors"])
    row = r["behavior"]["scenarios"][0] if "scenarios" in r["behavior"] else None
    assert row is not None
    assert row["asserts_missing"] == 1
    assert row["expected_asserts"] == 2


def test_complete_run_with_all_assertions_still_passes(monkeypatch, tmp_path):
    # Normal completion is untouched: the probe reached its budget and every
    # scheduled assertion came back.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True,
                                  "asserts": [{"name": "a", "passed": True,
                                               "actual": True, "observed": True}],
                                  "nodes": {}})
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    assert r["passed"] is True
    assert r["behavior"]["all_passed"] is True


def test_complete_report_with_failing_assertion_stays_advisory(monkeypatch, tmp_path):
    # A complete, clean run with a legitimately FALSE assertion keeps the old
    # advisory shape: hard pass, behaviour fail.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True,
                                  "asserts": [{"name": "a", "passed": False,
                                               "actual": False, "observed": 3.0}],
                                  "nodes": {}})
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    assert r["passed"] is True
    assert r["behavior"]["all_passed"] is False


def test_missing_baseline_report_fails_the_scenario(monkeypatch, tmp_path):
    # The probe reports a delta assertion whose frame-0 baseline was never
    # captured: explicit error + baseline_missing, passed False. That is an
    # INCOMPLETE measurement, so the run is a HARD failure -- it is not a green
    # run with a null comparison, and not merely an advisory miss.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "d", "expr": "grid_pos changed since frame 0", "passed": False,
         "error": "missing frame-0 baseline for grid_pos on Bird",
         "actual": {"baseline": None, "current": [3, 4], "baseline_missing": True}}],
        "nodes": {}})
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 8, "assert": [{"node": "Bird", "attr": "grid_pos",
                              "mode": "changed"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is False                     # incomplete, not advisory
    assert any("incomplete measurement" in e for e in r["spec_errors"])
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is False and row["incomplete_asserts"] == 1
    assert r["behavior"]["all_passed"] is False


def test_an_unsupported_observed_attr_row_is_hard_incomplete(monkeypatch, tmp_path):
    # An attr read of an unsupported raw type (Color/Dictionary/Array/Object) is
    # a TAGGED read failure, never str(v): the row is an incomplete measurement,
    # so it cannot read as a green observation of some string.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "a", "node": "Bird", "attr": "stuff", "expr": "stuff",
         "passed": False, "actual": None, "observed": None,
         "observed_error": "attribute is not a supported observable type (27)",
         "measurement": "incomplete",
         "error": "observed read failed: attribute is not a supported observable "
                  "type (27)"}], "nodes": {}})
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    assert r["passed"] is False
    assert any("incomplete measurement" in e for e in r["spec_errors"])
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is False and row["incomplete_asserts"] == 1
    assert r["behavior"]["all_passed"] is False


def test_a_direct_string_value_row_is_measured_not_vacuous(monkeypatch, tmp_path):
    # A DIRECT String observation is a measured value: complete, with actual and
    # observed retained; truth (non-empty) decides the advisory row.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "a", "node": "Bird", "attr": "label", "expr": "label",
         "passed": True, "actual": "ready", "observed": "ready"}], "nodes": {}})
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is True and row["incomplete_asserts"] == 0
    assert row["asserts"][0]["actual"] == "ready"
    assert row["asserts"][0]["observed"] == "ready"
    assert row["asserts"][0]["passed"] is True


# ── probe source: values, truth, baselines ────────────────────────────────
def test_probe_decides_truth_without_bool_of_string():
    # A String observation is a value; truth goes through _truthy, which takes
    # a Boolean as-is and a non-empty String as true. The old bool(val) path
    # (which coerced every value and read "false" for the empty String by
    # accident of casting) is gone from the assert path.
    assert 'res["passed"] = _truthy(val)' in _PROBE_SRC
    assert 'res["passed"] = bool(val)' not in _PROBE_SRC
    assert "func _truthy(v) -> bool:" in _PROBE_SRC
    _tail = _PROBE_SRC[_PROBE_SRC.index("func _truthy"):]
    truthy = _PROBE_SRC[_PROBE_SRC.index("func _truthy"):][
        :_tail.index("func _resolve")]
    assert "TYPE_STRING" in truthy and 'return v != ""' in truthy
    assert "TYPE_BOOL" in truthy and "return v" in truthy


def test_probe_records_the_string_observation_as_a_value():
    # The value the expression produced is recorded whatever its type, before
    # truth is decided -- an observation is reported even when it is not an
    # assertion.
    assert 'res["actual"] = _jsonable(val)' in _PROBE_SRC
    assert 'TYPE_INT, TYPE_FLOAT, TYPE_BOOL, TYPE_STRING:' in _PROBE_SRC


def test_probe_missing_baseline_is_explicit_not_null():
    # A delta without a captured frame-0 baseline is refused explicitly and
    # carries baseline_missing, instead of comparing against a fabricated null
    # (which made every `changed` assert pass vacuously).
    assert "if not _baselines.has(key):" in _PROBE_SRC
    assert '"baseline_missing": true' in _PROBE_SRC
    assert 'res["passed"] = false' in _PROBE_SRC
    # ...and the frame-0 walk records which keys it could not read at all.
    assert "_baseline_missing.append(" in _PROBE_SRC
    assert 'out["baseline_missing"] = _baseline_missing' in _PROBE_SRC

def test_probe_unsupported_observation_type_is_incomplete_not_vacuous():
    # bool() of an unsupported kind would let an arbitrary value decide truth
    # vacuously; the probe refuses to decide and marks the row incomplete.
    assert "unsupported observation type" in _PROBE_SRC
    assert 'res["measurement"] = "incomplete"' in _PROBE_SRC
    assert "TYPE_NIL" in _PROBE_SRC


def test_probe_jsonable_maps_nil_to_the_json_null_literal():
    # A real null must serialize as JSON null, not the "null" string the old `_`
    # branch produced via str(null). TYPE_NIL must precede the str() catch-all.
    start = _PROBE_SRC.index("func _jsonable(v):")
    body = _PROBE_SRC[start:_PROBE_SRC.index("\nfunc ", start + 1)]
    nil = body.index("TYPE_NIL")
    catchall = body.index("_:")
    assert nil < catchall, "TYPE_NIL must precede str() catch-all: %r" % body
    assert "return null" in body[nil:catchall]


def test_probe_read_attr_is_tagged_not_collapsed_onto_null():
    # A parse/execute failure used to `return null`, indistinguishable from a
    # legitimately null attribute. The read now returns a tagged dictionary.
    assert "func _read_attr(target: Object, attr: String) -> Dictionary:" in _PROBE_SRC
    assert '"ok": false' in _PROBE_SRC and '"ok": true' in _PROBE_SRC
    assert "read parse error: " in _PROBE_SRC
    assert "read execute failed: " in _PROBE_SRC
    assert 'if e.parse(attr) != OK:\n        return null' not in _PROBE_SRC
    assert 'if e.has_execute_failed():\n        return null' not in _PROBE_SRC


def test_probe_failed_frame0_read_never_becomes_a_baseline():
    # Only an OK frame-0 read is stored; a failed read is MISSING, so a later
    # `changed` cannot compare against a fabricated null and pass vacuously.
    assert 'if r["ok"]:' in _PROBE_SRC
    assert '_baselines[key] = r["value"]' in _PROBE_SRC
    assert "_baseline_missing.append(key)" in _PROBE_SRC


def test_probe_delta_current_read_error_is_incomplete():
    assert "delta read failed for" in _PROBE_SRC
    assert '"current_error"' in _PROBE_SRC
    assert "missing frame-0 baseline" in _PROBE_SRC


def test_probe_observed_read_reports_a_failed_read():
    # The observed value is retained for both polarities; a read that did not
    # happen is reported as an observed read error rather than a null value.
    assert 'if a.has("attr"):' in _PROBE_SRC
    assert 'res["observed"] = obs["value"]' in _PROBE_SRC
    assert 'res["observed_error"] = obs["error"]' in _PROBE_SRC
    assert 'if not res["passed"] and a.has("attr"):' not in _PROBE_SRC

def test_probe_reports_completeness_accounting():
    # The probe itself says whether it reached its frame budget and how many
    # scheduled assertions produced no result row.
    assert 'out["complete"] = _frame >= _max' in _PROBE_SRC
    assert 'out["asserts_missing"] = max(0, scheduled - _results.size())' in _PROBE_SRC
    assert 'out["scheduled_asserts"] = scheduled' in _PROBE_SRC
    assert 'out["asserts_omitted"] = omitted' in _PROBE_SRC
    assert 'out["asserts_incomplete"] = incomplete_count' in _PROBE_SRC
    assert 'if str(r.get("measurement", "")) == "incomplete":' in _PROBE_SRC


@pytest.mark.parametrize("polarity,expected", [(True, True), (False, False)])
def test_scenario_row_keeps_reported_asserts_for_both_polarities(
        monkeypatch, tmp_path, polarity, expected):
    # Whatever the polarity, the reported rows keep actual/observed and the
    # row records a complete measurement.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "a", "passed": polarity, "actual": polarity, "observed": 3}],
        "nodes": {}})
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    row = r["behavior"]["scenarios"][0] if "scenarios" in r["behavior"] else None
    assert row is not None
    assert row["complete"] is True and row["asserts_missing"] == 0
    assert row["asserts"][0]["observed"] == 3
    assert row["asserts"][0]["passed"] is expected


# ── report aggregation: an unmeasurable row is HARD incomplete ─────────────
@pytest.mark.parametrize("error", [
    "parse error: Identifier \"foo\" not declared in the current scope",
    "execute failed: Invalid operands",
    "unsupported observation type 27 cannot decide assertion truth",
    "observed read failed: read execute failed: nope",
])
def test_an_unmeasurable_reported_row_is_hard_incomplete(monkeypatch, tmp_path, error):
    # The row ARRIVED, so it is not "missing", but it could not be observed. A
    # complete frame budget must not launder it into a shorter success.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "a", "node": "Bird", "expr": "velocity.y < 0", "passed": False,
         "error": error, "measurement": "incomplete"}], "nodes": {}})
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    assert r["passed"] is False
    assert any("incomplete measurement" in e for e in r["spec_errors"])
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is False and row["incomplete_asserts"] == 1
    assert r["behavior"]["all_passed"] is False


def test_a_legitimately_null_baseline_delta_stays_measured(monkeypatch, tmp_path):
    # A delta whose frame-0 read was a REAL null captured ok, and whose current
    # value moved off null, is complete -- not baseline_missing.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "d", "expr": "grid_pos changed since frame 0", "passed": True,
         "actual": {"baseline": None, "current": [3, 4]}, "measurement": "ok"}],
        "nodes": {}})
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 8, "assert": [{"node": "Bird", "attr": "grid_pos",
                              "mode": "changed"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is True
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is True and row["incomplete_asserts"] == 0
    assert row["asserts"][0]["actual"]["baseline"] is None


# ── raw-type allowlist: unsupported values are refused BEFORE conversion ───
def test_probe_gates_observable_types_before_jsonable_conversion():
    # `_jsonable` keeps a str(v) catch-all for the public dumps, so if the read
    # converted first an unsupported value (Color, Dictionary, Array, Object)
    # would become a String and pass. The gate must run on the RAW type.
    assert "func _is_observable(v) -> bool:" in _PROBE_SRC
    assert "if not _is_observable(v):" in _PROBE_SRC
    # ...and the allowlist precedes the _jsonable(v) call it guards.
    read = _PROBE_SRC[_PROBE_SRC.index("func _read_attr("):]
    read = read[:read.index("\nfunc ")]
    assert read.index("_is_observable(v)") < read.index("_jsonable(v)")
    # The preserved supported kinds stay observable.
    gate = _PROBE_SRC[_PROBE_SRC.index("func _is_observable("):]
    gate = gate[:gate.index("\nfunc ")]
    for kind in ("TYPE_NIL", "TYPE_BOOL", "TYPE_INT", "TYPE_FLOAT",
                 "TYPE_STRING", "TYPE_VECTOR2", "TYPE_VECTOR3"):
        assert kind in gate, kind
    # ...and _jsonable's str() fallback is NOT removed globally.
    assert "return str(v)" in _PROBE_SRC


def test_probe_unsupported_raw_type_is_refused_not_stringified():
    # The error name is the raw-type refusal, and it applies to a frame-0
    # baseline record too (a baseline is stored only from an ok read).
    assert "attribute is not a supported observable type" in _PROBE_SRC
    # No path converts a raw unsupported value and then accepts it: the old
    # convert-then-check (j == null) form is gone.
    assert 'var j := _jsonable(v)\n    if j == null' not in _PROBE_SRC
