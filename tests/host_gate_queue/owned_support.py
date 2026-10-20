"""Only the State HTTP service is a fixture. Client/credentials/CLI/children are real."""
from __future__ import annotations

import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import select
import socket
import subprocess
import tarfile
import threading
import time

TIMEOUT = 20
BASE = "8a755972cc1733441c37408689d96fefe9ff28e7"
REPO = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(Path(__file__).with_name("owned-fixture.json").read_text())
SHA = FIXTURE["git_objects"]["source_sha"]

CHILD = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
parent = Path(sys.argv[2]); ticket = parent.name
control = Path(os.environ["QUEUE_TEST_CONTROL"])
identity = dict(nonce=os.environ.get("GATE_QUEUE_INVOCATION_NONCE"), pid=os.getpid())
def emit(event, **extra):
    row = dict(event=event, ticket=ticket, identity=identity, **extra)
    with (control / "events").open("w") as out:
        out.write(json.dumps(row) + "\\n"); out.flush()
(parent / "child-environment.json").write_text(json.dumps(dict(
    names=sorted(os.environ), binding=os.environ.get("GATE_BINDING_FILE"))))
emit("start")
with (control / ticket).open() as incoming:
    command = incoming.readline().strip()
if command != "noverdict":
    rc = int(command)
    (parent / "manifest.json").write_text("{}\\n")
    (parent / (sys.argv[3] + ".gate.exit")).write_text(str(rc) + "\\n")
else:
    rc = 0
emit("finish", rc=rc)
raise SystemExit(rc)
'''
# A deterministic pause around the real daemon's selected->run_one call. This
# changes no transition or client: cmd_daemon still holds its real lifetime lock.
SELECTED_DAEMON = '''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("gate_queue", sys.argv[1])
q = importlib.util.module_from_spec(spec); spec.loader.exec_module(q)
run_one = q.run_one
paused = False
def synchronized(record, adapter=None):
    global paused
    if not paused:
        paused = True
        print("selected", flush=True)
        assert sys.stdin.readline() == "continue\\n"
    return run_one(record, adapter)
