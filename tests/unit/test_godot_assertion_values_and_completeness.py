"""Assertion values and measurement completeness in the Godot playtest probe.

Godot-free: the probe report is faked the same way tests/unit/test_godot_harness.py
fakes it, plus source-level checks against the embedded GDScript. Pinned down:

  * A String observation is a VALUE, not assertion truth (`_truthy`, no
    bool(String)); truth is decided ONLY for the supported kinds (Boolean,
    non-empty String, number, null).
  * Any OTHER observation type is an INCOMPLETE measurement, never a vacuous
    pass through bool(value).
  * `_jsonable(null)` is JSON null, not the four-character string "null".
  * The observation walk is bounded by depth and node count AND a finite cap on
    the COMPLETE compact JSON encoding of the returned observation (nested tags,
    Array/Dictionary structure and JSON escaping counted), so a two-node
    MiB-sized String is refused, never truncated.
  * A non-finite Float or vector/Rect2/Color component has no legitimate
    observation (official JSON would fabricate null or overflow) and is refused
    before encoding or key sorting.
  * Finite Floats are exported losslessly as tagged values in the engine's
    SCIENTIFIC form (`String.num_scientific`, shortest round-trippable), so 1.0,
    1.000000000000001 and 1e-20 stay distinct wire tokens, +0.0/-0.0 share one
    canonical token, and dictionary key sorting is total and deterministic. A
    true null stays a true null.
  * A delta decides through the TYPE-AWARE walk (`_observe_equal`) in BOTH
    polarities, never native Godot `==`/`!=`: an int baseline against a
    tagged-Float current is an engine "Invalid operands" error, not an assert.
    The walk keeps type/tag/value distinctions, treats a Dictionary's entry
    order as canonical, and recurses only over shapes `_observe_value` admitted.

    true null stays a true null.

  * A read gates the RAW observable type BEFORE converting, and converts with a
    SEPARATE bounded, type-preserving walk (`_observe_value`) -- never the public
    `_jsonable` str() catch-all. An unsafe value (Object, RID, Callable), a cycle
    or an over-large value is a tagged read failure, never str(v).
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
    # An attr read of an UNSUPPORTED raw type (a live Object, RID or Callable) is
    # a TAGGED read failure, never str(v): the row is an incomplete measurement,
    # so it cannot read as a green observation of some string. Color/Dictionary/
    # Array/Vector2i/Rect2 are safely representable now, so this control stays
    # genuinely unsafe.
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
    # Conversion now goes through a SEPARATE bounded, type-preserving walk
    # (`_observe_value`), never the public `_jsonable` str() catch-all: an unsafe
    # value (Object, RID, Callable) must still be refused, and a String
    # conversion would let it pass as a comparable string.
    assert "func _is_observable(v) -> bool:" in _PROBE_SRC
    assert "if not _is_observable(v):" in _PROBE_SRC
    # ...and the allowlist precedes the observation conversion it guards.
    read = _PROBE_SRC[_PROBE_SRC.index("func _read_attr("):]
    read = read[:read.index("\nfunc ")]
    assert read.index("_is_observable(v)") < read.index("_observe_value(v)")
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


# ── safe structured values: a separate, type-preserving bounded walk ───────
def _observe_body():
    start = _PROBE_SRC.index("func _observe_at(")
    return _PROBE_SRC[start:_PROBE_SRC.index("\nfunc ", start + 1)]


def test_probe_converts_structured_values_with_a_tagged_envelope():
    # Vector2i/Rect2/Color/Array/Dictionary are representable, and each carries
    # its TYPE in a tagged envelope. Without the tag, Vector2i(3,4) would arrive
    # as the bare list [3, 4] -- indistinguishable from an Array observation.
    body = _observe_body()
    for kind in ("TYPE_VECTOR2I", "TYPE_RECT2", "TYPE_COLOR",
                 "TYPE_ARRAY", "TYPE_DICTIONARY"):
        assert kind in body, kind
    assert '"__t": "Vector2i"' in body
    assert '"__t": "Rect2"' in body
    assert '"__t": "Color"' in body
    assert '"__t": "Array"' in body
    assert '"__t": "Dictionary"' in body
    assert '"__v"' in body
    # Primitives keep their plain JSON value -- no envelope, no change.
    assert 'TYPE_BOOL, TYPE_INT, TYPE_FLOAT, TYPE_STRING:' in body
    assert 'return {"ok": true, "value": null}' in body


def test_probe_dictionary_order_is_canonical_so_order_alone_is_not_a_change():
    # Two dictionaries with the SAME entries in a different insertion order must
    # compare equal: the pairs list is canonically sorted, so reordering alone is
    # not a value change.
    body = _observe_body()
    assert "pairs.append(" in body
    assert "pairs.sort_custom(" in body
    assert "v.keys()" in body


def test_probe_refuses_unsafe_members_cycles_and_excessive_size():
    # An unsafe member (live Object/RID/Callable), a cycle (past the depth bound)
    # or a value past the node/depth bounds is REFUSED -- never str(v) and never
    # a silent truncation that would read as a smaller, equal value.
    body = _observe_body()
    assert "budget[\"n\"] = int(budget[\"n\"]) + 1" in body
    assert "_OBS_MAX_NODES" in body and "_OBS_MAX_DEPTH" in body
    assert "observation exceeds" in body
    assert 'return {"ok": false' in body
    # The unsafe kinds are NOT in the raw-type allowlist, so such a member hits
    # the refusal branch rather than being stringified.
    gate = _PROBE_SRC[_PROBE_SRC.index("func _is_observable("):]
    gate = gate[:gate.index("\nfunc ")]
    assert "TYPE_OBJECT" not in gate
    assert "TYPE_RID" not in gate
    assert "TYPE_CALLABLE" not in gate


def test_probe_observation_never_falls_back_to_jsonable_stringification():
    # The read uses `_observe_value`, and `_observe_value` has NO str(v) path: a
    # value it cannot represent is a refusal, not a string.
    assert "func _observe_value(v) -> Dictionary:" in _PROBE_SRC
    assert "var obs := _observe_value(v)" in _PROBE_SRC
    assert 'return {"ok": true, "value": _jsonable(v)}' not in _PROBE_SRC
    body = _observe_body()
    assert "str(v)" not in body


def test_probe_jsonable_public_catchall_is_untouched():
    # The public dump conversion keeps its str(v) catch-all for the node walk;
    # only the observation path is type-preserving. Removing it globally would be
    # an unrelated behaviour change.
    assert "func _jsonable(v):" in _PROBE_SRC
    assert "return str(v)" in _PROBE_SRC


def test_a_tagged_vector2i_observation_flows_through_unchanged(monkeypatch, tmp_path):
    # A tagged structured observation reaches the report as STRUCTURE, stays a
    # complete measured row, and is never stringified by the Python side.
    tagged = {"__t": "Vector2i", "__v": [3, 4]}
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "d", "expr": "cell unchanged since frame 0", "passed": True,
         "actual": {"baseline": tagged, "current": tagged}, "measurement": "ok"}],
        "nodes": {}})
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 8, "assert": [{"node": "Bird", "attr": "cell", "mode": "unchanged"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is True
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is True and row["incomplete_asserts"] == 0
    kept = row["asserts"][0]["actual"]["current"]
    assert isinstance(kept, dict) and kept["__t"] == "Vector2i" and kept["__v"] == [3, 4]


def test_a_refused_unsafe_observation_is_hard_incomplete(monkeypatch, tmp_path):
    # An unsafe member inside an otherwise-structured value is refused end to
    # end: the row is a hard incomplete measurement, never a str(v) green.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "a", "node": "Bird", "attr": "stuff", "expr": "stuff",
         "passed": False, "actual": None, "observed": None,
         "observed_error": "attribute is not a supported observable type (24): "
                           "unsupported observation type 24",
         "measurement": "incomplete",
         "error": "observed read failed: attribute is not a supported observable "
                  "type (24): unsupported observation type 24"}], "nodes": {}})
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    assert r["passed"] is False
    assert any("incomplete measurement" in e for e in r["spec_errors"])
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is False and row["incomplete_asserts"] == 1
    r = gh._playtest_spec(tmp_path / "proj", _spec_one_assert(), 300, 120)
    assert r["passed"] is False
    assert any("incomplete measurement" in e for e in r["spec_errors"])
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is False and row["incomplete_asserts"] == 1


# ── finite byte/payload budget (B): a big String cannot ride under the node cap ─
def test_probe_caps_the_complete_compact_observation_encoding():
    # Depth/node bounds alone cannot bound the whole payload, so the finished
    # observation is serialized with the ACTUAL compact JSON encoder and its
    # UTF-8 byte length is the cap -- nested tags, Array/Dictionary structure and
    # escaping all counted for real, not estimated by a character count.
    value_body = _PROBE_SRC[_PROBE_SRC.index("func _observe_value("):]
    value_body = value_body[:value_body.index("\nfunc ")]
    assert "JSON.stringify" in value_body
    assert "to_utf8_buffer().size()" in value_body
    assert "nbytes > _OBS_MAX_BYTES" in value_body
    assert "byte cap" in value_body
    assert "const _OBS_MAX_BYTES := 65536" in _PROBE_SRC
    # A raw-String PREFLIGHT inside the walk avoids constructing an already
    # over-cap giant leaf, but the postcondition above owns the final verdict.
    body = _observe_body()
    assert "to_utf8_buffer().size()" in body
    assert "_OBS_MAX_BYTES" in body


def test_probe_byte_cap_covers_string_keys_not_only_leaves():
    # A huge String KEY would otherwise slip past a leaf-only check: dictionary
    # keys go through the SAME _observe_at walk, so they are counted too.
    body = _observe_body()
    assert "for k in v.keys():" in body
    assert "_observe_at(k, depth + 1, budget)" in body


def test_probe_byte_cap_is_refused_not_truncated_or_nulled():
    # The refusal is a tagged failure the caller turns into an incomplete
    # measurement; there is no truncation and no fabricated null.
    body = _observe_body()
    assert "observation exceeds the %d byte cap" in body
    assert "v.substr(" not in body
    assert "v.left(" not in body


# ── non-finite Float/component refusal (C) ────────────────────────────────
def test_probe_refuses_nonfinite_float_before_encoding():
    # A NaN/INF Float has no legitimate observation: the official JSON path would
    # fabricate a null (NaN) or overflow to a huge number (INF), so the probe
    # refuses it BEFORE it reaches any encoder or key sort.
    body = _observe_body()
    assert "is_finite(v)" in body
    assert "non-finite Float is not an observation" in body
    # The Float guard sits above the shared primitive arm so no Float is exported
    # without the finiteness check.
    assert body.index("if vt == TYPE_FLOAT:") < body.index("match vt:")


def test_probe_refuses_nonfinite_vector_and_color_components():
    # Vector/Rect2/Color components are checked for finiteness too: a single NaN
    # or INF component refuses the whole observation.
    body = _observe_body()
    for kind in ("Vector2", "Vector3", "Rect2", "Color"):
        assert "non-finite %s component is not an observation" % kind in body, kind


def test_probe_true_null_stays_a_true_null_observation():
    # C refuses NON-FINITE Floats only: a genuine null is still an ok null value.
    body = _observe_body()
    assert 'return {"ok": true, "value": null}' in body


# ── lossless Float export and stable key sorting (D) ──────────────────────
def test_probe_exports_finite_floats_losslessly():
    # A Float observation is the engine's SCIENTIFIC form (String.num_scientific,
    # a shortest round-trippable token), so 1.0 and 1.000000000000001 -- and tiny
    # magnitudes like 1e-20 that fixed-decimal formatting would flatten -- are
    # DISTINCT wire tokens rather than collapsing under the report's default JSON
    # precision.
    assert "func _observe_float(f: float) -> Dictionary:" in _PROBE_SRC
    assert "String.num_scientific(f)" in _PROBE_SRC
    assert "String.num(f, 17)" not in _PROBE_SRC
    float_body = _PROBE_SRC[_PROBE_SRC.index("func _observe_float("):]
    float_body = float_body[:float_body.index("\nfunc ")]
    # +0.0 and -0.0 share ONE canonical token: Godot numeric equality treats them
    # as equal, so the raw sign bit must not invent a legacy delta.
    assert "f == 0.0" in float_body
    body = _observe_body()
    assert "_observe_float(v)" in body
    # Vector2/3 and Rect2/Color components use the same lossless export.
    for call in ("_observe_float(v.x)", "_observe_float(v.position.x)",
                 "_observe_float(v.r)"):
        assert call in body, call


def test_probe_dictionary_sort_key_is_the_lossless_observation():
    # The canonical key sort key is the OBSERVED (already lossless) key, so two
    # close finite Float keys never collapse into the same sort token and the
    # order stays total and deterministic.
    body = _observe_body()
    assert "pairs.sort_custom(" in body
    sort = body[body.index("pairs.sort_custom("):]
    sort = sort[:sort.index("\n")]
    assert "JSON.stringify(a[0])" in sort and "JSON.stringify(b[0])" in sort
    # ...not the raw float, whose default stringify precision would be lossy.
    assert "JSON.stringify(a[0]." not in sort


def test_probe_public_dump_and_primitive_truth_are_unchanged_by_float_export():
    # The Float export is confined to the observation walk: the public _jsonable
    # catch-all and the legacy primitive expression truth are untouched.
    assert "func _jsonable(v):" in _PROBE_SRC
    assert "return str(v)" in _PROBE_SRC
    jsonable = _PROBE_SRC[_PROBE_SRC.index("func _jsonable(v):"):]
    jsonable = jsonable[:jsonable.index("\nfunc ")]
    assert "_observe_float" not in jsonable
    assert 'res["passed"] = _truthy(val)' in _PROBE_SRC


def test_a_tagged_float_observation_flows_through_unchanged(monkeypatch, tmp_path):
    # A tagged Float observation reaches the report as structure and is a
    # complete measured row; close finite values stay distinct.
    tagged = {"__t": "Float", "__v": "1.0000000000000000"}
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "d", "expr": "ratio unchanged since frame 0", "passed": True,
         "actual": {"baseline": tagged, "current": tagged}, "measurement": "ok"}],
        "nodes": {}})
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 8, "assert": [{"node": "Bird", "attr": "ratio", "mode": "unchanged"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is True
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is True and row["incomplete_asserts"] == 0
    assert row["asserts"][0]["actual"]["current"]["__t"] == "Float"


# ── type-aware delta equality (never native heterogeneous Godot ==/!=) ───────
def _delta_body():
    start = _PROBE_SRC.index("func _eval_delta(")
    return _PROBE_SRC[start:_PROBE_SRC.index("\nfunc ", start + 1)]


def _observe_equal_body():
    start = _PROBE_SRC.index("func _observe_equal(")
    return _PROBE_SRC[start:_PROBE_SRC.index("\nfunc ", start + 1)]


def test_probe_delta_never_uses_native_heterogeneous_equality():
    # The native Godot `==`/`!=` on `cur`/`base` is exactly what raised
    # "Invalid operands int and Dictionary" when a baseline int met a tagged-Float
    # current, stopping the whole probe. Both polarities must decide through the
    # type-aware walk instead.
    body = _delta_body()
    assert "_observe_equal(" in body
    assert "cur != base" not in body
    assert "cur == base" not in body
    assert "not same" in body


def test_probe_observe_equal_distinguishes_tag_type_and_value():
    # Two tagged observations are the same only when they share `__t` AND match on
    # `__v`: Vector2i(3,4) is not Array [3,4], a Dictionary is not an Array, and a
    # key/value/type difference under a stable key set is a real change. A scalar
    # TYPE difference (int vs tagged Float) is a change too.
    body = _observe_equal_body()
    assert 'a.get("__t"' in body and 'b.get("__t"' in body
    assert "at != bt" in body
    assert "return false" in body
    assert "a.size() != b.size()" in body  # Array: order/arity matter
    assert "ta != tb" in body              # scalar TYPE difference
    # A Dictionary recurses as its canonically sorted pair list, so insertion
    # order alone is never a change while a real entry difference is.
    assert 'if at == "Dictionary":' in body


def test_probe_observe_equal_is_bounded_to_admitted_shapes():
    # `_observe_equal` walks only shapes `_observe_value` already admitted: it has
    # no encoder, no str(v), no native heterogeneous comparison, so it cannot see a
    # cycle, an over-cap payload or a live Object.
    body = _observe_equal_body()
    assert "JSON.stringify" not in body
    assert "str(v)" not in body
    assert "is_finite" not in body


def test_int_to_float_type_change_is_measured_not_an_operand_error(monkeypatch, tmp_path):
    # The producer's original failure: a `typed` attr is int at frame 0 and tagged
    # Float at frame 3. The report must carry both tagged/scalar sides as a
    # MEASURED change (complete, one passing assert), never an engine operand error
    # that reads as an incomplete measurement or stops the probe.
    _fake_run_probe(monkeypatch, {"frames": 50, "complete": True, "asserts": [
        {"name": "d", "expr": "typed changed since frame 0", "passed": True,
         "actual": {"baseline": 1, "current": {"__t": "Float", "__v": "1"}},
         "measurement": "ok"}], "nodes": {}})
    spec = {"scenarios": [{"name": "s", "timeline": [
        {"at": 3, "assert": [{"node": "Bird", "attr": "typed", "mode": "changed"}]}]}]}
    r = gh._playtest_spec(tmp_path / "proj", spec, 300, 120)
    assert r["passed"] is True
    row = r["behavior"]["scenarios"][0]
    assert row["complete"] is True and row["incomplete_asserts"] == 0
    assert row["asserts"][0]["passed"] is True
    assert row["asserts"][0]["actual"]["baseline"] == 1
    assert row["asserts"][0]["actual"]["current"]["__t"] == "Float"


def test_scientific_staging_controls_keep_cases_and_discriminate_nonzero():
    # The scientific token cases only prove an UNCHANGED observation keeps its wire
    # token; they cannot tell whether a literal like 5e-324 even STAGED nonzero. The
    # admitted native batch proved the 5e-324 / 2.2250738585072014e-308 source
    # literals stage as 0.0, so the fixture's raw staging controls (read back at
    # frame 3) are supplied from GENUINE IEEE-754 bit patterns decoded at runtime
    # rather than echoed from those defaults. The same values populate the existing
    # subnormal/minnormal/huge delta fields before baseline capture.
    root = _HARNESS.parents[2] / "tests" / "unit" / "fixtures" / "godot_assertion_value_controls"
    gd = (root / "main.gd").read_text(encoding="utf-8")
    spec = (root / "spec.json").read_text(encoding="utf-8")
    # Initialization declarations stay; _ready replaces them with decoded values.
    assert "var subnormal := 5e-324" in gd
    assert "var minnormal := 2.2250738585072014e-308" in gd
    assert "var huge := 1.7976931348623157e308" in gd
    # The raw staging controls are supplied from GENUINE IEEE-754 binary64 bit
    # patterns decoded at runtime (the engine's supported PackedByteArray
    # .decode_double), NOT echoed from the source literals that stage as 0.0 here.
    assert "decode_double(0)" in gd
    assert "PackedByteArray([1, 0, 0, 0, 0, 0, 0, 0])" in gd   # subnormal bits
    assert "PackedByteArray([0, 0, 0, 0, 0, 0, 16, 0])" in gd  # min-normal bits
    assert "PackedByteArray([255, 255, 255, 255, 255, 255, 239, 127])" in gd  # max finite
    assert "ieee_subnormal" in gd
    # A raw control must NOT be staged from the literal token var it echoes: that
    # would replay the very zero the batch proved instead of supplying a value.
    assert "subnormal_raw = subnormal" not in gd
    assert "minnormal_raw = minnormal" not in gd
    # The staging controls are driven as real native cases, not just declared.
    for name in ("subnormal_literal_staged_nonzero",
                 "ieee_subnormal_runtime_staged_nonzero",
                 "ieee_subnormal_unchanged_token_is_nonzero",
                 "minnormal_staged_nonzero",
                 "huge_staged_finite_positive"):
        assert name in spec, name
    # ...and the original tracked cases are still present.
    for name in ("subnormal_float_unchanged", "minnormal_float_unchanged",
                 "huge_float_unchanged"):
        assert name in spec, name

