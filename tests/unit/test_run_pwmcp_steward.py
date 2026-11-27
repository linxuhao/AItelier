"""deploy/pwmcp/run-pwmcp.sh `steward` and its per-driver siblings.

The script is run with a stub `docker` on PATH that records its arguments; nothing is started.
The iGPU arms are driven by a render-node fixture and an isolated browser root, so the
host's browser directory and devices are never touched.
"""
from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "pwmcp" / "run-pwmcp.sh"
BASE_ARGS = ["--disable-blink-features=AutomationControlled", "--font-render-hinting=none"]
GPU_ARGS = ["--use-angle=gl-egl", "--enable-gpu", "--ignore-gpu-blocklist"]


def base_config(tmp_path):
    """A base MCP config in an isolated browser root, with bytes worth comparing."""
    browser = tmp_path / "browser"
    browser.mkdir(exist_ok=True)
    config = browser / "pwmcp-config.json"
    config.write_text(json.dumps({
        "browser": {"launchOptions": {"args": list(BASE_ARGS)}},
        "capabilities": ["vision"],
    }, indent=2) + "\n")
    return config


def dri_fixture(tmp_path, *, present=True):
    """A fake /dev/dri: a renderD128 symlink to /dev/null is a character device."""
    dri = tmp_path / "dri"
    dri.mkdir(exist_ok=True)
    if present:
        (dri / "renderD128").symlink_to("/dev/null")
    return dri


