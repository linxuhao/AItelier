"""Unit tests for the unity_playtest tool (HTTP to unity-builder mocked).

Docker-free: the unity-builder call is patched. Covers the non-Unity no-op, the
pass/fail mapping, and the builder-unreachable liveness degrade. Mirrors
test_unity_compile.py.
"""

import json
import urllib.error
import urllib.request

from aitelier.tools.unity_playtest.impl import unity_playtest


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
    return json.loads((out_dir / "playtest_report.json").read_text())


def test_no_assets_is_noop(tmp_path, monkeypatch):
    # A non-Unity repo must pass WITHOUT contacting the builder.
    (tmp_path / "app.py").write_text("print('hi')")
    called = {"n": 0}
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    out = tmp_path / "out"
    r = unity_playtest(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    assert called["n"] == 0  # builder never called
    assert "not a Unity project" in _read_report(out)["summary"]


def test_playtest_pass(tmp_path, monkeypatch):
    (tmp_path / "Assets").mkdir()
    _mock_urlopen(monkeypatch, {"passed": True, "total": 1, "passed_count": 1,
                                "failed_count": 0, "failures": [],
                                "summary": "PlayMode: 1/1 passed."})
    out = tmp_path / "out"
    r = unity_playtest(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    assert _read_report(out)["passed"] is True


def test_playtest_fail_records_failures(tmp_path, monkeypatch):
    (tmp_path / "Assets").mkdir()
    _mock_urlopen(monkeypatch, {"passed": False, "total": 1, "passed_count": 0,
                                "failed_count": 1, "failures": [
        {"name": "AItelierSmokeTest.Scene_Builds_And_Runs_Without_Errors",
         "message": "Runtime errors during scene build/run:\n"
                    "Exception: InvalidOperationException ... UnityEngine.Input"}]})
    out = tmp_path / "out"
    r = unity_playtest(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is False
    report = _read_report(out)
    assert report["passed"] is False
    assert "InvalidOperationException" in report["failures"][0]["message"]


def test_builder_unreachable_degrades_to_pass(tmp_path, monkeypatch):
    # Infra down (or no license) must not stall the pipeline → pass with a note.
    (tmp_path / "Assets").mkdir()
    _mock_urlopen(monkeypatch, fail=True)
    out = tmp_path / "out"
    r = unity_playtest(project_root=str(tmp_path), out_dir=str(out))
    assert r["passed"] is True
    report = _read_report(out)
    assert "unreachable" in report["summary"]
    # Loud skip: real Unity project but smoke test didn't run → flag it.
    assert report["gate_skipped"] is True
