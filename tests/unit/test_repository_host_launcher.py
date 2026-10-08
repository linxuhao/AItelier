"""Host launcher policy regressions. UNRUN in this source step; executed by
the director-owned evaluator only."""
import hashlib
import json
import sqlite3
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading

import pytest

from core import repository_executor, repository_host_launcher as launcher


def _config(root: Path, **extra):
    cfg = {"allowed_roots": [str(root / "projects")],
           "aitelier_home": str(root / "home"),
           "state_db": str(root / "home" / "aitelier.db"),
           "test_image": "sha256:" + "a" * 64,
           "executor_entry": str(root / "repository_executor_entry.py"),
           "allowed_uids": [os.getuid()]}
    cfg.update(extra)
    return cfg


def _fixture(tmp_path: Path):
    (tmp_path / "projects" / "repo").mkdir(parents=True)
    (tmp_path / "reports").mkdir()
    (tmp_path / "repository_executor_entry.py").write_text("# entry\n")
    (tmp_path / "home").mkdir()
    with sqlite3.connect(tmp_path / "home" / "aitelier.db") as conn:
        conn.execute("CREATE TABLE run_isolation(run_id TEXT,mode TEXT,worktree_path TEXT,source_repo TEXT,project_id TEXT)")
        conn.execute("INSERT INTO run_isolation VALUES(?,?,?,?,?)",
                     ("normal-run", "worktree", str(tmp_path / "projects" / "repo"),
                      str(tmp_path / "projects" / "repo"), "project"))
    return _config(tmp_path)


def _request(tmp_path: Path, **overrides):
    req = {"op": "launch", "run_id": "normal-run", "repo": str(tmp_path / "projects" / "repo"),
           "args": ["python3", "-m", "pytest", "-q"], "timeout": 60}
    req.update(overrides)
    return req


def test_config_requires_all_operator_keys(tmp_path):
    with pytest.raises(launcher.LaunchRefused):
        launcher.load_config(_write(tmp_path, {"allowed_roots": ["/x"]}))


def _write(tmp_path: Path, cfg: dict) -> Path:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    return path


def test_config_rejects_injected_image(tmp_path):
    path = _write(tmp_path, _config(tmp_path, test_image="img --privileged"))
    with pytest.raises(launcher.LaunchRefused):
        launcher.load_config(path)


def test_valid_request_normalizes_paths(tmp_path):
    cfg = _fixture(tmp_path)
    repo = tmp_path / "projects" / "repo"
    report = (tmp_path / "home" / "gate-reports" /
              hashlib.sha256(b"normal-run").hexdigest() / "r1")
    report.mkdir(parents=True)
    fields = launcher.validate_request(cfg, _request(
        tmp_path, report_dir=str(report), writable_dirs=[str(report)], import_module="pkg"))
    assert fields["repo"] == str(repo)
    assert fields["report_dir"] == str(report)


def test_rejects_repo_outside_allowed_roots(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(launcher.LaunchRefused):
        launcher.validate_request(_fixture(tmp_path), _request(tmp_path, repo=str(outside)))


def test_rejects_symlink_escape(tmp_path):
    cfg = _fixture(tmp_path)
    link = tmp_path / "projects" / "repo" / "escape"
    link.symlink_to(tmp_path / "reports")
    with pytest.raises(launcher.LaunchRefused):
        launcher.validate_request(cfg, _request(
            tmp_path, writable_dirs=[str(link)]))


def test_rejects_missing_relay_socket(tmp_path):
    with pytest.raises(launcher.LaunchRefused):
        launcher.validate_request(_fixture(tmp_path), _request(
            tmp_path, relay_socket=str(tmp_path / "reports" / "nope.sock")))


def test_rejects_arbitrary_entrypoint_and_shell_chars(tmp_path):
    cfg = _fixture(tmp_path)
    with pytest.raises(launcher.LaunchRefused):
        launcher.validate_request(cfg, _request(tmp_path, args=["sh", "-c", "id"]))
    with pytest.raises(launcher.LaunchRefused):
        launcher.validate_request(cfg, _request(
            tmp_path, args=["python3", "-m", "pytest", "-q;id"]))


def test_gate_script_must_live_in_repo(tmp_path):
    cfg = _fixture(tmp_path)
    repo = tmp_path / "projects" / "repo"
    (repo / "gate.sh").write_text("#!/bin/sh\n")
    launcher.validate_request(cfg, _request(tmp_path, args=["bash", "gate.sh"]))
    with pytest.raises(launcher.LaunchRefused):
        launcher.validate_request(cfg, _request(
            tmp_path, args=["bash", str(tmp_path / "reports" / "x.sh")]))


def test_rejects_out_of_range_timeout(tmp_path):
    with pytest.raises(launcher.LaunchRefused):
        launcher.validate_request(_fixture(tmp_path), _request(tmp_path, timeout=0))


def test_docker_command_pins_image_and_bounds(tmp_path, monkeypatch):
    cfg = _fixture(tmp_path)
    fields = launcher.validate_request(cfg, _request(tmp_path))
    # Argv construction has no Docker runtime in this capped review container.
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/controlled/docker")
    command = launcher.docker_command(cfg, fields, "aitelier-cpu-abc")
    assert "sha256:" + "a" * 64 in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cpus") + 1] == "2"
    assert command[command.index("--memory") + 1] == "2g"
    assert command[command.index("--pids-limit") + 1] == "512"
    assert "--privileged" not in command
    assert "aitelier-cpu-abc" in command


def test_handle_request_refuses_without_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda _: None)
    response = launcher.handle_request(_fixture(tmp_path),
                                       json.dumps(_request(tmp_path)).encode())
    assert response["ok"] is False


