# cli/server.py
# Detect, start, and reuse the AItelier backend.
#
# The backend runs ONLY as a Docker container (docker-compose.yml): the CLI
# reuses the deployment if it is already up, otherwise starts all three
# Compose services through the deployment gate. There is no host-process fallback — running
# uvicorn directly on the host would make DPE git commits use the host
# developer's ~/.gitconfig identity instead of the image's AItelier identity.

import json
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
_COMPOSE_SERVICES = ("zvec-grep", "godot-builder", _COMPOSE_SERVICE)
_COMPOSE_CONTAINERS = {
    "zvec-grep": "aitelier-zg",
    "godot-builder": "aitelier-godot",
    _COMPOSE_SERVICE: "aitelier",
}
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
    in play, and on 2026-08-25 an unguarded direct Compose start
    recreated the container without the gateway network: healthy container,
    localhost still 200, public path gone, nothing said so. The network now
    lives in the base file, selected by name — one file, nothing to forget.
    """
    return ["-f", str(_COMPOSE_FILE)]


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

    The secrets dir is bind-mounted WHOLE (no per-key enumeration since
    2026-08-27), so a missing key file just means "this provider is unused" and
    optional secrets need no placeholder. Only the REQUIRED LLM keys get an
    empty file created — not invented: an empty one would turn a setup mistake
    into an authentication error on the first model call, so it is reported
    here instead, once, with the command that fixes it.

    Never raises: a read-only or unusual HOME must not stop a user who mounts
    their secrets some other way — Docker will report that in its own terms.
    """
    try:
        # The state root, owned by US. Must exist before compose runs.
        (Path(os.environ.get("AITELIER_STATE_DIR")
              or (Path.home() / ".AItelier"))).mkdir(parents=True, exist_ok=True)
        (Path(os.environ.get("AITELIER_STATE_DIR")
              or (Path.home() / ".AItelier")) / "godot-control").mkdir(
                  parents=True, exist_ok=True)
    except OSError:
        pass          # let Docker report it in its own terms
    try:
        env_dir = os.environ.get("AITELIER_SECRETS_DIR")
        if env_dir == "/run/aitelier-secrets":
            # The CONTAINER-side value, copy-pasted to the host (it is visible
            # in compose, docs, and any `docker exec env`). Following it would
            # provision /run/... on the host AND redirect the compose mount
            # source there — silently empty keys. Refuse the footgun — and
            # scrub it from os.environ, because _compose_env() passes the
            # whole environment through and compose's mount source
            # (${AITELIER_SECRETS_DIR:-...}) would otherwise still follow the
            # poisoned value while this message claimed it was ignored.
            print("AITELIER_SECRETS_DIR=/run/aitelier-secrets is the "
                  "CONTAINER-side path; on the host it must point at your "
                  "real secrets dir (default ~/.aitelier-secrets). Ignoring it.")
            os.environ.pop("AITELIER_SECRETS_DIR", None)
            env_dir = None
        d = Path(env_dir or (Path.home() / ".aitelier-secrets"))
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)
        except OSError:
            pass
        from core.external_deps import required_llm_keys
        needed = set(required_llm_keys())
        for name in sorted(needed):
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
    """True if every service in the guarded deployment is up."""
    try:
        res = _compose(
            "ps", "--status", "running", "--services",
            capture_output=True, text=True, timeout=15,
        )
        return set(_COMPOSE_SERVICES).issubset(res.stdout.split())
    except Exception:
        return False


def _compose_ps_rows(text: str) -> list[dict]:
    """Decode the two JSON shapes emitted by supported Compose releases."""
    text = text.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        try:
            value = [json.loads(line) for line in text.splitlines()]
        except (TypeError, json.JSONDecodeError):
            return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(row, dict) for row in value):
        return value
    return []


