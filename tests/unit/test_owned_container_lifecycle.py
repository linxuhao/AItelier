"""`owned_container`: a create is answered before cancellation is honored.

A stdlib `docker` stand-in records every call, with the moment it finished,
so each test can say what the daemon was asked and in which order. It keeps
containers under random IDs with their names and resolves a reference as the
daemon does (a full ID, else a name), so a controlled peer can hold a name:
this is CLI-shim evidence, not Docker's. Real Docker behavior (a killed
client's create landing after the removal) is measured by the operator
controls, not here.
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
store = os.path.join(root, "containers")
behaviour = json.load(open(os.path.join(root, "behaviour.json")))
blocked = next(l.split()[1] for l in open("/proc/self/status") if l.startswith("SigBlk:"))
def log(**extra):
    with open(os.path.join(root, "log"), "a") as stream:
        stream.write(json.dumps(dict(args=args, at=time.monotonic(), blocked=blocked, **extra)) + "\\n")
def named(name):
    return [c for c in os.listdir(store) if json.load(open(os.path.join(store, c)))["name"] == name]
def resolve(ref):                           # as the daemon does: a full ID, else a name
    return ref if ref in os.listdir(store) else next(iter(named(ref)), None)
if args[0] == "create":
    time.sleep(behaviour.get("create_seconds", 0))
    if behaviour.get("refuse"):
        log(answer="refused")
        print("Error response from daemon: refused", file=sys.stderr)
        sys.exit(125)
    name = args[args.index("--name") + 1]
    peer = next(iter(named(name)), None)
    if peer and not behaviour.get("exit_as"):
        log(answer="refused")
        print('Error response from daemon: Conflict. The container name "/' + name + '" is already '
              'in use by container "' + peer + '". You have to remove (or rename) that container '
              'to be able to reuse that name.', file=sys.stderr)
        sys.exit(125)
    if peer is None:                        # a taken name is refused, whether or not that is told
        cid = os.urandom(32).hex()
        at = args.index("--entrypoint")
        with open(os.path.join(store, cid), "w") as stream:
            json.dump(dict(name=name, command=[args[at + 1]] + args[at + 3:]), stream)
    exit_as = behaviour.get("exit_as")      # the daemon answered; the client says otherwise
    if exit_as == "killed":
        log(answer="killed")
        os.kill(os.getpid(), 9)
    if exit_as:
        log(answer=exit_as)
        print(behaviour["stderr"], file=sys.stderr)
        print(behaviour.get("stdout", ""))
        sys.exit(behaviour["rc"])
    log(answer="created", id=cid)
    print(cid)
    sys.exit(0)
if args[:2] == ["start", "-a"]:
    cid = resolve(args[2])
    log(id=cid)
    if cid is None:
        print("Error response from daemon: No such container: " + args[2], file=sys.stderr)
        sys.exit(1)
    command = json.load(open(os.path.join(store, cid)))["command"]
    os.execvp(command[0], command)
if args[:2] == ["rm", "-f"]:
    time.sleep(behaviour.get("rm_seconds", 0))
    rc = behaviour.get("rm_rc", 0)
    cid = resolve(args[2])
    log(rc=rc, id=cid)
    if rc == 0 and cid is not None:
        os.unlink(os.path.join(store, cid))
        print(args[2])
    sys.exit(rc)
sys.exit(2)
"""


@pytest.fixture
def docker(tmp_path):
    root = tmp_path / "daemon"
    root.mkdir()
    (root / "containers").mkdir()
    shim = tmp_path / "docker"
    shim.write_text(SHIM.format(python=sys.executable, root=str(root)))
    shim.chmod(0o755)

    def behave(**behaviour):
        (root / "behaviour.json").write_text(json.dumps(behaviour))
    behave()
    shim_calls = lambda: [json.loads(l) for l in (root / "log").read_text().splitlines()] \
        if (root / "log").exists() else []
    return str(shim), behave, shim_calls, root


@pytest.fixture
def spawned(monkeypatch):
    """Every argv spawned, recorded when Popen is called (a killed client may never log)."""
    seen = []
    real = subprocess.Popen

    def popen(args, *a, **k):
        seen.append([str(x) for x in args][1:3])
        return real(args, *a, **k)
    monkeypatch.setattr(subprocess, "Popen", popen)
    return seen


