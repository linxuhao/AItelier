"""Composition tests for bounded failures and durable operation ownership.

The component suites prove each boundary separately.  These fixtures keep the
boundaries in one process-isolated scenario so a transport observation cannot
be mistaken for authority to replace an executor, and one run cannot settle a
neighbour's work.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
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


def _drive_gate(db_path: str, workspace: str, projects: str, repo: str,
                run_id: str) -> None:
    sf = _engine(Path(db_path), Path(workspace), Path(projects), Path(repo))
    sf.advance_run(run_id)


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
        gate.write_text(
            f"#!/bin/bash\nset -eu\necho {label} >> {counter}\necho {label}\n",
            encoding="utf-8")
        gate.chmod(0o755)

    first = _engine(db_path, workspace, projects, repos)
    run_a = first.create_run(
        "isolated_recovery", {"project_id": "a"}, project_id="a")
    run_b = first.create_run(
        "isolated_recovery", {"project_id": "b"}, project_id="b")
    first.start_run(run_a)
    first.start_run(run_b)

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
    first_real_admit = first._admit_op
    second_real_admit = second._admit_op

    def hold_first_after_admission(*args, **kwargs):
        operation_id = first_real_admit(*args, **kwargs)
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
        except RuntimeError as exc:
            outcomes.append(("disconnect", str(exc)))

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
    assert sorted(kind for kind, _ in outcomes) == ["disconnect", "return"]
    assert sorted(counter.read_text().splitlines()) == ["a", "b"]
    assert first.get_run(run_a)["status"] == "completed"
    assert first.get_run(run_b)["status"] == "completed"


@pytest.mark.parametrize("interrupt_worker", [False, True], ids=[
    "controller-disconnect-worker-survives",
    "worker-loss-requires-manual-settlement",
])
def test_recovery_preserves_operation_identity_and_one_logical_effect(
        tmp_path, interrupt_worker):
    """A replacement controller observes the old identity and never replays it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    active = repo / "active"
    started = repo / "started"
    release = repo / "release"
    counter = repo / "logical-effects"
    overlap = repo / "OVERLAP"
    gate = repo / "run_tests.sh"
    gate.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        f"if ! mkdir {active}; then touch {overlap}; exit 91; fi\n"
        f"trap 'rmdir {active}' EXIT\n"
        f"touch {started}\n"
        f"while [ ! -f {release} ]; do sleep 0.02; done\n"
        f"echo logical-operation >> {counter}\n",
        encoding="utf-8")
    gate.chmod(0o755)

    db_path, workspace, projects = (
        tmp_path / "sf.db", tmp_path / "ws", tmp_path / "projects")
    setup = _engine(db_path, workspace, projects, repo)
    run_id = setup.create_run(
        "isolated_recovery", {"project_id": "p"}, project_id="p")
    setup.start_run(run_id)
    worker = multiprocessing.get_context("spawn").Process(
        target=_drive_gate,
        args=(str(db_path), str(workspace), str(projects), str(repo), run_id))
    worker.start()
    try:
        _wait_for(started)
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
        assert not counter.exists()
        assert not overlap.exists()

        release.touch()
        deadline = time.monotonic() + 10
        while active.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not active.exists()

        if interrupt_worker:
            assert counter.read_text().splitlines() == ["logical-operation"]
            # The worker could not retire its durable admission. Observation of
            # the effect does not authorize another invocation: manual evidence
            # is still required and the exact original identity remains held.
            assert recovered.advance_run(run_id) is None
            assert recovered.unsettled_operations(run_id)[0]["id"] == identity["id"]
        else:
            worker.join(timeout=10)
            assert worker.exitcode == 0
            assert counter.read_text().splitlines() == ["logical-operation"]
            assert recovered.unsettled_operations(run_id) == []
            assert recovered.get_run(run_id)["status"] == "completed"
        assert not overlap.exists()
    finally:
        release.touch(exist_ok=True)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=5)
