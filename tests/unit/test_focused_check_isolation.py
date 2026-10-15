"""Focused probes must use the same owned CPU transport as final gates.

Transport fault injection below is contract evidence, not a live host-launcher
or nested-container measurement. This suite itself runs only in a disposable CPU
container; the real entry tests exercise genuine pytest there.
"""
import asyncio
import subprocess
from pathlib import Path

import pytest

from aitelier.tools.focused_check import impl as focused
from core import repository_executor as executor


def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "test_probe.py").write_text("def test_probe(): assert True\n")
    return root


def call(root, **kwargs):
    return focused.focused_check(kind="pytest", targets=["test_probe.py"],
                                 project_root=str(root), run_id="owned-run",
                                 step_id="implement", **kwargs)


def test_focused_pytest_uses_existing_executor_without_local_popen(tmp_path, monkeypatch):
    root = repo(tmp_path)
    calls = []
    def execute(path, args, timeout, **kwargs):
        calls.append((path, args, timeout, kwargs))
        return subprocess.CompletedProcess(args, 7, "actual stdout\n", "actual stderr\n")
    monkeypatch.setattr(executor, "execute", execute)
    def forbidden(*args, **kwargs):
        raise AssertionError("focused pytest attempted local subprocess")
    monkeypatch.setattr(focused.subprocess, "Popen", forbidden)
    result = call(root, timeout_seconds=17)
    assert len(calls) == 1
    path, args, timeout, kwargs = calls[0]
    assert path == root and timeout == 17
    assert args == ["python3", "-m", "pytest", "-q", "-s", "--maxfail=1",
                    "-p", "no:cacheprovider", "--rootdir", str(root), "test_probe.py"]
    assert kwargs == {"run_id": "owned-run"}
    assert result["passed"] is False and result["exit_status"] == 7
    assert result["stdout"] == "actual stdout\n"
    assert result["stderr"] == "actual stderr\n"
    assert not result["timed_out"]


@pytest.mark.parametrize("run_id,step_id", [("", "implement"), ("r", ""), (" ", "i"), (None, "i")])
def test_missing_identity_refuses_before_transport(tmp_path, monkeypatch, run_id, step_id):
    root = repo(tmp_path)
    monkeypatch.setattr(executor, "execute", lambda *a, **k: pytest.fail("unbound execution"))
    result = focused.focused_check(kind="pytest", targets=["test_probe.py"],
                                  project_root=str(root), run_id=run_id, step_id=step_id)
    assert result["passed"] is False and "identity" in result["error"]


@pytest.mark.parametrize("failure", [executor.IsolationUnavailable("missing facility or ownership"),
                                     OSError("transport broken")])
def test_facility_or_binding_refusal_never_falls_back(tmp_path, monkeypatch, failure):
    root = repo(tmp_path)
    def refuse(*a, **k): raise failure
    monkeypatch.setattr(executor, "execute", refuse)
    monkeypatch.setattr(focused.subprocess, "Popen", lambda *a, **k: pytest.fail("local fallback"))
    result = call(root)
    assert not result["passed"] and result["exit_status"] is None
    assert not result["timed_out"] and result["error"]


def test_timeout_keeps_partial_streams_without_inventing_exit_code(tmp_path, monkeypatch):
    root = repo(tmp_path)
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(["python3", "-m", "pytest"], 1, b"partial-out", b"partial-err")
    monkeypatch.setattr(executor, "execute", timeout)
    result = call(root, timeout_seconds=1)
    assert not result["passed"] and result["timed_out"]
    assert result["exit_status"] is None
    assert result["stdout"] == "partial-out" and result["stderr"] == "partial-err"


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_cancellation_propagates_without_retry_or_local_fallback(tmp_path, monkeypatch, failure):
    root = repo(tmp_path); calls = []
    def cancel(*a, **k): calls.append(1); raise failure
    monkeypatch.setattr(executor, "execute", cancel)
    monkeypatch.setattr(focused.subprocess, "Popen", lambda *a, **k: pytest.fail("local fallback"))
    with pytest.raises(type(failure)): call(root)
    assert calls == [1]


