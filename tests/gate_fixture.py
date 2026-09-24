"""The real Godot harness, served in-process, and a repository gate that calls it.

`HarnessRig` serves the REAL `docker/godot/godot_harness.py:_Handler` over a
durable render-owner table in a temp sqlite: its admission code, its render
queue and its 409 payloads are the production ones. Only the render body
(`run_script`) is replaced, by a stand-in that returns `script_report` and,
for the holder operation, blocks until the test releases it.

`GATE_PY` is a repository gate with the transport and exit-code contract of
the game repository's `tools/godot_gate.py`: it POSTs to $GODOT_BUILDER_URL,
exits 2 when the engine does not answer (`... gate NOT run.`), 1 when the
engine answered and a check failed, 0 otherwise, and retains
`manifest.json` / `script.json` / `script-findings.json` in a fresh directory
under $GATE_REPORT_DIR.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[1] / "docker" / "godot" / "godot_harness.py"
HOLDER = "op-holder"

GATE_PY = r'''
import json, os, sys, tempfile, urllib.error, urllib.request

builder = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")
client_timeout = float(os.environ.get("FIXTURE_GATE_CLIENT_TIMEOUT", "60"))
noise = int(os.environ.get("FIXTURE_GATE_NOISE", "0"))
op = os.environ.get("FIXTURE_GATE_OP") or os.path.basename(os.getcwd())

parent = os.environ.get("GATE_REPORT_DIR") or tempfile.gettempdir()
os.makedirs(parent, exist_ok=True)
directory = tempfile.mkdtemp(prefix="fixture-gate-", dir=parent)
manifest = {"status": "incomplete", "stages": {}}

def write(name, value):
    with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2)

def finish(code):
    manifest.update(exit_code=code,
                    status={0: "passed", 1: "failed"}.get(code, "incomplete"))
    write("manifest.json", manifest)
    sys.exit(code)

# The repo's own python suite runs first in the real gate; its progress dots
# are what push the real output past any fixed tail.
print("." * noise, flush=True)
manifest["stages"]["script"] = {"status": "awaiting_response"}
write("manifest.json", manifest)
payload = {"project_dir": os.getcwd(), "project_id": "fixture",
           "run_id": "fixture", "operation_id": op,
           "scripts": ["res://tests/a.gd", "res://tests/b.gd"], "timeout": 60}
req = urllib.request.Request(builder.rstrip("/") + "/script",
                             data=json.dumps(payload).encode(),
                             headers={"Content-Type": "application/json"},
                             method="POST")
try:
    with urllib.request.urlopen(req, timeout=client_timeout) as r:
        report = json.loads(r.read())
except (urllib.error.URLError, OSError, TimeoutError) as exc:
    print("godot-builder unreachable at %s: %s -- gate NOT run." % (builder, exc),
          file=sys.stderr)
    finish(2)
write("script.json", report)
manifest["stages"]["script"] = {"status": "response_received",
                                "report": "script.json"}
findings = []
for r in report.get("results") or []:
    if not r.get("passed"):
        findings.append("script %s FAILED" % r.get("script"))
if not report.get("passed"):
    findings.append("script gate failed: %s" % report.get("summary", ""))
write("script-findings.json", findings)
print("unit suite %s" % ("FAILED" if findings else "OK"))
finish(1 if findings else 0)
'''

RUN_TESTS_SH = "#!/bin/sh\ncd \"$(dirname \"$0\")\"\nexec python3 gate.py\n"


def write_gate(repo: Path) -> None:
    """Install the gate as the repo's declared `run_tests.sh`."""
    (repo / "gate.py").write_text(GATE_PY, encoding="utf-8")
    script = repo / "run_tests.sh"
    script.write_text(RUN_TESTS_SH, encoding="utf-8")
    script.chmod(0o755)


def green_report() -> dict:
    return {"passed": True, "discovered": ["res://tests/a.gd", "res://tests/b.gd"],
            "results": [{"script": "res://tests/a.gd", "passed": True},
                        {"script": "res://tests/b.gd", "passed": True}],
            "summary": "2 passed"}


def red_report() -> dict:
    return {"passed": False, "discovered": ["res://tests/a.gd", "res://tests/b.gd"],
            "results": [{"script": "res://tests/a.gd", "passed": False},
                        {"script": "res://tests/b.gd", "passed": False}],
            "summary": "0 passed, 2 failed"}


class HarnessRig:
    """The real harness admission path, served on 127.0.0.1."""

    def __init__(self, tmp_path: Path, monkeypatch, *, render_seconds: float = 0.0):
        monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
        monkeypatch.setenv("GODOT_DEPLOYMENT_LOCK",
                           str(tmp_path / "deployment-admission.lock"))
        monkeypatch.setenv("GODOT_RENDER_EFFECT_LOCK",
                           str(tmp_path / "render-effect.lock"))
        spec = importlib.util.spec_from_file_location(
            f"godot_harness_rig_{id(self)}", HARNESS)
        gh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gh)
        gh.RENDER_OWNER_WAIT_POLL_SEC = 0.02
        self.gh = gh
        self.release = threading.Event()
        self.entered: dict = {}
        self.rendered: list = []
        self.script_report = green_report()
        self._lock = threading.Lock()

        def run_script(_proj, _scripts, timeout=600):
            active = [r for r in gh.render_owner_snapshot() if r["status"] == "active"]
            op = active[0]["operation_id"] if len(active) == 1 else None
            with self._lock:
                self.rendered.append(op)
                self.entered.setdefault(op, threading.Event()).set()
            if op == HOLDER:
                self.release.wait(120)
            elif render_seconds:
                time.sleep(render_seconds)
            return json.loads(json.dumps(self.script_report))

        monkeypatch.setattr(gh, "run_script", run_script)
        self.server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self._holder = None

    def entered_event(self, op: str) -> threading.Event:
        with self._lock:
            return self.entered.setdefault(op, threading.Event())

    def hold(self) -> None:
        """Put a live render in flight under another owner, and wait for it."""
        def go():
            req = urllib.request.Request(
                self.base + "/script",
                data=json.dumps({"project_dir": "/tmp/holder", "project_id": "holder",
                                 "run_id": "holder", "operation_id": HOLDER,
                                 "scripts": ["res://x.gd"]}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    r.read()
            except (urllib.error.URLError, OSError):
                pass
        self._holder = threading.Thread(target=go, daemon=True)
        self._holder.start()
        assert self.entered_event(HOLDER).wait(30), "the holder never started rendering"

    def let_go(self) -> None:
        self.release.set()
        if self._holder is not None:
            self._holder.join(30)

    def mark_owner_lost(self) -> dict:
        """A holder whose process died: it needs reconciliation, not a wait."""
        row = self.gh.acquire_render_owner("holder", "holder", "op-lost")
        return self.gh.mark_render_owner_lost(row["owner_id"], row["generation"],
                                              "fixture: the holder process died")

    def owners(self) -> list[dict]:
        return self.gh.render_owner_snapshot()

    def close(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
