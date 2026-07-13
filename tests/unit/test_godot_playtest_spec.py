"""Tests for the godot_playtest tool's spec plumbing (aitelier/tools/godot_playtest).

The tool reads an authored playtest_spec.yaml from the repo and forwards it to
the godot-builder sidecar. These tests cover spec reading (present / absent /
malformed / no-scenarios) and that a present spec is included in the sidecar
payload — all without a live builder (urlopen is mocked).
"""

import json

from aitelier.tools.godot_playtest.impl import godot_playtest, _read_spec


def test_read_spec_absent_is_none(tmp_path):
    assert _read_spec(tmp_path) is None


def test_read_spec_valid(tmp_path):
    (tmp_path / "playtest_spec.yaml").write_text(
        "scene: res://main.tscn\nscenarios:\n  - name: s\n    timeline: []\n")
    spec = _read_spec(tmp_path)
    assert spec and spec["scenarios"][0]["name"] == "s"


def test_read_spec_no_scenarios_is_none(tmp_path):
    # A spec with no scenarios can't drive anything → treat as absent (legacy path).
    (tmp_path / "playtest_spec.yaml").write_text("scene: res://main.tscn\n")
    assert _read_spec(tmp_path) is None


def test_read_spec_malformed_is_none(tmp_path):
    # A broken spec must never crash the gate.
    (tmp_path / "playtest_spec.yaml").write_text("::: not yaml :::\n[unterminated")
    assert _read_spec(tmp_path) is None


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def test_playtest_forwards_spec_to_builder(tmp_path, monkeypatch):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    (tmp_path / "playtest_spec.yaml").write_text(
        "scenarios:\n  - name: flap\n    timeline:\n      - {at: 8, assert: []}\n")
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp({"passed": True, "spec_used": True, "frames": 1,
                          "errors": [], "state": {}, "summary": "ok",
                          "behavior": {"all_passed": True, "scenarios": []}})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    out = godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert "spec" in captured["body"]
    assert captured["body"]["spec"]["scenarios"][0]["name"] == "flap"
    assert out["passed"] is True
    # The report was persisted for the reviewer.
    assert (tmp_path / "playtest_report.json").is_file()


def test_playtest_no_spec_omits_spec_key(tmp_path, monkeypatch):
    (tmp_path / "project.godot").write_text("config_version=5\n")
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp({"passed": True, "spec_used": False, "frames": 1,
                          "errors": [], "state": {}, "summary": "ok", "behavior": None})

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen)
    godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert "spec" not in captured["body"]


def test_playtest_non_godot_skips(tmp_path):
    # No project.godot → not a game → pass without touching the builder.
    out = godot_playtest(project_root=str(tmp_path), out_dir=str(tmp_path))
    assert out["passed"] is True