def run_script(root, *args, env=None):
    """Run the script with a recording docker stub; return (home, argv calls, stdout)."""
    bin_dir = Path(tempfile.mkdtemp(dir=root, prefix="bin-"))
    log = bin_dir / "docker.log"
    stub = bin_dir / "docker"
    # DOCKER_NETWORK_MISSING=1: `docker network inspect` fails, as for a network not created yet.
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done >> "$DOCKER_LOG"\n'
                    'printf -- "--\\n" >> "$DOCKER_LOG"\n'
                    '[ "$1 $2" = "network inspect" ] && [ -n "$DOCKER_NETWORK_MISSING" ] && exit 1\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    home = root / "home"
    home.mkdir(exist_ok=True)
    empty_dri = root / "empty-dri"  # deterministic absent arm unless env overrides it
    empty_dri.mkdir(exist_ok=True)
    full_env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home), "DOCKER_LOG": str(log),
                "PWMCP_BROWSER_DIR": str(root / "browser"), "PWMCP_DRI_DIR": str(empty_dri), **(env or {})}
    done = subprocess.run(["bash", str(SCRIPT), *args], env=full_env, capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    calls = [c.strip("\n").split("\n") for c in log.read_text().split("--\n") if c.strip()]
    return home, calls, done.stdout


def run_steward(tmp_path, *, port="8921", env=None):
    return run_script(tmp_path, "steward", port, env=env)


def run_instance(tmp_path, driver, env=None):
    if driver == "steward":
        return run_steward(tmp_path, env=env)
    return run_script(tmp_path, driver, "8932", env=env)


def the(calls, *prefix):
    found = [c for c in calls if c[:len(prefix)] == list(prefix)]
    assert len(found) == 1, (prefix, calls)
    return found[0]


def config_arg(run):
    flags = shlex.split(run[-1])
    return flags[flags.index("--config") + 1]


def assert_gpu_run(run, dri, generated):
    """The device/group pair and the generated config, on either docker arm."""
    node = dri / "renderD128"
    assert run.count("--device") == 1 and run.count("--group-add") == 1
    assert run[run.index("--device") + 1] == str(node)
    assert run[run.index("--group-add") + 1] == str(os.stat("/dev/null").st_gid)
    assert config_arg(run) == str(generated)
    assert generated.is_file()
    config = json.loads(generated.read_text())
    assert config["browser"]["launchOptions"]["args"] == BASE_ARGS + GPU_ARGS
    assert config["capabilities"] == ["vision"]
    assert run[-4:-1] == ["mcr.microsoft.com/playwright:v1.65.0-noble", "bash", "-c"]


def assert_plain_run(run, config):
    """Exactly today's argv: no device flags, and the untouched base config."""
    assert "--device" not in run and "--group-add" not in run
    assert config_arg(run) == str(config)
    assert run[-4:-1] == ["mcr.microsoft.com/playwright:v1.65.0-noble", "bash", "-c"]


# ── original steward contract ─────────────────────────────────────────────────


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


# ── present iGPU: both arms ───────────────────────────────────────────────────


def test_a_render_node_attaches_the_igpu_to_the_steward(tmp_path):
    config = base_config(tmp_path)
    before = config.read_bytes()
    dri = dri_fixture(tmp_path)
    home, calls, _out = run_steward(tmp_path, env={"PWMCP_DRI_DIR": str(dri)})
    run = the(calls, "run")
    generated = home / ".local/share/aitelier/steward-browser/pwmcp-config.gpu.json"
    assert_gpu_run(run, dri, generated)
    assert config.read_bytes() == before, "the base config is authoritative, never rewritten"
    assert generated.read_bytes() != before
    # The generated copy lives in the instance's writable dir, which is mounted rw.
    assert f"{generated.parent}:{generated.parent}" in [run[i + 1] for i, a in enumerate(run) if a == "-v"]


def test_a_render_node_attaches_the_igpu_to_a_driver(tmp_path):
    config = base_config(tmp_path)
    before = config.read_bytes()
    dri = dri_fixture(tmp_path)
    _home, calls, _out = run_script(tmp_path, "grok", "8932", env={"PWMCP_DRI_DIR": str(dri)})
    run = the(calls, "run")
    assert run[run.index("--name") + 1] == "grok-pwmcp"
    generated = tmp_path / "browser/drivers/grok/pwmcp-config.gpu.json"
    assert_gpu_run(run, dri, generated)
    assert config.read_bytes() == before
    assert run[run.index("-w") + 1] == str(generated.parent)


@pytest.mark.parametrize("value", ["", "auto", "ON"])
def test_only_exact_off_disables_the_auto_detection(tmp_path, value):
    config = base_config(tmp_path)
    before = config.read_bytes()
    dri = dri_fixture(tmp_path)
    _home, calls, _out = run_steward(tmp_path, env={"PWMCP_DRI_DIR": str(dri), "PWMCP_GPU": value})
    run = the(calls, "run")
    assert_gpu_run(run, dri, tmp_path / "home/.local/share/aitelier/steward-browser/pwmcp-config.gpu.json")
    assert config.read_bytes() == before


# ── absent / disabled iGPU: today's argv on both arms ─────────────────────────


@pytest.mark.parametrize("driver", ["steward", "grok"])
def test_an_absent_render_node_keeps_the_original_run(tmp_path, driver):
    config = base_config(tmp_path)
    before = config.read_bytes()
    empty = dri_fixture(tmp_path, present=False)
    home, calls, _out = run_instance(tmp_path, driver, env={"PWMCP_DRI_DIR": str(empty)})
    run = the(calls, "run")
    assert_plain_run(run, config)
    assert config.read_bytes() == before
    where = home / ".local/share/aitelier/steward-browser" if driver == "steward" else \
        config.parent / "drivers" / driver
    assert not (where / "pwmcp-config.gpu.json").exists()


@pytest.mark.parametrize("driver", ["steward", "grok"])
def test_pwmcp_gpu_off_keeps_the_original_run_even_with_a_node(tmp_path, driver):
    config = base_config(tmp_path)
    before = config.read_bytes()
    dri = dri_fixture(tmp_path)
    home, calls, _out = run_instance(tmp_path, driver, env={"PWMCP_DRI_DIR": str(dri), "PWMCP_GPU": "off"})
    run = the(calls, "run")
    assert_plain_run(run, config)
    assert config.read_bytes() == before
    where = home / ".local/share/aitelier/steward-browser" if driver == "steward" else \
        config.parent / "drivers" / driver
    assert not (where / "pwmcp-config.gpu.json").exists()