def test_handle_request_bad_json(tmp_path):
    response = launcher.handle_request(_fixture(tmp_path), b"{nope")
    assert response == {"ok": False, "error": "request is not valid JSON"}


class _FakeHost(threading.Thread):
    """One-shot unix-socket host launcher returning a canned response."""

    def __init__(self, response: dict):
        super().__init__(daemon=True)
        self.response = response
        self._dir = tempfile.mkdtemp()
        self.path = os.path.join(self._dir, "launcher.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        os.chmod(self.path, 0o600)
        self.server.listen(1)
        self.start()

    def run(self):
        conn, _ = self.server.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(json.dumps(self.response).encode() + b"\n")
        self.server.close()


def _execute_via_host(sock_path: str):
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv(repository_executor.HOST_LAUNCHER_ENV, sock_path)
        return repository_executor.execute(Path("/proj"), ["python3", "-m", "pytest"],
                                           60)


def test_execute_routes_through_host_launcher(monkeypatch):
    fake = _FakeHost({"ok": True, "result": {"returncode": 0, "stdout": "hi",
                                             "stderr": "", "timed_out": False,
                                             "import_error": ""}})
    monkeypatch.setattr(repository_executor, "_execute",
                        lambda *a, **k: pytest.fail("local docker fallback used"))
    done = _execute_via_host(fake.path)
    assert done.returncode == 0 and done.stdout == "hi"
    fake.join(5)


def test_execute_refusal_is_honest_nonpass(monkeypatch):
    fake = _FakeHost({"ok": False, "error": "repo escapes the operator-allowed roots: /proj"})
    monkeypatch.setattr(repository_executor, "_execute",
                        lambda *a, **k: pytest.fail("local docker fallback used"))
    with pytest.raises(repository_executor.IsolationUnavailable):
        _execute_via_host(fake.path)
    fake.join(5)


def test_execute_without_facility_never_falls_back(monkeypatch):
    monkeypatch.setattr(repository_executor, "_execute",
                        lambda *a, **k: pytest.fail("local docker fallback used"))
    missing = os.path.join(tempfile.mkdtemp(), "absent.sock")
    with pytest.raises(repository_executor.IsolationUnavailable):
        _execute_via_host(missing)


def test_timeout_maps_to_timeout_expired(monkeypatch):
    fake = _FakeHost({"ok": True, "result": {"returncode": -9, "stdout": "part",
                                             "stderr": "killed", "timed_out": True,
                                             "import_error": ""}})
    with pytest.raises(subprocess.TimeoutExpired):
        _execute_via_host(fake.path)
    fake.join(5)


@pytest.mark.parametrize("initially_present", [True, False])
@pytest.mark.parametrize("reply", ["success", "refusal", "timeout"])
def test_execute_via_host_preserves_launcher_environment(monkeypatch, tmp_path,
                                                          initially_present, reply):
    env_name = repository_executor.HOST_LAUNCHER_ENV
    original = str(tmp_path / "existing-launcher.sock")
    if initially_present:
        monkeypatch.setenv(env_name, original)
    else:
        monkeypatch.delenv(env_name, raising=False)
    result = {"returncode": 0, "stdout": "hi", "stderr": "",
              "timed_out": False, "import_error": ""}
    if reply == "refusal":
        response = {"ok": False, "error": "operator refused"}
        expected_error = repository_executor.IsolationUnavailable
    elif reply == "timeout":
        result.update(returncode=-9, timed_out=True)
        response = {"ok": True, "result": result}
        expected_error = subprocess.TimeoutExpired
    else:
        response = {"ok": True, "result": result}
        expected_error = None
    fake = _FakeHost(response)
    monkeypatch.setattr(repository_executor, "_execute",
                        lambda *a, **k: pytest.fail("local docker fallback used"))
    try:
        if expected_error:
            with pytest.raises(expected_error):
                _execute_via_host(fake.path)
        else:
            assert _execute_via_host(fake.path).stdout == "hi"
        if initially_present:
            assert os.environ[env_name] == original
        else:
            assert env_name not in os.environ
    finally:
        fake.join(5)
