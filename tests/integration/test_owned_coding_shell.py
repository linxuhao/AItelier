"""Genuine public SDK claims, existing host launcher, and actual owned containers."""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
import uuid

import pytest
from skillflow import PipelineGraph, SkillFlow

from core import coding_shell as shell, repository_executor as ex, run_isolation as ri
from core.db_manager import DBManager

pytestmark = pytest.mark.skipif("OWNED_SHELL_FIXTURE" not in os.environ,
                               reason="requires the owned stdlib-launcher test controller")


def wait_file(path, timeout=30):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, str(path)
        time.sleep(.02)
    return path


@pytest.fixture(scope="session")
def runtime():
    root = Path(os.environ["OWNED_SHELL_FIXTURE"])
    home = root / "h"
    home.mkdir(parents=True, exist_ok=True)
    previous = os.environ.get("AITELIER_HOME")
    os.environ["AITELIER_HOME"] = str(home)
    os.environ["AITELIER_ZVEC_LIFECYCLE"] = "0"
    db = DBManager(str(home / "aitelier.db"))
    sf = SkillFlow(str(home / "skillflow.db"), workspace_base=str(home / "workspaces"),
                   projects_base=str(home / "projects"),
                   code_path_resolver=lambda pid, run_id=None: ri.resolve_for_resolver(db, run_id, sf=sf))
    sf.register_agent_config("owned_shell_fixture", model="host", tools=[])
    graph = {"name": "owned_shell_fixture", "begin": "work", "steps": [
        {"id": "work", "step_type": "agent", "agent_config": "owned_shell_fixture",
         "timeout_seconds": 0, "context": [{"source": {"from": "repository", "mode": "tool"}}],
         "output": {"mode": "content", "fixed": {"summary": "summary.txt"}},
         "transitions": [{"to": None}]}]}
    graph_path = root / "graph.json"
    graph_path.write_text(json.dumps(graph))
    sf.register_graph(PipelineGraph.from_yaml(str(graph_path)))
    entry = Path(ex.__file__).with_name("repository_executor_entry.py")
    cfg = {"allowed_roots": [str(root)], "aitelier_home": str(home),
           "state_db": db.db_path, "test_image": os.environ["OWNED_SHELL_IMAGE"],
           "executor_entry": os.environ["OWNED_SHELL_ENTRY"], "allowed_uids": [1000],
           "coding_shell_engine_db": str(home / "skillflow.db"),
           "coding_shell_entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest()}
    assert importlib.metadata.version("skillflow-py") == "1.5.92"
    (root / "runtime-identity.json").write_text(json.dumps({
        "sdk": importlib.metadata.version("skillflow-py"), "sdk_core": __import__('skillflow.core', fromlist=['']).__file__,
        "host_entry": cfg["executor_entry"], "entry_sha256": cfg["coding_shell_entry_sha256"],
        "executor": ex.__file__, "grant_module": shell.__file__, "image": cfg["test_image"]}, indent=2))
    (root / "config.json").write_text(json.dumps(cfg))
    (root / "ready").touch()
    wait_file(root / "s")
    os.environ[ex.HOST_LAUNCHER_ENV] = str(root / "s")
    runs = []

    def make():
        pid = "shell-fixture-" + uuid.uuid4().hex
        source = root / pid
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        for name, value in {"seed.txt": "original\n", "test_tiny.py": "def test_two_assertions():\n    assert 6*7 == 42\n    assert 2+2 == 4\n"}.items():
            (source / name).write_text(value)
        subprocess.run(["git", "-C", str(source), "add", "seed.txt", "test_tiny.py"], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=fixture", "-c", "user.email=f@invalid",
                        "-c", "core.hooksPath=/dev/null", "commit", "-qm", "fixture"], check=True)
        db.ensure_project(pid, name=pid, repo_type="existing", repo_path=str(source))
        rid = sf.create_run("owned_shell_fixture", project_id=pid)
        sf.start_run(rid)
        owned = ri.ensure_for_run(db, run_id=rid, project_id=pid,
                                  config_name="owned_shell_fixture", repo_mode="code", requested_mode=ri.MODE_WORKTREE)
        sf.advance_run(rid)
        claim = sf.claim_next_step(rid)
        assert claim is not None and claim.token.claim_epoch > 0
        assert ri.resolve_for_resolver(db, rid, sf=sf) == owned["worktree_path"]
        grant = shell.bind_coding_shell(db, sf, claim)
        runs.append(rid)
        return grant, Path(owned["worktree_path"]), source

    yield root, db, sf, make
    assert ri.write_admissions(db) == [], "test left an unsettled owned write admission"
    for rid in runs:
        sf.stop_run(rid, "owned fixture completed")
    sf._conn.close()
    if previous is None:
        os.environ.pop("AITELIER_HOME", None)
    else:
        os.environ["AITELIER_HOME"] = previous


