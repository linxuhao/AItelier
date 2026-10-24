"""Native facts and retained PNG evidence; no automatic rendered-text verdict.

Set AITELIER_REQUIRE_NATIVE=1 for acceptance. Native tests require the official
Godot binary and software-rendering/Xvfb image. Full PNG review remains an
independent empirical gate: passing these tests does not certify pixel absence.
"""
import base64
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import struct
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[2] / "docker/godot/godot_harness.py"
_MODULE = importlib.util.spec_from_file_location("godot_ui_text_facts_harness", _SOURCE)
gh = importlib.util.module_from_spec(_MODULE)
_MODULE.loader.exec_module(gh)
_FIXTURE = Path(__file__).parent / "fixtures/godot_ui_text_facts"
_GODOT = os.environ.get("GODOT_BIN") or shutil.which("godot")
_OBJECT_LIMIT = 1048576
_TOTAL_LIMIT = 33554432


def _wire(value):
    return len(json.dumps(value).encode())


def _new_fields(report):
    return {"ui_text_snapshot": report["ui_text_snapshot"], "captures": [
        {"ui_text_facts": capture["ui_text_facts"]} for capture in report["captures"]]}


@pytest.fixture
def native(tmp_path, monkeypatch):
    if not _GODOT or not shutil.which(str(_GODOT)):
        if os.environ.get("AITELIER_REQUIRE_NATIVE") == "1":
            pytest.fail("native UI facts require a real official Godot binary")
        pytest.skip("Godot unavailable; this is not native acceptance")
    if not shutil.which("xvfb-run"):
        pytest.fail("native PNG proof requires Xvfb/software rendering")
    monkeypatch.setattr(gh, "GODOT_BIN", str(_GODOT))
    project = tmp_path / "project"
    shutil.copytree(_FIXTURE, project)
    actual_run = gh._run
    logs = []

    def record(*args, **kwargs):
        try:
            result = actual_run(*args, **kwargs)
            logs.append({"args": args[0], "render": kwargs.get("render", False),
                         "stdout": result.stdout, "stderr": result.stderr,
                         "returncode": result.returncode})
            return result
        except BaseException as error:
            logs.append({"args": args[0], "exception": repr(error),
                         "stdout": str(getattr(error, "output", "")),
                         "stderr": str(getattr(error, "stderr", ""))})
            raise
        finally:
            (tmp_path / "native-raw.json").write_text(json.dumps(logs, indent=2))

    monkeypatch.setattr(gh, "_run", record)

    def run(case, requested=True):
        scene = (project / "main.tscn").read_text()
        # Only the disposable test project is changed, never the caller's game.
        scene = scene[:scene.index("case_name =")] + "case_name = " + json.dumps(case) + "\n"
        (project / "main.tscn").write_text(scene)
        spec = {"frames": 8, "scenarios": [{"name": case, "timeline": [
            {"at": 4, "assert": {"Fixture.ready_done": True, "Fixture.sentinel": 73}},
        ]}]}
        kwargs = {"ui_text_snapshot": {"version": 1}} if requested else {}
        (tmp_path / (case + "-request.json")).write_text(json.dumps({
            "frames": 8, "captures": 1, "timeout": 120, "spec": spec, **kwargs,
        }, indent=2))
        report = gh.playtest_project(str(project), frames=8, captures=1,
                                     spec=spec, timeout=120, **kwargs)
        (tmp_path / (case + "-report.json")).write_text(json.dumps(report, indent=2))
        assert report.get("no_project") is not True and report.get("gate_skipped") is not True
        assert report["render_mode"] == "render", report
        assert report["errors"] == [] and report["spec_errors"] == [], report
        row = report["behavior"]["scenarios"][0]
        assert row["ran"] is True and row["timed_out"] is False
        assert row["complete"] is True and row["pressed"] is False
        assert row["input_dead"] is False and row["comparison_error"] is None
        assert all(a["passed"] is True for a in row["asserts"])
        assert report["timing"]["scenarios"][0]["frames_stepped"] == 34
        assert len(report["captures"]) == 1
        capture = report["captures"][0]
        assert capture["scenario"] == case and capture["frame"] == 4
        raw = base64.b64decode(capture["png_b64"], validate=True)
        assert raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) > 200
        assert struct.unpack(">II", raw[16:24]) == (640, 360)
        (tmp_path / (case + ".png")).write_bytes(raw)
        # This receipt names only empirical evidence still needing blind review.
        (tmp_path / (case + "-image-review-required.json")).write_text(json.dumps({
            "png": case + ".png", "png_sha256": hashlib.sha256(raw).hexdigest(),
            "image_size": [640, 360], "frame": 4, "scenario": case,
            "viewport_rect": [0, 0, 640, 360],
            "locale": report["state"]["/root/Fixture"]["vars"]["measured_locale"],
            "rendered_review": "not_observed", "automatic_clear": False,
        }, indent=2))
        if not requested:
            assert "ui_text_snapshot" not in report and "ui_text_facts" not in capture
            return report, capture, raw
        facts = capture["ui_text_facts"]
        assert facts["version"] == 1 and facts["scope"] == "native_ui_text_facts"
        assert facts["rendered_text_status"] == "not_observed"
        assert facts["frame"] == 4 and facts["scenario"] == case
        assert facts["locale"] == report["state"]["/root/Fixture"]["vars"]["measured_locale"] == "en"
        assert facts["image_size"] == [640, 360]
        assert facts["png_sha256"] == hashlib.sha256(raw).hexdigest()
        assert facts["png_bytes"] == len(raw)
        assert facts["viewport_rect"] == [0, 0, 640, 360]
        assert _wire(facts) == facts["bytes"] <= _OBJECT_LIMIT
        assert facts["bytes_encoding"] == "json.dumps"
        assert "all_render_complete" not in facts
        summary = report["ui_text_snapshot"]
        assert summary["requested_captures"] == summary["returned_captures"] == 1
        assert summary["missing"] == 0
        assert summary["complete"] + summary["incomplete"] + summary["refused"] == 1
        assert summary["rendered_text_status"] == "not_observed"
        assert _wire(_new_fields(report)) <= _TOTAL_LIMIT
        return report, capture, raw

    return run


