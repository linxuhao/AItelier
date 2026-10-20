"""Owned process tests; run only in a disposable, network-free CPU container.

GATE_QUEUE_SOURCE may select the exact frozen baseline for retained RED evidence.
No engine, app runtime, installed queue, mocked transitions, or timing sleeps.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

import pytest


SOURCE = Path(os.environ.get(
    "GATE_QUEUE_SOURCE", Path(__file__).resolve().parents[2] / "scripts/gate_queue.py"
)).resolve()
TIMEOUT = 15
FAKE_GATE = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
parent = Path(sys.argv[2])
ticket = parent.name
control = Path(os.environ["QUEUE_TEST_CONTROL"])
def emit(event, **extra):
    row = dict(event=event, ticket=ticket, pid=os.getpid(), **extra)
    with (control / "events").open("w") as out:
        out.write(json.dumps(row) + "\\n")
        out.flush()
emit("start")
with (control / ticket).open() as incoming:
    rc = int(incoming.readline())
(parent / "manifest.json").write_text("{}\\n")
(parent / (sys.argv[3] + ".gate.exit")).write_text(str(rc) + "\\n")
emit("finish", rc=rc)
raise SystemExit(rc)
'''
# A real competing executor keeps a selection in memory across a pipe barrier.
# It calls the actual transition functions, without replacing any implementation.
SELECTED_WORKER = '''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("gate_queue", sys.argv[1])
queue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue)
with queue.mutation_lock():
    record = next(r for r in queue.all_tickets() if r["ticket"] == sys.argv[2])
print("selected", flush=True)
assert sys.stdin.readline() == "continue\\n"
if sys.argv[3] == "admit":
    result = queue.run_one(record)
else:
    result = queue.finalize(record, 0)["state"]
print(json.dumps(result), flush=True)
'''


class Queue:
    def __init__(self, path):
        self.path = path
        self.root = path / "queue"
        self.reports = path / "reports"
        self.control = path / "control"
        self.control.mkdir()
        self.events = self.control / "events"
        os.mkfifo(self.events)
        self.events_fd = os.open(self.events, os.O_RDWR | os.O_NONBLOCK)
        self.buffer = b""
        self.timeline = []
        self.processes = []
        self.controls = {}
        gate = path / "fake_gate.py"
        gate.write_text(FAKE_GATE)
        gate.chmod(0o755)
        self.worktree = path / "worktree"
        self.worktree.mkdir()
        self.env = dict(os.environ, HOME=str(path), GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GATE_QUEUE_DIR=str(self.root),
                        GATE_QUEUE_REPORTS=str(self.reports), GATE_RUN_SH=str(gate),
                        QUEUE_TEST_CONTROL=str(self.control), GATE_QUEUE_POLL="0.01",
                        PYTHONDONTWRITEBYTECODE="1")
        self.git("init", "-q")
        self.git("-c", "user.name=Queue test", "-c", "user.email=queue@example.invalid",
                 "commit", "--allow-empty", "-qm", "owned fixture")
        self.sha = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.worktree), *args], env=self.env,
                              capture_output=True, text=True, check=True, timeout=TIMEOUT)

    def cli(self, *args, rc=0):
        result = subprocess.run([sys.executable, str(SOURCE), *args], env=self.env,
                                capture_output=True, text=True, timeout=TIMEOUT)
        self.timeline.append(dict(cli=list(args), rc=result.returncode,
                                  stdout=result.stdout, stderr=result.stderr))
        assert result.returncode == rc, (args, result.stdout, result.stderr)
        return result

    def submit(self, label):
        ticket = self.cli("submit-unowned", str(self.worktree), self.sha, "--label", label,
                          "--unowned-reason", "fixture").stdout.strip()
        fifo = self.control / ticket
        os.mkfifo(fifo)
        self.controls[ticket] = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        return ticket

    def raw(self, ticket):
        return (self.root / "tickets" / ticket / "ticket.json").read_bytes()

    def record(self, ticket):
        return json.loads(self.raw(ticket))

    def cancel(self, ticket, rc=0, sha=None, reason="withdraw owned ticket"):
        return self.cli("cancel", ticket, "--expected-sha", sha or self.sha,
                        "--reason", reason, "--actor", "queue-test", rc=rc)

    def spawn(self, args, *, worker=False):
        proc = subprocess.Popen(args, env=self.env, stdin=subprocess.PIPE if worker else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(proc)
        return proc

    def daemon(self, once=False):
        return self.spawn([sys.executable, str(SOURCE), "daemon", *(["--once"] if once else [])])

    def selected_worker(self, ticket, operation):
        proc = self.spawn([sys.executable, "-c", SELECTED_WORKER, str(SOURCE), ticket, operation],
                          worker=True)
        assert select.select([proc.stdout], [], [], TIMEOUT)[0], "selection barrier not reached"
        assert proc.stdout.readline() == "selected\n"
        return proc

    def finish_process(self, proc, rc=0):
        stdout, stderr = proc.communicate(timeout=TIMEOUT)
        self.timeline.append(dict(process=proc.pid, rc=proc.returncode, stdout=stdout, stderr=stderr))
        assert proc.returncode == rc, (stdout, stderr)
        return stdout

    def event(self, expected, ticket):
        deadline = time.monotonic() + TIMEOUT
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and select.select([self.events_fd], [], [], remaining)[0], self.timeline
            self.buffer += os.read(self.events_fd, 65536)
        line, self.buffer = self.buffer.split(b"\n", 1)
        row = json.loads(line)
        self.timeline.append(row)
        assert (row["event"], row["ticket"]) == (expected, ticket), self.timeline
        return row

    def release(self, ticket, rc=0):
        os.write(self.controls[ticket], (str(rc) + "\n").encode())

    def close(self):
        # Unblock only children created by this fixture, including on assertion failure.
        for ticket in self.controls:
            self.release(ticket)
        for proc in self.processes:
            if proc.poll() is None:
                proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=TIMEOUT)
            self.timeline.append(dict(cleanup_process=proc.pid, rc=proc.returncode,
                                      stdout=stdout, stderr=stderr))
        for fd in self.controls.values():
            os.close(fd)
        os.close(self.events_fd)
        print("QUEUE_TIMELINE " + json.dumps(self.timeline, sort_keys=True))


@pytest.fixture
def queue(tmp_path):
    instance = Queue(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def assert_cancelled(queue, ticket, original):
    record = queue.record(ticket)
    assert record["state"] == "cancelled"
    assert record["cancellation"]["original_text"].encode() == original
    assert record["cancellation"]["original_sha256"] == hashlib.sha256(original).hexdigest()
    assert record["cancellation"]["reason"] == "withdraw owned ticket"
    assert record["cancellation"]["actor"] == "queue-test"
    assert record["gate_exit_code"] is record["verdict"] is record["report_dir"] is None
    assert "started_at" not in record and "finished_at" not in record
    assert not (queue.reports / ticket).exists()


def test_withdraw_queued_b_while_a_and_daemon_live_then_c_runs_once(queue):
    a, b, c = [queue.submit(label) for label in ("A", "B", "C")]
    before_b = queue.raw(b)
    daemon = queue.daemon()
    queue.event("start", a)  # the actual owned gate CHILD is now blocked on its pipe
    assert daemon.poll() is None
    assert queue.record(a)["state"] == "running"
    queue.cli("daemon-status")
    queue.cli("daemon", "--once", rc=4)  # lifetime lock still excludes a second daemon
    queue.cancel(b)  # baseline RED: frozen daemon.lock implementation returns 4 here
    assert_cancelled(queue, b, before_b)
    before_a = queue.raw(a)
    queue.cancel(a, rc=5)  # admission won: no stop, no ticket mutation
    assert queue.raw(a) == before_a and daemon.poll() is None
    cancelled = queue.raw(b)
    queue.cancel(b, reason="must not replace the first decision")
    assert queue.raw(b) == cancelled
    assert "state:    cancelled" in queue.cli("status", b).stdout
    assert daemon.poll() is None
    queue.release(a)
    queue.event("finish", a)
    queue.event("start", c)
    # Ask the real daemon to stop only after C, through its own control path.
    queue.cli("stop")
    queue.release(c, rc=1)
    queue.event("finish", c)
    queue.finish_process(daemon)
    assert queue.record(a)["verdict"] == "passed"
    assert queue.record(c)["verdict"] == "failed"
    assert queue.record(c)["gate_exit_code"] == 1
    assert queue.raw(b) == cancelled
    assert [r["ticket"] for r in queue.timeline if r.get("event") == "start"] == [a, c]
    assert [r["rc"] for r in queue.timeline if r.get("event") == "finish"] == [0, 1]


def test_cancel_wins_after_selection_and_stale_admission_and_finalization_cannot_restore(queue):
    ticket = queue.submit("selected but never admitted")
    original = queue.raw(ticket)
    admission = queue.selected_worker(ticket, "admit")
    finalization = queue.selected_worker(ticket, "finalize")
    queue.cancel(ticket)
    cancelled = queue.raw(ticket)
    for proc, expected in ((admission, "false"), (finalization, '"cancelled"')):
        proc.stdin.write("continue\n")
        proc.stdin.flush()
        assert queue.finish_process(proc).strip() == expected
    assert queue.raw(ticket) == cancelled
    assert_cancelled(queue, ticket, original)
    daemon = queue.daemon(once=True)
    queue.finish_process(daemon)
    assert not any(r.get("event") == "start" for r in queue.timeline)
    assert not queue.reports.exists()


@pytest.mark.parametrize("case", ["wrong_sha", "unknown", "malformed", "nonobject", "started",
                                  "nonqueued", "report_exists", "wrong_id", "extra_field"])
def test_refusal_preserves_exact_ticket_bytes(queue, case):
    ticket = queue.submit(case)
    path = queue.root / "tickets" / ticket / "ticket.json"
    record = queue.record(ticket)
    if case == "malformed":
        path.write_bytes(b"{broken\n")
    elif case == "nonobject":
        path.write_text("[]\n")
    elif case in ("started", "nonqueued", "wrong_id", "extra_field"):
        if case == "started":
            record["started_at"] = None  # restart-requeued is still not never-started
        elif case == "nonqueued":
            record["state"] = "done"
        elif case == "wrong_id":
            record["ticket"] = "gq-20000101T000000Z-00000000"
        else:
            record["unexpected"] = True
        path.write_text(json.dumps(record) + "\n")
    elif case == "report_exists":
        (queue.reports / ticket).mkdir(parents=True)
    original = path.read_bytes()
    if case == "unknown":
        queue.cancel("gq-20000101T000000Z-00000000", rc=3)
    else:
        wrong = ("1" if queue.sha[0] != "1" else "2") + queue.sha[1:]
        queue.cancel(ticket, rc=5, sha=wrong if case == "wrong_sha" else None)
    assert path.read_bytes() == original
    assert sorted(p.name for p in path.parent.iterdir()) == ["ticket.json"]


def test_busy_short_transition_refuses_without_mutation_then_allows_cancel(queue):
    ticket = queue.submit("lock contention")
    original = queue.raw(ticket)
    with (queue.root / "mutation.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        queue.cancel(ticket, rc=4)
        assert queue.raw(ticket) == original
    queue.cancel(ticket)
    assert_cancelled(queue, ticket, original)


def test_restart_finalizes_existing_exit_preserving_started_history(queue):
    ticket = queue.submit("reconcile owned finished evidence")
    path = queue.root / "tickets" / ticket / "ticket.json"
    record = queue.record(ticket)
    record.update(state="running", started_at="2000-01-01T00:00:00+00:00")
    path.write_text(json.dumps(record) + "\n")
    parent = queue.reports / ticket
    parent.mkdir(parents=True)
    (parent / "gate.gate.exit").write_text("0\n")
    (parent / "manifest.json").write_text("{}\n")
    daemon = queue.daemon(once=True)
    queue.finish_process(daemon)
    final = queue.record(ticket)
    assert final["state"] == "done" and final["verdict"] == "passed"
    assert final["started_at"] == record["started_at"]
    before = queue.raw(ticket)
    queue.cancel(ticket, rc=5)
    assert queue.raw(ticket) == before
    assert not any(r.get("event") == "start" for r in queue.timeline)


@pytest.mark.parametrize("damage", ["hash", "field"])
def test_corrupt_cancelled_preimage_is_refused_without_replacing_history(queue, damage):
    ticket = queue.submit("cancelled history")
    queue.cancel(ticket)
    path = queue.root / "tickets" / ticket / "ticket.json"
    record = queue.record(ticket)
    if damage == "hash":
        record["cancellation"]["original_sha256"] = "0" * 64
    else:
        record["label"] = "different from the original"
    path.write_text(json.dumps(record) + "\n")
    original = path.read_bytes()
    queue.cancel(ticket, rc=5)
    assert path.read_bytes() == original
