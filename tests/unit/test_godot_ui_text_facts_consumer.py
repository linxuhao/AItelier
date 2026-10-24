"""Offline public-tool plumbing; these are not native/render acceptance."""
import base64
import copy
import hashlib
import io
import json
import struct
import urllib.error
import zlib
from pathlib import Path

import pytest

from aitelier.tools.godot_playtest import impl


def _png():
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + b"\xff" * 8))
            + chunk(b"IEND", b""))


def _seal(report):
    for cap in report.get("captures", []):
        facts = cap.get("ui_text_facts")
        if facts is not None:
            while facts.get("bytes") != len(json.dumps(facts).encode()):
                facts["bytes"] = len(json.dumps(facts).encode())
    summary = report.get("ui_text_snapshot")
    if summary is not None:
        def size():
            return len(json.dumps({"ui_text_snapshot": summary, "captures": [
                {"ui_text_facts": cap.get("ui_text_facts")} for cap in report["captures"]]}).encode())
        while summary.get("new_fields_bytes") != size():
            summary["new_fields_bytes"] = size()
    return report


def _report():
    png = _png()
    facts = {"version": 1, "scope": "native_ui_text_facts",
             "collection_state": "complete", "rendered_text_status": "not_observed",
             "frame": 4, "scenario": "s", "locale": "en",
             "viewport_rect": [0, 0, 2, 1], "image_size": [2, 1],
             "png_sha256": hashlib.sha256(png).hexdigest(), "png_bytes": len(png),
             "rows": [{"path": "/root/Label", "class": "Label",
                       "text": "VISIBLE_TABLE_ID", "text_kind": "resolved_logical"}],
             "hidden": {"count": 0, "reasons": {}}, "unknown_surfaces": [],
             "errors": [], "bytes_encoding": "json.dumps"}
    return _seal({"passed": True, "spec_used": False, "summary": "game passed",
                  "captures": [{"frame": 4, "scenario": "s", "file": "s_frame_4.png",
                                "png_b64": base64.b64encode(png).decode(),
                                "ui_text_facts": facts}],
                  "ui_text_snapshot": {"version": 1, "scope": "native_ui_text_facts",
                                       "rendered_text_status": "not_observed", "status": "complete",
                                       "requested_captures": 1, "returned_captures": 1,
                                       "complete": 1, "incomplete": 0, "refused": 0,
                                       "missing": 0, "bytes_encoding": "json.dumps", "errors": []}})


def _run(tmp_path, monkeypatch, response, **kwargs):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "project.godot").write_text("config_version=5\n")
    sent = []
    def post(payload):
        sent.append(copy.deepcopy(payload))
        return copy.deepcopy(response)
    monkeypatch.setattr(impl, "post_playtest", post)
    result = impl.godot_playtest(project_root=str(repo), out_dir=str(tmp_path / "out"), **kwargs)
    saved = json.loads((tmp_path / "out/playtest_report.json").read_text())
    return result, saved, sent


def test_optin_is_forwarded_and_written_png_matches_preserved_facts(tmp_path, monkeypatch):
    response = _report()
    result, saved, sent = _run(tmp_path, monkeypatch, response, ui_text_snapshot={"version": 1})
    assert sent[0]["ui_text_snapshot"] == {"version": 1}
    assert sent[0]["captures"] == impl._DPE_CAPTURES
    cap = saved["captures"][0]
    assert cap["ui_text_facts"] == response["captures"][0]["ui_text_facts"]
    assert saved["ui_text_snapshot"] == response["ui_text_snapshot"]
    png = (tmp_path / "out" / cap["file"]).read_bytes()
    assert png == _png() and hashlib.sha256(png).hexdigest() == cap["ui_text_facts"]["png_sha256"]
    assert result == {"written": "playtest_report.json", "passed": True}
    assert saved["ui_text_snapshot_host"]["status"] == "verified"
    assert saved["ui_text_snapshot_host"]["rendered_text_status"] == "not_observed"
    assert "reviewed_clear" not in json.dumps(saved).lower()


