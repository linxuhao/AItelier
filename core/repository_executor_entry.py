"""Container entry: capture actual command results, with no backend environment."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import errno
import hashlib
import stat
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import selectors
import time
import uuid


# ---------------------------------------------------------------------------
# Trusted storage observation.
#
# pytest's `tmp_path` machinery (`_pytest.pathlib.make_numbered_dir`) catches
# EVERY exception from its `mkdir` and, after ten tries, raises a bare
# "could not create numbered dir ... after 10 tries" -- the errno is gone. On
# 2026-09-19 a full root filesystem (100%, 349M free) turned 41 tests into such
# errors and the round was sent back to the implementer for code it had not
# broken. The repository's stdout/stderr is the repository's own text and proves
# nothing about the disk, so THIS entry (trusted code, outside the repository)
# asks the filesystem itself: one small mkdir + 4 KiB write in the namespace the
# child's pytest uses for temporary directories, with the errno kept, plus the
# statvfs bytes/inodes and the mount that backs the path. There is no free-space
# threshold anywhere: only an ENOSPC/EDQUOT answer from a real syscall counts.
# ---------------------------------------------------------------------------
STORAGE_SCHEMA = "storage-observation/1"
_STORAGE_FULL = (errno.ENOSPC, errno.EDQUOT)
_STATVFS_FIELDS = ("f_bsize", "f_frsize", "f_blocks", "f_bfree", "f_bavail",
                   "f_files", "f_ffree", "f_favail")


def _child_tmp(env):
    """The directory the CHILD's pytest uses for tmp_path (its own env)."""
    return env.get("TMPDIR") or "/tmp"


def _attempt(call):
    try:
        call()
    except OSError as exc:
        return {"ok": False, "errno": exc.errno,
                "errname": errno.errorcode.get(exc.errno, ""), "error": str(exc)[:200]}
    return {"ok": True, "errno": None}


def _mount_of(path):
    """The mount backing `path`, from the child's own /proc (best effort)."""
    try:
        real = os.path.realpath(path)
        best = None
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                left, _, right = line.partition(" - ")
                before, after = left.split(), right.split()
                if len(before) < 5 or len(after) < 2:
                    continue
                point = before[4].replace("\\040", " ")
                if real == point or real.startswith(point.rstrip("/") + "/"):
                    if best is None or len(point) >= len(best["mount_point"]):
                        best = {"mount_point": point, "fstype": after[0], "source": after[1]}
        return best
    except OSError:
        return None