def _guarded_service_errors() -> list[str]:
    """Return exact container identity/state/health failures for all services."""
    errors = []
    for service in _COMPOSE_SERVICES:
        expected_name = _COMPOSE_CONTAINERS[service]
        try:
            result = _compose(
                "ps", "--all", "--format", "json", service,
                capture_output=True, text=True, timeout=15,
            )
        except Exception as exc:
            errors.append(
                f"{service} readiness inventory failed: {type(exc).__name__}: {exc}")
            continue
        if result.returncode != 0:
            errors.append(
                f"{service} readiness inventory exited {result.returncode}")
            continue
        rows = _compose_ps_rows(result.stdout)
        if len(rows) != 1:
            errors.append(
                f"{service} expected exactly one {expected_name} container; "
                f"observed {len(rows)}")
            continue
        row = rows[0]
        if row.get("Service") != service or row.get("Name") != expected_name:
            errors.append(
                f"{service} identity mismatch: expected {expected_name}, observed "
                f"service={row.get('Service')!r} name={row.get('Name')!r}")
            continue
        state = str(row.get("State", "")).casefold()
        health = str(row.get("Health", "")).casefold()
        if state != "running" or health != "healthy":
            errors.append(
                f"{service} is not ready: state={state or 'missing'} "
                f"health={health or 'missing'}")
    return errors


def _require_guarded_services_ready() -> None:
    errors = _guarded_service_errors()
    if errors:
        raise RuntimeError("guarded deployment is not ready: " + "; ".join(errors))


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
    SOURCE — but its site-packages come from the image. An unguarded Compose start
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



def _warn_if_edge_network_is_alone() -> None:
    """The one silent failure the by-name design knowingly accepts.

    Dropping `external: true` is what lets a clean checkout start, but it also
    means a TYPO'd AITELIER_EDGE_NETWORK is not refused — compose creates a new
    empty network of that name and everything looks fine: container healthy,
    127.0.0.1:4444 answering 200, public path dark. That is the exact shape of
    the 2026-08-25 outage, only reached by a different route.

    The symptom is checkable: for the tunnel to reach us, the connector has to
    be ON that network. If we are the only container on it, there is nothing to
    route from. Warn — never fail — because a connector restarting is a
    legitimate way to be briefly alone, and a start must not hinge on it.
    """
    name = os.environ.get("AITELIER_EDGE_NETWORK", "").strip()
    if not name:
        return                      # unset: the throwaway network is expected
    try:
        res = subprocess.run(
            ["docker", "network", "inspect", name, "-f",
             "{{range $k, $v := .Containers}}{{$v.Name}} {{end}}"],
            capture_output=True, text=True, timeout=10)
        if res.returncode != 0:
            return                  # not there yet; compose will make it
        others = [c for c in res.stdout.split() if c != _COMPOSE_SERVICE]
        if others:
            return
        print(f"Warning: AITELIER_EDGE_NETWORK={name} has no OTHER container "
              f"on it. If the public URL is dark while localhost answers, "
              f"that name matched nothing and compose created an empty network — "
              f"check `docker network ls` for the connector's real name.")
    except Exception:
        pass                        # a diagnostic must never break the start


def _compose_up(max_wait: int = 120):
    """Start (building on first run) the guarded backend and sidecars."""
    _ensure_host_dirs()
    rebuild = []
    if not _image_exists():
        print("Building AItelier image (first run — this may take a few minutes)...")
    elif _image_deps_are_stale():
        print("Dependencies changed since the image was built — rebuilding "
              "(the source is mounted, but its packages are not).")
        rebuild = ["--build"]
    # Inherit stdout/stderr so build + startup progress is visible.
    res = _compose("up", "-d", *rebuild, "--wait", "--wait-timeout",
                   str(max_wait), *_COMPOSE_SERVICES)
    if res.returncode != 0:
        raise RuntimeError(
            "guarded Compose deployment failed (see output above)"
        )
    _warn_if_edge_network_is_alone()


