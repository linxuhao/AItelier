"""The zvec-grep sidecar runs its worker from the mounted checkout, not the image.

The sidecar image is pinned by digest with `pull_policy: never` and deliberately
no build stanza, and the entrypoint plus the worker module are COPY'd into it. So
a merge to core/semantic_index_control.py used to reach nothing the resident
worker loads: the container started `python3 /usr/local/lib/...`, and even
`up -d --force-recreate zvec-grep` re-executed the same baked copy. Measured
2026-11-10: the running container's worker was `/usr/local/lib/...`, whose
sha256 equalled the mounted /app copy already - the mount was correct and
unused.

docker/zvec-grep-entrypoint.sh therefore resolves its worker from
AITELIER_SOURCE_ROOT (default /app, the read-only `.:/app:ro` mount) and answers
`--path-only` with that path before it touches anything, so the delivery can be
proven without spending a recreate.

This file executes the real docker/zvec-grep-entrypoint.sh under `sh`, from the
repository root. It never greps the script: every arm asserts an observed exit
status, stream or recorded argv.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "docker" / "zvec-grep-entrypoint.sh"
WORKER_RELATIVE = Path("core") / "semantic_index_control.py"


def _env(**extra: str) -> dict[str, str]:
    """The ambient environment minus the sidecar's own variables, plus `extra`.

    The arms are assertions about AITELIER_SOURCE_ROOT, so a developer shell or
    a compose env_file that already exports AITELIER_HOME must not answer them.
    """
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(("AITELIER_", "ZVEC_GREP_"))}
    env.update(extra)
    return env


def _checkout(root: Path) -> Path:
    """A stand-in checkout: any bytes at the worker's repository-relative path."""
    (root / WORKER_RELATIVE.parent).mkdir(parents=True)
    (root / WORKER_RELATIVE).write_text("# stand-in worker\nVALUE = 42\n")
    return root


def _run(argv: list[str], env: dict[str, str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(ENTRYPOINT), *argv], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=timeout)


def test_path_only_prints_the_worker_the_mount_delivers(tmp_path):
    checkout = _checkout(tmp_path / "checkout")
    result = _run(["--path-only"], _env(AITELIER_SOURCE_ROOT=str(checkout)))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(checkout / WORKER_RELATIVE)
    assert result.stderr == ""


def test_path_only_refuses_a_checkout_without_the_worker(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    missing = str(checkout / WORKER_RELATIVE)
    result = _run(["--path-only"], _env(AITELIER_SOURCE_ROOT=str(checkout)))
    assert result.returncode == 78, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert missing in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1, result.stderr


def test_path_only_creates_no_control_directory_and_no_lock(tmp_path):
    """--path-only is handled before the flock, the mkdir and the daemon start, so
    a checkout with the worker leaves the state directory exactly as it found it."""
    checkout = _checkout(tmp_path / "checkout")
    home = tmp_path / "state" / "aitelier"
    home.mkdir(parents=True)
    result = _run(["--path-only"], _env(
        AITELIER_SOURCE_ROOT=str(checkout),
        AITELIER_HOME=str(home),
        AITELIER_PROJECTS_DIR=str(tmp_path / "state" / "projects"),
        AITELIER_WORKTREES_DIR=str(tmp_path / "state" / "worktrees"),
        ZVEC_GREP_HOME=str(tmp_path / "state" / "zvec-grep-home")))
    assert result.returncode == 0, result.stderr
    assert not (home / "semantic-index-control").exists()
    assert not (home / "semantic-index-control" / "service.lock").exists()
    assert list(home.iterdir()) == []


def test_the_normal_path_runs_the_worker_from_the_source_root(tmp_path):
    """The worker command comes from AITELIER_SOURCE_ROOT, not from /usr/local/lib.

    A `python3` stub earlier in PATH records the argv the script really executed
    and exits 3, so the assertion is on the executed command rather than on the
    script's text. `zg` is stubbed to report readiness at once (its `server run`
    blocks so the script's readiness loop passes on the first try), and the stub
    worker's exit ends the supervisor loop in bounded time.
    """
    checkout = _checkout(tmp_path / "checkout")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    recorded = tmp_path / "python3-argv.txt"
    (binaries / "python3").write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$*\" > '{recorded}'\nexit 3\n")
    (binaries / "zg").write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "server run") exec sleep 60 ;;\n'
        'esac\n'
        "exit 0\n")
    for stub in binaries.iterdir():
        stub.chmod(0o755)
    result = _run([], _env(
        AITELIER_SOURCE_ROOT=str(checkout),
        AITELIER_HOME=str(tmp_path / "state" / "aitelier"),
        AITELIER_PROJECTS_DIR=str(tmp_path / "state" / "projects"),
        AITELIER_WORKTREES_DIR=str(tmp_path / "state" / "worktrees"),
        ZVEC_GREP_HOME=str(tmp_path / "state" / "zvec-grep-home"),
        PATH=str(binaries) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")))
    assert recorded.exists(), (result.stdout, result.stderr)
    argv = recorded.read_text().split()
    assert argv[0] == str(checkout / WORKER_RELATIVE), argv
    assert "--control-dir" in argv, argv
    assert "/usr/local/lib/semantic_index_control.py" not in recorded.read_text()
    assert result.returncode != 0
