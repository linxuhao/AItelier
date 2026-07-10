"""Unit tests for the godot_compile tool (HTTP to godot-builder mocked).

Docker-free: the godot-builder call is patched. Covers the non-Godot no-op, the
pass/fail mapping, compile→playtest chaining, and the builder-unreachable
gate_skipped degrade.
"""

import json
import urllib.error
import urllib.request

from aitelier.tools.godot_compile.impl import godot_compile


def _mock_urlopen(monkeypatch, payload=None, fail=False):
    class _Resp:
        def __init__(self, p):
            self._b = json.dumps(p).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake(req, timeout=0):
        if fail:
            raise urllib.error.URLError("down")
        return _Resp(payload)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)


def _read_report(out_dir):
    return json.loads((out_dir / "compile_report.json").read_text())


def _read_playtest(out_dir):
    return json.loads((out_dir / "playtest_report.json").read_text())


def _make_godot_project(root):
    (root / "project.godot").write_text('config_version=5\n[application]\nconfig/name="t"\n')
    (root / "main.gd").write_text("extends Node\n")


def test_non_godot_is_noop(tmp_path, monkeypatch):
    # A Python-only repo must pass WITHOUT contacting the builder.
    (tmp_path / "app.py").write_text("print('hi')")
    called = {"n": 0}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = tmp_path / "out"
    r = godot_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    assert called["n"] == 0  # builder never called
    assert "not a Godot project" in _read_report(out)["summary"]
    assert "not a Godot project" in _read_playtest(out)["summary"]


def test_parse_fail_skips_playtest(tmp_path, monkeypatch):
    # When parsing fails, the play-test must be SKIPPED (no second builder call).
    _make_godot_project(tmp_path)
    calls = {"n": 0}

    class _Resp:
        def read(self): return json.dumps(
            {"passed": False, "file_count": 1, "errors": [
                {"kind": "parse", "msg": "Parse Error: bad", "file": "res://main.gd",
                 "line": 1}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake(req, timeout=0):
        calls["n"] += 1
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    out = tmp_path / "out"
    r = godot_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is False
    assert calls["n"] == 1  # only /compile was hit, NOT /playtest
    assert "Parse failed" in _read_playtest(out)["summary"]


def test_parse_pass_chains_playtest(tmp_path, monkeypatch):
    # When parsing passes, the play-test IS attempted (second builder call).
    _make_godot_project(tmp_path)
    calls = {"n": 0}

    def _fake(req, timeout=0):
        calls["n"] += 1
        payload = ({"passed": True, "errors": [], "file_count": 1, "summary": "ok"}
                   if calls["n"] == 1 else
                   {"passed": True, "frames": 180, "errors": [], "state": {}, "summary": "ran"})
        class _R:
            def read(self): return json.dumps(payload).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()
    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    out = tmp_path / "out"
    r = godot_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    assert calls["n"] == 2  # /compile then /playtest
    assert (out / "playtest_report.json").exists()


def test_parse_fail_records_errors(tmp_path, monkeypatch):
    _make_godot_project(tmp_path)
    _mock_urlopen(monkeypatch, {"passed": False, "file_count": 1, "errors": [
        {"kind": "parse", "msg": "Parse Error: Identifier not declared",
         "file": "res://main.gd", "line": 7}]})
    out = tmp_path / "out"
    r = godot_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is False
    report = _read_report(out)
    assert report["errors"][0]["file"] == "res://main.gd"
    assert report["errors"][0]["line"] == 7


def test_builder_unreachable_degrades_to_pass(tmp_path, monkeypatch):
    # Infra down must not stall the pipeline → pass with a LOUD gate_skipped flag.
    _make_godot_project(tmp_path)
    _mock_urlopen(monkeypatch, fail=True)
    out = tmp_path / "out"
    r = godot_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    report = _read_report(out)
    assert "unreachable" in report["summary"]
    assert report["gate_skipped"] is True