def test_omission_preserves_legacy_request_and_report(tmp_path, monkeypatch):
    response = {"passed": True, "summary": "legacy", "captures": [{
        "frame": 4, "file": "legacy.png", "png_b64": base64.b64encode(_png()).decode(),
        "text_observation": {"complete": False, "refused": ["legacy cap"]}}]}
    result, saved, sent = _run(tmp_path, monkeypatch, response)
    assert "ui_text_snapshot" not in sent[0]
    assert "ui_text_snapshot_host" not in saved and "ui_text_snapshot_host" not in result
    assert saved["captures"][0]["text_observation"] == response["captures"][0]["text_observation"]
    assert (tmp_path / "out/frames/legacy.png").read_bytes() == _png()


@pytest.mark.parametrize("option", [None, True, {}, {"version": True}, {"version": 2},
                                   {"version": 1, "extra": True}])
def test_malformed_option_refuses_before_provider_or_output(tmp_path, monkeypatch, option):
    monkeypatch.setattr(impl, "post_playtest", lambda *a, **k: pytest.fail("provider called"))
    with pytest.raises(ValueError, match="ui_text_snapshot"):
        impl.godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path / "out"),
                            ui_text_snapshot=option)
    assert not (tmp_path / "out").exists()


def test_feedback_cannot_silently_ignore_optin(tmp_path, monkeypatch):
    monkeypatch.setattr(impl, "post_playtest", lambda *a, **k: pytest.fail("provider called"))
    with pytest.raises(ValueError, match="provisional_round_feedback"):
        impl.godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path / "out"),
                            ui_text_snapshot={"version": 1}, purpose="provisional_round_feedback")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("field,value,code", [
    ("png_sha256", "0" * 64, "png_identity_mismatch"),
    ("png_bytes", 1, "png_identity_mismatch"),
    ("image_size", [1, 2], "png_dimensions_mismatch"),
    ("frame", 5, "frame_mismatch"),
    ("scenario", "other", "scenario_mismatch"),
])
def test_wrong_pairing_is_explicit_without_rewriting_game_truth(tmp_path, monkeypatch, field, value, code):
    response = _report()
    response["captures"][0]["ui_text_facts"][field] = value
    _seal(response)
    result, saved, _ = _run(tmp_path, monkeypatch, response, ui_text_snapshot={"version": 1})
    assert result["passed"] is True
    assert saved["captures"][0]["ui_text_facts"] == response["captures"][0]["ui_text_facts"]
    host = saved["ui_text_snapshot_host"]
    assert host["status"] == "incomplete" and any(e["code"] == code for e in host["errors"])


def test_hash_checks_bytes_actually_written_not_only_incoming_blob(tmp_path, monkeypatch):
    original = Path.write_bytes
    def altered(path, payload):
        return original(path, payload + b"altered") if path.suffix == ".png" else original(path, payload)
    monkeypatch.setattr(Path, "write_bytes", altered)
    result, saved, _ = _run(tmp_path, monkeypatch, _report(), ui_text_snapshot={"version": 1})
    assert result["passed"] is True and saved["ui_text_snapshot_host"]["status"] == "incomplete"
    assert any(e["code"] == "png_identity_mismatch" for e in saved["ui_text_snapshot_host"]["errors"])


@pytest.mark.parametrize("blob", [None, "%%%"])
def test_missing_or_corrupt_png_cannot_borrow_stale_file(tmp_path, monkeypatch, blob):
    response = _report()
    response["captures"][0]["png_b64"] = blob
    frames = tmp_path / "out/frames"
    frames.mkdir(parents=True)
    (frames / "s_frame_4.png").write_bytes(_png())
    result, saved, _ = _run(tmp_path, monkeypatch, response, ui_text_snapshot={"version": 1})
    assert result["passed"] is True and saved["ui_text_snapshot_host"]["status"] == "incomplete"
    assert saved["ui_text_snapshot_host"]["verified"] == 0


@pytest.mark.parametrize("case", ["facts", "summary", "captures", "count", "wire"])
def test_missing_and_inconsistent_accounting_stays_unknown(tmp_path, monkeypatch, case):
    response = _report()
    if case == "facts":
        del response["captures"][0]["ui_text_facts"]
    elif case == "summary":
        del response["ui_text_snapshot"]
    elif case == "captures":
        response["captures"] = []
    elif case == "count":
        response["ui_text_snapshot"]["requested_captures"] = 2
    else:
        response["ui_text_snapshot"]["new_fields_bytes"] = 33554433
    if case != "wire":
        _seal(response)
    result, saved, _ = _run(tmp_path, monkeypatch, response, ui_text_snapshot={"version": 1})
    assert result["passed"] is True and saved["ui_text_snapshot_host"]["status"] == "incomplete"
    assert saved["ui_text_snapshot_host"]["error_count"] > 0


