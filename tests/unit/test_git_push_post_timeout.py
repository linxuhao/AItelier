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
