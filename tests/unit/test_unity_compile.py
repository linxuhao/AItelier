"""Unit tests for the unity_compile tool (HTTP to unity-builder mocked).

Docker-free: the unity-builder call is patched. Covers the no-C#-project no-op,
the pass/fail mapping, and the builder-unreachable liveness degrade.
"""

import json
import urllib.error
import urllib.request

from aitelier.tools.unity_compile.impl import unity_compile


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


def test_no_cs_files_is_noop(tmp_path, monkeypatch):
    # A Python-only repo must pass WITHOUT contacting the builder — for either the
    # compile call OR the chained play-test.
    (tmp_path / "app.py").write_text("print('hi')")
    called = {"n": 0}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = tmp_path / "out"
    r = unity_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    assert called["n"] == 0  # builder never called
    assert "No C#" in _read_report(out)["summary"]
    # play-test report still written (skipped), so the reviewer always finds it
    assert "not a Unity project" in _read_playtest(out)["summary"]


def test_compile_fail_skips_playtest_without_running_editor(tmp_path, monkeypatch):
    # When compile fails, the play-test must be SKIPPED (no second builder call) —
    # running the editor on non-compiling code is pointless.
    (tmp_path / "Assets").mkdir()
    (tmp_path / "Assets" / "Bad.cs").write_text("// bad")
    calls = {"n": 0}

    class _Resp:
        def read(self): return json.dumps(
            {"passed": False, "file_count": 1, "errors": [
                {"file": "Assets/Bad.cs", "line": 1, "col": 1,
                 "code": "CS1002", "message": "; expected"}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake(req, timeout=0):
        calls["n"] += 1
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    out = tmp_path / "out"
    r = unity_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is False
    assert calls["n"] == 1  # only /compile was hit, NOT /playtest
    assert "Compile failed" in _read_playtest(out)["summary"]


def test_compile_pass_chains_playtest(tmp_path, monkeypatch):
    # When compile passes on a C# project, the play-test IS attempted (second
    # builder call) and its report is written.
    (tmp_path / "Assets").mkdir()
    (tmp_path / "Assets" / "Player.cs").write_text("// ok")
    calls = {"n": 0}

    def _fake(req, timeout=0):
        calls["n"] += 1
        class _R:
            def read(self): return json.dumps(
                {"passed": True, "total": 1, "passed_count": 1, "failed_count": 0,
                 "failures": [], "errors": [], "summary": "ok"}).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()
    monkeypatch.setattr(urllib.request, "urlopen", _fake)

    out = tmp_path / "out"
    r = unity_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    assert calls["n"] == 2  # /compile then /playtest
    assert (out / "playtest_report.json").exists()


def test_compile_pass(tmp_path, monkeypatch):
    (tmp_path / "Assets").mkdir()
    (tmp_path / "Assets" / "Player.cs").write_text("// script")
    _mock_urlopen(monkeypatch, {"passed": True, "errors": [], "file_count": 1,
                                "summary": "Compiled OK."})
    out = tmp_path / "out"
    r = unity_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    assert _read_report(out)["passed"] is True


def test_compile_fail_records_errors(tmp_path, monkeypatch):
    (tmp_path / "Assets").mkdir()
    (tmp_path / "Assets" / "Bad.cs").write_text("// bad")
    _mock_urlopen(monkeypatch, {"passed": False, "file_count": 1, "errors": [
        {"file": "Assets/Bad.cs", "line": 7, "col": 19,
         "code": "CS1061", "message": "no such member"}]})
    out = tmp_path / "out"
    r = unity_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is False
    report = _read_report(out)
    assert report["passed"] is False
    assert report["errors"][0]["code"] == "CS1061"


def test_builder_unreachable_degrades_to_pass(tmp_path, monkeypatch):
    # Infra down must not stall the pipeline → pass with a note.
    (tmp_path / "Assets").mkdir()
    (tmp_path / "Assets" / "Player.cs").write_text("// script")
    _mock_urlopen(monkeypatch, fail=True)
    out = tmp_path / "out"
    r = unity_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    report = _read_report(out)
    assert "unreachable" in report["summary"]
    # Loud skip: C# present but gate didn't run → flag it so 5_review surfaces
    # it rather than reading a bare passed:true as a clean compile.
    assert report["gate_skipped"] is True
