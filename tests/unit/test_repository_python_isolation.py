"""Bounded transport regressions, run only in an admitted disposable CPU job."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile

import pytest
from core import repository_executor as executor
from core import repository_executor_entry as entry
from aitelier.tools.run_tests import impl as candidate

@pytest.fixture
def rt():
    baseline = os.environ.get("REPOSITORY_ISOLATION_BASELINE")
    if not baseline:
        return candidate
    spec = importlib.util.spec_from_file_location("baseline_run_tests", baseline)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(tmp_path, body="assert True"):
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests/test_fixture.py").write_text("def test_fixture():\n    " + body + "\n")
    return repo


def test_missing_facility_never_runs_local_pytest(rt, tmp_path, monkeypatch):
    marker = tmp_path / "unsafe-local-test"
    repo = _fixture(tmp_path, f"open({str(marker)!r}, 'w').write('ran')")
    monkeypatch.setattr(executor.shutil, "which", lambda _name: None)
    monkeypatch.setattr(candidate.datadir, "aitelier_home", lambda: tmp_path / "control")
    result = rt.run_tests(project_root=str(repo), out_dir=str(tmp_path / "out"), repo_gate=False)
    report = json.loads((tmp_path / "out/test_report.json").read_text())
    assert result["passed"] is False, report
    assert report["infrastructure_unavailable"] is True
    assert report["skipped_because"] == "cpu_executor_unavailable"
    assert report["returncode"] == -1
    assert not marker.exists(), "authored pytest ran without the isolated facility"


@pytest.mark.skipif(not os.environ.get('REVIEW_AUTHORED_GODOT_GATE'), reason=(
    'needs REVIEW_AUTHORED_GODOT_GATE=<path to the reviewed authored tools/godot_gate.py '
    '(defines run_python_suite); that file is not in this tree>'))
def test_authored_godot_python_leg_is_not_a_backend_child(rt, tmp_path, monkeypatch):
    marker = tmp_path / "unsafe-authored-test"
    repo = _fixture(tmp_path, f"open({str(marker)!r}, 'w').write('ran')")
    source = os.environ["REVIEW_AUTHORED_GODOT_GATE"]
    # Real shipped run_python_suite, with no compile/render request.
    driver = ("import importlib.util,json,pathlib; "
              f"s=importlib.util.spec_from_file_location('authored', {source!r}); "
              "m=importlib.util.module_from_spec(s);s.loader.exec_module(m); "
              "r=m.run_python_suite(pathlib.Path.cwd());print(json.dumps(r)); "
              "raise SystemExit(r['returncode'])")
    script = repo / "run_tests.sh"
    script.write_text("#!/bin/sh\npython3 -c " + __import__('shlex').quote(driver) + "\n")
    script.chmod(0o755)
    monkeypatch.setattr(executor.shutil, "which", lambda _name: None)
    control = Path(tempfile.mkdtemp(prefix="cpu-isolation-control-"))
    monkeypatch.setattr(candidate.datadir, "aitelier_home", lambda: control)
    result = rt._run_repo_gate(repo)
    assert result["passed"] is False, result
    assert result["runner_error"] is True
    assert "Docker CPU execution facility" in result["output"]
    assert result["measured"] == candidate.REPO_GATE_UNMEASURED
    assert not marker.exists(), "authored gate launched pytest locally"


@pytest.mark.parametrize("failing", [False, True])
def test_actual_container_entry_captures_real_pytest_and_junit(tmp_path, failing):
    repo = _fixture(tmp_path, "print('fixture-stdout'); __import__('sys').stderr.write('fixture-stderr\\n'); assert " + str(not failing))
    junit = tmp_path / "junit.xml"
    result = entry.run({"repo":str(repo), "timeout":10,
                        "args":[sys.executable,"-m","pytest","tests","-q","-s","-p","no:cacheprovider",f"--junitxml={junit}","-o","junit_family=xunit1"]})
    _retain_bridge_result("pytest-fail" if failing else "pytest-pass", result, {"junit_ids": sorted(candidate._junit_node_ids(junit))})
    assert result["returncode"] == (1 if failing else 0), result
    assert result["timed_out"] is False
    assert "fixture-stdout" in result["stdout"]
    assert "fixture-stderr" in result["stderr"]
    assert candidate._junit_node_ids(junit) == {"tests/test_fixture.py::test_fixture"}


def _witness_pid(witness):
    """The pid a witness file carries, or a named failure -- never `int('')`.

    The child publishes its pid AS THE NAME of the witness, created by a single
    open(2) (`: > child.pid.$$`), so a witness that exists carries a COMPLETE
    pid: there is no "created but not yet written" state to read. (The previous
    shape wrote the pid INTO a file the shell had just created/truncated, and a
    SIGKILL inside that window left an empty file, so `int('')` reported a
    passed contract as `ValueError: invalid literal for int() with base 10: ''`.)
    Anything else is reported by name here instead of being raised from int().
    """
    suffix = witness.name.rpartition(".")[2]
    if not suffix.isdigit():
        pytest.fail(f"pid witness {witness} does not carry a pid in its name, so the "
                    "kill landed before the witness was published")
    return int(suffix)


def test_actual_entry_timeout_captures_partial_and_reaps_owned_child(tmp_path):
    # The budget clock starts when entry.run is called, so the child's first
    # byte must not depend on CPython start-up (interpreter init alone can exceed
    # 0.2 s under CPU contention). /bin/sh prints "partial" BEFORE it publishes
    # its pid, so a witness is proof that the byte already reached the pipe;
    # `exec` keeps that pid for the reap check below.
    #
    # The pid travels in the witness FILE NAME, published by one open(2) and by
    # nothing else, so existence implies a complete pid. Writing the pid INTO a
    # witness (`echo $$ > child.pid`) takes two steps instead -- the shell
    # creates/truncates the target, then writes into it -- and the 0.2 s SIGKILL
    # can land between them, leaving an EMPTY file: measured in a whole-suite run
    # on 2026-11-09, where int(pid_file.read_text()) raised ValueError from inside
    # a passed contract. A file name cannot be half-created, and publishing costs
    # the child no extra fork/exec (a shell has no rename builtin, so a
    # temp-file+rename witness would spend budget on starting `mv`).
    script = f"echo partial; : > {shlex.quote(str(tmp_path))}/child.pid.$$; exec sleep 20"
    result = entry.run({"repo":str(tmp_path),"timeout":0.2,"args":["/bin/sh","-c",script]})
    assert result["timed_out"] and result["returncode"] != 0
    witnesses = sorted(tmp_path.glob("child.pid.*"))
    # Child-side witness: the child got its first write out inside the budget,
    # and `witness.exists()` here means the pid was FULLY written -- the witness
    # carries the pid in its name, so nothing is ever written into it and there
    # is no half-published state. Without a witness, an empty stdout would be a
    # timing miss, not the partial-capture contract, and the assertion below
    # would blame the wrong thing.
    assert len(witnesses) == 1, (
        "child never published its pid inside the 0.2 s budget (start-up outran "
        f"the budget under load): witnesses={[w.name for w in witnesses]}: {result!r}")
    assert "partial" in result["stdout"]
    with pytest.raises(ProcessLookupError):
        os.kill(_witness_pid(witnesses[0]),0)


def test_actual_entry_timeout_reaps_the_owned_group_when_the_direct_child_dies(tmp_path):
    # The witness above ends its child with `exec`, so the DIRECT CHILD is the
    # owned process-group leader and `proc.kill()` alone reaps it: replacing
    # both `os.killpg(proc.pid, SIGKILL)` call sites in
    # core/repository_executor_entry.py with `pass` left it green
    # (iss-f3f078df588a402f) -- it could not see a removed group kill. Here the
    # direct child PUBLISHES, forks, and then kills itself with the SAME signal
    # a group kill delivers (`kill -9 $$`), while the grandchild (`sleep 20 &`
    # -- it inherits the shell's new session/group from start_new_session, and
    # `sleep` never calls setsid, so its process group is the dead direct
    # child's pid) stays alive holding BOTH output pipes open. `communicate`
    # wants EOF on the pipes, so the 0.2 s budget expires on the SURVIVING
    # writer, never on a slow child, and the returncode is -9 in both arms:
    # the three contracts above are unchanged (timed_out true, returncode
    # nonzero, "partial" already in the pipe) and the only thing left to
    # observe is whether the survivor died. `proc.kill()` can only address the
    # direct child's pid, which is a zombie by then, so a live survivor means
    # the GROUP was never killed.
    #
    # Both pids travel as witness FILE NAMES, one open(2) each, for the reason
    # recorded on `_witness_pid`: a name cannot be half-written, so an existing
    # witness always carries a complete pid. The second witness carries the
    # direct child's own pid, which makes "the pid polled below is a
    # grandchild, not the direct child" an assertion instead of a comment.
    root = shlex.quote(str(tmp_path))
    script = (f"echo partial; sleep 20 & : > {root}/child.pid.$!; "
              f": > {root}/shell.pid.$$; kill -9 $$")
    result = entry.run({"repo":str(tmp_path),"timeout":0.2,"args":["/bin/sh","-c",script]})
    assert result["timed_out"] and result["returncode"] != 0, result
    survivors = sorted(tmp_path.glob("child.pid.*"))
    direct = sorted(tmp_path.glob("shell.pid.*"))
    # Child-side witness, same contract as above: both pids were published
    # inside the budget, after `echo partial` reached the pipe.
    assert len(survivors) == 1 and len(direct) == 1, (
        "child never published both pids inside the 0.2 s budget (start-up "
        f"outran the budget under load): survivors={[w.name for w in survivors]}: "
        f"direct={[w.name for w in direct]}: {result!r}")
    assert "partial" in result["stdout"], result
    survivor, direct_pid = _witness_pid(survivors[0]), _witness_pid(direct[0])
    assert survivor != direct_pid, (
        f"the polled pid must be the GRANDCHILD's: survivor={survivor} "
        f"direct child={direct_pid}")
    try:
        os.kill(survivor,0)
        alive = True
    except ProcessLookupError:
        alive = False
    if alive:
        # Never leave a survivor holding this tmp_path for the rest of the
        # suite when the entry did not kill the group it owns.
        os.kill(survivor,signal.SIGKILL)
    assert alive is False, (
        f"the surviving grandchild {survivor} (process group {direct_pid}) "
        f"outlived the timeout: proc.kill() addresses the already-dead direct "
        f"child, so only the group kill can reach it: {result!r}")


def _grandchild_state(pid):
    """`pid`'s /proc state right now, or 'gone' once the kernel has no trace of it.

    Read from /proc/<pid>/stat rather than `os.kill(pid, 0)` because a ZOMBIE
    answers signal 0: the contract here is that the detached grandchild was
    KILLED AND COLLECTED, not merely that it stopped running. The comm field is
    wrapped in parentheses and may itself contain them, so the state is the
    first field after the LAST ')'. Anything unreadable is reported by name
    instead of raising, so a failed contract is a legible line rather than an
    exception from inside the assertion.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return "gone"
    except OSError as exc:
        return f"unreadable ({type(exc).__name__})"
    fields = raw.rpartition(")")[2].split()
    return fields[0] if fields else "unparsable"