def test_success_and_multibyte_streams_share_the_existing_output_cap(tmp_path, monkeypatch):
    root = repo(tmp_path)
    stdout, stderr = "out:" + "😀" * 5000, "err:" + "界" * 5000
    monkeypatch.setattr(executor, "execute", lambda path, args, *a, **k:
                        subprocess.CompletedProcess(args, 0, stdout, stderr))
    result = call(root)
    assert result["passed"] and result["exit_status"] == 0
    assert result["output_bytes"] == len((stdout + stderr).encode())
    assert result["output_truncated"]
    assert len(result["output"].encode()) <= 12000
    assert len(result["stdout"].encode()) + len(result["stderr"].encode()) <= 12000


def test_host_ingress_removes_caller_identity_and_roots_before_sdk_injection(monkeypatch):
    from core.skillflow_host import AItelierSkillFlow
    from skillflow import SkillFlow
    host = object.__new__(AItelierSkillFlow)
    monkeypatch.setattr(host, "_tool_accepts_keyword", lambda *a: True)
    monkeypatch.setattr(host, "_get_project_id", lambda run: "owned-project")
    captured = {}
    def execute(self, name, params, **kwargs): captured.update(params=params, host=kwargs); return captured
    monkeypatch.setattr(SkillFlow, "_execute_tool_impl", execute)
    params = {"kind":"pytest", "targets":["test_probe.py"], **dict.fromkeys(
        ["run_id", "step_id", "project_id", "operation_id", "config_name", "project_root", "workspace_root"], "foreign")}
    host._execute_tool_impl("focused_check", params, run_id="owned-run", step_id="implement", project_root="/owned")
    assert captured["params"] == {"kind":"pytest", "targets":["test_probe.py"], "project_id":"owned-project"}
    assert captured["host"] == {"run_id":"owned-run", "step_id":"implement", "project_root":"/owned"}
    assert params["run_id"] == "foreign"


@pytest.mark.parametrize("bad", [
    {"targets":["test_probe.py"] * 9},
    {"targets":["--help"]},
    {"targets":["../test_foreign.py"]},
    {"targets":["test_alias.py"]},
    {"targets":["test_probe.py"], "timeout_seconds":301},
    {"targets":["test_probe.py"], "timeout_seconds":True},
    {"targets":["test_probe.py"], "env":{"SECRET":"x"}},
])
def test_narrow_bounds_and_symlink_escape_refuse_before_transport(tmp_path, monkeypatch, bad):
    root=repo(tmp_path)
    foreign=tmp_path/'test_foreign.py';foreign.write_text('raise AssertionError("foreign")\n')
    (root/'test_alias.py').symlink_to(foreign)
    monkeypatch.setattr(executor,'execute',lambda *a,**k:pytest.fail('invalid request reached executor'))
    result=focused.focused_check(kind='pytest',project_root=str(root),run_id='owned-run',step_id='implement',**bad)
    assert result['passed'] is False and result['error']
    assert foreign.read_text()=='raise AssertionError("foreign")\n'


def test_actual_missing_docker_facility_never_executes_authored_marker(tmp_path, monkeypatch):
    root=repo(tmp_path);marker=tmp_path/'unsafe-marker'
    (root/'test_probe.py').write_text(f'def test_probe(): open({str(marker)!r}, "w").write("ran")\n')
    monkeypatch.delenv(executor.HOST_LAUNCHER_ENV,raising=False)
    monkeypatch.setattr(executor.shutil,'which',lambda name:None)
    from core import datadir
    monkeypatch.setattr(datadir,'aitelier_home',lambda:tmp_path/'control')
    result=call(root)
    assert result['passed'] is False and result['exit_status'] is None
    assert 'local execution refused' in result['error']
    assert not marker.exists()


@pytest.mark.parametrize('returncode',[True,None,'0'])
def test_malformed_result_cannot_become_pass(tmp_path,monkeypatch,returncode):
    root=repo(tmp_path)
    monkeypatch.setattr(executor,'execute',lambda *a,**k:subprocess.CompletedProcess([],returncode,'out','err'))
    result=call(root)
    assert not result['passed'] and result['exit_status'] is None
    assert 'malformed' in result['error']