def test_actual_rw_shell_git_metadata_pytest_and_namespace(runtime):
    root, db, sf, make = runtime
    grant, work, source = make()
    outside = root / "outside-secret"
    outside.write_text("OWNED_BACKEND_SENTINEL")
    (source / ".git/config").write_text((source / ".git/config").read_text() + "\n[credential]\n helper = OWNED_BACKEND_SENTINEL\n")
    probe = '''import json,os,pathlib,socket,subprocess
w=pathlib.Path.cwd()
assert os.getuid()==1000
assert not pathlib.Path(SOURCE).exists()
for p in (OUTSIDE,DB,ENGINE,LAUNCHER,'/var/run/docker.sock','/run/aitelier-secrets'):
 assert not pathlib.Path(p).exists(),p
assert not {'ROOT_TEST_SECRET','AITELIER_ADMIN_TOKEN','OPENAI_API_KEY'} & os.environ.keys()
fds=[]
for fd in os.listdir('/proc/self/fd'):
 try:fds.append(os.readlink('/proc/self/fd/'+fd))
 except FileNotFoundError:pass
assert not any(link.startswith('socket:') for link in fds),fds
assert pathlib.Path('/sys/fs/cgroup/memory.max').read_text().strip()=='2147483648'
assert pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().strip()=='200000 100000'
assert pathlib.Path('/sys/fs/cgroup/pids.max').read_text().strip()=='512'
assert set(os.listdir('/sys/class/net'))=={'lo'}
s=socket.socket();s.settimeout(.2)
try:s.connect(('1.1.1.1',53))
except OSError:pass
else:raise AssertionError('network escape')
finally:s.close()
assert subprocess.run(['git','show','HEAD:seed.txt'],capture_output=True,text=True,check=True).stdout=='original\\n'
(w/'seed.txt').write_text('owned edit\\n');(w/'new.txt').write_text('owned new\\n')
assert 'owned edit' in subprocess.run(['git','diff','--','seed.txt'],capture_output=True,text=True,check=True).stdout
assert subprocess.run(['git','add','seed.txt'],capture_output=True).returncode!=0
dot=w/'.git';meta=pathlib.Path('/aitelier-git')
for p in (dot,meta/'HEAD',meta/'config',meta/'index'):
 try:p.write_text('forged')
 except OSError:pass
 else:raise AssertionError('metadata writable '+str(p))
try:dot.unlink()
except OSError:pass
else:raise AssertionError('git pointer removable')
assert 'OWNED_BACKEND_SENTINEL' not in (meta/'config').read_text()
result=subprocess.run(['python3','-m','pytest','-q','test_tiny.py'],capture_output=True,text=True)
assert result.returncode==0,result.stdout+result.stderr
mounts=[line for line in pathlib.Path('/proc/self/mountinfo').read_text().splitlines() if str(w) in line or '/aitelier-git' in line]
print(json.dumps({'uid':os.getuid(),'pytest':result.stdout,'mountinfo':'\\n'.join(mounts),'env':sorted(os.environ)}))
'''
    probe = probe.replace("SOURCE", repr(str(source))).replace("OUTSIDE", repr(str(outside)))
    probe = probe.replace("ENGINE", repr(str(root/'h/skillflow.db'))).replace("LAUNCHER", repr(str(root/'s'))).replace("DB", repr(db.db_path))
    (work / "probe.py").write_text(probe)
    source_before = (source / "seed.txt").read_bytes()
    result = shell.execute_coding_shell(grant, "python3 probe.py", 30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.physical_quiet and not result.timed_out and result.git["provided"]
    measured = json.loads(result.stdout)
    mounts = [line for line in measured["mountinfo"].splitlines() if str(work) in line or '/aitelier-git' in line]
    assert any(str(work) + " rw," in line for line in mounts)
    assert any(str(work / '.git') + " ro," in line for line in mounts)
    assert any("/aitelier-git ro," in line for line in mounts)
    assert (work / "seed.txt").read_text() == "owned edit\n"
    assert (work / "new.txt").read_text() == "owned new\n"
    assert (source / "seed.txt").read_bytes() == source_before
    assert outside.read_text() == "OWNED_BACKEND_SENTINEL"
    assert ri.write_admissions(db, str(work)) == []
    (root / "positive-result.json").write_text(json.dumps({"run": grant.binding, "result": vars(result), "measured": measured}, indent=2))


def test_actual_bare_failure_and_bounded_output(runtime):
    root, db, sf, make = runtime
    grant, work, _ = make()
    result = shell.execute_coding_shell(grant, "printf actual-out; printf actual-error >&2; exit 7", 10)
    assert (result.returncode, result.stdout, result.stderr) == (7, "actual-out", "actual-error")
    assert result.physical_quiet and not result.timed_out
    (root / "bare-failure-result.json").write_text(json.dumps(vars(result), indent=2))
    result = shell.execute_coding_shell(grant, "python3 -c 'print(\"x\"*50000)'", 10)
    assert result.returncode == 0 and result.output_truncated
    assert len(result.stdout.encode()) + len(result.stderr.encode()) <= shell.OUTPUT_LIMIT
    (root / "bounded-output-result.json").write_text(json.dumps(vars(result), indent=2))


def test_actual_timeout_preserves_partial_effect_and_bare_status(runtime):
    root, db, sf, make = runtime
    grant, work, _ = make()
    result = shell.execute_coding_shell(grant, "printf partial; printf kept > partial.txt; sleep 20", 1)
    assert result.timed_out and result.physical_quiet and result.returncode != 0
    assert result.stdout == "partial" and (work / "partial.txt").read_text() == "kept"
    assert ri.write_admissions(db, str(work)) == []
    (root / "timeout-result.json").write_text(json.dumps(vars(result), indent=2))


def test_client_disconnect_keeps_admission_until_real_destroy_observation(runtime):
    root, db, sf, make = runtime
    grant, work, _ = make()
    previous = signal.getsignal(signal.SIGUSR1)
    def interrupt(_signum, _frame):
        raise InterruptedError("owned client interrupted after partial write")
    signal.signal(signal.SIGUSR1, interrupt)
    def after_write():
        wait_file(work / "disconnect-partial.txt")
        os.kill(os.getpid(), signal.SIGUSR1)
    watcher = threading.Thread(target=after_write)
    watcher.start()
    try:
        with pytest.raises(ex.IsolationUnavailable):
            shell.execute_coding_shell(grant, "printf retained > disconnect-partial.txt; sleep 30", 40)
    finally:
        watcher.join(35)
        signal.signal(signal.SIGUSR1, previous)
    rows = ri.write_admissions(db, str(work))
    assert len(rows) == 1 and "PENDING WRITERS" in rows[0]["detail"]
    with pytest.raises(ex.IsolationUnavailable, match="unsettled"):
        shell.execute_coding_shell(grant, "printf bypass", 1)
    quiet = wait_file(root / "physical" / (grant.binding["run_id"] + ".json"))
    proof = json.loads(quiet.read_text())
    assert proof["action"] == "destroy" and proof["run_id"] == grant.binding["run_id"]
    assert (work / "disconnect-partial.txt").read_text() == "retained"
    (root / "disconnect-result.json").write_text(json.dumps({"retained_admission": rows[0], "positive_quiet": proof,
                                                            "partial_effect": "retained"}, indent=2))
    # Explicit fixture settlement AFTER actual owned destroy; no product auto-clear.
    ri.retire_write(db, rows[0]["id"])


def test_forged_grant_absent_facility_and_stale_real_epoch_refuse(runtime, monkeypatch):
    root, db, sf, make = runtime
    grant, work, _ = make()
    with pytest.raises(ex.IsolationUnavailable, match="cannot supply"):
        shell.execute_coding_shell(dict(grant.binding), "printf forged")
    with monkeypatch.context() as patch:
        patch.delenv(ex.HOST_LAUNCHER_ENV)
        with pytest.raises(ex.IsolationUnavailable, match="no local fallback"):
            shell.execute_coding_shell(grant, "printf fallback > absent.txt")
    assert not (work / "absent.txt").exists() and ri.write_admissions(db, str(work)) == []
    sf.release_claim(grant.claimed.token, "owned test reclaims normally")
    next_claim = sf.claim_next_step(grant.binding["run_id"])
    assert next_claim.token.claim_epoch > grant.claimed.token.claim_epoch
    with pytest.raises(ex.IsolationUnavailable, match="current pinned"):
        shell.execute_coding_shell(grant, "printf stale > stale.txt")
    assert not (work / "stale.txt").exists()


def test_actual_host_refuses_changed_scope_or_extra_authority(runtime):
    from core import repository_host_launcher as host
    root, db, sf, make = runtime
    grant, work, source = make()
    command = "printf forbidden > forged.txt"
    binding = {**grant.binding, "command_sha256": hashlib.sha256(command.encode()).hexdigest()}
    adm = ri.admit_write(db, str(work), kind=shell.ADMISSION_KIND, detail=shell._detail(binding))
    packet = {**binding, "admission_id": adm["id"], "admission_owner": adm["owner"]}
    request = {"op": "launch", "run_id": binding["run_id"], "repo": str(work),
               "args": ["/bin/sh", "-c", command], "timeout": 5,
               "profile": shell.PROFILE, "coding_grant": packet}
    cfg = json.loads((root / "config.json").read_text())
    for changed_cfg in ({**cfg, "coding_shell_entry_sha256": "0" * 64},
                        {k: v for k, v in cfg.items() if k != "coding_shell_engine_db"}):
        with pytest.raises(host.LaunchRefused, match="operator|Source"):
            host.validate_request(changed_cfg, request)
    cases = []
    for field in ("run_id", "project_id", "step_id", "step_instance_id", "claimed_by", "base_sha", "graph_digest", "admission_owner"):
        changed = json.loads(json.dumps(request))
        changed["coding_grant"][field] = "foreign"
        cases.append(changed)
    for field in ("claim_epoch", "version", "graph_version", "admission_id"):
        changed = json.loads(json.dumps(request))
        changed["coding_grant"][field] += 1
        cases.append(changed)
    for field, value in (("repo", str(source)), ("writable_dirs", [str(source)]),
                         ("env", {"PATH": "/forged"}), ("image", "forged"),
                         ("network", "host"), ("workdir", str(source))):
        cases.append({**request, field: value})
    denials = []
    for changed in cases:
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(str(root / "s"))
            conn.sendall((json.dumps(changed) + "\n").encode())
            reply = b""
            while b"\n" not in reply:
                chunk = conn.recv(65536)
                assert chunk
                reply += chunk
        parsed = json.loads(reply)
        assert parsed["ok"] is False
        denials.append({"request": changed, "response": parsed})
    assert not (work / "forged.txt").exists() and len(ri.write_admissions(db, str(work))) == 1
    ri.retire_write(db, adm["id"])
    (root / "host-denials.json").write_text(json.dumps({"count": len(cases), "scope": binding, "denials": denials}, indent=2))


def test_exact_owned_source_identity():
    assert Path(ex.__file__).resolve() == Path('/src/core/repository_executor.py')
    assert Path(shell.__file__).resolve() == Path('/src/core/coding_shell.py')
