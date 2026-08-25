# cli/server.py
# Detect, start, and reuse the AItelier backend.
#
# The backend runs ONLY as a Docker container (docker-compose.yml): the CLI
# reuses the container if it is already up, otherwise starts it with
# `docker compose up -d aitelier`. There is no host-process fallback — running
# uvicorn directly on the host would make DPE git commits use the host
# developer's ~/.gitconfig identity instead of the image's AItelier identity.

import os
import re
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
# Secret FILES the compose service mounts. Docker refuses to start a service
# whose secret source is missing — "invalid mount config for type bind: bind
# source path does not exist: …/.aitelier-secrets/GITHUB_TOKEN" — which named
# a path but not that an EMPTY file is the correct content for "I do not use
# this". A clean checkout hit that four times in a row before anything ran.
# Only the LLM key carries meaning; the rest are opt-in integrations whose
# readers already treat empty as "not configured".
# Which key actually matters is DERIVED from the shipped agent_configs (see
# core.external_deps.required_llm_keys) — naming one here would pin a vendor
# into a provider-agnostic system, and it went stale exactly that way once: the
# CLI told a new user to create DEEPSEEK_API_KEY on the same install where the
# README correctly said ARK_API_KEY.
_OPTIONAL_SECRETS = ("GITHUB_TOKEN",)

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


def _compose_files() -> list[str]:
    """The -f arguments for every compose call: the base file, and only it.

    There used to be an opt-in `docker-compose.edge.yml` overlay carrying the
    cloudflared network, added here whenever AITELIER_EDGE_NETWORK was set. That
    made the CLI and a hand-run `docker compose` disagree about which files were
    in play, and on 2026-08-25 a rebuild run as a plain `docker compose up -d`
    recreated the container without the gateway network: healthy container,
    localhost still 200, public path gone, nothing said so. The network now
    lives in the base file, selected by name — one file, nothing to forget.
    """
    return ["-f", str(_COMPOSE_FILE)]


def _mounted_secrets() -> tuple[str, ...]:
    """The secret names docker-compose.yml mounts. Read from the file, so a
    secret added there cannot be silently left unprovisioned here."""
    try:
        import yaml
        data = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8")) or {}
        return tuple(data.get("secrets") or ())
    except Exception:
        return ("DEEPSEEK_API_KEY", "ARK_API_KEY", "GITHUB_TOKEN",
                "LOCAL_QWEN_API_KEY")


def _ensure_host_dirs() -> None:
    """Create what compose BIND-MOUNTS, before Docker does it for us.

    Docker creates a missing bind-mount source itself — as **root**. The
    container runs as the host uid, so an auto-created `~/.AItelier` is
    `root:root` and the very first thing the app does dies with
    `sqlite3.OperationalError: unable to open database file`, then crash-loops.
    Nothing in that message mentions permissions, bind mounts, or the directory.
    It hits every install where `~/.AItelier` does not already exist — i.e. every
    NEW one, which is why it never showed up on a machine that has had it for
    months. Found by actually starting a cold container on a second host.

    Also creates the DIRECTORY and an EMPTY file for each optional secret; every
    reader of these treats empty as "not configured" (the git credential helper
    says so explicitly). The LLM key is not invented: an empty one would turn a
    setup mistake into an authentication error on the first model call, so it is
    reported here instead, once, with the command that fixes it.

    Never raises: a read-only or unusual HOME must not stop a user who mounts
    their secrets some other way — Docker will report that in its own terms.
    """
    try:
        # The state root, owned by US. Must exist before compose runs.
        (Path(os.environ.get("AITELIER_STATE_DIR")
              or (Path.home() / ".AItelier"))).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass          # let Docker report it in its own terms
    try:
        d = Path(os.environ.get("AITELIER_SECRETS_DIR")
                 or (Path.home() / ".aitelier-secrets"))
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:
            pass
        from core.external_deps import required_llm_keys
        needed = set(required_llm_keys())
        # Create EVERY secret compose mounts, needed or not: Docker refuses a
        # missing secret source, so an unused one still has to exist.
        for name in sorted(set(_mounted_secrets()) | needed):
            f = d / name
            if not f.exists():
                f.write_text("", encoding="utf-8")
                try:
                    f.chmod(0o600)
                except OSError:
                    pass
        blank = sorted(k for k in needed if not (d / k).read_text().strip())
        if blank:
            print("No LLM key yet. AItelier will start, but every model call "
                  "will fail until you write one:")
            for k in blank:
                print(f"  printf '%s' \"<your-key>\" > {d / k} "
                      f"&& chmod 600 {d / k}")
    except OSError:
        pass          # let Docker report it in its own terms


def _compose(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run `docker compose -f <file> [-f <overlay>] <args>`."""
    return subprocess.run(
        ["docker", "compose", *_compose_files(), *args],
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


def _image_deps_are_stale() -> bool:
    """True when the image was built BEFORE the current dependency list.

    The repo is bind-mounted at /app, so the container always runs the current
    SOURCE — but its site-packages come from the image. `docker compose up -d`
    happily reuses an existing `aitelier:latest`, so new code meets old
    dependencies and the app dies at import:

        ModuleNotFoundError: No module named 'mcp'

    Observed on a machine whose image predated the `mcp` dependency by five
    days. It is invisible to anyone who habitually runs `up -d --build` — which
    is every developer, and no new user.

    Compares the image's creation time against `pyproject.toml`'s mtime. A git
    checkout stamps mtime at checkout, so pulling a change that touches
    dependencies makes this true; editing anything else does not.
    """
    try:
        res = subprocess.run(
            ["docker", "image", "inspect", "-f", "{{.Created}}", _IMAGE_NAME],
            capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return False
        import datetime as _dt
        raw = res.stdout.strip()
        # Docker emits more precision than fromisoformat accepts pre-3.11-ish.
        raw = re.sub(r"(\.\d{6})\d+", r"\1", raw).replace("Z", "+00:00")
        built = _dt.datetime.fromisoformat(raw).timestamp()
        return (_PROJECT_ROOT / "pyproject.toml").stat().st_mtime > built
    except Exception:
        return False          # never block a start on a freshness heuristic


def _compose_up():
    """Start (building on first run) the backend container."""
    _ensure_host_dirs()
    rebuild = []
    if not _image_exists():
        print("Building AItelier image (first run — this may take a few minutes)...")
    elif _image_deps_are_stale():
        print("Dependencies changed since the image was built — rebuilding "
              "(the source is mounted, but its packages are not).")
        rebuild = ["--build"]
    # Inherit stdout/stderr so build + startup progress is visible.
    res = _compose("up", "-d", *rebuild, _COMPOSE_SERVICE)
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
