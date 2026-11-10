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


def test_vanished_checkout_marker_is_disposed_terminally(setup):
    # Measured 2026-11-10: the live marker held status="error" for a project
    # directory that kept only its .zvec-grep cache after the checkout was
    # removed. The ownership check raised before any marker write, so the worker
    # re-raised every tick and deployment clearance vetoed every deployment.
    from core import semantic_index_control as module
    projects, runs, control, _ = setup
    gone = projects / "a-vanished"
    gone.mkdir()
    (gone / ".zvec-grep").mkdir()
    error = "checkout ownership was unavailable when the operation failed"
    older = {"root": str(projects / "a-older"), "status": "error",
             "error": "older retained failure", "updated_at": 0.5}
    first = {"root": str(gone), "status": "error", "error": error, "updated_at": 0.25}
    excluded = {"root": str(projects / "a-frozen"), "status": "excluded",
                "error": "", "updated_at": 0.75}
    prior = {"root": str(gone), "status": "error", "error": error, "updated_at": 1.0,
             "failure_history": [older], "first_failure": first,
             "excluded_owners": [excluded]}
    marker = control / "project-owner.json"
    marker.write_text(json.dumps(prior))
    # The ownership check is untouched: this root is still not owned, and on the
    # base implementation that ValueError escaped before any marker write.
    with pytest.raises(ValueError, match="not an owned project root"):
        module.project_root(gone, projects, runs)
    module._index_project_once(control, projects, runs, execute=no_commands)
    settled = json.loads(marker.read_text())
    module.validate_project_owner(settled)
    assert settled["status"] == "idle" and settled["error"] == ""
    assert settled["root"] == str(gone)
    assert dq._semantic_worker_errors(control) == []
    # The failure is retained verbatim, not rewritten into a fake success.
    assert settled["first_failure"] == first
    assert settled["first_failure"]["error"] == error
    assert settled["failure_history"][0] == older
    assert {"root": str(gone), "status": "error", "error": error,
            "updated_at": 1.0} in settled["failure_history"]
    assert error in [row["error"] for row in settled["failure_history"]]
    assert settled["excluded_owners"] == [excluded]
    assert [p.name for p in gone.iterdir()] == [".zvec-grep"]


def test_vanished_checkout_with_noncanonical_root_keeps_first_failure(setup):
    # Reviewer probe on b2848f78f5: the abandoned root is rebuilt with
    # Path(prior["root"]), which normalises a trailing slash, so record() no
    # longer sees prior["root"] == str(root) and dropped the retained
    # first_failure row on the way to idle. No recorded evidence may vanish.
    from core import semantic_index_control as module
    projects, runs, control, _ = setup
    gone = projects / "a-gone2"
    gone.mkdir()
    (gone / ".zvec-grep").mkdir()
    error = "checkout ownership was unavailable when the operation failed"
    first = {"root": str(gone) + "/", "status": "error", "error": error,
             "updated_at": 0.25}
    prior = {"root": str(gone) + "/", "status": "error",
             "error": "the last attempt found the checkout gone", "updated_at": 1.0,
             "first_failure": first}
    marker = control / "project-owner.json"
    marker.write_text(json.dumps(prior))
    module.validate_project_owner(prior)
    with pytest.raises(ValueError, match="not an owned project root"):
        module.project_root(Path(prior["root"]), projects, runs)

    def no_execute(*args, **kwargs):
        raise AssertionError("a vanished checkout must not run an index command")

    module._index_project_once(control, projects, runs, execute=no_execute)
    settled = json.loads(marker.read_text())
    module.validate_project_owner(settled)
    assert settled["status"] == "idle" and settled["error"] == ""
    assert settled["root"] == str(gone)
    assert first in settled["failure_history"]
    assert error in [row["error"] for row in settled["failure_history"]]
    assert json.dumps(first) in marker.read_text()
    assert [p.name for p in gone.iterdir()] == [".zvec-grep"]


def test_immutable_owned_root_with_noncanonical_spelling_stays_valid(setup):
    # Measured 2026-11-10 on 6a5d0988d730: an owned immutable checkout whose
    # marker recorded the root with a trailing slash settled "excluded" with
    # settlement.failure still naming the old spelling and no first_failure, so
    # validate_project_owner refused the marker ("excluded project owner
    # settlement is malformed") and the next worker pass exited 1 with the same
    # error -- a marker the worker itself could never repair. The prior row keeps
    # its own spelling in failure_history; the evidence written beside the
    # normalised root names the written root.
    from core import semantic_index_control as module
    projects, runs, control, root = setup
    prior = failed(control, str(root) + "/")
    root.chmod(0o555)
    first = worker(projects, runs, control)
    assert first.returncode == 0, first.stderr
    marker = control / "project-owner.json"
    settled = json.loads(marker.read_text())
    module.validate_project_owner(settled)
    assert dq._semantic_worker_errors(control) == []
    assert settled["status"] == "excluded" and settled["root"] == str(root)
    assert prior["root"] == str(root) + "/"
    assert prior in settled["failure_history"]
    assert json.dumps(prior) in marker.read_text()
    assert settled["first_failure"] == settled["settlement"]["failure"]
    assert settled["first_failure"]["root"] == str(root)
    assert settled["settlement"]["failure"]["root"] == str(root)
    assert settled["first_failure"]["error"] == prior["error"] == settled["error"]
    second = worker(projects, runs, control)
    assert second.returncode == 0, second.stderr
    assert json.loads(marker.read_text()) == settled


def test_writable_owned_root_with_noncanonical_spelling_keeps_first_failure(setup):
    # Same spelling defect on the writable branch: the pass wrote "idle" but
    # dropped the prior failure row because record() found prior["root"] !=
    # str(root), leaving first_failure and failure_history both empty.
    from core import semantic_index_control as module
    projects, runs, control, root = setup
    prior = failed(control, str(root) + "/")
    calls = []

    def provider(command, **kwargs):
        calls.append(command)
        if command[1] == "index":
            (Path(command[2]) / ".zvec-grep").mkdir()

    index_project_once(control, projects, runs, execute=provider)
    owner = json.loads((control / "project-owner.json").read_text())
    module.validate_project_owner(owner)
    assert dq._semantic_worker_errors(control) == []
    assert owner["status"] == "idle" and owner["root"] == str(root)
    assert prior in owner["failure_history"]
    assert owner["first_failure"]["root"] == str(root)
    assert owner["first_failure"]["error"] == prior["error"]
    assert [c[1] for c in calls] == ["index", "status"]


def test_writable_owned_root_with_canonical_spelling_keeps_first_failure(setup):
    # Control: the canonical spelling already took the working path, so the fix
    # must leave it unchanged (first_failure is the prior row, no history).
    from core import semantic_index_control as module
    projects, runs, control, root = setup
    prior = failed(control, root)
    calls = []

    def provider(command, **kwargs):
        calls.append(command)
        if command[1] == "index":
            (Path(command[2]) / ".zvec-grep").mkdir()

    index_project_once(control, projects, runs, execute=provider)
    owner = json.loads((control / "project-owner.json").read_text())
    module.validate_project_owner(owner)
    assert dq._semantic_worker_errors(control) == []
    assert owner["status"] == "idle" and owner["root"] == str(root)
    assert owner["first_failure"] == prior
    assert "failure_history" not in owner
    assert [c[1] for c in calls] == ["index", "status"]


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
