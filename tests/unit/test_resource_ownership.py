"""Resource authority conformance without launching Godot, zg, or services."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import deployment_quiescence as dq
from core import resource_ownership as ro


class QuietSkillFlow:
    def list_runs(self):
        return []


def history(authority):
    with authority.connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM owners ORDER BY generation")]


def measure(authority, *, commands="", docker=""):
    def runner(argv):
        return subprocess.CompletedProcess(argv, 0, docker if argv[0] == "docker" else commands, "")
    return dq.measure(skillflow=QuietSkillFlow(), ownership_dir=authority.root, command_runner=runner)


@pytest.mark.parametrize("resource", [r for r in ro.RESOURCES if r != "semantic-request"])
def test_admission_lifetime_uniqueness_and_completion(resource_authority, resource):
    authority = resource_authority
    with ro.operation(resource, authority=authority, operation_id="exact-operation") as lease:
        rows, errors = authority.snapshot()
        assert errors == [] and len(rows) == 1
        assert rows[0]["owner_id"] == lease.owner_id
        assert rows[0]["lock_held"] is True
        assert rows[0]["active"] is (not resource.endswith("-service"))
        assert ro.pass_fds() == (lease.fd, lease.capability_fd)
        with pytest.raises(RuntimeError, match="unsettled"):
            authority.acquire(resource)
        with pytest.raises(BlockingIOError):
            authority.recover(lease.owner_id, lease.generation, actor="review", reason="not settled")
    assert authority.snapshot() == ([], [])
    with pytest.raises(sqlite3.IntegrityError):
        authority.acquire(resource, operation_id="exact-operation")
    assert history(authority)[0]["status"] == "released"
    # A new generation may begin, but a completed operation is never replayed.
    with ro.operation(resource, authority=authority) as second:
        assert second.generation > lease.generation


def test_crash_retains_identity_and_requires_exact_explicit_recovery(resource_authority):
    authority = resource_authority
    code = "from core.resource_ownership import Authority; import os; a=Authority(); l=a.acquire('godot',operation_id='crash'); print(l.owner_id,l.generation,flush=True); os._exit(9)"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 9
    owner_id, generation = result.stdout.split()
    rows, errors = authority.snapshot()
    assert len(rows) == 1 and rows[0]["id"] == owner_id
    assert errors and not measure(authority)["quiescent"]
    with pytest.raises(RuntimeError, match="recovery"):
        ro.Authority(authority.root).acquire("godot")
    for supplied in (int(generation) + 1, 0):
        with pytest.raises(RuntimeError, match="exact"):
            authority.recover(owner_id, supplied, actor="operator", reason="closed")
    authority.recover(owner_id, int(generation), actor="operator", reason="stub process exited with no children")
    assert measure(authority)["quiescent"]
    assert history(authority)[0]["status"] == "reconciled"


def test_descendant_lock_survives_launcher_close(resource_authority):
    lease = resource_authority.acquire("godot")
    child = subprocess.Popen([sys.executable, "-c", "import sys; print('ready',flush=True); sys.stdin.read()"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, pass_fds=(lease.fd,))
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(BlockingIOError):
            lease.close(settled=True)
        with pytest.raises(BlockingIOError):
            resource_authority.recover(lease.owner_id, lease.generation, actor="operator", reason="child still lives")
        assert not measure(resource_authority)["quiescent"]
    finally:
        child.stdin.close()
        child.wait(timeout=5)
    resource_authority.recover(lease.owner_id, lease.generation, actor="operator", reason="stub child ended")


def test_semantic_client_failure_cannot_recover_while_daemon_lives(resource_authority):
    service = resource_authority.acquire("semantic-service")
    work = resource_authority.acquire("semantic")
    work.close(settled=False)
    try:
        with pytest.raises(BlockingIOError):
            resource_authority.recover(work.owner_id, work.generation, actor="operator", reason="client ended only")
    finally:
        service.close(settled=True)
    resource_authority.recover(work.owner_id, work.generation, actor="operator", reason="service and effects ended")


def test_concurrent_admission_has_exactly_one_winner(resource_authority):
    barrier = threading.Barrier(8)

    def attempt(_):
        barrier.wait()
        try:
            return resource_authority.acquire("godot")
        except RuntimeError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    leases = [result for result in results if isinstance(result, ro.Lease)]
    losers = [result for result in results if isinstance(result, RuntimeError)]
    assert len(leases) == 1
    assert len(losers) == 7
    assert len(history(resource_authority)) == 1
    leases[0].close(settled=True)


def test_effect_lock_contention_is_a_fail_closed_admission_loser(
        resource_authority, monkeypatch):
    original_lock = resource_authority._lock

    def contended(name, **kwargs):
        if name == "godot.effect.lock":
            raise BlockingIOError("simultaneous owner holds the effect lock")
        return original_lock(name, **kwargs)

    monkeypatch.setattr(resource_authority, "snapshot", lambda **kwargs: ([], []))
    monkeypatch.setattr(resource_authority, "_lock", contended)
    with pytest.raises(RuntimeError, match="concurrent unsettled owner"):
        resource_authority.acquire("godot")
    assert history(resource_authority) == []


def test_recovery_and_fresh_admission_race_finishes_without_deadlock(resource_authority):
    for index in range(30):
        stale = resource_authority.acquire("godot", operation_id=f"stale-{index}")
        stale.close(settled=False)
        barrier = threading.Barrier(2)
        result = {}

        def recover():
            barrier.wait()
            resource_authority.recover(
                stale.owner_id, stale.generation,
                actor="pytest", reason="fixture has no child")

        def acquire():
            barrier.wait()
            try:
                result["lease"] = resource_authority.acquire(
                    "godot", operation_id=f"next-{index}")
            except RuntimeError:
                pass

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(recover), pool.submit(acquire)]
            for future in futures:
                future.result(timeout=2)
        lease = result.get("lease") or resource_authority.acquire(
            "godot", operation_id=f"after-{index}")
        lease.close(settled=True)
        assert resource_authority.snapshot() == ([], [])


def test_cutover_excludes_sidecar_admission(resource_authority):
    code = "from core.resource_ownership import operation; print('before',flush=True);\nwith operation('godot'): print('admitted',flush=True)"
    fence = dq.acquire_cutover_fence()
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "before"
        import select
        assert select.select([child.stdout], [], [], .2)[0] == []
        assert history(resource_authority) == []
        assert measure(resource_authority)["quiescent"]
    finally:
        dq.release_cutover_fence(fence)
    out, _ = child.communicate(timeout=5)
    assert out.strip() == "admitted" and child.returncode == 0


@pytest.mark.parametrize("fault", ["missing-db", "missing-root", "malformed-db", "unknown-schema", "missing-identity", "missing-lock", "symlink-lock", "held-unregistered-lock", "unknown-status"])
def test_unavailable_or_inconsistent_authority_cannot_authorize(resource_authority, tmp_path, fault):
    authority = resource_authority
    lock = None
    if fault == "missing-db":
        authority.database.unlink()
    elif fault == "missing-root":
        authority = ro.Authority(tmp_path / "absent")
    elif fault == "malformed-db":
        authority.database.write_bytes(b"not a database")
    elif fault == "unknown-schema":
        with sqlite3.connect(authority.database) as conn:
            conn.execute("PRAGMA user_version=999")
    elif fault == "missing-identity":
        with authority.connection(write=True) as conn:
            conn.execute("DELETE FROM authority")
    elif fault in {"missing-lock", "symlink-lock"}:
        path = authority.root / "godot.effect.lock"
        path.unlink()
        if fault == "symlink-lock":
            path.symlink_to(authority.root / "semantic.effect.lock")
    elif fault == "held-unregistered-lock":
        lock = authority._lock("godot.effect.lock")
    else:
        lease = authority.acquire("godot")
        lease.close(settled=False)
        with authority.connection(write=True) as conn:
            conn.execute("UPDATE owners SET status='alien'")
    try:
        result = measure(authority)
        assert result["errors"] and result["quiescent"] is False
        with pytest.raises(dq.DeploymentBlocked):
            dq.authorize("restart", result, journal=tmp_path / "journal.json")
    finally:
        if lock is not None:
            os.close(lock)


@pytest.mark.parametrize("text", [
    "123 1 python -c 'exec(open(\"godot_harness.py\").read())'",
    "123 1 sh -c '. godot_harness.py'", "123 1 python < godot_harness.py",
    "123 1 sh -c 'printf godot_harness.py | python'", "123 1 eval_worker --long-gate",
    "123 1 bash -c 'git -C \"$repo\" status'", "123 1 zg index /data",
    "123 1 python", "123 1 node -e 'require(\"zvec-grep-proxy.js\")'",
])
def test_process_text_has_no_ownership_authority(resource_authority, text):
    # Such strings are neither evidence of ownership nor evidence of its absence.
    result = measure(resource_authority, commands=text)
    assert result["quiescent"] and result["errors"] == []
    assert all(row["diagnostic_only"] and not row["active"] for row in result["external_owners"])
    with ro.operation("godot", authority=resource_authority):
        assert not measure(resource_authority, commands="123 1 harmless-name")["quiescent"]


@pytest.mark.parametrize("resource,name,service", [("semantic", "aitelier-zg", ""), ("semantic", "unrelated", "zvec-grep"), ("godot", "aitelier-godot", ""), ("godot", "unrelated", "godot-builder")])
def test_exact_compose_identity_requires_live_authority(resource_authority, resource, name, service):
    result = measure(resource_authority, docker=f"id\t{name}\t{service}\tUp 1 hour\n")
    assert not result["quiescent"]
    assert any("mandatory service authority" in error for error in result["errors"])


@pytest.mark.parametrize("name", ["aitelier-zg-backup", "not-godot", "godot-report", "zvec-grep-docs"])
def test_unrelated_containers_do_not_require_sidecar_ledgers(resource_authority, name):
    assert measure(resource_authority, docker=f"id\t{name}\tother\tUp 1 hour\n")["quiescent"]


@pytest.mark.parametrize("name", ["compile_project", "playtest_project", "check_gdscript", "run_script", "x11_input_smoke"])
def test_every_public_godot_path_enters_authority_before_body(resource_authority, monkeypatch, name):
    spec = importlib.util.spec_from_file_location("owned_harness", "docker/godot/godot_harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    wrapper = getattr(module, name)
    # Replace ONLY the engine body, preserving the actual owned public entry.
    def body(*args, **kwargs):
        assert resource_ownership.pass_fds()
        return resource_ownership.Authority().snapshot()
    monkeypatch.setattr(wrapper.__wrapped__, "__code__", body.__code__)
    rows, errors = wrapper("fixture", _ownership={"project_id": "p", "run_id": "r", "operation_id": name})
    assert errors == [] and rows[0]["resource"] == "godot"
    assert rows[0]["operation_id"] == name and rows[0]["project_id"] == "p"
    assert history(resource_authority)[0]["status"] == "released"


def test_failure_and_caught_uncertainty_never_release(resource_authority):
    with pytest.raises(ValueError):
        with ro.operation("godot", authority=resource_authority):
            raise ValueError("effect unsettled")
    # Lost Godot ownership blocks its own family and global deployment, while
    # an unrelated semantic effect can still register honestly.
    with ro.operation("semantic", authority=resource_authority):
        ro.retain()
    assert all(row["status"] == "active" for row in history(resource_authority))
    assert len(resource_authority.snapshot()[1]) == 2


def test_parallel_requests_have_independent_durable_lifetimes(resource_authority):
    first = resource_authority.acquire("semantic-request", operation_id="request-a")
    second = resource_authority.acquire("semantic-request", operation_id="request-b")
    assert len(resource_authority.snapshot()[0]) == 2
    first.close(settled=True)
    rows, errors = resource_authority.snapshot()
    assert not errors and [row["owner_id"] for row in rows] == [second.owner_id]
    assert not measure(resource_authority)["quiescent"]
    second.close(settled=True)
    assert measure(resource_authority)["quiescent"]


@pytest.mark.parametrize("prefix", ["before-insert", "before-commit", "after-commit"])
def test_crash_prefixes_never_expose_unregistered_effects(resource_authority, prefix):
    code = r'''
import os, sys
from core.resource_ownership import Authority
class Crash(Authority):
    def _lock(self, name, **kwargs):
        fd = super()._lock(name, **kwargs)
        if name == 'godot.effect.lock' and sys.argv[1] == 'before-insert' and kwargs.get('create') is False:
            os._exit(11)
        return fd
    def _check(self):
        result = super()._check()
        if sys.argv[1] == 'before-commit':
            import sqlite3
            # The final connection check occurs with an uncommitted row visible
            # only to that transaction; kill immediately before the commit.
            self.checks = getattr(self, 'checks', 0) + 1
            if self.checks == 4: os._exit(12)
        return result
lease = Crash().acquire('godot')
if sys.argv[1] == 'after-commit': os._exit(13)
print('effect may begin only here')
'''
    # before-insert uses the actual lock boundary; before-commit uses the final
    # connection identity check (snapshot entry/exit then admission entry/exit).
    result = subprocess.run([sys.executable, "-c", code, prefix], capture_output=True, text=True)
    assert result.returncode in {11, 12, 13}
    assert "effect may begin" not in result.stdout
    rows, errors = resource_authority.snapshot()
    if prefix == "after-commit":
        assert rows and errors and not measure(resource_authority)["quiescent"]
    else:
        assert rows == [] and errors == []
        assert measure(resource_authority)["quiescent"]


def test_internal_index_entry_refuses_unregistered_invocation(resource_authority, tmp_path):
    env = {**os.environ, "AITELIER_ZG_INDEXER_LIB_ONLY": "1"}
    env.pop("AITELIER_RESOURCE_LEASE", None)
    marker = tmp_path / "missing-repo"
    result = subprocess.run(["sh", "docker/zvec-grep-entrypoint.sh", "--index-one", str(marker)],
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "requires inherited" in result.stderr
    assert not marker.exists() and history(resource_authority) == []


@pytest.mark.parametrize("borrowed", ["effect-lock", "anonymous-capability"])
def test_inherited_verifier_rejects_borrowed_fd(resource_authority, borrowed):
    lease = resource_authority.acquire("semantic", operation_id="legitimate-owner")
    code = r'''
import json, os, sys, tempfile
from core.resource_ownership import require_inherited
if sys.argv[4] == "effect-lock":
    fd = os.open(sys.argv[1], os.O_RDWR)
else:
    borrowed = tempfile.TemporaryFile()
    borrowed.write(b"x" * 32)
    borrowed.flush()
    fd = borrowed.fileno()
os.environ["AITELIER_RESOURCE_LEASE"] = json.dumps({
    "resource": "semantic", "owner_id": sys.argv[2],
    "generation": int(sys.argv[3]), "fd": fd,
})
try:
    require_inherited("semantic")
except Exception as exc:
    print(type(exc).__name__)
    raise SystemExit(0)
raise SystemExit(9)
'''
    try:
        result = subprocess.run(
            [sys.executable, "-c", code,
             str(resource_authority.root / "semantic.effect.lock"),
             lease.owner_id, str(lease.generation), borrowed],
            capture_output=True, text=True, env={**os.environ})
        assert result.returncode == 0
        assert result.stdout.strip() == "RuntimeError"
        assert resource_authority.snapshot()[0][0]["lock_held"]
    finally:
        lease.close(settled=True)


@pytest.mark.parametrize("resource", ["godot", "semantic-service"])
def test_command_wrapper_inherits_and_forwards_graceful_stop(resource_authority, resource):
    code = "import signal,sys; signal.signal(signal.SIGTERM,lambda *_: sys.exit(0)); print('ready',flush=True); signal.pause()"
    child = subprocess.Popen([sys.executable, "-m", "core.resource_ownership", "run", resource, "--", sys.executable, "-c", code],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        assert resource_authority.snapshot()[0][0]["lock_held"]
        child.terminate()
        _, error = child.communicate(timeout=5)
        assert child.returncode == 0, error
        assert resource_authority.snapshot() == ([], [])
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_missing_database_prevents_effect_callback(resource_authority, tmp_path):
    resource_authority.database.unlink()
    marker = tmp_path / "must-not-exist"
    result = subprocess.run([sys.executable, "-m", "core.resource_ownership", "run", "godot", "--", sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)], capture_output=True)
    assert result.returncode != 0 and not marker.exists()


def test_mixed_version_container_cannot_borrow_another_service_registration(resource_authority):
    with ro.operation("semantic-service", authority=resource_authority):
        result = measure(resource_authority, docker="different-runtime\taitelier-zg\tzvec-grep\tUp 1 minute\n")
        assert any("mandatory service authority" in error for error in result["errors"])


@pytest.mark.parametrize("route,function", [("/compile", "compile_project"), ("/checkgd", "check_gdscript"), ("/playtest", "playtest_project"), ("/script", "run_script"), ("/x11_input_smoke", "x11_input_smoke")])
def test_http_routes_conform_to_same_public_admission(resource_authority, tmp_path, monkeypatch, route, function):
    import threading
    import urllib.request
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "godot" / "owners.sqlite3"))
    spec = importlib.util.spec_from_file_location("http_owned_harness", "docker/godot/godot_harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def body(*args, **kwargs):
        assert resource_ownership.pass_fds()
        rows, errors = resource_ownership.Authority().snapshot()
        return {"owners": rows, "errors": errors}
    monkeypatch.setattr(getattr(module, function).__wrapped__, "__code__", body.__code__)
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module._Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{route}",
            data=json.dumps({"project_dir": str(tmp_path), "project_id": "project", "run_id": "run", "operation_id": "operation"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        assert result["errors"] == []
        assert result["owners"][0]["operation_id"] == "operation"
        assert result["owners"][0]["resource"] == "godot"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert resource_authority.snapshot() == ([], [])


def test_zg_public_wrapper_conforms_before_raw_binary(resource_authority, tmp_path):
    raw = tmp_path / "zg-raw"
    raw.write_text("#!/usr/bin/env python3\nfrom core.resource_ownership import Authority, require_inherited\nimport json\nrequire_inherited('semantic')\nprint(json.dumps(Authority().snapshot()),flush=True)\n")
    raw.chmod(0o755)
    result = subprocess.run([sys.executable, "docker/zg-owned.py", "index", "/fixture"],
        env={**os.environ, "AITELIER_ZG_EXECUTABLE": str(raw)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    rows, errors = json.loads(result.stdout)
    assert not errors and rows[0]["resource"] == "semantic" and rows[0]["lock_held"]
    assert resource_authority.snapshot() == ([], [])


def test_index_control_batch_enters_authority_before_provider(resource_authority, tmp_path, monkeypatch):
    from core import semantic_index_control as sic
    control = sic.IndexControl(tmp_path / "control", tmp_path / "worktrees")
    with control.connection() as conn:
        conn.execute("INSERT INTO indexes(run_id,root,source,desired,revision,activity_at) VALUES('run','/fixture','/source','ready',1,1)")
    def batch(_jobs, _execute, _timeout, _retry, _embedding):
        assert ro.pass_fds()
        rows, errors = resource_authority.snapshot()
        assert not errors and rows[0]["resource"] == "semantic"
        ro.retain()
        return [{"outcome": "error"}]
    monkeypatch.setattr(control, "_process_jobs", batch)
    assert control.process_once() == [{"outcome": "error"}]
    assert not measure(resource_authority)["quiescent"]
    with pytest.raises(RuntimeError, match="recovery"):
        control.process_once()


def test_idle_index_worker_does_not_create_owner_history(resource_authority, tmp_path):
    from core.semantic_index_control import IndexControl
    control = IndexControl(tmp_path / "control", tmp_path / "worktrees")
    assert control.process_once() == []
    assert history(resource_authority) == []


@pytest.mark.parametrize("completion", ["complete", "disconnect"])
def test_proxy_stream_and_parallel_request_own_independent_lifetimes(resource_authority, completion):
    import http.client
    import shutil
    import time
    node = os.environ.get("AITELIER_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node 22 unavailable")
    code = r'''
const http=require('http'); const {createProxy}=require('./docker/zvec-grep-proxy.js');
let streams=[];
const upstream=http.createServer((req,res)=>{
 if(req.url==='/stream'){res.writeHead(200);res.write('open\n');streams.push(res);}
 else res.end('ok');
});
upstream.listen(0,'127.0.0.1',()=>{
 const proxy=createProxy({host:'127.0.0.1',port:upstream.address().port});
 proxy.listen(0,'127.0.0.1',()=>console.log(proxy.address().port));
 process.stdin.on('data',data=>{for(const r of streams)r.end('done\n'); streams=[];});
});
'''
    child = subprocess.Popen([node, "-e", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stream = second = None
    try:
        port = int(child.stdout.readline())
        fence = dq.acquire_cutover_fence()
        try:
            health = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            health.request("GET", "/health")
            probe = health.getresponse()
            assert probe.status == 200
            probe.read()
            health.close()
            assert history(resource_authority) == []
        finally:
            dq.release_cutover_fence(fence)
        stream = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        stream.request("GET", "/stream")
        response = stream.getresponse()
        assert response.status == 200 and response.readline() == b"open\n"
        assert any(row["resource"] == "semantic-request" for row in resource_authority.snapshot()[0])
        assert not measure(resource_authority)["quiescent"]
        second = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        second.request("POST", "/complete", body=b"fixture")
        other = second.getresponse()
        assert other.status == 200 and other.read() == b"ok"
        if completion == "complete":
            child.stdin.write("complete\n")
            child.stdin.flush()
            assert response.read() == b"done\n"
        else:
            response.close()
            stream.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows, errors = resource_authority.snapshot()
            if (completion == "complete" and not rows) or (completion == "disconnect" and errors):
                break
            time.sleep(.01)
        if completion == "complete":
            assert resource_authority.snapshot() == ([], [])
        else:
            assert rows and errors and not measure(resource_authority)["quiescent"]
        assert len(history(resource_authority)) == 2
    finally:
        if stream:
            stream.close()
        if second:
            second.close()
        child.terminate()
        child.wait(timeout=5)


def test_service_registration_does_not_deadlock_cutover_but_effects_wait(resource_authority):
    code = "from core.resource_ownership import operation;\nwith operation('godot-service'):\n print('healthy',flush=True)\n with operation('godot'): print('effect',flush=True)"
    fence = dq.acquire_cutover_fence()
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "healthy"
        rows, errors = resource_authority.snapshot()
        assert not errors and [row["resource"] for row in rows] == ["godot-service"]
        assert measure(resource_authority)["quiescent"]
    finally:
        dq.release_cutover_fence(fence)
    out, _ = child.communicate(timeout=5)
    assert out.strip() == "effect" and child.returncode == 0
    assert resource_authority.snapshot() == ([], [])


@pytest.mark.parametrize("resource,service,name", [("godot", "godot-builder", "aitelier-godot"), ("semantic", "zvec-grep", "aitelier-zg")])
def test_matching_idle_service_with_own_ledger_is_quiet(resource_authority, tmp_path, monkeypatch, resource, service, name):
    monkeypatch.setattr(ro.socket, "gethostname", lambda: "cid")
    semantic_db = tmp_path / "semantic-index-control" / "control.sqlite3"
    godot_db = tmp_path / "godot-control" / "owners.sqlite3"
    if resource == "semantic":
        from core.semantic_index_control import IndexControl
        IndexControl(semantic_db.parent, tmp_path / "worktrees")
    else:
        godot_db.parent.mkdir()
        with sqlite3.connect(godot_db) as conn:
            conn.execute("CREATE TABLE render_owners(owner_id TEXT, generation INTEGER, status TEXT)")
    def runner(argv):
        return subprocess.CompletedProcess(argv, 0, f"cid\t{name}\t{service}\tUp 1 minute\n" if argv[0] == "docker" else "", "")
    with ro.operation(resource + "-service", authority=resource_authority):
        result = dq.measure(skillflow=QuietSkillFlow(), ownership_dir=resource_authority.root,
                            sidecar_db=semantic_db, command_runner=runner)
        assert result["errors"] == [] and result["quiescent"]
        assert dq._validate_observation(result) is None
    if resource == "godot":
        assert not semantic_db.exists()


@pytest.mark.parametrize("status", [
    "Up 1 minute (unhealthy)",
    "Up 1 second (health: starting)",
])
def test_exact_semantic_resident_must_be_stably_healthy(
        resource_authority, tmp_path, monkeypatch, status):
    monkeypatch.setattr(ro.socket, "gethostname", lambda: "runtime-cid")
    from core.semantic_index_control import IndexControl
    semantic_db = tmp_path / "semantic-index-control" / "control.sqlite3"
    IndexControl(semantic_db.parent, tmp_path / "worktrees")

    def runner(argv):
        output = (f"runtime-cid\taitelier-zg\tzvec-grep\t{status}\n"
                  if argv[0] == "docker" else "")
        return subprocess.CompletedProcess(argv, 0, output, "")

    with ro.operation("semantic-service", authority=resource_authority):
        result = dq.measure(
            skillflow=QuietSkillFlow(), ownership_dir=resource_authority.root,
            sidecar_db=semantic_db, command_runner=runner)
    assert not result["quiescent"]
    assert any("stable running state" in error for error in result["errors"])


def test_proxy_refuses_unavailable_authority_before_upstream(resource_authority, tmp_path):
    import http.client
    import shutil
    node = os.environ.get("AITELIER_TEST_NODE") or shutil.which("node")
    if not node:
        pytest.skip("Node 22 unavailable")
    marker = tmp_path / "upstream-invoked"
    code = r'''
const http=require('http'), fs=require('fs'); const {createProxy}=require('./docker/zvec-grep-proxy.js');
const upstream=http.createServer((req,res)=>{fs.writeFileSync(process.argv[1],'invoked');res.end('ok');});
upstream.listen(0,'127.0.0.1',()=>{const proxy=createProxy({host:'127.0.0.1',port:upstream.address().port});proxy.listen(0,'127.0.0.1',()=>console.log(proxy.address().port));});
'''
    resource_authority.database.unlink()
    child = subprocess.Popen([node, "-e", code, str(marker)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        port = int(child.stdout.readline())
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("POST", "/mcp", body=b"fixture")
        response = connection.getresponse()
        assert response.status == 503
        response.read()
        connection.close()
        assert not marker.exists()
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_thread_effect_waits_while_cutover_held(resource_authority):
    import threading
    entered, ready = threading.Event(), threading.Event()
    def launch():
        ready.set()
        with ro.operation("godot", authority=resource_authority):
            entered.set()
    fence = dq.acquire_cutover_fence()
    thread = threading.Thread(target=launch)
    thread.start()
    try:
        assert ready.wait(2)
        assert not entered.wait(.2)
    finally:
        dq.release_cutover_fence(fence)
    thread.join(timeout=5)
    assert entered.is_set() and not thread.is_alive()


@pytest.mark.parametrize("transport", ["inline", "source", "tty", "shell"])
@pytest.mark.parametrize("registered", [False, True])
def test_live_harmless_interpreters_obey_resource_acquisition_not_spelling(resource_authority, tmp_path, transport, registered):
    import pty
    body = ("import sys\nfrom contextlib import nullcontext\nfrom core.resource_ownership import operation\n"
            + ("with operation('godot'):\n" if registered else "with nullcontext():\n")
            + " print('ready',flush=True)\n sys.stdin.readline()\n")
    master = slave = None
    if transport == "source":
        source = tmp_path / "godot_harness.py"
        source.write_text(body)
        command = [sys.executable, "-c", "import sys; exec(open(sys.argv[1]).read())", str(source)]
    elif transport == "shell":
        command = ["sh", "-c", 'exec "$1" -c "$2"', "fixture-shell", sys.executable, body]
    else:
        command = [sys.executable, "-c", body]
    if transport == "tty":
        master, slave = pty.openpty()
    child = subprocess.Popen(command, stdin=slave if slave is not None else subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if slave is not None:
            os.close(slave)
            slave = None
        assert child.stdout.readline().strip() == "ready"
        def runner(argv):
            if argv[0] == "docker":
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        observation = dq.measure(skillflow=QuietSkillFlow(), ownership_dir=resource_authority.root,
                                 command_runner=runner)
        assert observation["errors"] == []
        assert observation["quiescent"] is (not registered)
        if master is not None:
            os.write(master, b"done\n")
        else:
            child.stdin.write("done\n")
            child.stdin.flush()
        child.wait(timeout=5)
        assert child.returncode == 0
        assert resource_authority.snapshot() == ([], [])
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if master is not None:
            os.close(master)
        if slave is not None:
            os.close(slave)



def test_compose_authority_namespace_and_unique_service_keys():
    from cli import server
    import yaml
    class UniqueLoader(yaml.SafeLoader):
        pass
    def mapping(loader, node, deep=False):
        result = {}
        for key, value in node.value:
            name = loader.construct_object(key, deep=deep)
            assert name not in result, f"duplicate YAML key: {name}"
            result[name] = loader.construct_object(value, deep=deep)
        return result
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    root = Path(__file__).resolve().parents[2]
    services = yaml.load((root / "docker-compose.yml").read_text(), Loader=UniqueLoader)["services"]
    assert set(server._COMPOSE_SERVICES) == {
        "aitelier", "zvec-grep", "godot-builder",
    }
    semantic = services["zvec-grep"]
    assert semantic["environment"]["AITELIER_OWNERSHIP_DIR"] == "${HOME}/.AItelier/godot-control"
    assert semantic["environment"]["AITELIER_SEMANTIC_CONTROL_DIR"] == "${HOME}/.AItelier/semantic-index-control"
    assert "${HOME}/.AItelier:${HOME}/.AItelier" in semantic["volumes"]
    godot = services["godot-builder"]
    assert godot["environment"]["AITELIER_OWNERSHIP_DIR"] == "/var/lib/aitelier-godot"
    assert "${HOME}/.AItelier/godot-control:/var/lib/aitelier-godot" in godot["volumes"]
    assert services["aitelier"]["environment"]["HOME"] == "${HOME}"
