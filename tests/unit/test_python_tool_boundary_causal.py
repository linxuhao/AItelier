"""Causal checks written only against surfaces that exist before and after the fix.

Each test drives the host's REAL composed loader / host ingress, so it can run
unchanged on the parent commit (where it must fail) and on the fix.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from skillflow import SkillFlow

from core import repository_executor


@pytest.fixture
def host_loader(monkeypatch):
    import api.dependencies as deps
    monkeypatch.setattr(deps, "_tool_loader_instance", None)
    yield deps.get_tool_loader()
    monkeypatch.setattr(deps, "_tool_loader_instance", None)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "test_probe.py").write_text("VALUE = 1\n")          # collects nothing
    return root


def test_native_pytest_never_starts_a_local_interpreter(host_loader, repo, monkeypatch):
    starts = []
    def local(*args, **kwargs):
        starts.append(args[0] if args else kwargs.get("args"))
        raise AssertionError("local process started for repository pytest")
    monkeypatch.setattr(subprocess, "run", local)
    monkeypatch.setattr(subprocess, "Popen", local)
    seen = []
    def owned(root, args, timeout, **kwargs):
        seen.append(kwargs.get("run_id"))
        return subprocess.CompletedProcess(args, 5, "no tests ran", "")
    monkeypatch.setattr(repository_executor, "execute", owned)
    fn = host_loader.load_fn("pytest")
    # Offered the way SkillFlow offers it: filtered to the callable's signature.
    import inspect
    offered = {"file": "test_probe.py", "workspace_root": str(repo), "run_id": "owned-run"}
    accepted = inspect.signature(fn).parameters
    try:
        result = fn(**{k: v for k, v in offered.items() if k in accepted})
    except AssertionError:
        result = None
    assert starts == [] and seen == ["owned-run"]
    # pytest exit 5 collected nothing: that must not read as a pass to the
    # validator (passed/verdict) or to a lifecycle hook (passed/error).
    assert result["verdict"] != "passed" and result.get("passed") is False


def test_run_tests_agent_ingress_does_not_accept_a_foreign_run(monkeypatch):
    from core.skillflow_host import AItelierSkillFlow
    host = object.__new__(AItelierSkillFlow)
    monkeypatch.setattr(host, "_tool_accepts_keyword", lambda *a: False)
    captured = {}
    monkeypatch.setattr(SkillFlow, "_execute_tool_impl",
                        lambda self, name, params, **k: captured.update(params=params) or {})
    host._execute_tool_impl("run_tests", {"run_id": "foreign-run",
                                          "project_root": "/foreign/worktree"},
                            run_id="owned-run", project_root="/owned")
    assert "run_id" not in captured["params"]
    assert "project_root" not in captured["params"]
