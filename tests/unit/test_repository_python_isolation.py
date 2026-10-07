"""Bounded transport regressions, run only in an admitted disposable CPU job."""
import importlib.util
import json
import os
from pathlib import Path
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


def test_actual_entry_timeout_captures_partial_and_reaps_owned_child(tmp_path):
    pid_file = tmp_path / "child.pid"
    code = f"import os,time;open({str(pid_file)!r},'w').write(str(os.getpid()));print('partial',flush=True);time.sleep(20)"
    result = entry.run({"repo":str(tmp_path),"timeout":0.2,"args":[sys.executable,"-c",code]})
    assert result["timed_out"] and result["returncode"] != 0
    assert "partial" in result["stdout"]
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()),0)


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired(["docker"],1), KeyboardInterrupt()])
def test_adapter_timeout_cancel_cleanup_is_name_scoped(monkeypatch, tmp_path, failure):
    calls=[]
    class Client:
        def communicate(self, timeout):
            raise failure
        def poll(self):
            return None
        def kill(self):
            calls.append(["client-kill"])
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(executor.shutil,"which",lambda _name:"/trusted/docker")
    monkeypatch.setattr(executor.subprocess,"Popen",lambda command,**kwargs:(calls.append(command) or Client()))
    monkeypatch.setattr(executor.subprocess,"run",lambda command,**kwargs:calls.append(command))
    with pytest.raises(type(failure)):
        executor._execute(tmp_path,["python3","-m","pytest"],1)
    launch,remove=calls[:2]
    name=launch[launch.index("--name")+1]
    assert name.startswith("aitelier-cpu-")
    assert remove == ["/trusted/docker","rm","-f",name]
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
        result=bridge_entry.run({"repo":str(tmp_path),"timeout":3,"args":[sys.executable,"-c",client],"relay_socket":socket_path})
        assert done.wait(2)
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

