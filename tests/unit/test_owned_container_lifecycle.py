"""`owned_container`: a create is answered before cancellation is honored.

A stdlib `docker` stand-in records every call, with the moment it finished,
so each test can say what the daemon was asked and in which order. Real
Docker behavior (a killed client's create landing after the removal) is
measured by the operator controls, not here.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest
from core import repository_executor as executor

SHIM = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
root = {root!r}
behaviour = json.load(open(os.path.join(root, "behaviour.json")))
blocked = next(l.split()[1] for l in open("/proc/self/status") if l.startswith("SigBlk:"))
def log(**extra):
    with open(os.path.join(root, "log"), "a") as stream:
        stream.write(json.dumps(dict(args=args, at=time.monotonic(), blocked=blocked, **extra)) + "\\n")
if args[0] == "create":
    time.sleep(behaviour.get("create_seconds", 0))
    if behaviour.get("refuse"):
        log(answer="refused")
        print("Error response from daemon: refused", file=sys.stderr)
        sys.exit(125)
    name = args[args.index("--name") + 1]
    at = args.index("--entrypoint")
    json.dump([args[at + 1]] + args[at + 3:], open(os.path.join(root, name), "w"))
    log(answer="created")
    print("0123456789ab")
    sys.exit(0)
if args[:2] == ["start", "-a"]:
    log()
    command = json.load(open(os.path.join(root, args[2])))
    os.execvp(command[0], command)
if args[:2] == ["rm", "-f"]:
    time.sleep(behaviour.get("rm_seconds", 0))
    rc = behaviour.get("rm_rc", 0)
    log(rc=rc)
    if rc == 0 and os.path.exists(os.path.join(root, args[2])):
        os.unlink(os.path.join(root, args[2]))
        print(args[2])
    sys.exit(rc)
sys.exit(2)
"""


@pytest.fixture
def docker(tmp_path):
    root = tmp_path / "daemon"
    root.mkdir()
    shim = tmp_path / "docker"
    shim.write_text(SHIM.format(python=sys.executable, root=str(root)))
    shim.chmod(0o755)

    def behave(**behaviour):
        (root / "behaviour.json").write_text(json.dumps(behaviour))
    behave()
    shim_calls = lambda: [json.loads(l) for l in (root / "log").read_text().splitlines()] \
        if (root / "log").exists() else []
    return str(shim), behave, shim_calls, root


def _create(docker, name, *command):
    return [docker, "create", "--name", name, "--entrypoint", command[0], "image", *command[1:]]


def _interrupt_after(seconds):
    timer = threading.Timer(seconds, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    return timer


def test_sigint_during_create_is_honored_after_the_answer_and_before_start(docker):
    shim, behave, calls, root = docker
    behave(create_seconds=0.6)
    timer = _interrupt_after(0.2)
    with pytest.raises(KeyboardInterrupt):
        with executor.owned_container(_create(shim, "own-1", "sleep", "30"), shim, "own-1"):
            pytest.fail("the cancelled launch was started")
    timer.join()
    done = calls()
    assert [c["args"][0] for c in done] == ["create", "rm"]
    assert done[0]["answer"] == "created" and done[0]["at"] < done[1]["at"]
    assert not (root / "own-1").exists()                # removed after it existed
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_sigint_inside_spawn_before_the_handle_exists(docker, monkeypatch):
    """The signal lands after the child exists but before Popen returns it."""
    shim, behave, calls, root = docker
    real = subprocess.Popen._execute_child
    fired = []

    def execute_child(self, args, *rest, **kwargs):
        real(self, args, *rest, **kwargs)
        if not fired and "create" in list(args):
            fired.append(self.pid)
            os.kill(os.getpid(), signal.SIGINT)
    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute_child)
    with pytest.raises(KeyboardInterrupt):
        with executor.owned_container(_create(shim, "own-2", "sleep", "30"), shim, "own-2"):
            pytest.fail("the cancelled launch was started")
    assert fired
    with pytest.raises(ChildProcessError):
        os.waitpid(fired[0], os.WNOHANG)               # reaped by its own handle
    assert [c["args"][0] for c in calls()] == ["create", "rm"]
    assert not (root / "own-2").exists()


def test_sigint_inside_start_spawn_stops_the_recorded_client(docker, monkeypatch, tmp_path):
    shim, behave, calls, root = docker
    real = subprocess.Popen._execute_child
    started = []

    def execute_child(self, args, *rest, **kwargs):
        real(self, args, *rest, **kwargs)
        if not started and "start" in list(args):
            started.append(self.pid)
            os.kill(os.getpid(), signal.SIGINT)
    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute_child)
    with pytest.raises(KeyboardInterrupt):
        with executor.owned_container(_create(shim, "own-3", "sleep", "30"), shim, "own-3"):
            pass
    with pytest.raises(ChildProcessError):
        os.waitpid(started[0], os.WNOHANG)               # killed and reaped by its handle
    # The killed client may die before it logs; the removal is last either way.
    assert [c["args"][0] for c in calls() if c["args"][0] != "start"] == ["create", "rm"]
    assert calls()[-1]["args"] == ["rm", "-f", "own-3"]