_REAPED_STATES = frozenset({"gone", "Z", "X"})   # no /proc entry, zombie, dead


def test_actual_entry_normal_exit_reaps_a_detached_grandchild(tmp_path):
    # This witness exists for the exit path that fires NO timeout. Both
    # witnesses above build their shape by timing out, so they are answered by
    # the TIMEOUT branch's own `os.killpg`: with only the finally-branch call
    # replaced by `pass` they stay green, i.e. they cannot see that site at all
    # (iss-4de060cff56140e9). Here the direct child exits 0 while leaving a
    # `sleep 20 &` grandchild in the same process group (it inherits the new
    # session/group that `start_new_session=True` gave the shell, and `sleep`
    # never calls setsid), so:
    #   * `communicate()` returns because the DIRECT CHILD exited -- which is
    #     why the grandchild MUST redirect its stdout/stderr (`>/dev/null
    #     2>&1`); without that it holds both pipes open, communicate has no
    #     EOF, the budget expires and this becomes the timeout shape again;
    #   * `proc.kill()` cannot help: communicate has already reaped the direct
    #     child, so its pid is gone and only the group can be addressed;
    #   * no other code runs between the return and the `finally:` block.
    # So the surviving-or-not grandchild is an observation about exactly one
    # statement: the finally branch's `os.killpg(proc.pid, signal.SIGKILL)`
    # around core/repository_executor_entry.py:612.
    #
    # Measured on the delivered tree with ONLY that call site replaced by
    # `pass` (a writable copy inside the pytest container): after `run()`
    # returned rc 0 / timed_out False the grandchild's /proc state was `S` at
    # +0.0, +0.3 and +1.5 s. The 1 s bounded wait below is therefore long
    # enough to report the survivor's state and short enough not to mistake a
    # reap-in-progress for a contract breach.
    import time
    root = shlex.quote(str(tmp_path))
    # `$!` is the fork'd grandchild, live from the instant of the fork; the
    # shell writes both pids BEFORE it exits, so `run()` returning implies the
    # two witness files carry complete values (nothing here races a 0.2 s
    # budget). The direct child's pid is published too, so "the pid polled
    # below is a grandchild, not the direct child" is an assertion and not a
    # comment.
    script = (f"sleep 20 >/dev/null 2>&1 & echo $! > {root}/grandchild.pid; "
              f"echo $$ > {root}/shell.pid; echo partial; exit 0")
    result = entry.run({"repo":str(tmp_path),"timeout":30,"args":["/bin/sh","-c",script]})
    # The NORMAL exit path: rc 0, no timeout. If this ever becomes a timeout,
    # the assertions below would be about the timeout branch and the witness
    # would be blind in exactly the way it was written to fix.
    assert result["returncode"] == 0, result
    assert result["timed_out"] is False, result
    assert "partial" in result["stdout"], result
    pid_file, shell_file = tmp_path / "grandchild.pid", tmp_path / "shell.pid"
    assert pid_file.exists() and shell_file.exists(), (
        f"the child never published both pids: files={sorted(p.name for p in tmp_path.iterdir())}: "
        f"{result!r}")
    grandchild, direct_pid = int(pid_file.read_text().strip()), int(shell_file.read_text().strip())
    assert grandchild != direct_pid, (
        f"the polled pid must be the GRANDCHILD's: grandchild={grandchild} "
        f"direct child={direct_pid}")
    deadline = time.monotonic() + 1.0
    state = _grandchild_state(grandchild)
    while state not in _REAPED_STATES and time.monotonic() < deadline:
        time.sleep(0.02)
        state = _grandchild_state(grandchild)
    if state not in _REAPED_STATES:
        # Never leave a survivor holding this tmp_path for the rest of the
        # suite when the entry did not kill the group it owns.
        try:
            os.kill(grandchild,signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert state in _REAPED_STATES, (
        f"the detached grandchild {grandchild} (process group {direct_pid}) "
        f"survived a NORMAL exit: run() returned rc 0 / timed_out False and "
        f"/proc/{grandchild}/stat still reports state {state!r} after the 1 s "
        f"bound; the direct child is already reaped by communicate(), so only "
        f"the finally-branch group kill can reach it: {result!r}")


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["docker"],1), KeyboardInterrupt()])
def test_adapter_timeout_cancel_cleanup_is_id_scoped(monkeypatch, tmp_path, failure):
    calls=[]
    class Created:
        returncode = 0
        def communicate(self, timeout):
            return "c" * 64 + "\n", ""
    class Client:
        stdout = stderr = None
        def communicate(self, timeout):
            raise failure
        def poll(self):
            return None
        def kill(self):
            calls.append(["client-kill"])
        def wait(self, timeout):
            calls.append(["client-reaped"])
            return 0
    def popen(command, **kwargs):
        calls.append(command)
        if command[1] == "create":
            return Created()
        client = Client()
        client.stdout, client.stderr = tempfile.TemporaryFile(), tempfile.TemporaryFile()
        return client
    monkeypatch.setattr(executor.shutil,"which",lambda _name:"/trusted/docker")
    monkeypatch.setattr(executor.subprocess,"Popen",popen)
    monkeypatch.setattr(executor.subprocess,"run",lambda command,**kwargs:(
        calls.append(command) or subprocess.CompletedProcess(command, 0, "", "")))
    with pytest.raises(type(failure)):
        executor._execute(tmp_path,["python3","-m","pytest"],1)
    launch,start,kill,reaped,remove=calls
    name=launch[launch.index("--name")+1]
    assert name.startswith("aitelier-cpu-") and launch[:2] == ["/trusted/docker","create"]
    assert start == ["/trusted/docker","start","-a","c" * 64]
    # Own client stopped and reaped before removal of the acknowledged ID.
    assert kill == ["client-kill"] and reaped == ["client-reaped"]
    assert remove == ["/trusted/docker","rm","-f","c" * 64]
    for flag,value in [("--network","none"),("--cpus","2"),("--memory","2g"),("--pids-limit","512")]:
        assert launch[launch.index(flag)+1] == value
    assert "--rm" in launch and "--init" in launch
    assert not any('docker.sock' in value or '/run/aitelier-secrets' in value for value in launch)