q.run_one = synchronized
raise SystemExit(q.main(["daemon", "--once"]))
'''


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


class Harness:
    def __init__(self, path):
        path.mkdir(parents=True, exist_ok=True)
        self.path, self.root, self.reports = path, path / "queue", path / "reports"
        self.aroot, self.home = path / "aroot", path / "home"
        self.aroot.mkdir(); self.home.mkdir()
        archive = subprocess.run(["git", "-C", str(REPO), "archive", "HEAD"],
                                 capture_output=True, check=True, timeout=TIMEOUT).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(self.aroot, filter="data")
        self.source = self.aroot / "scripts/gate_queue.py"
        self.python = os.environ.get("GATE_QUEUE_TEST_VENV_PYTHON")
        self.ordinary = os.environ.get("GATE_QUEUE_TEST_ORDINARY_PYTHON", "/usr/bin/python3")
        if not self.python or not Path(self.python).is_absolute() or not Path(self.python).is_file():
            raise RuntimeError("GATE_QUEUE_TEST_VENV_PYTHON must name the existing read-only CLI venv interpreter")
        self.timeline, self.requests, self.processes, self.controls = [], [], [], {}
        self.child_pidfds = []
        self.nodes, self.attempts, self.evidence = {}, {}, {}
        self.mode = {}
        self.me = copy.deepcopy(FIXTURE["whoami"])
        self.token = secrets.token_hex(32)
        self.control = path / "control"; self.control.mkdir()
        os.mkfifo(self.control / "events")
        self.events = os.open(self.control / "events", os.O_RDWR | os.O_NONBLOCK)
        self.buffer = b""
        child = path / "harmless_gate.py"; child.write_text(CHILD); child.chmod(0o755)
        binding = path / "fixture-binding.json"; binding.write_text("{}\n")
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(("AITELIER_", "GATE_"))
                    and k.upper() not in ("PYTHONPATH", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
        self.env.update(HOME=str(self.home), AITELIER_HOME=str(self.home / ".AItelier"),
                        AITELIER_DRIVER_ID="fixture-driver", GATE_QUEUE_AITELIER_ROOT=str(self.aroot),
                        GATE_QUEUE_DIR=str(self.root), GATE_QUEUE_REPORTS=str(self.reports),
                        GATE_RUN_SH=str(child), GATE_BINDING_FILE=str(binding),
                        QUEUE_TEST_CONTROL=str(self.control), GATE_QUEUE_POLL="0.01",
                        PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        NO_PROXY="localhost,127.0.0.1")
        # Check the actual credential module's resolution before writing a fixture credential.
        probe = subprocess.run([self.python, "-c", "import sys;sys.path.insert(0,sys.argv[1]);"
                                "import httpx, cli.client;from core.driver_credentials import token_file_for;"
                                "print(token_file_for('fixture-driver'))", str(self.aroot)],
                               env=self.env, capture_output=True, text=True, timeout=TIMEOUT)
        if probe.returncode:
            raise RuntimeError("real client bootstrap precondition failed: " + probe.stderr)
        token_path = Path(probe.stdout.strip())
        if not token_path.is_relative_to(self.home):
            raise RuntimeError("fixture token path escaped disposable HOME")
        token_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        token_path.write_text(self.token + "\n"); token_path.chmod(0o600)
        self.timeline.append(dict(event="bootstrap_provenance", python=self.python, root=str(self.aroot)))
        self.worktree = path / "worktree"; self.worktree.mkdir()
        self.git("init", "-q")
        blob = self.git("hash-object", "-w", "--stdin", input=FIXTURE["git_objects"]["README_bytes"]).stdout.strip()
        assert blob == FIXTURE["git_objects"]["blob_sha"]
        tree = self.git("mktree", input="100644 blob " + blob + "\tREADME\n").stdout.strip()
        assert tree == FIXTURE["git_objects"]["tree_sha"]
        commit = self.git("hash-object", "-t", "commit", "-w", "--stdin",
                          input=FIXTURE["git_objects"]["commit_text"]).stdout.strip()
        assert commit == SHA
        self.git("checkout", "--detach", "-q", SHA)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.serve(None)

            def do_POST(self):
                self.serve(json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0")))))

            def serve(self, body):
                row = dict(method=self.command, path=self.path, body=body,
                           driver_header_matches=self.headers.get("X-AItelier-Driver-Token") == outer.token,
                           admin_header_present="X-AItelier-Admin-Token" in self.headers)
                outer.requests.append(row); outer.timeline.append(dict(event="HTTP", **row))
                code, value = 200, None
                if self.path == "/api/drivers/me":
                    value = outer.me
                elif self.path.startswith("/api/state/query/"):
                    if not isinstance(body, dict) or "arguments" in body:
                        code, value = 400, {"error": "plain arguments required"}
                    else:
                        action = self.path.rsplit("/", 1)[1]
                        if action == "get_node":
                            key = body["node_key"]
                            if outer.mode.get(key) == "unavailable":
                                code, value = 503, {"error": "fixture unknown"}
                            else:
                                value = outer.nodes[key]
                        elif action == "get_attempt":
                            value = outer.attempts[body["attempt_id"]]
                        elif action == "evidence":
                            value = outer.evidence.get(body["attempt_id"], [])
                        else:
                            code, value = 404, {}
                elif self.path == "/api/state/commands/report_external_attempt":
                    # Observe disk at the actual HTTP boundary, rather than trusting a logged claim.
                    tickets = [json.loads(p.read_bytes()) for p in (outer.root / "tickets").glob("*/ticket.json")]
                    bound = next((r for r in tickets if any(c["attempt_id"] == body["attempt_id"]
                                                          for c in r.get("contexts", []))), None)
                    row["queue_state_at_POST"] = bound["state"] if bound else None
                    row["failures_at_POST"] = len(bound["failures"]) if bound else 0
                    row["history_at_POST"] = [h["event"] for h in bound["history"]] if bound else []
                    if outer.mode.get("write") == "refuse":
                        code, value = 409, {"error": "fixture refusal"}
                    else:
                        attempt = outer.attempts[body["attempt_id"]]
                        previous = outer.mode.setdefault("observations", {})
                        replayed = body["observation_id"] in previous
                        if replayed:
                            assert previous[body["observation_id"]] == body
                        else:
                            previous[body["observation_id"]] = copy.deepcopy(body)
                            terminal = body["status"] in ("candidate", "failed")
                            attempt.update(status=body["status"], observation_version=body["expected_version"] + 1,
                                           terminal_observation_id=body["observation_id"] if terminal else None,
                                           lease_state=None if terminal else "healthy")
                            if body["status"] == "candidate":
                                attempt.update(artifact_ref=body["artifact"], artifact_kind=body["artifact_kind"])
                        value = dict(attempt=copy.deepcopy(attempt), replayed=replayed)
                        if outer.mode.get("write") == "commit-disconnect":
                            outer.mode["write"] = "normal"
                            self.connection.shutdown(socket.SHUT_RDWR); self.connection.close()
                            return
                else:
                    code, value = 404, {}
                data = json.dumps(value).encode()
                self.send_response(code); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.env["AITELIER_PORT"] = str(self.server.server_port)
        self.install_review()

    def git(self, *args, input=None):
        return subprocess.run(["git", "-C", str(self.worktree), *args], input=input, env=self.env,
                              capture_output=True, text=True, check=True, timeout=TIMEOUT)

    def file(self, name, value, raw=None):
        path = self.path / name
        path.write_bytes(canonical(value) + b"\n" if raw is None else raw)
        return str(path)

    def install_review(self):
        report = copy.deepcopy(FIXTURE["source_review_report"])
        ref = self.file("source-review.json", report)
        assert checksum(Path(ref).read_bytes()) == FIXTURE["source_failure_evidence"]["report_sha256"]
        source = copy.deepcopy(FIXTURE["source_review_context"])
        self.attempts[source["attempt_id"]] = source
        evidence = copy.deepcopy(FIXTURE["source_failure_evidence"]); evidence["report_ref"] = ref
        self.evidence[source["attempt_id"]] = [evidence]

    def registrations(self, suffix=""):
        registration = copy.deepcopy(FIXTURE["registration_file"])
        registration["source"]["source"] = str(self.worktree)
        for row in registration["registrations"]:
            a, c = row["attempt"], row["claim"]
            for value, key in ((a,"node_key"), (a,"attempt_id"), (c,"node_key"), (c,"claim_id")):
                value[key] += suffix
            row["node"] = a["node_key"]
            older = copy.deepcopy(a); older.update(seq=a["seq"]-50, attempt_id="older-"+a["attempt_id"])
            a, c = copy.deepcopy(a), copy.deepcopy(c)
            self.nodes[a["node_key"]] = dict(node=dict(project_id=a["project_id"], node_key=a["node_key"],
                revision=1, contract_hash=a["contract_hash"], status="OPEN", hold=None), attempts=[a, older], claims=[c])
            self.attempts[a["attempt_id"]] = a
        return registration

    def cli(self, *args, rc=0, owned=True, changes=None, source=None):
        env = dict(self.env)
        if not owned:
            env.pop("GATE_QUEUE_AITELIER_ROOT", None); env.pop("AITELIER_DRIVER_ID", None)
        for key, value in (changes or {}).items():
            if value is None: env.pop(key, None)
            else: env[key] = str(value)
        result = subprocess.run([self.python if owned else self.ordinary, str(source or self.source), *args],
                                env=env, capture_output=True, text=True, timeout=TIMEOUT)
        self.timeline.append(dict(event="CLI", argv=list(args), rc=result.returncode,
                                  stdout=result.stdout, stderr=result.stderr))
        assert self.token not in result.stdout + result.stderr
        if rc is not None:
            assert result.returncode == rc, (args, result.returncode, result.stdout, result.stderr)
        return result

    def submit(self, suffix="", registration=None):
        registration = registration or self.registrations(suffix)
        filename = self.file("registrations" + suffix + ".json", registration)
        result = self.cli("submit", "--registrations", filename, "--purpose", "final-composed-normal",
                          "--label", suffix or "A", str(self.worktree), SHA)
        ticket = result.stdout.strip()
        os.mkfifo(self.control / ticket)
        self.controls[ticket] = os.open(self.control / ticket, os.O_RDWR | os.O_NONBLOCK)
        self.timeline.append(dict(event="ticket_published", ticket=ticket))
        return ticket

    def raw(self, ticket):
        return (self.root / "tickets" / ticket / "ticket.json").read_bytes()

    def record(self, ticket):
        return json.loads(self.raw(ticket))

    def identity_args(self, ticket, request):
        record = self.record(ticket)
        return [ticket, "--expected-sha", SHA, "--binding-sha256", record["binding_sha256"],
                "--expected-revision", str(record["ticket_revision"]), "--request-id", request]

    def failure(self, ticket, **changes):
        r = self.record(ticket); source = FIXTURE["source_review_context"]
        decision = dict(decision_id="fixture-failure-one", irreversible_for_attempt=True,
            controller_driver_id="fixture-driver", source_sha=SHA, ticket=ticket,
            binding_sha256=r["binding_sha256"], original_purpose=r["purpose"],
            affected_context_ids=[r["contexts"][0]["attempt_id"]], source_project_id="fixture-project",
            source_node_key="fixture.review", source_node_revision=1,
            source_attempt_id_or_null=source["attempt_id"], source_context_hash_or_null=source["context_hash"],
            source_criterion_id="source-review", source_artifact_kind="sha256", source_artifact_ref="a"*64,
            source_report_ref=str(self.path / "source-review.json"),
            source_report_sha256=FIXTURE["source_failure_evidence"]["report_sha256"],
            source_evidence_id_or_null="ev-fixture-source-fail", applicability_reason="actual synthetic reviewed Source governs this proof input",
            disposition="hold")
        decision.update(changes)
        return self.file("failure-" + decision["decision_id"] + ".json", decision)

    def observation(self, ticket, status="failed", observation_id="fixture-observation-one", context=None):
        r = self.record(ticket); context = context or r["contexts"][0]
        report = dict(status="completed", settled=True, usable=True, criterion_id="native-result",
                      verdict="skip", detail="Owned fixture completion; source review failure is separate; native unrun unless receipt present")
        ref = self.file(observation_id + "-report.json", report)
        value = dict(attempt_id=context["attempt_id"], observation_id=observation_id, expected_version=self.attempts[context["attempt_id"]]["observation_version"],
                     context_hash=context["context_hash"], status=status, report_ref=ref,
                     report_sha256=checksum(Path(ref).read_bytes()), quiescent=True, detail="fixture report", fence=1)
        if status == "candidate": value.update(artifact="c"*64, artifact_kind="sha256")
        return self.file(observation_id + ".json", value)

    def report(self, ticket, request="report-one", failure=True, observation=None, physical=None, rc=0):
        args = ["report-context", *self.identity_args(ticket, request), "--context", self.record(ticket)["contexts"][0]["attempt_id"],
                "--observation", observation or self.observation(ticket)]
        if failure: args.extend(["--failure-decision", self.failure(ticket) if failure is True else failure])
        if physical: args.extend(["--physical-receipt", physical])
        return self.cli(*args, rc=rc)

    def decide(self, ticket, action, request="decision-one", retain=None, ack=None, purpose=None,
               authority=None, rc=0, extra=()):
        args = ["decide", *self.identity_args(ticket, request), "--action", action, "--reason", "fixture explicit decision"]
        if purpose: args.extend(["--reuse-purpose", purpose])
        if retain is not None: args.extend(["--retain-contexts", self.file(request+"-retain.json", retain)])
        if ack is not None: args.extend(["--ack-failures", self.file(request+"-ack.json", ack)])
        if authority: args.extend(["--baseline-authority", authority])
        return self.cli(*args, *extra, rc=rc)

    def daemon(self, selected=False, owned=True, once=True):
        env = dict(self.env)
        if not owned:
            env.pop("GATE_QUEUE_AITELIER_ROOT", None); env.pop("AITELIER_DRIVER_ID", None)
        python = self.python if owned else self.ordinary
        argv = [python, "-c", SELECTED_DAEMON, str(self.source)] if selected else [python, str(self.source), "daemon", *(["--once"] if once else [])]
        p = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE if selected else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(p)
        if selected:
            assert select.select([p.stdout], [], [], TIMEOUT)[0], self.timeline
            assert p.stdout.readline() == "selected\n", self.timeline
            self.timeline.append(dict(event="selected-before-mutation", pid=p.pid))
        return p

    def finish(self, p, rc=0):
        stdout, stderr = p.communicate(timeout=TIMEOUT)
        self.timeline.append(dict(event="process-exit", pid=p.pid, rc=p.returncode, stdout=stdout, stderr=stderr))
        assert self.token not in stdout + stderr
        assert p.returncode == rc, (p.returncode, stdout, stderr)
        return stdout, stderr

    def event(self, event, ticket):
        deadline = time.monotonic()+TIMEOUT
        while b"\n" not in self.buffer:
            left = deadline-time.monotonic()
            assert left > 0 and select.select([self.events], [], [], left)[0], self.timeline
            self.buffer += os.read(self.events, 65536)
        line, self.buffer = self.buffer.split(b"\n", 1)
        row=json.loads(line); self.timeline.append(row)
        if row["event"] == "start":
            try:
                self.child_pidfds.append(os.pidfd_open(row["identity"]["pid"]))
            except ProcessLookupError:
                # A fast completion was already observed dead; no live child remains.
                pass
        assert (row["event"], row["ticket"]) == (event, ticket), self.timeline
        return row

    def log_event(self, process, text):
        deadline=time.monotonic()+TIMEOUT
        captured=b""
        while text.encode() not in captured:
            left=deadline-time.monotonic()
            assert left > 0 and select.select([process.stderr],[],[],left)[0], self.timeline
            chunk=os.read(process.stderr.fileno(),65536)
            assert chunk, self.timeline
            captured+=chunk
        self.timeline.append(dict(event="daemon-log",line=captured.decode()))

    def release(self, ticket, rc=0):
        os.write(self.controls[ticket], (str(rc)+"\n").encode())

    def complete(self, ticket, rc=0):
        daemon=self.daemon(); self.event("start", ticket); self.release(ticket,rc)
        self.event("finish",ticket); self.finish(daemon)

    def physical(self, ticket):
        r=self.record(ticket)
        return self.file("physical.json", dict(ticket=ticket, source_sha=SHA,
            invocation_identity=r["invocation_identity"], outcome="owned-quiescent",
            actual_ownership_refs_and_sha256=[r["invocation_exit"]], raw_gate_exit_ref_or_null=str(self.reports/ticket/"gate.gate.exit"),
            report_scope=dict(attempt_ids=[c["attempt_id"] for c in r["contexts"]])))

    def no_run(self, ticket, state):
        r=self.record(ticket)
        assert r["state"] == state and r["admitted_at"] is None
        assert r["gate_exit_code"] is r["verdict"] is r["report_dir"] is None
        assert not (self.reports/ticket).exists()
        assert not any(e.get("event")=="start" and e["ticket"]==ticket for e in self.timeline)

    def write_posts(self):
        return [r for r in self.requests if r["path"]=="/api/state/commands/report_external_attempt"]

    def close(self):
        for ticket in self.controls: self.release(ticket)
        for p in self.processes:
            try:
                stdout,stderr=p.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                p.terminate()
                try: stdout,stderr=p.communicate(timeout=TIMEOUT)
                except subprocess.TimeoutExpired:
                    p.kill(); stdout,stderr=p.communicate(timeout=TIMEOUT)
            self.timeline.append(dict(event="cleanup-owned-process",pid=p.pid,rc=p.returncode,stdout=stdout,stderr=stderr))
        for fd in self.child_pidfds:
            assert select.select([fd], [], [], TIMEOUT)[0], "owned harmless child did not exit"
            os.close(fd)
        for fd in self.controls.values(): os.close(fd)
        os.close(self.events)
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=TIMEOUT)
        assert not self.thread.is_alive()
        for path in self.root.rglob("*"):
            if path.is_file(): assert self.token.encode() not in path.read_bytes(), path
        assert self.timeline
        print("OWNED_QUEUE_TIMELINE " + json.dumps(self.timeline,sort_keys=True))
        print("STATE_HTTP_REQUESTS " + json.dumps(self.requests,sort_keys=True))
