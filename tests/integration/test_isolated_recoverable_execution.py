"""Composition tests for bounded failures and durable operation ownership.

The component suites prove each boundary separately.  These fixtures keep the
boundaries in one process-isolated scenario so a transport observation cannot
be mistaken for authority to replace an executor, and one run cannot settle a
neighbour's work.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import sqlite3
import subprocess
from types import SimpleNamespace
import threading
import time
from pathlib import Path

import pytest
import skillflow as skillflow_package
from skillflow.graph import (
    EndCondition,
    EndConditions,
    PipelineGraph,
    StepNode,
    Transition,
)
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader

from core import scheduler
from core.db_manager import DBManager
from core.skillflow_host import AItelierSkillFlow
from core.state_attempts import StateAttempts
from core.state_external import ExternalAttempts
from core.state_graph import StateConflict, StateGraphStore

_ROOT = Path(__file__).resolve().parents[2]
_REPORT_SHA = "b" * 64


def _tool_graph(name: str = "isolated_recovery") -> PipelineGraph:
    return PipelineGraph(
        name=name,
        begin="effect",
        end_conditions=EndConditions(conditions=[
            EndCondition(
                type="node_reached", node="effect", result="completed",
                require_completed=True),
        ]),
        steps=[StepNode(
            id="effect",
            step_type="tool",
            tool_name="run_tests",
            tool_params={"out_dir": "$STEP_DIR"},
            timeout_seconds=30,
            transitions=[Transition(to=None)],
        )],
    )


def _engine(db_path: Path, workspace: Path, projects: Path,
            repo: Path | dict[str, Path],
            *, graph_name: str = "isolated_recovery") -> AItelierSkillFlow:
    loader = ToolLoader(Path(skillflow_package.__file__).parent / "tools")
    loader.add_tools_dir(_ROOT / "aitelier" / "tools")
    native = loader.is_native
    loader.is_native = lambda name: name == "run_tests" or native(name)
    def resolve(project_id, run_id=None):
        return repo[project_id] if isinstance(repo, dict) else repo

    sf = AItelierSkillFlow(
        str(db_path),
        tool_loader=loader,
        workspace_base=str(workspace),
        projects_base=str(projects),
        code_path_resolver=resolve,
        stale_threshold_seconds=0.05,
    )
    sf.register_graph(_tool_graph(graph_name))
    return sf


# The recovery gate keeps EVERY mutable file (active/started/release/effect
# counter/overlap marker and the structured report) inside the entry
# directory it creates first under its own report ticket. The repository is
# never written. A replacement invocation would get a different ticket, so the
# test looks across every ticket of the run for a second effect or overlap.
_RECOVERY_GATE = """#!/bin/bash
set -eu
entry="$GATE_REPORT_DIR/effect"
mkdir "$entry"
if ! mkdir "$entry/active"; then touch "$entry/OVERLAP"; exit 91; fi
trap 'rmdir "$entry/active"' EXIT
touch "$entry/started"
while [ ! -f "$entry/release" ]; do sleep 0.02; done
echo logical-operation >> "$entry/logical-effects"
python3 - "$entry" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path
entry = Path(sys.argv[1])
head = subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip()
scope = {'coverage': 'full', 'all_scenarios': ['effect'],
         'selected_scenarios': ['effect'], 'unselected_scenarios': [],
         'fallback_full': False, 'selection_basis': {
             'head_sha': head,
             'spec_sha256': hashlib.sha256(b'effect').hexdigest(),
             'method': 'full_default'}}
(entry / 'manifest.json').write_text(json.dumps({
    'repo': str(Path.cwd()),
    'stages': {'effect': {'report': 'effect.json'}},
    'gate_coverage': scope}))