def _probe_dir(path):
    """Observe `path`: statvfs, backing mount, and a real mkdir + small write."""
    obs = {"path": path}
    try:
        info = os.statvfs(path)
        obs["statvfs"] = {name: getattr(info, name) for name in _STATVFS_FIELDS}
    except OSError as exc:
        obs["statvfs"] = {"errno": exc.errno, "errname": errno.errorcode.get(exc.errno, "")}
    try:
        obs["st_dev"] = os.stat(path).st_dev
    except OSError:
        pass
    obs["mount"] = _mount_of(path)
    probe = os.path.join(path, ".aitelier-storage-probe-" + uuid.uuid4().hex)
    canary = os.path.join(probe, "canary")
    obs["mkdir"] = _attempt(lambda: os.mkdir(probe, 0o700))
    if not obs["mkdir"]["ok"]:
        obs["write"] = {"ok": False, "errno": None, "skipped": "mkdir failed"}
        return obs

    def write():
        descriptor = os.open(canary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, b"\0" * 4096)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    obs["write"] = _attempt(write)
    leftovers = []
    for remove, target in ((os.unlink, canary), (os.rmdir, probe)):  # only our own names
        try:
            remove(target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            leftovers.append({"path": target, "errno": exc.errno})
    if leftovers:
        obs["cleanup_errors"] = leftovers
    return obs


def _is_full(observations):
    for obs in observations:
        for name in ("mkdir", "write"):
            if (obs.get(name) or {}).get("errno") in _STORAGE_FULL:
                return obs, name
    return None


def _storage_roots(env, command):
    """Every directory this pytest leg must be able to write, deduplicated.

    The child's tmp namespace, plus the directory of the junit record the
    backend asked for (`--junitxml=`, an argument the TRUSTED caller built;
    it is the mounted output root pytest writes its result record into).
    """
    roots = [_child_tmp(env)]
    for arg in command:
        if isinstance(arg, str) and arg.startswith("--junitxml="):
            roots.append(os.path.dirname(arg.split("=", 1)[1]) or ".")
    seen, unique = set(), []
    for root in roots:
        key = os.path.realpath(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _coding_shell(payload, env):
    """Bound retained bytes while draining arbitrary shell output."""
    limit = 12_000
    output = {"stdout": bytearray(), "stderr": bytearray()}
    kept, truncated, timed_out = 0, False, False
    deadline = time.monotonic() + payload["timeout"]
    proc = subprocess.Popen(payload["args"], cwd=payload["repo"], env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True, close_fds=True)

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
            selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    kill()
                    break
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    take = chunk[:max(0, limit - kept)]
                    output[key.data].extend(take)
                    kept += len(take)
                    truncated = truncated or len(take) < len(chunk)
        try:
            proc.wait(timeout=max(.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            timed_out = True
            kill()
            proc.wait(timeout=2)
        rendered, remaining = {}, limit
        for key, value in output.items():
            encoded = bytes(value).decode("utf-8", errors="replace").encode("utf-8")
            rendered[key] = encoded[:remaining].decode("utf-8", errors="ignore")
            truncated = truncated or len(encoded) > remaining
            remaining -= len(rendered[key].encode("utf-8"))
        return {"returncode": proc.returncode, **rendered,
                "timed_out": timed_out, "output_truncated": truncated, "import_error": ""}
    finally:
        kill()
        proc.stdout.close()
        proc.stderr.close()


def run(payload):
    repo = payload["repo"]
    env = {"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin",
           "PYTHONPATH": os.pathsep.join([repo, repo + "/src"]),
           "PYTHONDONTWRITEBYTECODE": "1"}
    if payload.get("profile") == "coding_shell":
        return _coding_shell(payload, env)
    if payload.get("report_dir"):
        env["GATE_REPORT_DIR"] = payload["report_dir"]
    if payload.get("npm_cache"):
        # Node profiles: the pinned npm first on PATH (never the image's own
        # npm), the ticket's own cache; HOME stays /tmp in the container.
        env["PATH"] = payload["node_bin"] + ":" + env["PATH"]
        env["npm_config_cache"] = payload["npm_cache"]
        env["npm_config_update_notifier"] = "false"
        try:
            found = subprocess.run(["npm", "--version"], cwd=repo, env=env, capture_output=True,
                                   text=True, timeout=60).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            found = f"none ({type(exc).__name__})"
        if found != payload["expect_npm"]:
            return {"returncode": -1, "stdout": "", "stderr": "", "timed_out": False,
                    "import_error": "", "unavailable":
                    f"pinned npm {payload['expect_npm']} not in this image (found {found[:80]})"}
    stdin_data = None
    if payload.get("lint_input"):
        try:
            for command, expected in ((["/opt/aitelier-node/bin/node", "--version"], "v24.22.0"),
                                      (["/opt/aitelier-node/bin/node",
                                        "/opt/aitelier-eslint/node_modules/eslint/bin/eslint.js",
                                        "--version"], "v10.12.0")):
                found = subprocess.run(command, cwd="/tmp", env=env, capture_output=True,
                                       text=True, timeout=min(10, payload["timeout"]))
                if found.returncode or found.stdout.strip() != expected:
                    raise ValueError(f"pinned lint runtime {expected} is unavailable")
            descriptor = os.open(payload["lint_input"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("lint input must be a single-link regular file")
                stdin_data = stream.read((8 << 20) + 1)
            if len(stdin_data) > (8 << 20) or hashlib.sha256(stdin_data).hexdigest() != payload["input_sha256"]:
                raise ValueError("lint input changed after validation")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return {"returncode": -1, "stdout": "", "stderr": "", "timed_out": False,
                    "import_error": "", "unavailable": f"JavaScript lint unavailable: {exc}"}
    # A pytest leg observes its own temp namespace BEFORE repository code runs:
    # a namespace the filesystem itself refuses (ENOSPC/EDQUOT) is not worth an
    # hour of tests that cannot create tmp_path. Nothing here depends on the
    # repository's text or on a size watermark.
    storage = None
    if payload.get("pytest_timeout") and not payload.get("profile"):
        roots = _storage_roots(env, payload["args"])
        storage = {"schema": STORAGE_SCHEMA, "tmp_root": _child_tmp(env),
                   "roots": roots, "before": [_probe_dir(r) for r in roots]}
        full = _is_full(storage["before"])
        if full:
            storage["preflight_refused"] = True
            obs, name = full
            return {"returncode": -1, "stdout": "", "timed_out": False, "import_error": "",
                    "stderr": (f"storage preflight refused: {obs[name]['errname']} on {name} in "
                               f"{obs['path']}; no repository code was executed"),
                    "storage": storage}
    relay = None
    if payload.get("relay_socket"):
        class Handler(BaseHTTPRequestHandler):
            def forward(self):
                # Admission sends interim HTTP responses while queued. Parsing
                # them through HTTPConnection would swallow those keepalives,
                # and reading a complete body would reset the gate's timeout.
                # Forward the existing HTTP wire, including each interim reply.
                self.close_connection = True
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                    upstream.settimeout(payload["timeout"])
                    upstream.connect(payload["relay_socket"])
                    length = int(self.headers.get("Content-Length", "0"))
                    request = (f"{self.command} {self.path} {self.request_version}\r\n"
                               + "".join(f"{key}: {value}\r\n" for key, value in self.headers.items()) + "\r\n")
                    upstream.sendall(request.encode("iso-8859-1") + self.rfile.read(length))
                    # A TCP read EOF alone cannot distinguish a full client
                    # close from a supported write half-close (RFC 9112 9.6).
                    # A readable / EOF client socket therefore must NOT be read
                    # as an abandoned request: keep forwarding the queued
                    # response and end only on an actual client-bound write
                    # failure, upstream EOF, or the bounded upstream timeout.
                    client_write_open = True
                    while True:
                        watch = [upstream, self.connection] if client_write_open else [upstream]
                        ready, _, _ = select.select(watch, [], [], payload["timeout"])
                        if not ready:
                            raise TimeoutError("admission relay response timed out")
                        if client_write_open and self.connection in ready:
                            client_write_open = False
                            continue
                        chunk = upstream.recv(65536)
                        if not chunk:
                            return
                        try:
                            self.connection.sendall(chunk)
                        except OSError:
                            # A real write failure means the client is gone,
                            # unlike a supported write half-close.
                            return

            do_GET = do_POST = forward
            def log_message(self, *_args):
                pass
        relay = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        relay.daemon_threads = True
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        env["GODOT_BUILDER_URL"] = f"http://127.0.0.1:{relay.server_port}"
    command = list(payload["args"])
    if payload.get("pytest_timeout") and importlib.util.find_spec("pytest_timeout"):
        command += ["--timeout=120", "--timeout-method=thread"]
    execution_identity = None
    if payload.get("pytest_timeout") and not payload.get("profile"):
        try:
            from importlib import metadata
            from pathlib import Path
            distributions = sorted([d.metadata["Name"], d.version]
                                   for d in metadata.distributions())
            execution_identity = {
                "schema": "pytest-execution/1", "effective_argv": command,
                "entry_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "runtime": {"python_executable": sys.executable, "python_version": sys.version},
                "distributions": distributions,
                "distributions_digest": hashlib.sha256(json.dumps(
                    distributions, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "env": dict(env)}
        except Exception:
            pass  # Optional diagnostics must never prevent the ordinary gate.
    import_error = ""
    if payload.get("import_module"):
        probe = subprocess.run([sys.executable, "-c", "import importlib,sys; importlib.import_module(sys.argv[1])",
                                payload["import_module"]], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=min(60, payload["timeout"]))
        if probe.returncode:
            import_error = f"import {payload['import_module']} failed: {probe.stderr[-600:]}"
    proc = None
    try:
        proc = subprocess.Popen(command, cwd=repo, env=env, text=stdin_data is None,
                                stdin=subprocess.PIPE if stdin_data is not None else None,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            if stdin_data is None:
                stdout, stderr = proc.communicate(timeout=payload["timeout"])
            else:
                stdout, stderr = proc.communicate(input=stdin_data, timeout=payload["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        if isinstance(stdout, bytes):
            stdout, stderr = stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        if storage is not None and proc.returncode != 0:
            # What the namespace looks like AFTER a non-clean session: pytest has
            # swallowed the errno by now, the filesystem has not.
            storage["after"] = [_probe_dir(r) for r in storage["roots"]]
        return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr,
                "timed_out": timed_out, "import_error": import_error,
                **({"storage": storage} if storage is not None else {}),
                **({"execution_identity": execution_identity} if execution_identity else {})}
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if relay:
            relay.shutdown()
            relay.server_close()

if __name__ == "__main__":
    print(json.dumps(run(json.loads(sys.argv[1]))))
