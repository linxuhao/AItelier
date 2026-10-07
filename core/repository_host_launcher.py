"""Trusted host-side CPU container launcher for repository test gates.

The production backend must not hold the Docker CLI or Docker socket. This
module is the only supported *product* launch surface: an operator-enabled
host service that owns the single Docker invocation for repository test
gates, reached by the backend over a least-privilege local Unix socket
(filesystem mode 0600).

Every request is validated against operator-owned configuration before any
Docker argv is built:

- ``repo`` must canonically resolve (``realpath``) inside one of the
  configured ``allowed_roots``; symlink escapes are refused.
- ``writable_dirs`` / ``report_dir`` / ``relay_socket`` must resolve inside
  the repo or a configured ``report_roots`` entry — no arbitrary mounts.
- ``image`` is never taken from the request; the reviewed, immutable test
  image and the trusted executor entry come from host configuration only.
- repository command forms are allow-listed: pytest runs, the import-smoke
  probe, and the authored gate script. No daemon flags, no raw Docker argv,
  no shell metacharacters.

Cleanup touches only the UUID-named container this invocation created.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import uuid


MAX_REQUEST_BYTES = 1 << 20


class LaunchRefused(ValueError):
    """Request rejected by the host policy; never a Docker argv."""


def load_config(path: Path) -> dict:
    """Load and shape-check the operator-owned host launcher configuration."""
    cfg = json.loads(Path(path).read_text())
    for key in ("allowed_roots", "report_roots", "test_image", "executor_entry"):
        if key not in cfg:
            raise LaunchRefused(f"host launcher config missing {key!r}")
    if not isinstance(cfg["allowed_roots"], list) or not cfg["allowed_roots"]:
        raise LaunchRefused("allowed_roots must be a non-empty list")
    if not isinstance(cfg["report_roots"], list):
        raise LaunchRefused("report_roots must be a list")
    image = str(cfg["test_image"])
    if not image or any(c in image for c in " \t\n;|&`$"):
        raise LaunchRefused("test_image must be a single reviewed image reference")
    uids = cfg.get("allowed_uids", [])
    if not isinstance(uids, list) or any(not isinstance(u, int) for u in uids):
        raise LaunchRefused("allowed_uids must be a list of integers")
    return cfg


def _resolve_inside(path: str, roots: list[str], what: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise LaunchRefused(f"{what} does not resolve: {path}") from exc
    for root in roots:
        root_path = Path(root).resolve(strict=True)
        if resolved == root_path or root_path in resolved.parents:
            return resolved
    raise LaunchRefused(f"{what} escapes the operator-allowed roots: {path}")


_FORBIDDEN_CHARS = set("\0\n\r;|&`$><\\\"'")



def _check_token(token: str) -> str:
    if not isinstance(token, str) or not token:
        raise LaunchRefused("command tokens must be non-empty strings")
    if _FORBIDDEN_CHARS & set(token):
        raise LaunchRefused(f"command token contains a refused character: {token!r}")
    return token


def validate_request(cfg: dict, request: dict) -> dict:
    """Validate a launch request against host policy; return normalized fields."""
    if not isinstance(request, dict) or request.get("op") != "launch":
        raise LaunchRefused("unsupported operation")
    repo = _resolve_inside(str(request.get("repo", "")), cfg["allowed_roots"], "repo")
    if not repo.is_dir():
        raise LaunchRefused("repo is not a directory")

    report_roots = list(cfg["report_roots"])
    writable = []
    for directory in request.get("writable_dirs") or ():
        writable.append(str(_resolve_inside(str(directory),
                                           cfg["allowed_roots"] + report_roots,
                                           "writable_dir")))

    report_dir = ""
    if request.get("report_dir"):
        report_dir = str(_resolve_inside(str(request["report_dir"]),
                                        cfg["allowed_roots"] + report_roots,
                                        "report_dir"))

    relay_socket = ""
    if request.get("relay_socket"):
        relay = _resolve_inside(str(request["relay_socket"]),
                                cfg["allowed_roots"] + report_roots,
                                "relay_socket")
        if not relay.exists():
            raise LaunchRefused("relay_socket must already exist (created by the backend)")
        relay_socket = str(relay)

    raw_args = request.get("args")
    if not isinstance(raw_args, list) or not raw_args:
        raise LaunchRefused("args must be a non-empty list")
    args = [_check_token(a) for a in raw_args]
    _validate_command_form(repo, args, request)

    try:
        timeout = int(request.get("timeout", 0))
    except (TypeError, ValueError) as exc:
        raise LaunchRefused("timeout must be an integer") from exc
    if not 1 <= timeout <= 7200:
        raise LaunchRefused("timeout out of range")
    return {"repo": str(repo), "args": args, "timeout": timeout,
            "writable_dirs": writable, "relay_socket": relay_socket,
            "import_module": str(request.get("import_module", "")),
            "pytest_timeout": bool(request.get("pytest_timeout", False)),
            "report_dir": report_dir}


def _validate_command_form(repo: Path, args: list[str], request: dict) -> None:
    first = args[0]
    if first not in ("python", "python3", "bash"):
        raise LaunchRefused(f"command entrypoint not allow-listed: {first!r}")
    if first == "bash":
        # Authored gate script only; it must canonically live inside the repo
        # (absolute or repo-relative are both accepted, the repo is mounted at
        # the same path inside the container).
        if len(args) < 2:
            raise LaunchRefused("bash form requires the authored gate script path")
        raw = args[1]
        _resolve_inside(str(raw if os.path.isabs(raw) else repo / raw),
                        [str(repo)], "gate script")
        return
    # The import-smoke probe runs inside the container entry via the
    # import_module field; there is no client-supplied ``python -c`` form.
    if len(args) >= 3 and args[1] == "-m" and args[2] == "pytest":
        return
    raise LaunchRefused("repository command form not allow-listed (pytest/import-smoke/gate only)")


def docker_command(cfg: dict, fields: dict, name: str) -> list[str]:
    """Build the one and only Docker argv the host service will run."""
    docker = shutil.which("docker")
    if not docker:
        raise LaunchRefused("Docker execution facility is unavailable on the host")
    repo = fields["repo"]
    entry = str(cfg["executor_entry"])
    command = [docker, "run", "--rm", "--init", "--network", "none",
               "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", f"{os.getuid()}:{os.getgid()}", "--name", name,
               "-v", f"{repo}:{repo}:ro", "-v", f"{entry}:/executor.py:ro",
               "-w", repo]
    for directory in fields["writable_dirs"]:
        command += ["-v", f"{directory}:{directory}:rw"]
    if fields["relay_socket"]:
        command += ["-v", f"{fields['relay_socket']}:{fields['relay_socket']}:ro"]
    payload = {"args": fields["args"], "timeout": fields["timeout"], "repo": repo,
               "relay_socket": fields["relay_socket"],
               "import_module": fields["import_module"],
               "pytest_timeout": fields["pytest_timeout"],
               "report_dir": fields["report_dir"]}
    command += ["--entrypoint", "python3", str(cfg["test_image"]),
                "/executor.py", json.dumps(payload)]
    return command


def _run_docker(cfg: dict, fields: dict) -> dict:
    name = "aitelier-cpu-" + uuid.uuid4().hex
    command = docker_command(cfg, fields, name)
    proc = None
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=fields["timeout"] + 15)
        except subprocess.TimeoutExpired:
            raise LaunchRefused("host launcher timed out waiting for the container")
        if proc.returncode != 0:
            raise LaunchRefused(f"isolated container exited {proc.returncode}: {stderr[-2000:]}")
        try:
            return json.loads(stdout)
        except ValueError as exc:
            raise LaunchRefused("isolated executor returned no result") from exc
    finally:
        # Only this invocation's UUID-named container is touched; peers survive.
        try:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def handle_request(cfg: dict, raw: bytes) -> dict:
    """Validate and execute one request; return the JSON response object."""
    if len(raw) > MAX_REQUEST_BYTES:
        return {"ok": False, "error": "request too large"}
    try:
        request = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "request is not valid JSON"}
    try:
        fields = validate_request(cfg, request)
        result = _run_docker(cfg, fields)
    except LaunchRefused as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result": result}


def _serve_connection(conn: socket.socket, cfg: dict, lock: threading.Semaphore) -> None:
    try:
        peer = _peer_uid(conn)
        if cfg.get("allowed_uids") and peer not in cfg["allowed_uids"]:
            conn.sendall(json.dumps(
                {"ok": False, "error": f"peer uid {peer} not operator-allowed"}).encode() + b"\n")
            return
        conn.settimeout(7200 + 60)
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk or sum(len(c) for c in chunks) > MAX_REQUEST_BYTES:
                break
        with lock:
            response = handle_request(cfg, b"".join(chunks).split(b"\n")[0])
        conn.sendall(json.dumps(response).encode() + b"\n")
    except OSError:
        # Client disconnect before the reply: the container cleanup in
        # _run_docker has already run; nothing of ours is left behind.
        pass
    finally:
        conn.close()


def _peer_uid(conn: socket.socket) -> int:
    import struct
    size = struct.calcsize("3i")
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    if len(creds) != size:
        return -1
    return struct.unpack("3i", creds)[0]


def serve(socket_path: Path, config_path: Path) -> None:  # pragma: no cover - operator entry
    """Operator entry: bind the 0600 Unix socket and serve launch requests."""
    cfg = load_config(config_path)
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        socket_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        # Least-privilege exposure: owner-only connect, regardless of umask.
        os.chmod(socket_path, 0o600)
        server.listen(16)
        lock = threading.Semaphore(4)  # global bound of four CPU test containers
        while True:
            conn, _ = server.accept()
            threading.Thread(target=_serve_connection, args=(conn, cfg, lock),
                             daemon=True).start()
    finally:
        server.close()


if __name__ == "__main__":  # pragma: no cover - operator entry
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    serve(args.socket, args.config)