def test_provider_refusal_is_retained_without_claiming_text_success(tmp_path, monkeypatch):
    response = _report()
    facts = response["captures"][0]["ui_text_facts"]
    facts.update(collection_state="refused", rows=[], errors=["snapshot byte limit"])
    response["ui_text_snapshot"].update(status="refused", complete=0, refused=1,
                                        errors=["snapshot byte limit"])
    _seal(response)
    result, saved, _ = _run(tmp_path, monkeypatch, response, ui_text_snapshot={"version": 1})
    assert result["passed"] is True and saved["ui_text_snapshot_host"]["status"] == "incomplete"
    assert saved["captures"][0]["ui_text_facts"]["errors"] == ["snapshot byte limit"]
    assert saved["ui_text_snapshot"]["status"] == "refused"


def test_http_snapshot_refusal_is_not_unreachable_skip(monkeypatch):
    payload = {"error": "ui_text_snapshot cannot fit minimum refusal envelopes"}
    def refused(*a, **k):
        raise urllib.error.HTTPError("http://builder/playtest", 400, "Bad Request", {},
                                     io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(impl.urllib.request, "urlopen", refused)
    result = impl.post_playtest({"project_dir": "/repo", "ui_text_snapshot": {"version": 1}})
    assert result["passed"] is False and result["gate_refused"] is True
    assert not result.get("gate_skipped") and result["builder_refusal"]["response"] == payload


@pytest.mark.parametrize("field,value", [("rows", None), ("hidden", []),
                                       ("unknown_surfaces", None), ("errors", None)])
def test_missing_native_fact_fields_are_not_verified(tmp_path, monkeypatch, field, value):
    response = _report()
    response["captures"][0]["ui_text_facts"][field] = value
    _seal(response)
    _, saved, _ = _run(tmp_path, monkeypatch, response, ui_text_snapshot={"version": 1})
    assert saved["ui_text_snapshot_host"]["status"] == "incomplete"
    assert any(e["code"] == "facts_fields_missing" for e in saved["ui_text_snapshot_host"]["errors"])


def test_write_failure_preserves_observation_and_game_result(tmp_path, monkeypatch):
    response = _report()
    original = Path.write_bytes
    def refused(path, payload):
        if path.suffix == ".png":
            raise OSError("fixture write refused")
        return original(path, payload)
    monkeypatch.setattr(Path, "write_bytes", refused)
    result, saved, _ = _run(tmp_path, monkeypatch, response, ui_text_snapshot={"version": 1})
    assert result["passed"] is True
    assert saved["captures"][0]["ui_text_facts"] == response["captures"][0]["ui_text_facts"]
    assert saved["ui_text_snapshot_host"]["status"] == "incomplete"
    assert any(e["code"] == "png_materialization_failed" for e in saved["ui_text_snapshot_host"]["errors"])


def test_host_receipt_is_bounded_separately_from_untouched_wire(tmp_path, monkeypatch):
    response = _report()
    response["captures"] = [copy.deepcopy(response["captures"][0]) for _ in range(100)]
    response["ui_text_snapshot"].update(requested_captures=100, returned_captures=100, complete=100)
    _seal(response)
    result, saved, _ = _run(tmp_path, monkeypatch, response, ui_text_snapshot={"version": 1})
    host = saved["ui_text_snapshot_host"]
    assert result == {"written": "playtest_report.json", "passed": True}
    assert host["status"] == "incomplete" and host["error_count"] >= 99
    assert len(host["errors"]) == 32 and host["errors_omitted"] == host["error_count"] - 32
    size = len(json.dumps({"ui_text_snapshot_host": host}, indent=2).encode())
    assert host["local_projection_bytes"] == size <= 8192
    assert saved["ui_text_snapshot"] == response["ui_text_snapshot"]
