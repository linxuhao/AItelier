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


def test_no_cs_files_is_noop(tmp_path, monkeypatch):
    # A Python-only repo must pass WITHOUT contacting the builder.
    (tmp_path / "app.py").write_text("print('hi')")
    called = {"n": 0}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = tmp_path / "out"
    r = unity_compile(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    assert called["n"] == 0  # builder never called
    assert "No C#" in _read_report(out)["summary"]


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
    assert "unreachable" in _read_report(out)["summary"]
