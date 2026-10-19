"""Negative controls, output-root failures and transport for the storage diagnosis.

Companion of test_run_tests_storage_exhaustion.py (same fixtures, same limits):
fault-injected at the syscall, real executor entry, real public run_tests.
UNRUN by their author (source-only workflow).
"""
import errno
import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

import aitelier.tools.run_tests.impl as rt
from aitelier.gate_evidence import report_state
from core import repository_executor
from tests.unit.test_run_tests_storage_exhaustion import (  # noqa: F401 (fixture re-export)
    GREEN, RED, TMP_GREEN, _baseline, _fail, _inject, _run, _vfs, env)


def test_repository_text_claiming_numbered_dir_failure_is_not_trusted_evidence(env):
    (env.repo / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def tmp_path():\n"
        "    raise OSError('could not create numbered dir with prefix pytest- in /tmp "
        "after 10 tries (No space left on device)')\n")
    result, report = _run(env, "def test_uses_tmp(tmp_path):\n    assert tmp_path\n")
    assert "could not create numbered dir" in report["stdout"]       # the label is there ...
    assert report["storage_diagnosis"]["state"] == "clean"          # ... the filesystem says fine
    assert report["passed"] is False and not report.get("storage_unmeasured")
    assert not report.get("infrastructure_unavailable") and not report.get("skipped")
    assert report_state(report) in ("failed", "known_failure", "unattributed")


@pytest.mark.parametrize("number", [errno.EACCES, errno.EROFS])
def test_permission_or_readonly_mkdir_is_ambiguous_not_exhaustion(env, number):
    _inject(env, number, lambda n: True)
    result, report = _run(env, TMP_GREEN)
    diagnosis = report["storage_diagnosis"]
    assert diagnosis["state"] == "ambiguous" and diagnosis["findings"][0]["errno"] == number
    assert diagnosis["proof"] == []
    assert report["returncode"] == 0 and result["passed"] is True      # nothing waived or invented
    assert not report.get("storage_unmeasured") and not report.get("infrastructure_unavailable")


@pytest.mark.parametrize("fields,resource", [
    ({"f_bavail": 0, "f_bfree": 0}, ["bytes"]),
    ({"f_ffree": 0, "f_favail": 0}, ["inodes"])])
def test_bytes_and_inode_exhaustion_are_both_retained_through_the_public_path(env, fields, resource):
    _inject(env, errno.ENOSPC, lambda n: True)
    _vfs(env, **fields)
    _, report = _run(env, TMP_GREEN)
    assert report["storage_diagnosis"]["proof"][0]["resource"] == resource
    assert report["storage_unmeasured"] and report["passed"] is False
    stored = [o for o in report["storage_diagnosis"]["observation"]["before"]
              if "pytest_tmp" in o["roles"]][0]["statvfs"]
    assert all(stored[k] == v for k, v in fields.items())


def test_backend_output_root_enospc_is_unmeasured_and_never_starts_pytest(env):
    real = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        if kwargs.get("prefix") == "run_tests_junit_":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(*args, **kwargs)

    env.monkeypatch.setattr(rt.tempfile, "mkdtemp", mkdtemp)
    result, report = _run(env, GREEN)
    assert not env.calls
    assert report["storage_diagnosis"]["scope"] == "backend_output_root"
    assert report["storage_diagnosis"]["proof"][0]["errno"] == errno.ENOSPC
    assert report["storage_unmeasured"] and report["passed"] is False
    assert result["baseline_state"] == "unmeasured" and not _baseline(env).exists()


def test_backend_output_root_permission_error_is_unavailable_but_not_called_exhaustion(env):
    def mkdtemp(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    env.monkeypatch.setattr(rt.tempfile, "mkdtemp", mkdtemp)
    result, report = _run(env, GREEN)
    assert not env.calls and report["storage_diagnosis"]["state"] == "ambiguous"
    assert report["passed"] is False and report["infrastructure_unavailable"] is True
    assert not report.get("storage_unmeasured")
    assert report["skipped_because"] == "test_workspace_unavailable"
    assert result["baseline_state"] == "unmeasured" and not _baseline(env).exists()


@pytest.mark.parametrize("source", [GREEN, RED])
def test_report_write_failure_never_fabricates_success(env, source):
    real = Path.write_text

    def write_text(self, *args, **kwargs):
        if self.name == "test_report.json" or self.name.startswith(".test_report.json.tmp"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, *args, **kwargs)

    env.monkeypatch.setattr(Path, "write_text", write_text)
    result, report = _run(env, source)
    assert report is None and result["written"] is None
    assert result["passed"] is False and result["_error"] is True
    assert result["report_write_failed"] and result["storage_exhausted"] is True
    assert result["evidence_state"] == "infrastructure_unavailable"
    assert result["release_evidence"] == "unresolved" and "ENOSPC" in result["error"]


def test_report_directory_failure_is_an_error_not_a_pass(env):
    real = Path.mkdir

    def mkdir(self, *args, **kwargs):
        if self == env.out:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(self, *args, **kwargs)

    env.monkeypatch.setattr(Path, "mkdir", mkdir)
    result, report = _run(env, GREEN)
    assert report is None and result["written"] is None and result["passed"] is False
    assert result["storage_exhausted"] is False and result["report_write_failed"]


def test_host_launcher_transport_carries_the_entry_observation(monkeypatch):
    storage = {"schema": rt.STORAGE_SCHEMA, "preflight_refused": True,
               "before": [{"path": "/tmp", "mkdir": _fail(errno.ENOSPC)}]}
    response = {"ok": True, "result": {"returncode": -1, "stdout": "", "stderr": "refused",
                                       "timed_out": False, "import_error": "", "storage": storage}}
    path = os.path.join(tempfile.mkdtemp(), "launcher.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(json.dumps(response).encode() + b"\n")
        server.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setenv(repository_executor.HOST_LAUNCHER_ENV, path)
    done = repository_executor.execute(Path("/proj"), ["python3", "-m", "pytest"], 60)
    thread.join(5)
    assert done.returncode == -1 and done.stderr == "refused" and done.storage == storage
    assert rt.diagnose_storage(done.storage, done.returncode)["state"] == "exhausted_preflight"
