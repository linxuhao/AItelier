"""Container entry: capture actual command results, with no backend environment."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
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
        return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr,
                "timed_out": timed_out, "import_error": import_error,
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