def test_run_tests_timeout_has_no_success_exit_code(tmp_path, monkeypatch):
    repo = _fixture(tmp_path)
    monkeypatch.setattr(candidate.datadir, "aitelier_home", lambda: tmp_path / "control")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(["python3","-m","pytest"], 1, "partial-out", "partial-err")
    monkeypatch.setattr(executor,"execute",timeout)
    candidate.run_tests(project_root=str(repo),out_dir=str(tmp_path/"out"),repo_gate=False)
    report=json.loads((tmp_path/"out/test_report.json").read_text())
    assert report["passed"] is False and report["timed_out"]
    assert report["returncode"] is None
    assert report["stdout"]=="partial-out" and report["stderr"]=="partial-err"
    assert report["skipped_because"]=="pytest_timeout"


def test_actual_unix_bridge_preserves_existing_admission_observation(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from aitelier.gate_admission import AdmissionRelay
    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(b'{"fixture":"echo"}')
        def log_message(self,*_args):
            pass
    upstream=ThreadingHTTPServer(("127.0.0.1",0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    relay=AdmissionRelay(f"http://127.0.0.1:{upstream.server_port}",render_wait_sec=3,upstream_timeout=5)
    socket_path=str(Path(tempfile.mkdtemp(prefix="cpu-relay-"))/"a.sock")
    relay.start(unix_socket=socket_path)
    try:
        command=[sys.executable,"-c","import os,urllib.request;print(urllib.request.urlopen(os.environ['GODOT_BUILDER_URL']+'/health').read().decode())"]
        result=entry.run({"repo":str(tmp_path),"timeout":5,"args":command,"relay_socket":socket_path})
        assert result["returncode"]==0 and '"fixture":"echo"' in result["stdout"],result
        records=relay.snapshot()
        assert len(records)==1 and records[0]["route"]=="/health"
        assert records[0]["status"]==200 and records[0]["outcome"]=="answered"
    finally:
        relay.stop();upstream.shutdown();upstream.server_close()
        Path(socket_path).unlink(missing_ok=True)


@pytest.fixture
def bridge_entry():
    baseline = os.environ.get("REPOSITORY_INTERIM_BASELINE")
    if not baseline:
        return entry
    spec = importlib.util.spec_from_file_location("baseline_entry", baseline)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _retain_bridge_result(name, result, observations):
    directory = os.environ.get("REPOSITORY_INTERIM_REPORTS")
    if directory:
        path = Path(directory)
        label = "baseline" if os.environ.get("REPOSITORY_INTERIM_BASELINE") else "candidate"
        prefix = path / (label + "-" + name)
        prefix.with_suffix(".json").write_text(json.dumps({"child": result, "observations": observations}, indent=2))
        prefix.with_suffix(".stdout").write_text(result["stdout"])
        prefix.with_suffix(".stderr").write_text(result["stderr"])
        prefix.with_suffix(".rc").write_text(str(result["returncode"]) + "\n")


_WIRE_CLIENT = r"""
import os,socket,time,json,urllib.parse
url=urllib.parse.urlsplit(os.environ['GODOT_BUILDER_URL']);s=socket.create_connection((url.hostname,url.port));s.settimeout(.2)
s.sendall(b'POST /script HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}')
started=time.monotonic();chunks=[]
try:
 while True:
  chunk=s.recv(65536)
  if not chunk:break
  chunks.append({'elapsed':time.monotonic()-started,'wire':chunk.decode('iso-8859-1')})
finally:
 print(json.dumps(chunks),flush=True);s.close()
"""
_HALFCLOSE_BEFORE_CLIENT = r"""
import os,socket,time,json,urllib.parse
url=urllib.parse.urlsplit(os.environ['GODOT_BUILDER_URL']);s=socket.create_connection((url.hostname,url.port));s.settimeout(2.0)  # receive budget (.2s raced the server's intentional .2s pre-response delay)
s.sendall(b'POST /script HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}')
s.shutdown(socket.SHUT_WR)
started=time.monotonic();chunks=[]
try:
 while True:
  chunk=s.recv(65536)
  if not chunk:break
  chunks.append({'elapsed':time.monotonic()-started,'wire':chunk.decode('iso-8859-1')})
finally:
 print(json.dumps(chunks),flush=True);s.close()
"""


_HALFCLOSE_AFTER_CLIENT = r"""
import os,socket,time,json,urllib.parse
url=urllib.parse.urlsplit(os.environ['GODOT_BUILDER_URL']);s=socket.create_connection((url.hostname,url.port));s.settimeout(.2)
s.sendall(b'POST /script HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}')
started=time.monotonic();chunks=[];half_closed=False
try:
 while True:
  chunk=s.recv(65536)
  if not chunk:break
  chunks.append({'elapsed':time.monotonic()-started,'wire':chunk.decode('iso-8859-1')})
  if not half_closed and b'\r\n\r\n' in b''.join(c['wire'].encode('iso-8859-1') for c in chunks):
   s.shutdown(socket.SHUT_WR);half_closed=True
finally:
 print(json.dumps(chunks),flush=True);s.close()
"""


_FULLCLOSE_CLIENT = r"""
import os,socket,time,urllib.parse
url=urllib.parse.urlsplit(os.environ['GODOT_BUILDER_URL']);s=socket.create_connection((url.hostname,url.port))
s.sendall(b'POST /script HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{}')
time.sleep(.05);s.close();print('client-left')
"""

def test_bridge_preserves_admission_keepalives(bridge_entry, tmp_path):
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from aitelier.gate_admission import AdmissionRelay
    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            body=b'{"owners":[]}'
            self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(.6)
            body=b'{"control":true}'
            self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):
            pass
    upstream=ThreadingHTTPServer(("127.0.0.1",0),Upstream)
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    relay=AdmissionRelay(f"http://127.0.0.1:{upstream.server_port}",render_wait_sec=2,upstream_timeout=3,keepalive_sec=.04)
    socket_path=str(Path(tempfile.mkdtemp(prefix="interim-relay-"))/"r.sock")
    relay.start(unix_socket=socket_path)
    try:
        result=bridge_entry.run({"repo":str(tmp_path),"timeout":3,"args":[sys.executable,"-c",_WIRE_CLIENT],"relay_socket":socket_path})
        time.sleep(.65)
        records=relay.snapshot()
        _retain_bridge_result("interim",result,records)
        assert result["returncode"]==0,result
        chunks=json.loads(result["stdout"])
        wire="".join(c["wire"] for c in chunks)
        assert wire.count("HTTP/1.1 100 Continue") == records[0]["keepalives"] > 0
        assert '200 OK' in wire and wire.endswith('{"control":true}')
        assert chunks[0]["elapsed"] < .2
    finally:
        relay.stop();upstream.shutdown();upstream.server_close();Path(socket_path).unlink(missing_ok=True)


@pytest.mark.parametrize("disconnect",[False,True])
def test_bridge_streams_final_and_closes_abandoned_request(bridge_entry,tmp_path,disconnect):
    import threading
    import time
    from socketserver import StreamRequestHandler,ThreadingUnixStreamServer
    observations={}
    done=threading.Event()
    class Upstream(StreamRequestHandler):
        def handle(self):
            while self.rfile.readline().strip():
                pass
            self.rfile.read(2)
            if disconnect:
                # The peer closed both directions, so a client-bound write
                # must eventually fail. That write error (plus the prompt
                # Unix-request close it triggers) is the observable signal,
                # not the bare read EOF that a supported write half-close
                # also produces.
                time.sleep(.2)
                started=time.monotonic()
                try:
                    for piece in (b'HTTP/1.0 201 Created\r\nContent-Length: 4\r\n\r\n',b'abcd',b' ',b' ',b' ',b' ',b' ',b' ',b' ',b' ',b' ',b' '):
                        self.wfile.write(piece);self.wfile.flush();time.sleep(.03)
                except (BrokenPipeError,ConnectionResetError):
                    observations["write_failed"]=True
                observations["closed_after_sec"]=time.monotonic()-started
            else:
                self.wfile.write(b'HTTP/1.0 201 Created\r\nContent-Length: 4\r\nX-Control: retained\r\n\r\n');self.wfile.flush()
                try:
                    for byte in b'abcd':
                        time.sleep(.1);self.wfile.write(bytes([byte]));self.wfile.flush()
                except BrokenPipeError:
                    observations["broken_pipe"] = True
            done.set()
    socket_path=str(Path(tempfile.mkdtemp(prefix="stream-relay-"))/"r.sock")
    upstream=ThreadingUnixStreamServer(socket_path,Upstream);upstream.daemon_threads=True
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    client=_FULLCLOSE_CLIENT if disconnect else _WIRE_CLIENT
    try:
        result=bridge_entry.run({"repo":str(tmp_path),"timeout":3,"args":[sys.executable,"-c",client],"relay_socket":socket_path})
        assert done.wait(2)
        _retain_bridge_result("disconnect" if disconnect else "body",result,observations)
        assert result["returncode"] == 0,result
        if disconnect:
            # A readable client socket / read EOF is NOT abandonment proof;
            # only the failed client-bound write is. The request is dropped
            # on that write error, well inside the bounded timeout, and the
            # finite cleanup is asserted rather than an impossible instant
            # EOF signal.
            assert observations.get("write_failed") is True,observations
            assert observations["closed_after_sec"] < 1.0,observations
        else:
            chunks=json.loads(result["stdout"]);wire="".join(c["wire"] for c in chunks)
            assert '201 Created' in wire and 'X-Control: retained' in wire and wire.endswith('abcd')
            assert chunks[0]["elapsed"] < .2
    finally:
        upstream.shutdown();upstream.server_close();Path(socket_path).unlink(missing_ok=True)


@pytest.mark.parametrize("halfclose_after_headers",[False,True])
def test_bridge_keeps_forwarding_after_client_write_halfclose(bridge_entry,tmp_path,halfclose_after_headers):
    import threading
    import time
    from socketserver import StreamRequestHandler,ThreadingUnixStreamServer
    observations={}
    done=threading.Event()
    class Upstream(StreamRequestHandler):
        def handle(self):
            while self.rfile.readline().strip():
                pass
            self.rfile.read(2)
            if not halfclose_after_headers:
                time.sleep(.2)
            self.wfile.write(b'HTTP/1.0 201 Created\r\nContent-Length: 8\r\nX-Control: retained\r\n\r\n');self.wfile.flush()
            try:
                for byte in b'complete':
                    time.sleep(.06);self.wfile.write(bytes([byte]));self.wfile.flush()
            except BrokenPipeError:
                observations["broken_pipe"]=True
            done.set()
    socket_path=str(Path(tempfile.mkdtemp(prefix="halfclose-relay-"))/"r.sock")
    upstream=ThreadingUnixStreamServer(socket_path,Upstream);upstream.daemon_threads=True
    threading.Thread(target=upstream.serve_forever,daemon=True).start()
    client=_HALFCLOSE_AFTER_CLIENT if halfclose_after_headers else _HALFCLOSE_BEFORE_CLIENT
    try:
        # The budget below is scaffolding, NOT the assertion: no assertion in
        # this witness is about elapsed time (return code, broken_pipe, the
        # headers and the complete body are). It has to outlast interpreter
        # start-up plus scheduling delay, not the paced upstream (8 bytes at
        # 0.06 s), or CPU contention spends it and reports a false red
        # (iss-22d5084f51764d66). The same bound must be used for the
        # post-condition wait, since the upstream handler's last write and
        # `done.set()` run inside the child's execution.
        result=bridge_entry.run({"repo":str(tmp_path),"timeout":30,"args":[sys.executable,"-c",client],"relay_socket":socket_path})
        assert done.wait(30)
        _retain_bridge_result("halfclose-after" if halfclose_after_headers else "halfclose-before",result,observations)
        assert result["returncode"] == 0,result
        # A supported write half-close (RFC 9112 9.6) must not be mistaken
        # for abandonment, so the upstream never sees a broken pipe and the
        # client still receives the complete, correct response body.
        assert observations.get("broken_pipe") is not True,observations
        chunks=json.loads(result["stdout"]);wire="".join(c["wire"] for c in chunks)
        assert '201 Created' in wire and 'X-Control: retained' in wire,wire
        assert wire.endswith('complete'),wire
    finally:
        upstream.shutdown();upstream.server_close();Path(socket_path).unlink(missing_ok=True)