def _require_deployment_clearance(action: str) -> dict:
    """Measure every project and external owner before changing the backend."""
    from api.dependencies import get_db_manager, get_skillflow
    from core import datadir
    from core import deployment_quiescence as dq

    fence = dq.acquire_cutover_fence()
    try:
        try:
            observation = dq.measure(
                skillflow=get_skillflow(),
                db=get_db_manager(),
                sidecar_db=datadir.semantic_index_control_dir() / "control.sqlite3",
            )
        except Exception as exc:  # noqa: BLE001 -- unavailable measurement blocks and persists
            observation = dq.failed_observation(
                f"deployment quiescence measurement could not start: "
                f"{type(exc).__name__}: {exc}")
        override = dq.load_override(os.environ.get("AITELIER_DEPLOY_OVERRIDE_FILE"))
        clearance = dq.authorize(action, observation, override=override)
        clearance["_cutover_fence"] = fence
        return clearance
    except BaseException as exc:
        dq.release_cutover_fence(fence)
        if isinstance(exc, dq.DeploymentBlocked):
            raise RuntimeError(str(exc)) from exc
        raise


def _finish_deployment(clearance: dict, *, success: bool,
                       error: BaseException | None = None) -> dict:
    from core import deployment_quiescence as dq

    # Unit callers may replace the gate with a no-op; a real gate always has
    # an event and therefore always receives a terminal journal record.
    try:
        if not (clearance or {}).get("event"):
            return clearance
        return dq.finalize(clearance, success=success,
                           error=None if error is None else str(error))
    finally:
        dq.release_cutover_fence((clearance or {}).get("_cutover_fence"))


def _ensure_docker_backend(base_url: str, max_wait: int) -> bool:
    client = httpx.Client(base_url=base_url, timeout=2.0)

    # Already running and healthy → reuse it.
    if _container_running() and _is_healthy(client):
        # Reuse is read-only. If exact identity or health cannot be proved,
        # refuse it and let the operator choose the guarded --recreate path.
        return not _guarded_service_errors()

    # A stopped or unhealthy container is a redeploy boundary.  The gate is
    # deliberately after Docker availability checks and before compose changes
    # anything, so an unreadable runtime inventory cannot turn into a replay.
    clearance = _require_deployment_clearance("redeploy")
    try:
        _compose_up(max_wait)
        if not _wait_healthy(client, max_wait):
            raise RuntimeError(
                f"Docker backend did not become healthy within {max_wait}s "
                f"(check: docker compose -f {_COMPOSE_FILE} logs)"
            )
        _require_guarded_services_ready()
    except BaseException as exc:
        _finish_deployment(clearance, success=False, error=exc)
        raise
    _finish_deployment(clearance, success=True)
    return True


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
    """Ensure the Docker backend and sidecars are up through the cutover gate.

    Reuse the deployment if every service is running and the backend is healthy.
    Raises if Docker is unavailable
    — there is no host-process fallback."""
    _require_docker()
    ready = _ensure_docker_backend(base_url, max_wait)
    if not ready:
        raise RuntimeError(
            "refusing to reuse the existing deployment because exact service "
            "identity and health could not be proved; run "
            "`aitelier server --recreate` through the deployment gate")
    return True


def restart_server(base_url: str = _DEFAULT_URL, max_wait: int = 120) -> bool:
    """Recreate the guarded backend and both sidecars."""
    _require_docker()
    clearance = _require_deployment_clearance("restart")
    try:
        restarted = _compose(
            "up", "-d", "--force-recreate", "--wait", "--wait-timeout",
            str(max_wait), *_COMPOSE_SERVICES)
        if restarted.returncode != 0:
            raise RuntimeError(
                "guarded Compose recreation failed with exit "
                f"{restarted.returncode}")
        client = httpx.Client(base_url=base_url, timeout=2.0)
        if not _wait_healthy(client, max_wait):
            raise RuntimeError(f"Docker backend did not restart within {max_wait}s")
        _require_guarded_services_ready()
    except BaseException as exc:
        _finish_deployment(clearance, success=False, error=exc)
        raise
    _finish_deployment(clearance, success=True)
    return True
