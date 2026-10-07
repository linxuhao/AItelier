"""Operator-owned host CPU launcher, reached only by the trusted backend UID.

Requests name an existing run. The host reads its run_isolation record from
operator-configured existing State storage (read-only, no runtime/schema init)
and permits only that exact repository plus that run's report tickets. Socket
permissions/UID trust the backend, not arbitrary untrusted same-UID programs;
repository containers receive neither the launcher nor Docker socket.
"""
from __future__ import annotations
from contextlib import closing

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import sqlite3
import stat
import socket
import subprocess
import threading
import time
from urllib.parse import quote
import uuid


MAX_REQUEST_BYTES = 1 << 20


class LaunchRefused(ValueError):
    """Request rejected by the host policy; never a Docker argv."""


def load_config(path: Path) -> dict:
    """Operator inputs only; a mutable image tag cannot be a reviewed pin."""
    cfg = json.loads(Path(path).read_text())
    _validate_config(cfg)
    return cfg


def _validate_config(cfg: dict) -> None:
    if not isinstance(cfg, dict):
        raise LaunchRefused("host config must be an object")
    roots = cfg.get("allowed_roots")
    if not isinstance(roots, list) or not roots or any(
            not isinstance(p, str) or not Path(p).is_absolute() for p in roots):
        raise LaunchRefused("allowed_roots must name absolute operator roots")
    image = cfg.get("test_image")
    if not isinstance(image, str) or not re.fullmatch(
            r"(?:[A-Za-z0-9._:/-]+@)?sha256:[0-9a-f]{64}", image):
        raise LaunchRefused("test_image must be an immutable sha256 digest")
    for key in ("executor_entry", "state_db", "aitelier_home"):
        value = cfg.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).exists():
            raise LaunchRefused(f"host config requires existing absolute {key}")
    if not Path(cfg["executor_entry"]).is_file() or not Path(cfg["state_db"]).is_file():
        raise LaunchRefused("executor_entry/state_db must be regular files")
    if not Path(cfg["aitelier_home"]).is_dir():
        raise LaunchRefused("aitelier_home must be a directory")
    uids = cfg.get("allowed_uids")
    if not isinstance(uids, list) or not uids or any(type(u) is not int or u < 0 for u in uids):
        raise LaunchRefused("allowed_uids must explicitly name trusted backend UIDs")


def _run_binding(cfg: dict, run_id: str) -> tuple[Path, Path, str]:
    if not isinstance(run_id, str) or not run_id:
        raise LaunchRefused("existing normal run_id is required")
    uri = "file:" + quote(str(Path(cfg["state_db"]).resolve()), safe="/") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT mode,worktree_path,source_repo,project_id "
                               "FROM run_isolation WHERE run_id=?", (run_id,)).fetchone()
    except sqlite3.Error as exc:
        raise LaunchRefused("existing run ownership could not be read") from exc
    if row is None or row["mode"] not in {"worktree", "direct", "read_snapshot"}:
        raise LaunchRefused("run has no supported existing repository ownership")
    source = row["source_repo"] if row["mode"] == "direct" else row["worktree_path"]
    repo = _resolve_inside(source or "", cfg["allowed_roots"], "owned repository")
    report_root = (Path(cfg["aitelier_home"]).resolve() / "gate-reports" /
                   hashlib.sha256(run_id.encode()).hexdigest())
    return repo, report_root, row["project_id"]


