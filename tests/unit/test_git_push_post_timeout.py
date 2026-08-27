"""A slow push must not fail the run — that is the tool's entire contract."""
import subprocess
from pathlib import Path

from aitelier.tools.git_push_post import impl


def test_timeout_becomes_a_failed_call_not_an_exception(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=impl._TIMEOUT_S)
    monkeypatch.setattr(impl.subprocess, "run", boom)
    r = impl._git(tmp_path, "push", "origin", "main")
    assert r.returncode != 0
    assert "timed out" in r.stderr


def test_probe_timeout_is_an_error_result_not_local_only(monkeypatch, tmp_path):
    """rc-124 on `git remote` used to read 'no remote configured (local-only)'
    — a confident wrong diagnosis after which pushes silently stop forever."""
    (tmp_path / ".git").mkdir()

    def fake_run(cmd, **k):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=impl._TIMEOUT_S)
    monkeypatch.setattr(impl.subprocess, "run", fake_run)
    r = impl.git_push_post(project_root=str(tmp_path))
    assert r["action"] == "error"
    assert "timed out" in r["error"]


def test_nothing_raises_out_of_the_tool(monkeypatch, tmp_path):
    """The whole contract: a push problem may never fail (or wedge) the run.
    An OSError escaping would make skillflow reopen the step and re-raise on
    every tick — never failed, never done."""
    (tmp_path / ".git").mkdir()

    def fake_run(cmd, **k):
        raise OSError("git binary missing")
    monkeypatch.setattr(impl.subprocess, "run", fake_run)
    r = impl.git_push_post(project_root=str(tmp_path))
    assert r["action"] == "error"
    assert "OSError" in r["error"]
