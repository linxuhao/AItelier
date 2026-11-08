"""The host CPU launcher's shipped activation: unit render, pre-start probe, installer.

The launcher is the only authorised transport for repository gates, run_tests
pytest and coding shells, so its supervision cannot be an undocumented operator
step. These tests measure the three shipped pieces without a systemd user
session: the render is checked as text (the container image has no
systemd-analyze, which this module reports instead of pretending), the probe is
driven as a subprocess against real Unix-socket states, and the installer runs
against a temporary HOME-shaped target with a recording systemctl stub on PATH.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys

import pytest

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy"
INSTALLER = DEPLOY / "install_host_cpu_launcher.py"
PROBE = DEPLOY / "cpu-launcher-preexec.py"
TEMPLATE = DEPLOY / "aitelier-host-cpu-launcher.service"

# The values the live unit on this host carries (systemctl --user show
# aitelier-host-cpu-launcher.service), used here as a render input.
LIVE = {
    "REPO": "/home/linxuhao/AItelier",
    "PYTHON": "/home/linxuhao/AItelier/.venv/bin/python",
    "SOCKET": "/home/linxuhao/.AItelier/cpu-launcher.sock",
    "CONFIG": ("/home/linxuhao/.AItelier/director/reports/"
               "storage-normal-runtime-r2-root-20261019/cpu-storage-operator-config.json"),
    "PREEXEC": "/home/linxuhao/.AItelier/bin/cpu-launcher-preexec.py",
}
OTHER = {
    "REPO": "/srv/aitelier",
    "PYTHON": "/srv/aitelier/venv/bin/python3",
    "SOCKET": "/run/aitelier/cpu-launcher.sock",
    "CONFIG": "/etc/aitelier/host-launcher.json",
    "PREEXEC": "/usr/local/libexec/cpu-launcher-preexec.py",
}


def _installer_module():
    spec = importlib.util.spec_from_file_location("install_host_cpu_launcher", INSTALLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _installer_module()


def _render(values):
    return installer.render(TEMPLATE.read_text(), values)


def test_render_carries_the_live_unit_values():
    rendered = _render(LIVE)
    for expected in (
            "Type=simple",
            "WorkingDirectory=/home/linxuhao/AItelier",
            "ExecStart=/home/linxuhao/AItelier/.venv/bin/python -m core.repository_host_launcher"
            " --socket /home/linxuhao/.AItelier/cpu-launcher.sock"
            " --config " + LIVE["CONFIG"],
            "ExecStartPre=/home/linxuhao/.AItelier/bin/cpu-launcher-preexec.py",
            "Restart=on-failure",
            "RestartSec=5",
            "KillMode=control-group",
            "UMask=0002",
            "LimitNOFILE=1048576",
            "MemoryMax=536870912",
            "StandardOutput=journal",
            "StandardError=journal",
            "WantedBy=default.target",
            "Documentation=file:///home/linxuhao/AItelier/docs/repository-host-launcher.md"):
        assert expected in rendered.splitlines(), expected
    assert not re.search(r"@[A-Z_]+@", rendered), "unreplaced template token"


def test_render_follows_different_parameters():
    rendered = _render(OTHER)
    assert "WorkingDirectory=/srv/aitelier" in rendered
    assert ("ExecStart=/srv/aitelier/venv/bin/python3 -m core.repository_host_launcher"
            " --socket /run/aitelier/cpu-launcher.sock"
            " --config /etc/aitelier/host-launcher.json") in rendered
    assert "ExecStartPre=/usr/local/libexec/cpu-launcher-preexec.py" in rendered
    assert "/home/linxuhao" not in rendered, "render is a hardcoded copy, not a substitution"
    assert not re.search(r"@[A-Z_]+@", rendered)


def test_render_refuses_an_unknown_token():
    with pytest.raises(ValueError, match="@NOT_A_TOKEN@"):
        installer.render(TEMPLATE.read_text() + "Extra=@NOT_A_TOKEN@\n", LIVE)


def test_rendered_unit_is_accepted_by_systemd_analyze(tmp_path):
    if shutil.which("systemd-analyze") is None:
        pytest.skip("this container image has no systemd-analyze, so it cannot verify units; "
                    "the rendered unit above is checked as text only")
    unit = tmp_path / "aitelier-host-cpu-launcher.service"
    unit.write_text(_render(LIVE))
    done = subprocess.run(["systemd-analyze", "verify", str(unit)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def _run_probe(path):
    return subprocess.run([sys.executable, str(PROBE), str(path)],
                          capture_output=True, text=True)


def test_probe_refuses_a_live_listener(tmp_path):
    path = tmp_path / "live.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(4)
    try:
        done = _run_probe(path)
        assert done.returncode != 0, done.stdout
        assert str(path) in done.stderr, done.stderr
        assert "refusing to replace" in done.stderr
        # The probe must not have unlinked the live listener's path.
        assert path.exists()
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            peer.connect(str(path))
        finally:
            peer.close()
    finally:
        listener.close()


def test_probe_removes_a_dead_socket_path(tmp_path):
    path = tmp_path / "dead.sock"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(path))
    dead.close()
    assert path.exists(), "an unclean stop leaves the socket file behind"
    done = _run_probe(path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert str(path) in done.stdout and "removed dead socket path" in done.stdout
    assert not path.exists()


def test_probe_creates_nothing_for_an_absent_path(tmp_path):
    path = tmp_path / "absent.sock"
    before = sorted(p.name for p in tmp_path.iterdir())
    done = _run_probe(path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert str(path) in done.stdout
    assert not path.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def _files(root, skip=(".git", "__pycache__")):
    """Every file under ``root``, repo-relative; git status is NOT used because the
    container mounts the real .git read-only (git would need to write its index)."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*")
                  if p.is_file() and not set(p.relative_to(root).parts) & set(skip))