def _texts(facts):
    return [row["text"] for row in facts["rows"]]


def test_native_useful_translation_positive_and_legacy_default(native):
    report, capture, _ = native("positive")
    facts = capture["ui_text_facts"]
    assert facts["collection_state"] == "complete"
    assert facts["rows"] and "Start" in _texts(facts)
    row = next(row for row in facts["rows"] if row["text"] == "Start")
    assert row["class"] == "Label" and row["text_kind"] == "resolved_logical"
    assert row["visible_in_tree"] is True
    assert row["visibility"] in ("possibly_visible", "unknown")
    assert row["geometry"]["screen_rect"][2:] == [550, 48]
    assert report["ui_text_snapshot"]["complete"] == 1
    assert report["passed"] is True
    legacy, _, _ = native("positive", requested=False)
    assert legacy["passed"] is True


@pytest.mark.parametrize("case,expected", [
    ("visible_token", "VISIBLE_TABLE_ID"),
    ("missing_translation", "missing_catalog.key"),
])
def test_native_real_visible_mutations_retain_png_and_unexempted_facts(native, case, expected):
    _, capture, _ = native(case)
    facts = capture["ui_text_facts"]
    assert facts["collection_state"] == "complete" and expected in _texts(facts)
    assert any(row["class"] in ("Label", "Button") and row["text"] == expected for row in facts["rows"])


def test_native_hidden_token_is_counted_without_copying_hidden_string(native):
    _, capture, _ = native("hidden")
    facts = capture["ui_text_facts"]
    assert "Start" in _texts(facts) and "VISIBLE_TABLE_ID" not in _texts(facts)
    assert facts["hidden"]["count"] >= 1 and facts["hidden"]["reasons"]
    assert "VISIBLE_TABLE_ID" not in json.dumps(facts)


@pytest.mark.parametrize("case", ["custom_draw", "server_after_free", "scripted_label", "draw_signal", "bitmap", "rich_effect"])
def test_native_cannot_certify_custom_server_script_image_or_effect_pixels(native, case):
    report, capture, _ = native(case)
    facts = capture["ui_text_facts"]
    assert facts["rendered_text_status"] == "not_observed"
    assert facts["limitations"] or facts["unknown_surfaces"]
    if case in ("custom_draw", "server_after_free", "bitmap"):
        assert "VISIBLE_TABLE_ID" not in _texts(facts)
    if case == "server_after_free":
        assert not any("FreedDrawer" in path for path in report["state"])
    if case == "scripted_label":
        assert any(row["class"] == "Label" and row["limitations"] for row in facts["rows"])
    if case == "draw_signal":
        assert any("draw" in json.dumps(row["limitations"]).lower() for row in facts["rows"])
    if case == "rich_effect":
        row = next(row for row in facts["rows"] if row["class"] == "RichTextLabel")
        assert row["text_kind"] == "parsed" and row["limitations"]
    # No native-empty assertion is a rendered-negative result. The retained
    # original image must be read independently before composing a verdict.


