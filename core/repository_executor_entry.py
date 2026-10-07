"""Container entry: capture actual command results, with no backend environment."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading


def run(payload):
    repo = payload["repo"]
    env = {"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin",
           "PYTHONPATH": os.pathsep.join([repo, repo + "/src"]),
           "PYTHONDONTWRITEBYTECODE": "1"}
    if payload.get("report_dir"):
        env["GATE_REPORT_DIR"] = payload["report_dir"]
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
    import_error = ""
    if payload.get("import_module"):
        probe = subprocess.run([sys.executable, "-c", "import importlib,sys; importlib.import_module(sys.argv[1])",
                                payload["import_module"]], cwd=repo, env=env,
                               capture_output=True, text=True, timeout=min(60, payload["timeout"]))
        if probe.returncode:
            import_error = f"import {payload['import_module']} failed: {probe.stderr[-600:]}"
    proc = None
    try:
        proc = subprocess.Popen(command, cwd=repo, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=payload["timeout"])
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
        return {"returncode": proc.returncode, "stdout": stdout, "stderr": stderr,
                "timed_out": timed_out, "import_error": import_error}
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
