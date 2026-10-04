"""Required pytest-asyncio must be present before taking the fast path."""
from pathlib import Path
import sys

import aitelier.tools.run_tests.impl as rt


def test_healthy_toolchain_uses_current_python(tmp_path, monkeypatch):
    monkeypatch.setattr(rt.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(rt.tempfile, "mkdtemp",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("provisioned")))
    assert rt._resolve_pytest_python(tmp_path, {}) == (sys.executable, None)


def test_missing_async_plugin_provisions_and_cleans_failed_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(rt.importlib.util, "find_spec",
                        lambda name: object() if name == "pytest" else None)
    monkeypatch.setattr(rt.time, "sleep", lambda *_: None)
    created = []
    original_mkdtemp = rt.tempfile.mkdtemp
    def make_venv(**kwargs):
        path = original_mkdtemp(dir=tmp_path, **kwargs)
        created.append(Path(path))
        return path
    monkeypatch.setattr(rt.tempfile, "mkdtemp", make_venv)
    calls = []
    def unavailable(command, **kwargs):
        calls.append(command)
        raise OSError("owned provisioning unavailable")
    monkeypatch.setattr(rt.subprocess, "run", unavailable)
    report = {}
    assert rt._resolve_pytest_python(tmp_path, report) == (None, None)
    assert len(calls) == len(created) == 3
    assert all(not path.exists() for path in created)
    assert report["passed"] is False
    assert report["infrastructure_unavailable"] is True
    assert report["evidence_state"] == "infrastructure_unavailable"
    assert "3 attempts" in report["summary"]


def test_run_tests_missing_plugin_cannot_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(rt.importlib.util, "find_spec",
                        lambda name: object() if name == "pytest" else None)
    monkeypatch.setattr(rt.time, "sleep", lambda *_: None)
    def unavailable(*args, **kwargs):
        raise OSError("owned provisioning unavailable")
    monkeypatch.setattr(rt.subprocess, "run", unavailable)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_async.py").write_text("async def test_async():\n    assert True\n")
    result = rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / "out"))
    assert result["passed"] is False
    import json
    report = json.loads((tmp_path / "out" / "test_report.json").read_text())
    assert report["infrastructure_unavailable"] is True