(entry / 'effect.json').write_text(json.dumps({'passed': True, 'returncode': 0}))
(entry / 'effect-findings.json').write_text('[]')
PY
"""


def _owned_executor(home: Path, allowed_root: Path):
    """The current closed validator + container entry, inside the outer sandbox.

    The enclosing disposable CPU job supplies isolation; there is no nested
    Docker daemon, socket mount or host fallback. Every request still passes
    `validate_request` against a real run binding, then runs the real entry.
    """
    from core import repository_executor_entry as entry
    from core import repository_host_launcher as launcher

    config = {"allowed_roots": [str(allowed_root)], "aitelier_home": str(home),
              "state_db": str(home / "bindings.db"),
              "test_image": "sha256:" + "a" * 64,
              "executor_entry": entry.__file__, "allowed_uids": [os.getuid()]}

    def execute_owned(repo, args, timeout, **kwargs):
        fields = launcher.validate_request(config, {
            "op": "launch", "repo": str(repo), "args": args, "timeout": timeout,
            **{key: [str(p) for p in value] if key == "writable_dirs" else value
               for key, value in kwargs.items()},
        })
        return SimpleNamespace(**entry.run(fields))

    return execute_owned


def _provision_bindings(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(home / "bindings.db") as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS run_isolation("
                     "run_id,mode,worktree_path,source_repo,project_id)")


def _bind_run(home: Path, run_id: str, repo: Path, project_id: str) -> None:
    with sqlite3.connect(home / "bindings.db") as conn:
        conn.execute("INSERT INTO run_isolation VALUES(?,?,?,?,?)",
                     (run_id, "direct", str(repo), str(repo), project_id))


def _drive_gate(db_path: str, workspace: str, projects: str, repo: str,
                run_id: str, home: str, allowed_root: str) -> None:
    # A spawned worker shares no module state: it provisions its own isolated
    # home and its own closed validator/entry before it can reach the gate.
    os.environ["AITELIER_HOME"] = home
    from core import repository_executor as executor
    executor.execute = _owned_executor(Path(home), Path(allowed_root))
    sf = _engine(Path(db_path), Path(workspace), Path(projects), Path(repo))
    sf.advance_run(run_id)


def _gate_entries(home: Path, run_id: str) -> list[Path]:
    root = home / "gate-reports" / hashlib.sha256(run_id.encode()).hexdigest()
    return sorted(root.glob("rt-*/effect"))


def _wait_for_started(home: Path, run_id: str, timeout: float = 15) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for entry in _gate_entries(home, run_id):
            if (entry / "started").exists():
                return entry
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for the gate start marker under {home}")


def _logical_effects(home: Path, run_id: str) -> list[str]:
    lines: list[str] = []
    for entry in _gate_entries(home, run_id):
        if (entry / "logical-effects").exists():
            lines += (entry / "logical-effects").read_text().splitlines()
    return lines


def _overlap_markers(home: Path, run_id: str) -> list[Path]:
    return [entry / "OVERLAP" for entry in _gate_entries(home, run_id)
            if (entry / "OVERLAP").exists()]


def _source_state(repo: Path):
    files = {str(path.relative_to(repo)): path.read_bytes()
             for path in sorted(repo.rglob("*"))
             if ".git" not in path.relative_to(repo).parts and path.is_file()}

    def git_out(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout
    return files, git_out("rev-parse", "HEAD").strip(), git_out("status", "--porcelain")


def _wait_for(path: Path, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _state_fixture(tmp_path: Path):
    store = StateGraphStore(DBManager(str(tmp_path / "state.db")), project_read_trusted=True)
    store.create_project("fixture", "fixture")
    store.add_nodes("fixture", [{
        "key": "operation",
        "goal": "controlled operation",
        "dependencies": [],
        "acceptance": [{
            "id": "effect",
            "kind": "integration",
            "description": "one controlled effect",
        }],
    }])
    attempts = StateAttempts(store)
    external = ExternalAttempts(attempts, "fixture-controller")
    return store, attempts, external


@pytest.mark.asyncio
async def test_bounded_failure_timeout_disconnect_and_unknown_retain_distinct_state(
        tmp_path, monkeypatch):
    """Only the deterministic claim error terminates; observation loss does not."""
    code = tmp_path / "dirty-code"
    code.mkdir()
    git(code, "init", "-q")
    (code / "base.py").write_text("baseline = True\n")
    git(code, "add", "--", "base.py")
    git(code, "commit", "-qm", "base")
    retained = code / "unowned-draft.bin"
    retained.write_bytes(b"retained\x00owner bytes")

    sf = AItelierSkillFlow(
        str(tmp_path / "claims.db"),
        workspace_base=str(tmp_path / "claim-artifacts"),
        code_path_resolver=lambda project_id, run_id=None: code,
    )
    node = StepNode(
        id="implement",
        output_mode="write",
        output_target="code",
        output_allow_full_write=True,
        transitions=[Transition(to=None)],
    )
    sf.register_graph(PipelineGraph(
        name="dirty_claim", begin=node.id, steps=[node]))
    run_id = sf.create_run("dirty_claim", project_id="dirty-project")
    sf.start_run(run_id)
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(
        scheduler, "_get_or_create_skillflow_run", lambda project_id: run_id)
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *args: False)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda _pid: None)
    monkeypatch.setattr(scheduler, "_claim_retry_after", {})

    for _ in range(scheduler._MAX_IDENTICAL_CLAIM_PRECONDITIONS):
        await scheduler._run_skillflow_tick("dirty-project", None)

    failed = sf.get_run(run_id)
    assert failed["status"] == "failed"
    assert "owning step implement" in failed["error_reason"]
    assert f"run_id={run_id}" in failed["error_reason"]
    assert retained.read_bytes() == b"retained\x00owner bytes"
    assert git(code, "status", "--porcelain").strip() == "?? unowned-draft.bin"

    store, attempts, external = _state_fixture(tmp_path)
    active = external.register(
        "fixture", "operation", 1, "controlled-harness", "worker-1",
        "request-1")

    # A bounded wait with no observation is a timeout.  Cancelling a longer wait
    # models a disconnected controller.  Neither writes a terminal observation.
    from core.state_service import StateService

    service = StateService(store.db, actor="fixture-controller", project_read_trusted=True)
    timeout = await service.wait_for_state_change(
        "fixture", after=store.events("fixture")[-1]["seq"],
        attempt_ids=[active["attempt_id"]], timeout_seconds=0)
    assert timeout["timed_out"] is True
    waiter = asyncio.create_task(service.wait_for_state_change(
        "fixture", after=timeout["next_after"],
        attempt_ids=[active["attempt_id"]], timeout_seconds=30))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert attempts.get(active["attempt_id"])["status"] == "running"

    unknown = external.observe(
        active["attempt_id"], "observation-unavailable", 0,
        active["context_hash"], "unknown", "reports/unknown.json", _REPORT_SHA,
        detail="controller transport ended before worker status was observable")
    assert unknown["status"] == "unknown"
    assert unknown["terminal_observation_id"] is None
    with pytest.raises(StateConflict):
        external.register(
            "fixture", "operation", 1, "controlled-harness", "worker-2",
            "request-2")


def test_parallel_runs_progress_but_competing_controllers_admit_one_owner(
        tmp_path, monkeypatch):
    """Admission is per run, while two hosts racing one run get one operation."""
    db_path, workspace, projects = (
        tmp_path / "sf.db", tmp_path / "ws", tmp_path / "projects")
    counter = tmp_path / "effects"
    repos = {label: tmp_path / f"repo-{label}" for label in ("a", "b", "same")}
    for label, repo in repos.items():
        repo.mkdir()
        gate = repo / "run_tests.sh"
        (repo / "identity").write_text(label)
        (repo / "test_identity.py").write_text(
            "from pathlib import Path\ndef test_identity():\n"
            f"    assert Path('identity').read_text() == {label!r}\n")
        gate.write_text(
            "#!/bin/bash\nset -eu\npython3 - <<'PY'\n"
            "import hashlib,json,os,subprocess\nfrom pathlib import Path\n"
            f"label = {label!r}\n"
            "assert Path('identity').read_text() == label\n"
            f"with open({str(counter)!r}, 'a') as stream: stream.write(label + '\\n')\n"
            "report = Path(os.environ['GATE_REPORT_DIR']) / 'identity'\n"
            "report.mkdir()\n"
            "scope = {'coverage':'full','all_scenarios':['identity'],"
            "'selected_scenarios':['identity'],'unselected_scenarios':[],"
            "'fallback_full':False,'selection_basis':{"
            "'head_sha':subprocess.check_output(['git','rev-parse','HEAD']).decode().strip(),"
            "'spec_sha256':hashlib.sha256(b'identity').hexdigest(),'method':'full_default'}}\n"
            "(report/'manifest.json').write_text(json.dumps({'repo':str(Path.cwd()),"
            "'stages':['identity'],'gate_coverage':scope}))\n"
            "(report/'identity.json').write_text(json.dumps({'passed':True,'returncode':0}))\n"
            "(report/'identity-findings.json').write_text('[]')\n"
            "print(label)\nPY\n", encoding="utf-8")
        gate.chmod(0o755)
        git(repo, "init", "-q")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "identity fixture")

    # The outer disposable CPU job supplies isolation. Exercise the real closed
    # request validator and container entry here, without a nested Docker daemon.
    from core import datadir, repository_executor as executor
    from core import repository_executor_entry as entry
    from core import repository_host_launcher as launcher

    home = tmp_path / "control"
    home.mkdir()
    monkeypatch.setattr(datadir, "aitelier_home", lambda: home)
    binding_db = home / "bindings.db"
    with sqlite3.connect(binding_db) as conn:
        conn.execute("CREATE TABLE run_isolation(run_id,mode,worktree_path,source_repo,project_id)")
    config = {"allowed_roots": [str(tmp_path)], "aitelier_home": str(home),
              "state_db": str(binding_db), "test_image": "sha256:" + "a" * 64,
              "executor_entry": entry.__file__, "allowed_uids": [os.getuid()]}
    gate_overlap = threading.Barrier(2)
    executed = []

    def execute_owned_fixture(repo, args, timeout, **kwargs):
        fields = launcher.validate_request(config, {
            "op": "launch", "repo": str(repo), "args": args, "timeout": timeout,
            **{key: [str(p) for p in value] if key == "writable_dirs" else value
               for key, value in kwargs.items()},
        })
        if args[0] == "bash":
            gate_overlap.wait(timeout=10)
        result = entry.run(fields)
        executed.append((fields["run_id"], args[0], result["returncode"]))
        return SimpleNamespace(**result)

    monkeypatch.setattr(executor, "execute", execute_owned_fixture)

    first = _engine(db_path, workspace, projects, repos)
    run_a = first.create_run(
        "isolated_recovery", {"project_id": "a"}, project_id="a")
    run_b = first.create_run(
        "isolated_recovery", {"project_id": "b"}, project_id="b")
    first.start_run(run_a)
    first.start_run(run_b)
    with sqlite3.connect(binding_db) as conn:
        conn.executemany("INSERT INTO run_isolation VALUES(?,?,?,?,?)", [
            (run_id, "direct", str(repos[label]), str(repos[label]), label)
            for label, run_id in (("a", run_a), ("b", run_b))])

    start = threading.Barrier(2)
    parallel_outcomes = {}

    def drive_parallel(label, run_id):
        try:
            start.wait(timeout=5)
            parallel_outcomes[label] = {
                "return": first.advance_run(run_id),
                "run_id": run_id,
            }
        except BaseException as exc:
            parallel_outcomes[label] = {
                "exception": type(exc).__name__,
                "detail": str(exc),
                "run_id": run_id,
            }

    threads = [threading.Thread(
        target=drive_parallel, args=(label, run_id), daemon=True)
        for label, run_id in (("a", run_a), ("b", run_b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    assert set(parallel_outcomes) == {"a", "b"}
    assert all("exception" not in result
               for result in parallel_outcomes.values()), parallel_outcomes
    assert {result["run_id"] for result in parallel_outcomes.values()} == {
        run_a, run_b}
    assert counter.exists(), {
        label: json.loads((workspace / label / "isolated_recovery" / "effect" /
                           "test_report.json").read_text()) for label in ("a", "b")}
    assert sorted(counter.read_text().splitlines()) == ["a", "b"]
    assert first.get_run(run_a)["status"] == "completed"
    assert first.get_run(run_b)["status"] == "completed"
    assert first.unsettled_operations() == []
    report_a = json.loads(
        (workspace / "a" / "isolated_recovery" / "effect" /
         "test_report.json").read_text())
    report_b = json.loads(
        (workspace / "b" / "isolated_recovery" / "effect" /
         "test_report.json").read_text())
    assert report_a["repo_gate"]["output"] == "a"
    assert report_a["passed"] is True, report_a
    assert report_b["passed"] is True, report_b
    assert report_a["repo_gate"]["measured"] == "measured_pass"
    assert report_b["repo_gate"]["measured"] == "measured_pass"
    assert sorted(executed) == sorted([
        (run_id, command, code) for run_id in (run_a, run_b)
        for command, code in (("python3", 0), ("bash", 0))])
    assert report_b["repo_gate"]["output"] == "b"

    # A new run is raced by two independent controller objects. Force both past
    # the advisory read so the atomic admission transaction is the proven fence.
    competing = first.create_run(
        "isolated_recovery", {"project_id": "same"}, project_id="same")
    first.start_run(competing)
    second = _engine(db_path, workspace, projects, repos)
    passed_preflight = threading.Barrier(2)
    first_real_preflight = first._operation_blocks_reentry
    second_real_preflight = second._operation_blocks_reentry

    def synchronized_preflight(real):
        def wrapped(run_id, trigger):
            blocked = real(run_id, trigger)
            assert blocked is False
            passed_preflight.wait(timeout=5)
            return False
        return wrapped

    monkeypatch.setattr(
        first, "_operation_blocks_reentry",
        synchronized_preflight(first_real_preflight))
    monkeypatch.setattr(
        second, "_operation_blocks_reentry",
        synchronized_preflight(second_real_preflight))
    admitted = threading.Event()
    original_operation = {}
    first_real_admit = first._admit_op
    second_real_admit = second._admit_op

    def hold_first_after_admission(*args, **kwargs):
        operation_id = first_real_admit(*args, **kwargs)
        original_operation.update(first.unsettled_operations(competing)[0])
        admitted.set()
        raise RuntimeError(f"controller disconnected after operation {operation_id}")

    def race_second_admission(*args, **kwargs):
        assert admitted.wait(timeout=5)
        return second_real_admit(*args, **kwargs)

    monkeypatch.setattr(first, "_admit_op", hold_first_after_admission)
    monkeypatch.setattr(second, "_admit_op", race_second_admission)
    outcomes = []

    def drive(engine):
        try:
            outcomes.append(("return", engine.advance_run(competing)))
        except BaseException as exc:
            kind = "disconnect" if isinstance(exc, RuntimeError) else "exception"
            outcomes.append((kind, f"{type(exc).__name__}: {exc}"))

    racers = [threading.Thread(target=drive, args=(engine,))
              for engine in (first, second)]
    for thread in racers:
        thread.start()
    for thread in racers:
        thread.join(timeout=15)
        assert not thread.is_alive()

    operations = second.unsettled_operations(competing)
    assert len(operations) == 1
    assert operations[0]["run_id"] == competing
    assert operations[0]["step_instance_id"] is not None
    assert operations[0]["owner"]
    assert operations[0] == original_operation
    assert sorted(kind for kind, _ in outcomes) == ["disconnect", "return"]
    assert sorted(counter.read_text().splitlines()) == ["a", "b"]
    assert first.get_run(run_a)["status"] == "completed"
    assert first.get_run(run_b)["status"] == "completed"


@pytest.mark.parametrize("interrupt_worker", [False, True], ids=[
    "controller-disconnect-worker-survives",
    "worker-loss-requires-manual-settlement",
])
def test_recovery_preserves_operation_identity_and_one_logical_effect(
        tmp_path, monkeypatch, interrupt_worker):
    """A replacement controller observes the old identity and never replays it."""
    # A valid Git source holding only the authored gate; every mutable marker
    # lives in the gate's report-ticket entry directory, never in the source.
    repo = tmp_path / "repo"
    repo.mkdir()
    gate = repo / "run_tests.sh"
    gate.write_text(_RECOVERY_GATE, encoding="utf-8")
    gate.chmod(0o755)
    (repo / "test_fixture.py").write_text(
        "def test_fixture():\n    assert True\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "recovery fixture")
    source_before = _source_state(repo)

    # Isolated home + binding DB inside the enclosing disposable sandbox; the
    # controller process and the spawned worker both use the current closed
    # request validator and container entry (no host fallback, no Docker socket).
    from core import repository_executor as executor
    home = tmp_path / "h"
    _provision_bindings(home)
    monkeypatch.setenv("AITELIER_HOME", str(home))
    monkeypatch.setattr(executor, "execute", _owned_executor(home, tmp_path))

    db_path, workspace, projects = (
        tmp_path / "sf.db", tmp_path / "ws", tmp_path / "projects")
    setup = _engine(db_path, workspace, projects, repo)
    run_id = setup.create_run(
        "isolated_recovery", {"project_id": "p"}, project_id="p")
    setup.start_run(run_id)
    _bind_run(home, run_id, repo, "p")
    worker = multiprocessing.get_context("spawn").Process(
        target=_drive_gate,
        args=(str(db_path), str(workspace), str(projects), str(repo), run_id,
              str(home), str(tmp_path)))
    worker.start()
    try:
        entry = _wait_for_started(home, run_id)
        release = entry / "release"
        observer = _engine(db_path, workspace, projects, repo)
        original = observer.unsettled_operations(run_id)
        assert len(original) == 1
        identity = {
            key: original[0][key]
            for key in ("id", "run_id", "step_instance_id", "claim_epoch", "owner")
        }
        assert identity["run_id"] == run_id
        assert identity["step_instance_id"] is not None
        assert identity["claim_epoch"] == 1
        assert identity["owner"]

        if interrupt_worker:
            worker.kill()
            worker.join(timeout=5)
            assert worker.exitcode is not None

        recovered = _engine(db_path, workspace, projects, repo)
        assert recovered.advance_run(run_id) is None
        still_owned = recovered.unsettled_operations(run_id)
        assert [{key: row[key] for key in identity} for row in still_owned] == [identity]
        assert _logical_effects(home, run_id) == []
        assert _overlap_markers(home, run_id) == []
        assert _gate_entries(home, run_id) == [entry]

        release.touch()
        deadline = time.monotonic() + 10
        while (entry / "active").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not (entry / "active").exists()

        if interrupt_worker:
            assert _logical_effects(home, run_id) == ["logical-operation"]
            # The worker could not retire its durable admission. Observation of
            # the effect does not authorize another invocation: manual evidence
            # is still required and the exact original identity remains held.
            assert recovered.advance_run(run_id) is None
            assert recovered.unsettled_operations(run_id)[0]["id"] == identity["id"]
        else:
            worker.join(timeout=10)
            assert worker.exitcode == 0
            assert _logical_effects(home, run_id) == ["logical-operation"]
            assert recovered.unsettled_operations(run_id) == []
            assert recovered.get_run(run_id)["status"] == "completed"
        assert _gate_entries(home, run_id) == [entry]
        assert _overlap_markers(home, run_id) == []
        # The authored source is unchanged: the gate mutated only its ticket.
        assert _source_state(repo) == source_before
    finally:
        for stale in _gate_entries(home, run_id):
            (stale / "release").touch(exist_ok=True)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
