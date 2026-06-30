# cli/server.py
# Detect, start, and reuse the AItelier backend.
#
# The backend runs ONLY as a Docker container (docker-compose.yml): the CLI
# reuses the container if it is already up, otherwise starts it with
# `docker compose up -d aitelier`. There is no host-process fallback — running
# uvicorn directly on the host would make DPE git commits use the host
# developer's ~/.gitconfig identity instead of the image's AItelier identity.

import os
import subprocess
import time
from pathlib import Path

# Load .env so the CLI process picks up config (AITELIER_PORT, admin token, …).
# The container receives .env separately via compose `env_file`.
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    with open(_env_file) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _val = _line.split("=", 1)
                _key = _key.strip().removeprefix("export ")
                _val = _val.strip().strip("\"'")
                if _key not in os.environ:
                    os.environ[_key] = _val

import httpx

_DEFAULT_PORT = os.environ.get("AITELIER_PORT", "4444")
_DEFAULT_URL = f"http://localhost:{_DEFAULT_PORT}"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE_FILE = _PROJECT_ROOT / "docker-compose.yml"
_COMPOSE_SERVICE = "aitelier"
_IMAGE_NAME = "aitelier:latest"


# ── Health ────────────────────────────────────────────────────────────────

def _is_healthy(client: httpx.Client) -> bool:
    """True if the backend answers /health and /api/projects."""
    try:
        if client.get("/health").status_code != 200:
            return False
        return client.get("/api/projects", timeout=5.0).status_code < 500
    except httpx.HTTPError:
        return False


def _wait_healthy(client: httpx.Client, max_wait: int) -> bool:
    """Poll /health until it returns 200 or max_wait seconds elapse."""
    for _ in range(max_wait * 2):
        time.sleep(0.5)
        try:
            if client.get("/health").status_code == 200:
                return True
        except httpx.HTTPError:
            continue
    return False


def _find_server_pid(port: str) -> int | None:
    """PID of the process listening on the given port (non-Docker squatter)."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line:
                import re
                m = re.search(r"pid=(\d+)", line)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None


# ── Docker backend ──────────────────────────────────────────────────────────

def _docker_available() -> bool:
    """True if a Docker daemon is reachable."""
    if not _COMPOSE_FILE.exists():
        return False
    try:
        return subprocess.run(
            ["docker", "info"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _compose_env() -> dict:
    """Environment for `docker compose` so paths/ownership match the host user."""
    env = dict(os.environ)
    if hasattr(os, "getuid"):
        env.setdefault("AITELIER_UID", str(os.getuid()))
        env.setdefault("AITELIER_GID", str(os.getgid()))
    return env


def _compose(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run `docker compose -f <file> <args>`."""
    return subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), *args],
        env=_compose_env(),
        **kwargs,
    )


def _container_running() -> bool:
    """True if the compose service container is up."""
    try:
        res = _compose(
            "ps", "--status", "running", "--services",
            capture_output=True, text=True, timeout=15,
        )
        return _COMPOSE_SERVICE in res.stdout.split()
    except Exception:
        return False


def _image_exists() -> bool:
    try:
        return subprocess.run(
            ["docker", "image", "inspect", _IMAGE_NAME],
            capture_output=True, timeout=10,
        ).returncode == 0
    except Exception:
        return False


def _compose_up():
    """Start (building on first run) the backend container."""
    if not _image_exists():
        print("Building AItelier image (first run — this may take a few minutes)...")
    # Inherit stdout/stderr so build + startup progress is visible.
    res = _compose("up", "-d", _COMPOSE_SERVICE)
    if res.returncode != 0:
        raise RuntimeError(
            f"`docker compose up -d {_COMPOSE_SERVICE}` failed (see output above)"
        )


def _ensure_docker_backend(base_url: str, max_wait: int) -> bool:
    client = httpx.Client(base_url=base_url, timeout=2.0)

    # Already running and healthy → reuse it.
    if _container_running() and _is_healthy(client):
        return True

    # If the container is down, free the port from any stale non-Docker server
    # squatting on it so the published port can bind.
    if not _container_running():
        pid = _find_server_pid(_DEFAULT_PORT)
        if pid:
            print("Stopping stale non-Docker server before starting Docker backend...")
            try:
                os.kill(pid, 9)
                time.sleep(0.5)
            except ProcessLookupError:
                pass

    _compose_up()

    if _wait_healthy(client, max_wait):
        return True
    raise RuntimeError(
        f"Docker backend did not become healthy within {max_wait}s "
        f"(check: docker compose -f {_COMPOSE_FILE} logs)"
    )


# ── Public API ───────────────────────────────────────────────────────────────

def _require_docker() -> None:
    """Raise a clear error if no Docker daemon is reachable. The backend has no
    host-process fallback, so Docker is mandatory."""
    if not _docker_available():
        raise RuntimeError(
            "Docker is required to run the AItelier backend but no Docker daemon "
            f"is reachable (need Docker running and {_COMPOSE_FILE}). "
            "Start Docker and retry."
        )


def ensure_server_running(base_url: str, max_wait: int = 120) -> bool:
    """Ensure the Docker backend is up: reuse the container if it is running,
    otherwise `docker compose up -d aitelier`. Raises if Docker is unavailable
    — there is no host-process fallback."""
    _require_docker()
    return _ensure_docker_backend(base_url, max_wait)


def restart_server(base_url: str = _DEFAULT_URL, max_wait: int = 120) -> bool:
    """Restart the Docker backend."""
    _require_docker()
    _compose("restart", _COMPOSE_SERVICE)
    client = httpx.Client(base_url=base_url, timeout=2.0)
    if _wait_healthy(client, max_wait):
        return True
    raise RuntimeError(f"Docker backend did not restart within {max_wait}s")