def _held(root):
    """The fake daemon's containers: ID -> name."""
    return {p.name: json.loads(p.read_text())["name"] for p in (root / "containers").iterdir()}


def _peer(root, name):
    """A controlled peer holding ``name``, created by someone else; its ID."""
    cid = os.urandom(32).hex()
    (root / "containers" / cid).write_text(json.dumps({"name": name, "command": ["sleep", "30"]}))
    return cid


def _created_id(calls):
    """The ID this create was answered with."""
    return next(c["id"] for c in calls() if c.get("answer") == "created")


def _create(docker, name, *command):
    return [docker, "create", "--name", name, "--entrypoint", command[0], "image", *command[1:]]


def _interrupt_after(seconds):
    timer = threading.Timer(seconds, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    return timer


def test_sigint_during_create_is_honored_after_the_answer_and_before_start(docker, spawned):
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
    assert done[1]["args"] == ["rm", "-f", done[0]["id"]]  # by the ID it answered with
    assert _held(root) == {}                             # removed after it existed
    assert [a[0] for a in spawned] == ["create", "rm"]  # START was never spawned
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
    assert calls()[-1]["args"] == ["rm", "-f", _created_id(calls)]
    assert _held(root) == {}


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
    assert calls()[-1]["args"] == ["rm", "-f", _created_id(calls)]


def test_cancellation_is_checked_after_the_answer_and_before_start(docker, spawned):
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
    assert calls()[-1]["args"] == ["rm", "-f", _created_id(calls)]
    assert [a[0] for a in spawned] == ["create", "rm"]  # START was never spawned


def test_create_that_never_answers_is_unconfirmed(docker, spawned, monkeypatch):
    shim, behave, calls, root = docker
    behave(create_seconds=5)
    monkeypatch.setattr(executor, "CREATE_TIMEOUT", 0.3)
    with pytest.raises(executor.CleanupUnconfirmed, match="may still create it") as raised:
        with executor.owned_container(_create(shim, "own-5", "sleep", "30"), shim, "own-5"):
            pytest.fail("an unanswered create was started")
    assert raised.value.name == "own-5"                  # kept for reconciliation
    assert calls() == []                                 # the create client was killed unanswered
    assert [a[0] for a in spawned] == ["create"]         # and nothing was removed by name


def test_refused_create_is_reported_not_created_and_removes_nothing(docker):
    shim, behave, calls, root = docker
    behave(refuse=True)
    with pytest.raises(executor.IsolationUnavailable, match="not created") as raised:
        with executor.owned_container(_create(shim, "own-6", "sleep", "30"), shim, "own-6"):
            pytest.fail("a refused create was started")
    assert not isinstance(raised.value, executor.CleanupUnconfirmed)
    assert [c["args"][0] for c in calls()] == ["create"]


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
    assert {tuple(c["args"]) for c in calls() if c["args"][0] == "rm"} == {("rm", "-f", _created_id(calls))}


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
    assert removal[0]["args"] == ["rm", "-f", _created_id(calls)] and _held(root) == {}


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


def test_success_starts_and_removes_exactly_the_created_id(docker, spawned):
    shim, behave, calls, root = docker
    create = _create(shim, "own-13", "echo", "useful")
    with executor.owned_container(create, shim, "own-13", text=True) as proc:
        out, _ = proc.communicate(timeout=10)
    assert out == "useful\n" and proc.returncode == 0    # the command really ran
    own = _created_id(calls)
    assert [c["args"] for c in calls()] == [create[1:], ["start", "-a", own], ["rm", "-f", own]]
    assert [a[0] for a in spawned] == ["create", "start", "rm"]
    assert _held(root) == {}


def test_create_refused_for_a_name_a_peer_holds_touches_nothing(docker, spawned):
    shim, behave, calls, root = docker
    other = _peer(root, "own-14")
    with pytest.raises(executor.IsolationUnavailable, match="not created.*already in use") as raised:
        with executor.owned_container(_create(shim, "own-14", "sleep", "30"), shim, "own-14"):
            pytest.fail("a refused create was started")
    assert not isinstance(raised.value, executor.CleanupUnconfirmed)
    assert [c["args"][0] for c in calls()] == ["create"]  # no START, no removal
    assert [a[0] for a in spawned] == ["create"]
    assert _held(root) == {other: "own-14"}               # the peer survives


@pytest.mark.parametrize("gone", ["removed", "renamed"])
def test_a_peer_that_later_holds_the_name_survives_the_cleanup(docker, gone):
    """The own container is gone (`--rm`) or renamed; a peer then takes its name."""
    shim, behave, calls, root = docker
    with executor.owned_container(_create(shim, "own-15", "true"), shim, "own-15") as proc:
        proc.wait(timeout=10)
        own = root / "containers" / _created_id(calls)
        if gone == "removed":
            own.unlink()
        else:
            own.write_text(json.dumps({"name": "own-15-renamed", "command": ["true"]}))
        other = _peer(root, "own-15")
    assert calls()[-1]["args"] == ["rm", "-f", own.name]
    assert _held(root) == {other: "own-15"}               # the peer survives; own is gone


def test_a_create_client_that_cannot_spawn_removes_nothing(docker, spawned, tmp_path):
    shim, behave, calls, root = docker
    absent = str(tmp_path / "absent-docker")
    with pytest.raises(FileNotFoundError):
        with executor.owned_container(_create(absent, "own-17", "true"), absent, "own-17"):
            pytest.fail("a launch without a create was started")
    assert [a[0] for a in spawned] == ["create"] and calls() == []


EOF_TEXT = 'error during connect: Post "http://%2Fd.sock/v1.56/containers/create?name=x": EOF'


@pytest.mark.parametrize("exit_as", [
    {"exit_as": "eof", "rc": 1, "stderr": EOF_TEXT},                       # transport EOF
    {"exit_as": "killed"},                                                  # signal-killed client
    {"exit_as": "blank", "rc": 1, "stderr": ""},                            # nonzero, nothing said
    {"exit_as": "late", "rc": 1, "stderr": "Error response from daemon: x\n" + EOF_TEXT},
    {"exit_as": "no-id", "rc": 0, "stderr": "", "stdout": "not an id"},     # rc 0 without the ID
])
def test_a_create_exit_without_the_daemons_answer_is_unconfirmed(docker, spawned, exit_as):
    shim, behave, calls, root = docker
    behave(**exit_as)
    with pytest.raises(executor.CleanupUnconfirmed, match="no answer from the daemon") as raised:
        with executor.owned_container(_create(shim, "own-12", "sleep", "30"), shim, "own-12"):
            pytest.fail("an unanswered create was started")
    assert raised.value.name == "own-12"                 # kept for reconciliation
    assert "not confirmed" in str(raised.value)
    assert [a[0] for a in spawned] == ["create"]         # never started, nothing removed by name
    assert [c["args"][0] for c in calls()] == ["create"]
    assert list(_held(root).values()) == ["own-12"]      # what the daemon made is left to reconcile


@pytest.mark.parametrize("exit_as", [
    {"exit_as": "eof", "rc": 1, "stderr": EOF_TEXT},                       # transport EOF
    {"exit_as": "killed"},                                                  # signal-killed client
    {"create_seconds": 5},                                                  # no answer in time
])
def test_an_unanswered_create_leaves_a_peer_holding_the_name(docker, spawned, monkeypatch, exit_as):
    """The daemon refused it (a peer holds the name), but that answer never arrived."""
    shim, behave, calls, root = docker
    other = _peer(root, "own-16")
    behave(**exit_as)
    monkeypatch.setattr(executor, "CREATE_TIMEOUT", 2)
    with pytest.raises(executor.CleanupUnconfirmed, match="no answer from the daemon") as raised:
        with executor.owned_container(_create(shim, "own-16", "sleep", "30"), shim, "own-16"):
            pytest.fail("an unanswered create was started")
    assert raised.value.name == "own-16"                 # kept for reconciliation
    assert [a[0] for a in spawned] == ["create"]         # never started, nothing removed
    assert _held(root) == {other: "own-16"}              # the peer survives


def test_the_daemons_refusal_is_the_only_negative_answer():
    answer = executor._create_answer
    assert answer(0, "a" * 64 + "\n", "") == "created"
    assert answer(1, "", "Unable to find image 'x' locally\nError response from daemon: no such\n") == "refused"
    assert answer(1, "", EOF_TEXT) is None
    assert answer(-9, "", "Error response from daemon: x") is None     # killed: no answer
    assert answer(1, "", "") is None
    assert answer(0, "", "") is None
