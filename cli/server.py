# cli/server.py
# Detect, start, and restart the AItelier backend.
#
# Primary path: a Docker container (docker-compose.yml). The CLI starts the
# container if it is not running and reuses it if it is already up. If Docker is
# unavailable, it falls back to running uvicorn as a local subprocess.

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

# Load .env before spawning the server so it inherits API keys (local fallback;
# the Docker path passes .env via compose env_file).
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

_LOG_DIR = Path.home() / ".AItelier"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_SERVER_LOG = _LOG_DIR / "server.log"

# Local-subprocess fallback state
_server_process = None
_log_file = None

# Track code hash to detect stale local-fallback servers
_HASH_FILE = _LOG_DIR / "server_code_hash"
_WATCHED_DIRS = ["api", "core"]


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
    res = _compose("up", "-d")
    if res.returncode != 0:
        raise RuntimeError("`docker compose up -d` failed (see output above)")


def _ensure_docker_backend(base_url: str, max_wait: int) -> bool:
    client = httpx.Client(base_url=base_url, timeout=2.0)

    # Already running and healthy → reuse it.
    if _container_running() and _is_healthy(client):
        return True

    # If the container is down, free the port from any stale non-Docker server
    # (e.g. an old local-subprocess backend) so the published port can bind.
    if not _container_running():
        pid = _find_server_pid(_DEFAULT_PORT)
        if pid:
            print("Stopping stale local backend before starting Docker backend...")
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


# ── Local subprocess fallback ────────────────────────────────────────────────

def _compute_code_hash() -> str:
    h = hashlib.md5()
    for dirname in _WATCHED_DIRS:
        d = _PROJECT_ROOT / dirname
        if not d.is_dir():
            continue
        for py in sorted(d.rglob("*.py")):
            try:
                h.update(str(py.relative_to(_PROJECT_ROOT)).encode())
                h.update(py.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def _is_local_stale() -> bool:
    current = _compute_code_hash()
    if not _HASH_FILE.exists():
        _HASH_FILE.write_text(current)
        return False
    return _HASH_FILE.read_text().strip() != current


def _ensure_local_backend(base_url: str, max_wait: int) -> bool:
    global _server_process, _log_file
    client = httpx.Client(base_url=base_url, timeout=2.0)

    stale = _is_local_stale()
    healthy = _is_healthy(client)

    if healthy and not stale:
        return True

    if healthy and stale:
        print("Server code changed — restarting with latest code...")
    pid = _find_server_pid(_DEFAULT_PORT)
    if pid:
        os.kill(pid, 9)
        time.sleep(0.5)

    _HASH_FILE.write_text(_compute_code_hash())
    _log_file = open(_SERVER_LOG, "w", encoding="utf-8")
    _server_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app",
         "--host", "127.0.0.1", "--port", _DEFAULT_PORT],
        stdout=_log_file, stderr=_log_file,
    )

    if _wait_healthy(client, max_wait):
        return True
    raise RuntimeError(f"Server did not start within {max_wait}s (pid={_server_process.pid})")


# ── Public API ───────────────────────────────────────────────────────────────

def ensure_server_running(base_url: str, max_wait: int = 120) -> bool:
    """Ensure the backend is up: start the Docker backend if down, reuse if up.

    Falls back to a local uvicorn subprocess when Docker is unavailable.
    """
    if _docker_available():
        return _ensure_docker_backend(base_url, max_wait)
    return _ensure_local_backend(base_url, max_wait)


def restart_server(base_url: str = _DEFAULT_URL, max_wait: int = 120) -> bool:
    """Restart the backend."""
    global _server_process, _log_file

    if _docker_available():
        _compose("restart", _COMPOSE_SERVICE)
        client = httpx.Client(base_url=base_url, timeout=2.0)
        if _wait_healthy(client, max_wait):
            return True
        raise RuntimeError(f"Docker backend did not restart within {max_wait}s")

    # Local fallback
    if _server_process:
        _server_process.terminate()
        try:
            _server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_process.kill()
            _server_process.wait(timeout=3)
        _server_process = None
    if _log_file:
        _log_file.close()
        _log_file = None

    return _ensure_local_backend(base_url, max_wait)