def _stub_systemctl(root, record):
    root.mkdir(parents=True, exist_ok=True)
    stub = root / "systemctl"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$RECORD"\n')
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = str(root) + os.pathsep + env.get("PATH", "")
    env["RECORD"] = str(record)
    return env


def _run_installer(target, env, *extra):
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--target", str(target),
         "--config", str(target.parent / "operator-config.json"), *extra],
        capture_output=True, text=True, env=env)


def test_installer_writes_only_the_rendered_unit_and_probe_without_enable(tmp_path):
    target, record = tmp_path / "home", tmp_path / "record.txt"
    env = _stub_systemctl(tmp_path / "stub", record)
    repo_before = _files(REPO)
    done = _run_installer(target, env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not record.exists() or record.read_text() == "", "no --enable must run no systemctl"
    assert _files(target) == [".AItelier/bin/cpu-launcher-preexec.py",
                              ".config/systemd/user/aitelier-host-cpu-launcher.service"]
    unit = (target / ".config/systemd/user/aitelier-host-cpu-launcher.service").read_text()
    assert f"ExecStartPre={target}/.AItelier/bin/cpu-launcher-preexec.py" in unit
    assert f"WorkingDirectory={REPO}" in unit
    probe = target / ".AItelier/bin/cpu-launcher-preexec.py"
    assert probe.read_text() == PROBE.read_text()
    assert os.access(probe, os.X_OK)
    assert _files(REPO) == repo_before, "the installer wrote into the repository tree"


def test_installer_enable_records_daemon_reload_enable_start_in_order(tmp_path):
    target, record = tmp_path / "home", tmp_path / "record.txt"
    env = _stub_systemctl(tmp_path / "stub", record)
    repo_before = _files(REPO)
    done = _run_installer(target, env, "--enable")
    assert done.returncode == 0, done.stdout + done.stderr
    assert record.read_text().splitlines() == [
        "--user daemon-reload",
        "--user enable aitelier-host-cpu-launcher.service",
        "--user start aitelier-host-cpu-launcher.service",
    ]
    assert _files(REPO) == repo_before, "the installer wrote into the repository tree"