def _resolve_inside(path: str, roots: list[str], what: str) -> Path:
    if not isinstance(path, str) or not path:
        raise LaunchRefused(f"{what} must be an existing path")
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, ValueError) as exc:
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
    """Use existing run ownership, never broad roots as write authority."""
    _validate_config(cfg)
    if not isinstance(request, dict) or request.get("op") != "launch":
        raise LaunchRefused("unsupported operation")
    allowed = {"op", "repo", "run_id", "args", "timeout", "writable_dirs",
               "report_dir", "relay_socket", "import_module", "pytest_timeout"}
    if set(request) - allowed:
        raise LaunchRefused("unsupported request fields; mounts/image come from host policy")
    owned_repo, report_root, project_id = _run_binding(cfg, request.get("run_id"))
    repo = _resolve_inside(request.get("repo", ""), cfg["allowed_roots"], "repo")
    if repo != owned_repo or not repo.is_dir():
        raise LaunchRefused("repo does not match the existing run's owned repository")
    directories = request.get("writable_dirs", [])
    if not isinstance(directories, list) or len(directories) > 1:
        raise LaunchRefused("only one owned report ticket may be writable")
    writable = []
    for directory in directories:
        if not isinstance(directory, str):
            raise LaunchRefused("writable_dir must be a path")
        path = _resolve_inside(directory, [str(report_root)], "owned report ticket")
        if path == report_root or path.parent != report_root or not path.is_dir():
            raise LaunchRefused("writable_dir must be one direct run-owned report ticket")
        if path == repo or path in repo.parents or repo in path.parents:
            raise LaunchRefused("a writable mount must not overlap the read-only repository")
        if path != Path(directory).absolute():
            raise LaunchRefused("report ticket aliases/symlinks are refused")
        writable.append(str(path))
    report_dir = request.get("report_dir", "")
    if not isinstance(report_dir, str) or report_dir and report_dir not in writable:
        raise LaunchRefused("report_dir must be the one run-owned writable ticket")
    relay_socket = request.get("relay_socket", "")
    if relay_socket:
        if not writable or not isinstance(relay_socket, str):
            raise LaunchRefused("relay requires the run-owned report ticket")
        relay = _resolve_inside(relay_socket, writable, "owned relay socket")
        if relay.parent != Path(writable[0]) or relay != Path(relay_socket).absolute() or not stat.S_ISSOCK(relay.stat().st_mode):
            raise LaunchRefused("relay must be a real socket in this report ticket")
        relay_socket = str(relay)
    raw_args = request.get("args")
    if not isinstance(raw_args, list) or not raw_args:
        raise LaunchRefused("args must be a non-empty list")
    args = [_check_token(a) for a in raw_args]
    _validate_command_form(repo, args, request)
    timeout = request.get("timeout")
    if type(timeout) is not int or not 1 <= timeout <= 7200:
        raise LaunchRefused("timeout must be an integer within 1..7200")
    module = request.get("import_module", "")
    if not isinstance(module, str) or module and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", module):
        raise LaunchRefused("import_module must be a dotted module name")
    return {"repo": str(repo), "args": args, "timeout": timeout,
            "writable_dirs": writable, "relay_socket": relay_socket,
            "import_module": module, "pytest_timeout": request.get("pytest_timeout") is True,
            "report_dir": report_dir, "run_id": request["run_id"], "project_id": project_id}


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
    _validate_config(cfg)
    docker = shutil.which("docker")
    if not docker:
        raise LaunchRefused("Docker execution facility is unavailable on the host")
    repo = fields["repo"]
    entry = str(cfg["executor_entry"])
    command = [docker, "run", "--rm", "--init", "--network", "none",
               "--cpus", "2", "--memory", "2g", "--pids-limit", "512",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", f"{os.getuid()}:{os.getgid()}", "--name", name,
               "--label", "aitelier.run_id=" + fields["run_id"],
               "--label", "aitelier.project_id=" + fields["project_id"],
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


def _run_docker(cfg: dict, fields: dict, conn: socket.socket | None = None) -> dict:
    name = "aitelier-cpu-" + uuid.uuid4().hex
    command = docker_command(cfg, fields, name)
    proc = None
    try:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        output = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + fields["timeout"] + 15
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
            selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
            if conn is not None:
                selector.register(conn, selectors.EVENT_READ, "client")
            pipes = 2
            while pipes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LaunchRefused("host launcher timed out waiting for the container")
                ready = selector.select(remaining)
                if not ready:
                    raise LaunchRefused("host launcher timed out waiting for the container")
                for key, _ in ready:
                    if key.data == "client":
                        # Protocol is one complete request followed by a reply;
                        # EOF/write-half-close is cancellation, not a second RPC.
                        if not conn.recv(1, socket.MSG_PEEK):
                            raise LaunchRefused("client disconnected during owned execution")
                        raise LaunchRefused("unexpected data after launch request")
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        pipes -= 1
                    else:
                        output[key.data].extend(chunk)
        proc.wait(timeout=max(.1, deadline - time.monotonic()))
        stderr = output["stderr"].decode(errors="replace")
        if proc.returncode != 0:
            raise LaunchRefused(f"isolated container exited {proc.returncode}: {stderr[-2000:]}")
        try:
            result = json.loads(output["stdout"])
        except ValueError as exc:
            raise LaunchRefused("isolated executor returned no result") from exc
        if not isinstance(result, dict) or type(result.get("returncode")) is not int or not all(
                isinstance(result.get(k), str) for k in ("stdout", "stderr")):
            raise LaunchRefused("isolated executor returned a malformed result")
        return result
    finally:
        try:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            if proc is not None:
                proc.stdout.close()
                proc.stderr.close()


def handle_request(cfg: dict, raw: bytes, conn: socket.socket | None = None) -> dict:
    """Validate and execute one request; return the JSON response object."""
    if len(raw) > MAX_REQUEST_BYTES:
        return {"ok": False, "error": "request too large"}
    try:
        request = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "request is not valid JSON"}
    from core.repository_executor import _slot, IsolationUnavailable
    try:
        fields = validate_request(cfg, request)
        with _slot(Path(cfg["aitelier_home"]) / "cpu-test-slots"):
            result = _run_docker(cfg, fields, conn)
    except (LaunchRefused, IsolationUnavailable, OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result": result}


def _serve_connection(conn: socket.socket, cfg: dict, lock: threading.Semaphore) -> None:
    try:
        peer = _peer_uid(conn)
        if peer not in cfg["allowed_uids"]:
            conn.sendall(json.dumps(
                {"ok": False, "error": f"peer uid {peer} not operator-allowed"}).encode() + b"\n")
            return
        conn.settimeout(10)
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk or sum(len(c) for c in chunks) > MAX_REQUEST_BYTES:
                break
        response = handle_request(cfg, b"".join(chunks).split(b"\n")[0], conn)
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
    return struct.unpack("3i", creds)[1]


def serve(socket_path: Path, config_path: Path) -> None:  # pragma: no cover - operator entry
    """Operator entry: bind the 0600 Unix socket and serve launch requests."""
    cfg = load_config(config_path)
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists() or socket_path.is_symlink():
        raise LaunchRefused("launcher socket already exists; never replace a live listener")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(socket_path))
        # Least-privilege exposure: owner-only connect, regardless of umask.
        os.chmod(socket_path, 0o600)
        server.listen(16)
        lock = threading.BoundedSemaphore(4)  # bounds request threads, not global CPU ownership
        while True:
            conn, _ = server.accept()
            if not lock.acquire(blocking=False):
                conn.close()  # bounded admission; never accumulate waiting threads
                continue
            def owned(connection=conn):
                try:
                    _serve_connection(connection, cfg, lock)
                finally:
                    lock.release()
            threading.Thread(target=owned, daemon=True).start()
    finally:
        server.close()


if __name__ == "__main__":  # pragma: no cover - operator entry
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    serve(args.socket, args.config)