@pytest.mark.parametrize("case", ["rich_reveal", "rich_scroll"])
def test_native_parsed_rich_text_is_separate_from_reveal_and_scroll(native, case):
    _, capture, _ = native(case)
    facts = capture["ui_text_facts"]
    rich = next(row for row in facts["rows"] if row["class"] == "RichTextLabel")
    assert rich["text_kind"] == "parsed" and "VISIBLE_TABLE_ID" in rich["text"]
    assert "[b]" not in rich["text"]
    assert rich["limitations"] and rich["state"]
    if case == "rich_reveal":
        assert any(row["text"] == "VISIBLE_TABLE_IDsuffix" for row in facts["rows"] if row["class"] == "Label")


@pytest.mark.parametrize("case", ["option_closed", "option_open"])
def test_native_selected_option_and_actual_internal_popup(native, case):
    _, capture, _ = native(case)
    facts = capture["ui_text_facts"]
    selected = next(row for row in facts["rows"] if row["class"] == "OptionButton")
    assert selected["text"] == "Start" and selected["state"]
    if case == "option_closed":
        assert "VISIBLE_TABLE_ID" not in _texts(facts)
    else:
        popup_rows = [row for row in facts["rows"] if row["class"] == "PopupMenu"]
        assert (any(row["text"] == "VISIBLE_TABLE_ID" for row in popup_rows)
                or any("popup" in json.dumps(item).lower() or "window" in json.dumps(item).lower()
                       for item in facts["unknown_surfaces"]))


@pytest.mark.parametrize("case,text,placeholder", [
    ("line_edit", "Typed literal", False),
    ("line_placeholder", "Start", True),
])
def test_native_plain_line_edit_and_translated_placeholder(native, case, text, placeholder):
    _, capture, _ = native(case)
    facts = capture["ui_text_facts"]
    row = next(row for row in facts["rows"] if row["class"] == "LineEdit")
    assert row["text"] == text and row["text_kind"] == "resolved_logical"
    assert row["state"] == {"placeholder": placeholder, "secret": False, "ime": False}
    assert facts["collection_state"] == "complete"


@pytest.mark.parametrize("case", ["ambiguous", "secret"])
def test_native_ambiguous_clip_and_secret_do_not_create_visible_text_claim(native, case):
    _, capture, _ = native(case)
    facts = capture["ui_text_facts"]
    assert facts["rendered_text_status"] == "not_observed"
    if case == "secret":
        assert "VISIBLE_TABLE_ID" not in _texts(facts)
        assert "secret" in json.dumps({"rows": facts["rows"], "unknown": facts["unknown_surfaces"]}).lower()
    else:
        assert facts["rows"] or facts["unknown_surfaces"]
        assert any(row["limitations"] or row["geometry"]["clip_rect"] is not None for row in facts["rows"])


@pytest.mark.parametrize("case", ["oversize", "nodes", "depth"])
def test_native_actual_collection_limits_refuse_whole_snapshot(native, case):
    report, capture, _ = native(case)
    facts = capture["ui_text_facts"]
    assert facts["collection_state"] == "refused"
    assert facts["rows"] == [] and facts["errors"]
    assert report["ui_text_snapshot"]["refused"] == 1
    assert _wire(facts) <= _OBJECT_LIMIT
    if case == "oversize":
        legacy = capture["text_observation"]
        assert legacy["byte_capped"] is True and legacy["controls"] == []
        assert _wire(legacy) <= 65536
        # This new refusal is not the historical1950 refusal or its size.


@pytest.mark.parametrize("bad", [None, {}, {"version": 2}, {"version": True}, {"version": 1, "extra": 1}, []])
def test_optin_refuses_malformed_before_copy_or_native_effects(monkeypatch, tmp_path, bad):
    project = tmp_path / "project"
    shutil.copytree(_FIXTURE, project)
    def forbidden(*args, **kwargs):
        pytest.fail("malformed opt-in reached native/copy effects")
    monkeypatch.setattr(gh, "_copy_project", forbidden)
    monkeypatch.setattr(gh, "_run", forbidden)
    with pytest.raises(ValueError):
        gh.playtest_project(str(project), captures=1, ui_text_snapshot=bad)


