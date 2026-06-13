# cli/server.py
# Auto-detect, start, and restart the AItelier backend server.
# Detects code changes via hash and forces restart when stale.

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

# Load .env before spawning server subprocess so it inherits API keys
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

_server_process = None
_log_file = None

_LOG_DIR = Path.home() / ".AItelier"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_SERVER_LOG = _LOG_DIR / "server.log"

# Track code hash to detect stale servers
_HASH_FILE = _LOG_DIR / "server_code_hash"
_WATCHED_DIRS = ["api", "core"]


def _compute_code_hash() -> str:
    """Hash key source files to detect code changes."""
    h = hashlib.md5()
    project_root = Path(__file__).resolve().parent.parent
    for dirname in _WATCHED_DIRS:
        d = project_root / dirname
        if not d.is_dir():
            continue
        for py in sorted(d.rglob("*.py")):
            try:
                h.update(str(py.relative_to(project_root)).encode())
                h.update(py.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


def _find_server_pid(port: str) -> int | None:
    """Find the PID of the process listening on the given port."""
    try:
        result = subprocess.run(
            ["ss", "-tlnp"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if f":{port}" in line:
                import re
                m = re.search(r'pid=(\d+)', line)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    return None


def _is_server_stale() -> bool:
    """Check if the running server was started with different code."""
    current_hash = _compute_code_hash()
    if not _HASH_FILE.exists():
        # No previous hash — save current and assume not stale
        _HASH_FILE.write_text(current_hash)
        return False
    saved_hash = _HASH_FILE.read_text().strip()
    if saved_hash != current_hash:
        return True
    return False


def ensure_server_running(base_url: str, max_wait: int = 30) -> bool:
    """Check if the server is running and healthy; if not, start/restart it."""
    global _server_process
    client = httpx.Client(base_url=base_url, timeout=2.0)

    # Check if running server is stale (code changed since last start)
    stale = _is_server_stale()

    # Quick health check
    healthy = False
    try:
        resp = client.get("/health")
        if resp.status_code == 200:
            try:
                api_resp = client.get("/api/projects", timeout=5.0)
                if api_resp.status_code < 500:
                    healthy = True
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
    except (httpx.ConnectError, httpx.TimeoutException):
        pass

    # Kill stale server even if healthy
    if healthy and not stale:
        return True

    if healthy and stale:
        # Server is running but with old code — kill it
        print("Server code changed — restarting with latest code...")
        stale_pid = _find_server_pid(_DEFAULT_PORT)
        if stale_pid:
            os.kill(stale_pid, 9)
            time.sleep(0.5)
    elif not healthy:
        # Kill any stale process on this port
        stale_pid = _find_server_pid(_DEFAULT_PORT)
        if stale_pid:
            os.kill(stale_pid, 9)
            time.sleep(0.5)

    # Save current code hash before starting
    _HASH_FILE.write_text(_compute_code_hash())

    # Start server as a subprocess, logging to file
    global _log_file
    _log_file = open(_SERVER_LOG, "w", encoding="utf-8")
    _server_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api.main:app",
         "--host", "127.0.0.1", "--port", _DEFAULT_PORT],
        stdout=_log_file,
        stderr=_log_file,
    )

    # Wait for health check
    for _ in range(max_wait * 2):
        time.sleep(0.5)
        try:
            resp = client.get("/health")
            if resp.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.TimeoutException):
            continue

    raise RuntimeError(f"Server did not start within {max_wait}s (pid={_server_process.pid})")


def restart_server(base_url: str = _DEFAULT_URL, max_wait: int = 30) -> bool:
    """Kill the running server and start a fresh one."""
    global _server_process, _log_file

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

    return ensure_server_running(base_url, max_wait)
