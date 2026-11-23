"""deploy/pwmcp/run-pwmcp.sh `steward`: the steward's own browser instance (design-steward 7.8.13).

The script is run with a stub `docker` on PATH that records its arguments; nothing is started.
"""
from __future__ import annotations

import os
import shlex
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "pwmcp" / "run-pwmcp.sh"


def run_steward(tmp_path, *, port="8921", env=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    stub = bin_dir / "docker"
    # DOCKER_NETWORK_MISSING=1: `docker network inspect` fails, as for a network not created yet.
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> "$DOCKER_LOG"\n'
                    'printf -- "--\\n" >> "$DOCKER_LOG"\n'
                    '[ "$1 $2" = "network inspect" ] && [ -n "$DOCKER_NETWORK_MISSING" ] && exit 1\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    home = tmp_path / "home"
    home.mkdir()
    full_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home), "DOCKER_LOG": str(log), **(env or {})}
    done = subprocess.run(["bash", str(SCRIPT), "steward", port], env=full_env, capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    calls = [c.strip("\n").split("\n") for c in log.read_text().split("--\n") if c.strip()]
    return home, calls, done.stdout


def the(calls, *prefix):
    found = [c for c in calls if c[:len(prefix)] == list(prefix)]
    assert len(found) == 1, (prefix, calls)
    return found[0]


def test_the_steward_instance_is_on_its_own_network_with_its_own_profile(tmp_path):
    home, calls, out = run_steward(tmp_path)
    assert calls.index(the(calls, "rm", "-f", "steward-pwmcp")) < calls.index(the(calls, "run"))
    run = the(calls, "run")
    assert run[run.index("--name") + 1] == "steward-pwmcp"
    # Not aitelier_default: every container there (aitelier-zg, aitelier-godot) could drive it.
    assert run[run.index("--network") + 1] == "steward-browser"
    assert run.count("--network") == 1
    assert "host" not in run and "--ipc=host" not in run and "-p" not in run and "--publish" not in run
    assert run[run.index("--restart") + 1] == "unless-stopped"
    profile_root = home / ".local" / "share" / "aitelier" / "steward-browser"
    assert (profile_root / "profile").is_dir()
    assert stat.S_IMODE(profile_root.stat().st_mode) == 0o700
    mounts = [run[i + 1] for i, a in enumerate(run) if a == "-v"]
    assert f"{profile_root}:{profile_root}" in mounts
    # Only the profile is writable; nothing under ~/.AItelier is mounted read-write.
    assert all(m.endswith(":ro") for m in mounts if m != f"{profile_root}:{profile_root}")
    assert run[-4:-1] == ["mcr.microsoft.com/playwright:v1.65.0-noble", "bash", "-c"]
    flags = shlex.split(run[-1])
    assert flags[0] == "exec"
    assert flags[flags.index("--host") + 1] == "0.0.0.0" and flags[flags.index("--port") + 1] == "8921"
    assert flags[flags.index("--allowed-hosts") + 1] == "steward-pwmcp:8921"
    assert flags[flags.index("--user-data-dir") + 1] == str(profile_root / "profile")
    for absent in ("--allowed-origins", "--caps", "--snapshot-mode", "--save-session"):
        assert absent not in flags, absent
    assert "http://steward-pwmcp:8921/mcp" in out and "on steward-browser" in out
    # The network is there already (the stub's inspect succeeds): nothing is created.
    assert the(calls, "network", "inspect", "steward-browser")
    assert not [c for c in calls if c[:2] == ["network", "create"]]


def test_a_missing_network_is_created_before_the_browser_starts_and_reaches_the_web(tmp_path):
    _home, calls, _out = run_steward(tmp_path, env={"DOCKER_NETWORK_MISSING": "1"})
    create = the(calls, "network", "create")
    assert create == ["network", "create", "steward-browser"]  # a plain bridge: not --internal
    assert calls.index(create) < calls.index(the(calls, "run"))


def test_the_network_and_the_profile_dir_can_be_named(tmp_path):
    where = tmp_path / "elsewhere"
    _home, calls, _out = run_steward(tmp_path, port="8922", env={
        "STEWARD_BROWSER_DIR": str(where), "STEWARD_BROWSER_NETWORK": "aitelier_other"})
    run = the(calls, "run")
    assert run[run.index("--network") + 1] == "aitelier_other"
    assert the(calls, "network", "inspect", "aitelier_other")
    assert (where / "profile").is_dir()
    flags = shlex.split(run[-1])
    assert flags[flags.index("--allowed-hosts") + 1] == "steward-pwmcp:8922"