def test_preflight_capacity_refuses_before_any_execution(monkeypatch, tmp_path):
    # Genuine minimum envelopes at the production32MiB ceiling; no cap override.
    with pytest.raises(ValueError):
        gh._ui_text_check_capacity(_TOTAL_LIMIT)


def test_missing_capture_and_missing_facts_never_become_observed():
    missing = gh._seal_ui_text_report({"captures": []}, 1)
    assert missing["ui_text_snapshot"]["missing"] == 1
    assert missing["ui_text_snapshot"]["status"] != "complete"
    absent = gh._seal_ui_text_report({"captures": [{"frame": 4, "file": "missing.png"}]}, 1)
    assert absent["ui_text_snapshot"]["returned_captures"] == 1
    assert absent["ui_text_snapshot"]["refused"] == 1
    assert absent["captures"][0]["ui_text_facts"]["collection_state"] == "refused"
    assert absent["captures"][0]["ui_text_facts"]["errors"]


def test_actual_native_png_loss_is_not_complete_native_fact(native, tmp_path):
    _, capture, raw = native("positive")
    candidate = copy.deepcopy(capture)
    candidate["file"] = "paired.png"
    capdir = tmp_path / "attach"
    capdir.mkdir()
    (capdir / "paired.png").write_bytes(raw)
    paired = gh._attach_pngs([copy.deepcopy(candidate)], capdir)[0]
    assert paired["ui_text_facts"]["png_sha256"] == hashlib.sha256(raw).hexdigest()
    (capdir / "paired.png").unlink()
    lost = gh._attach_pngs([copy.deepcopy(candidate)], capdir)[0]
    sealed = gh._seal_ui_text_report({"captures": [lost]}, 1)
    assert sealed["ui_text_snapshot"]["complete"] == 0
    assert sealed["captures"][0]["ui_text_facts"]["collection_state"] != "complete"
    assert sealed["captures"][0]["ui_text_facts"]["errors"]


def _fact_at_bytes(prototype, target):
    """Literal final JSON byte target, including escapes and the bytes field."""
    fact = copy.deepcopy(prototype)
    fact["rows"][0]["text"] = "中\"\\\n"
    fact["bytes"] = target
    padding = target - _wire(fact)
    assert padding >= 0
    fact["rows"][0]["text"] += "x" * padding
    assert _wire(fact) == target
    return fact


def test_actual_native_fact_final_object_limit_is_inclusive_and_not_truncated(native):
    report, capture, _ = native("positive")
    for target in (_OBJECT_LIMIT, _OBJECT_LIMIT + 1):
        candidate = copy.deepcopy(report)
        candidate["captures"][0]["ui_text_facts"] = _fact_at_bytes(capture["ui_text_facts"], target)
        sealed = gh._seal_ui_text_report(candidate, 1)
        fact = sealed["captures"][0]["ui_text_facts"]
        assert _wire(fact) == fact["bytes"] <= _OBJECT_LIMIT
        assert sealed["passed"] is report["passed"]
        if target == _OBJECT_LIMIT:
            assert fact["collection_state"] == "complete"
            assert fact["rows"][0]["text"].startswith("中\"\\\n")
            assert len(fact["rows"][0]["text"]) > 1000000
        else:
            assert fact["collection_state"] == "refused"
            assert fact["rows"] == [] and fact["errors"]
            assert sealed["ui_text_snapshot"]["refused"] == 1


