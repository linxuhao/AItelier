"""UID-owned immutable snapshots: real serial worker, source and owner fences."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core import deployment_quiescence as dq
from core.semantic_index_control import index_project_once, service_lock

ROOT = Path(__file__).resolve().parents[2]
QUIET = {"queued_jobs": 0, "running_jobs": 0, "shutting_down": False}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(root):
    root.mkdir(parents=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    (root / "code.py").write_text("VALUE = 1\n")
    git(root, "add", "code.py")
    git(root, "commit", "-qm", "fixture")
    return root


def bytes_snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    assert os.getuid() == 1000, "causal fixture must run as the production UID 1000"
    projects, runs, control = (tmp_path / n for n in ("projects", "runs", "control"))
    control.mkdir()
    root = repo(projects / "a-frozen")
    binaries = tmp_path / "bin"; binaries.mkdir()
    node = binaries / "node"
    node.write_text(f"#!{sys.executable}\nimport os, sys\nprint(os.environ['FIXTURE_DAEMON_STATUS'])\n"
                    "sys.exit(int(os.environ.get('FIXTURE_DAEMON_RC', '0')))\n")
    node.chmod(0o700)
    zg = binaries / "zg"
    zg.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
                  "root = Path(sys.argv[2])\n"
                  "if sys.argv[1] == 'index': (root / '.zvec-grep').mkdir(exist_ok=True)\n"
                  "else: assert (root / '.zvec-grep').is_dir()\n")
    zg.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FIXTURE_DAEMON_STATUS", json.dumps(QUIET))
    yield projects, runs, control, root
    root.chmod(0o755)  # only this owned fixture is thawed for pytest cleanup


def failed(control, root, status="error"):
    prior = {"root": str(root), "status": status,
             "error": "first observed permission failure; raw evidence retained", "updated_at": 1.0}
    (control / "project-owner.json").write_text(json.dumps(prior))
    (control / "original.raw").write_bytes(b"original diagnostic bytes")
    return prior


def no_commands(*args, **kwargs):
    pytest.fail("immutable snapshot submitted an index/readiness command")


def worker(projects, runs, control):
    return subprocess.run([sys.executable, str(ROOT / "core/semantic_index_control.py"),
                           "--control-dir", str(control), "--worktrees-root", str(runs),
                           "--projects-root", str(projects), "--once"], capture_output=True, text=True)


def test_uid1000_immutable_discovery_does_not_mutate_or_retry(setup):
    projects, runs, control, root = setup
    root.chmod(0o555)
    assert not os.access(root, os.W_OK, effective_ids=True)
    before = bytes_snapshot(root)
    for _ in range(3):
        index_project_once(control, projects, runs, execute=no_commands)
    assert bytes_snapshot(root) == before
    assert not (root / ".zvec-grep").exists()
    assert not (control / "project-owner.json").exists()


def test_real_owner_settles_failed_excluded_root_and_preserves_history(setup):
    projects, runs, control, root = setup
    prior = failed(control, root)
    root.chmod(0o555)
    before = bytes_snapshot(root)
    result = worker(projects, runs, control)
    assert result.returncode == 0, result.stderr
    terminal = json.loads((control / "project-owner.json").read_text())
    assert terminal["status"] == "excluded"
    assert terminal["settlement"]["failure"] == prior
    assert terminal["settlement"]["reason"] == "immutable-root-without-index"
    assert terminal["settlement"]["daemon"] == QUIET
    assert dq._semantic_worker_errors(control) == []
    assert bytes_snapshot(root) == before
    assert (control / "original.raw").read_bytes() == b"original diagnostic bytes"
    writable = repo(projects / "b-writable")
    calls = []
    def provider(command, **kwargs):
        calls.append(command)
        if command[1] == "index":
            (Path(command[2]) / ".zvec-grep").mkdir()
    index_project_once(control, projects, runs, execute=provider)
    owner = json.loads((control / "project-owner.json").read_text())
    assert owner["status"] == "idle" and owner["root"] == str(writable)
    assert owner["excluded_owners"] == [terminal]
    assert [c[1] for c in calls] == ["index", "status"]
    assert bytes_snapshot(root) == before


@pytest.mark.parametrize("inventory", [
    {**QUIET, "running_jobs": 1}, {**QUIET, "queued_jobs": 1},
    {**QUIET, "shutting_down": True}, {"queued_jobs": 0},
    {**QUIET, "running_jobs": False}, [], "not-json",
])
def test_failed_immutable_owner_stays_failed_if_daemon_active_or_unknown(setup, monkeypatch, inventory):
    projects, runs, control, root = setup
    prior = failed(control, root); root.chmod(0o555)
    monkeypatch.setenv("FIXTURE_DAEMON_STATUS", inventory if isinstance(inventory, str) else json.dumps(inventory))
    worker(projects, runs, control)
    assert json.loads((control / "project-owner.json").read_text()) == prior
    assert dq._semantic_worker_errors(control)


@pytest.mark.parametrize("lock", ["worker.lock", "operation.lock"])
def test_live_owner_fences_prevent_settlement(setup, lock):
    projects, runs, control, root = setup
    prior = failed(control, root); root.chmod(0o555)
    with service_lock(control, lock):
        result = worker(projects, runs, control)
        assert json.loads((control / "project-owner.json").read_text()) == prior
    if lock == "worker.lock":
        assert result.returncode != 0


def test_cutover_fence_prevents_settlement(setup):
    projects, runs, control, root = setup
    prior = failed(control, root); root.chmod(0o555)
    fence = control.parent / "godot-control/deployment-admission.lock"
    fence.parent.mkdir()
    with fence.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        worker(projects, runs, control)
        assert json.loads((control / "project-owner.json").read_text()) == prior


@pytest.mark.parametrize("status", ["active", "unknown", "corrupt"])
def test_immutable_root_does_not_downgrade_unproven_owner(setup, status):
    projects, runs, control, root = setup
    prior = failed(control, root, status); root.chmod(0o555)
    worker(projects, runs, control)
    assert json.loads((control / "project-owner.json").read_text()) == prior
    assert dq._semantic_worker_errors(control)


def test_indexed_immutable_root_remains_usable(setup):
    projects, runs, control, root = setup
    cache = root / ".zvec-grep"; cache.mkdir()
    (cache / "manifest.json").write_text("retained index bytes")
    root.chmod(0o555); before = bytes_snapshot(root)
    index_project_once(control, projects, runs, execute=no_commands)
    assert bytes_snapshot(root) == before
    assert not (control / "project-owner.json").exists()


def test_symlink_error_owner_is_not_excluded(setup):
    projects, runs, control, root = setup
    outside = repo(root.parent.parent / "outside")
    root.chmod(0o555)
    redirect = projects / "redirect"; redirect.symlink_to(outside, target_is_directory=True)
    prior = failed(control, redirect)
    worker(projects, runs, control)
    assert json.loads((control / "project-owner.json").read_text()) == prior
    assert dq._semantic_worker_errors(control)


@pytest.mark.parametrize("patch", [{"status": "excluded"}, {"status": "excluded", "settlement": {}},
                                    {"status": "idle", "updated_at": float("nan")}])
def test_forged_or_corrupt_terminal_marker_blocks_guard(setup, patch):
    projects, runs, control, root = setup
    prior = failed(control, root); prior.update(patch)
    (control / "project-owner.json").write_text(json.dumps(prior))
    assert dq._semantic_worker_errors(control)


def test_excluded_marker_stays_terminal_but_live_operation_still_blocks(setup):
    projects, runs, control, root = setup
    prior = failed(control, root); root.chmod(0o555)
    worker(projects, runs, control)
    terminal = json.loads((control / "project-owner.json").read_text())
    assert terminal["first_failure"] == prior
    for _ in range(3):
        index_project_once(control, projects, runs, execute=no_commands)
    assert json.loads((control / "project-owner.json").read_text()) == terminal
    with service_lock(control, "operation.lock"):
        assert dq._semantic_worker_errors(control) == ["semantic indexing operation is active"]
    assert dq._semantic_worker_errors(control) == []


@pytest.mark.parametrize("field", ["failure", "proof", "daemon", "reason", "first_failure"])
def test_corrupt_exclusion_evidence_fails_closed(setup, field):
    projects, runs, control, root = setup
    failed(control, root); root.chmod(0o555)
    worker(projects, runs, control)
    terminal = json.loads((control / "project-owner.json").read_text())
    if field == "first_failure":
        terminal[field] = {"status": "error"}
    else:
        terminal["settlement"][field] = None
    (control / "project-owner.json").write_text(json.dumps(terminal))
    assert dq._semantic_worker_errors(control)
    worker(projects, runs, control)
    assert json.loads((control / "project-owner.json").read_text()) == terminal


def test_writable_retry_and_ready_retain_first_failure(setup):
    projects, runs, control, root = setup
    original = failed(control, root)
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(9, command, output=b"second failure evidence")
    with pytest.raises(subprocess.CalledProcessError):
        index_project_once(control, projects, runs, execute=fail)
    retry = json.loads((control / "project-owner.json").read_text())
    assert retry["status"] == "error" and retry["first_failure"] == original
    assert "second failure evidence" in retry["error"]
    calls = []
    def ready(command, **kwargs):
        calls.append(command)
        if command[1] == "index": (root / ".zvec-grep").mkdir()
    index_project_once(control, projects, runs, execute=ready)
    terminal = json.loads((control / "project-owner.json").read_text())
    assert terminal["status"] == "idle" and terminal["first_failure"] == original
    assert [c[1] for c in calls] == ["index", "status"]


def test_no_write_bits_with_effective_write_access_is_not_immutable(setup, monkeypatch):
    # Root/ACL permission semantics must come from effective access, not mode
    # bits alone. The UID0 access result is injected; this test never escalates.
    from core import semantic_index_control as module
    projects, runs, control, root = setup
    root.chmod(0o555)
    calls = []
    def effective_access(path, mode, *, effective_ids):
        calls.append((path, mode, effective_ids))
        return True
    monkeypatch.setattr(module.os, "access", effective_access)
    assert module.immutable_project(root) is False
    assert calls == [(root, os.W_OK, True)]


def test_project_marker_symlink_stays_unknown_and_unchanged(setup):
    projects, runs, control, root = setup
    outside = control.parent / "outside-marker.json"
    outside.write_text(json.dumps({"root": str(root), "status": "idle", "error": "", "updated_at": 1.0}))
    (control / "project-owner.json").symlink_to(outside)
    root.chmod(0o555)
    before = outside.read_bytes()
    with pytest.raises(ValueError, match="symlink"):
        index_project_once(control, projects, runs, execute=no_commands)
    assert dq._semantic_worker_errors(control)
    assert outside.read_bytes() == before


def test_failed_daemon_probe_cannot_settle_failed_owner(setup, monkeypatch):
    projects, runs, control, root = setup
    original = failed(control, root); root.chmod(0o555)
    monkeypatch.setenv("FIXTURE_DAEMON_RC", "3")
    worker(projects, runs, control)
    assert json.loads((control / "project-owner.json").read_text()) == original
    assert dq._semantic_worker_errors(control)