def test_cancellation_is_checked_after_the_answer_and_before_start(docker):
    shim, behave, calls, root = docker
    behave(create_seconds=0.3)
    seen = []

    def cancelled():
        seen.append([c["args"][0] for c in calls()])
        raise executor.PreparationCancelled("client left")
    with pytest.raises(executor.PreparationCancelled):
        with executor.owned_container(_create(shim, "own-4", "sleep", "30"), shim, "own-4",
                                      cancelled=cancelled):
            pytest.fail("the cancelled launch was started")
    assert seen == [["create"]]
    assert [c["args"][0] for c in calls()] == ["create", "rm"]


def test_create_that_never_answers_is_unconfirmed(docker, monkeypatch):
    shim, behave, calls, root = docker
    behave(create_seconds=5)
    monkeypatch.setattr(executor, "CREATE_TIMEOUT", 0.3)
    with pytest.raises(executor.CleanupUnconfirmed, match="may still create it"):
        with executor.owned_container(_create(shim, "own-5", "sleep", "30"), shim, "own-5"):
            pytest.fail("an unanswered create was started")
    assert [c["args"][0] for c in calls()] == ["rm"]   # the create client was killed unanswered


def test_refused_create_is_removed_by_name_and_reported_not_created(docker):
    shim, behave, calls, root = docker
    behave(refuse=True)
    with pytest.raises(executor.IsolationUnavailable, match="not created") as raised:
        with executor.owned_container(_create(shim, "own-6", "sleep", "30"), shim, "own-6"):
            pytest.fail("a refused create was started")
    assert not isinstance(raised.value, executor.CleanupUnconfirmed)
    assert [c["args"][0] for c in calls()] == ["create", "rm"]


def test_removal_that_keeps_failing_is_unconfirmed_within_its_bound(docker, monkeypatch):
    shim, behave, calls, root = docker
    behave(rm_rc=1)
    monkeypatch.setattr(executor, "REMOVE_DEADLINE", 0.5)
    started = time.monotonic()
    with pytest.raises(executor.CleanupUnconfirmed, match="could not be removed"):
        with executor.owned_container(_create(shim, "own-7", "true"), shim, "own-7") as proc:
            proc.wait(timeout=10)
    assert time.monotonic() - started < 10
    assert len([c for c in calls() if c["args"][0] == "rm"]) >= 2


def test_second_sigint_during_removal_does_not_cut_it_short(docker):
    shim, behave, calls, root = docker
    behave(rm_seconds=0.6)
    timer = None
    with pytest.raises(KeyboardInterrupt):
        with executor.owned_container(_create(shim, "own-8", "sleep", "30"), shim, "own-8"):
            timer = _interrupt_after(0.3)                 # lands inside the slow rm
            raise KeyboardInterrupt
    timer.join()
    removal = [c for c in calls() if c["args"][0] == "rm"]
    assert len(removal) == 1 and removal[0]["rc"] == 0
    assert not (root / "own-8").exists()


def test_clients_inherit_no_blocked_signal(docker):
    shim, behave, calls, root = docker
    with executor.owned_container(_create(shim, "own-9", "true"), shim, "own-9") as proc:
        proc.wait(timeout=10)
    for call in calls():
        assert int(call["blocked"], 16) & (1 << (signal.SIGINT - 1)) == 0, call


def test_a_held_signal_reaches_a_non_raising_handler_once(docker):
    shim, behave, calls, root = docker
    behave(create_seconds=0.4)
    got = []
    previous = signal.signal(signal.SIGINT, lambda s, f: got.append(s))
    try:
        timer = _interrupt_after(0.1)
        with executor.owned_container(_create(shim, "own-10", "true"), shim, "own-10") as proc:
            assert got == [signal.SIGINT]               # delivered before START ran
            proc.wait(timeout=10)
        timer.join()
    finally:
        signal.signal(signal.SIGINT, previous)
    assert got == [signal.SIGINT]


def test_outside_the_main_thread_no_handler_is_touched(docker):
    shim, behave, calls, root = docker
    seen = []

    def work():
        before = signal.getsignal(signal.SIGINT)
        with executor.owned_container(_create(shim, "own-11", "true"), shim, "own-11") as proc:
            seen.append(signal.getsignal(signal.SIGINT) is before)
            proc.wait(timeout=10)
    thread = threading.Thread(target=work)
    thread.start()
    thread.join(10)
    assert seen == [True]
    assert [c["args"][0] for c in calls()] == ["create", "start", "rm"]
