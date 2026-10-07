"""Container entry: capture actual command results, with no backend environment."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
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
        class Connection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(payload["timeout"])
                self.sock.connect(payload["relay_socket"])
        class Handler(BaseHTTPRequestHandler):
            def forward(self):
                connection = Connection("localhost")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    connection.request(self.command, self.path, self.rfile.read(length),
                                       dict(self.headers))
                    reply = connection.getresponse()
                    body = reply.read()
                    self.send_response(reply.status)
                    self.send_header("Content-Type", reply.getheader("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    connection.close()
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