def test_actual_final_new_field_total_is_inclusive_and_refuses_every_object(native):
    report, capture, _ = native("positive")
    # Synthetic transport fault: all objects start with genuinely native facts
    # and PNG. These copies are NOT 32 new actual native capture observations.
    candidate = copy.deepcopy(report)
    candidate["captures"] = [copy.deepcopy(capture) for _ in range(32)]
    for entry in candidate["captures"][:-1]:
        entry["ui_text_facts"] = _fact_at_bytes(capture["ui_text_facts"], _OBJECT_LIMIT)
    candidate = gh._seal_ui_text_report(candidate, 32)
    assert candidate["ui_text_snapshot"]["complete"] == 32
    # Freeze the expected eight-digit total before calculating exact padding.
    candidate["ui_text_snapshot"]["new_fields_bytes"] = _TOTAL_LIMIT
    last = candidate["captures"][-1]["ui_text_facts"]
    delta = _TOTAL_LIMIT - _wire(_new_fields(candidate))
    assert delta > 0
    candidate["captures"][-1]["ui_text_facts"] = _fact_at_bytes(last, _wire(last) + delta)
    # The bytes field grows from four to seven digits, already included by
    # _fact_at_bytes; compensate only actual final serialized length.
    delta = _TOTAL_LIMIT - _wire(_new_fields(candidate))
    if delta:
        last = candidate["captures"][-1]["ui_text_facts"]
        candidate["captures"][-1]["ui_text_facts"] = _fact_at_bytes(last, _wire(last) + delta)
    assert _wire(_new_fields(candidate)) == _TOTAL_LIMIT
    assert all(_wire(entry["ui_text_facts"]) <= _OBJECT_LIMIT for entry in candidate["captures"])
    exact = gh._seal_ui_text_report(candidate, 32)
    assert exact["ui_text_snapshot"]["complete"] == 32
    assert exact["ui_text_snapshot"]["new_fields_bytes"] == _wire(_new_fields(exact)) == _TOTAL_LIMIT
    oversized = copy.deepcopy(exact)
    last = oversized["captures"][-1]["ui_text_facts"]
    last["rows"][0]["text"] += "x"
    last["bytes"] = _wire(last)
    assert _wire(_new_fields(oversized)) == _TOTAL_LIMIT + 1
    refused = gh._seal_ui_text_report(oversized, 32)
    summary = refused["ui_text_snapshot"]
    assert summary["returned_captures"] == summary["refused"] == 32
    assert summary["complete"] == summary["missing"] == 0
    assert summary["status"] == "refused"
    assert len(refused["captures"]) == 32
    assert all(entry["ui_text_facts"]["collection_state"] == "refused" for entry in refused["captures"])
    assert all(entry["ui_text_facts"]["rows"] == [] for entry in refused["captures"])
    assert summary["new_fields_bytes"] == _wire(_new_fields(refused)) <= _TOTAL_LIMIT


def test_native_distinct_images_cannot_be_relabelled_as_one_capture(native, tmp_path):
    _, safe, safe_raw = native("positive")
    _, visible, visible_raw = native("visible_token")
    safe_hash, visible_hash = hashlib.sha256(safe_raw).hexdigest(), hashlib.sha256(visible_raw).hexdigest()
    assert safe_hash != visible_hash
    assert safe["ui_text_facts"]["png_sha256"] == safe_hash
    assert visible["ui_text_facts"]["png_sha256"] == visible_hash
    assert safe["ui_text_facts"]["scenario"] != visible["ui_text_facts"]["scenario"]
    wrong = copy.deepcopy(visible)
    wrong["ui_text_facts"] = copy.deepcopy(safe["ui_text_facts"])
    # Actual wrong-pair wire for the host validator's companion test/review.
    # This author does not invent a renderer reviewer or a consumer verdict.
    (tmp_path / "wrong-pair-actual-wire.json").write_text(json.dumps(wrong, indent=2))
    assert hashlib.sha256(base64.b64decode(wrong["png_b64"])).hexdigest() != wrong["ui_text_facts"]["png_sha256"]


def test_native_hidden_bulk_is_useful_while_same_capture_legacy_refuses(native, tmp_path):
    report, capture, _ = native("hidden_bulk")
    facts = capture["ui_text_facts"]
    assert facts["collection_state"] == "complete"
    assert len(facts["rows"]) == 1 and facts["rows"][0]["text"] == "Start"
    assert facts["rows"][0]["class"] == "Label"
    assert facts["hidden"]["count"] >= 96 and facts["hidden"]["reasons"]
    assert "HIDDEN_BULK_" not in json.dumps(facts)
    assert _wire(facts) == facts["bytes"] <= _OBJECT_LIMIT
    legacy = capture["text_observation"]
    assert legacy["complete"] is False and legacy["byte_capped"] is True
    assert legacy["controls"] == [] and _wire(legacy) <= 65536
    assert report["ui_text_snapshot"]["complete"] == 1
    # This receipt records emitted wire state/size, not discarded payload size
    # or any explanation of the historical1950 capture's refusal.
    (tmp_path / "hidden-bulk-measurement.json").write_text(json.dumps({
        "fixture_hidden_labels": 96, "fixture_hidden_chars_each": 192,
        "ui_collection_state": facts["collection_state"],
        "ui_emitted_bytes": _wire(facts), "ui_reported_bytes": facts["bytes"],
        "legacy_complete": legacy["complete"], "legacy_byte_capped": legacy["byte_capped"],
        "legacy_emitted_bytes": _wire(legacy), "legacy_reported_bytes": legacy.get("bytes"),
        "legacy_refused_bytes": legacy.get("refused_bytes"),
        "historical1950_cause": "not_established",
    }, indent=2))
