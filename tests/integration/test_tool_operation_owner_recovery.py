"""A long tool operation survives controller loss without concurrent replay."""

from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import pytest
import skillflow as skillflow_package
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.identity import worker_identity
from skillflow.tool_loader import ToolLoader

from core import scheduler
from core.skillflow_host import AItelierSkillFlow

_ROOT = Path(__file__).resolve().parents[2]


def _graph() -> PipelineGraph:
    return PipelineGraph(
        name="owner_recovery_gate", begin="gate",
        steps=[StepNode(
            id="gate", step_type="tool", tool_name="run_tests",
            tool_params={"out_dir": "$STEP_DIR"}, timeout_seconds=1,
            transitions=[Transition(to=None)])])


def _engine(db_path: Path, workspace: Path, projects: Path,
            repo: Path) -> AItelierSkillFlow:
    loader = ToolLoader(Path(skillflow_package.__file__).parent / "tools")
    loader.add_tools_dir(_ROOT / "aitelier" / "tools")
    native = loader.is_native
    loader.is_native = lambda name: name == "run_tests" or native(name)
    sf = AItelierSkillFlow(
        str(db_path), tool_loader=loader, workspace_base=str(workspace),
        projects_base=str(projects), stale_threshold_seconds=0.05,
        code_path_resolver=lambda project_id, run_id=None: repo)
    sf.register_graph(_graph())
    return sf


def _invoke_long_gate(db_path: str, workspace: str, projects: str,
                      repo: str, run_id: str) -> None:
    sf = _engine(Path(db_path), Path(workspace), Path(projects), Path(repo))
    sf.advance_run(run_id)


def _wait_for(path: Path, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


@pytest.mark.parametrize(
    ("state", "dead", "decision"),
    [("alive", False, "retain_original_owner"),
     ("dead", True, "block_retry_pending_explicit_settlement"),
     ("unknown", None, "block_retry_pending_explicit_settlement")],
)
def test_recovery_records_identity_and_blocks_all_owner_states(
        tmp_path, monkeypatch, state, dead, decision):
    sf = _engine(tmp_path / "sf.db", tmp_path / "ws", tmp_path / "projects",
                 tmp_path / "repo")
    run_id = sf.create_run("owner_recovery_gate", {"project_id": "p"},
                           project_id="p")
    sf.start_run(run_id)
    # Reproduce the durable shape left by an interrupted inline tool without
    # executing it: claimed step plus an admitted operation tied to its epoch.
    with sf._tx() as conn:
        row = conn.execute(
            "SELECT id, claim_epoch FROM skillflow_steps "
            "WHERE run_id=? AND step_id='gate'", (run_id,)).fetchone()
        conn.execute(
            "UPDATE skillflow_steps SET status='claimed', claim_epoch=1, "
            "claimed_at='2026-09-12T21:15:10Z', claimed_by=? WHERE id=?",
            (worker_identity("tool-inline"), row["id"]))
    op_id = sf._admit_op(
        "tool_step", run_id, step_instance_id=row["id"], claim_epoch=1,
        detail="run_tests")

    from skillflow import identity

    import core.skillflow_host as host
    monkeypatch.setattr(identity, "owner_is_dead", lambda owner: dead)
    monkeypatch.setattr(host, "owner_is_dead", lambda owner: dead)

    report = sf.reconcile_active_operations(
        run_id, trigger="test_restart_before_claim_recovery")

    assert report["operations"][0]["id"] == op_id
    assert sf.advance_run(run_id) is None
    assert sf.claim_next_step(run_id) is None
    operation = sf.unsettled_operations(run_id)[0]
    assert operation["step_instance_id"] == row["id"]
    if state == "dead":
        assert operation["owner_lost_at"]
    else:
        assert operation["owner_lost_at"] is None

    traces = sf.trace_query(
        run_id,
        "SELECT payload_json FROM skillflow_trace "
        "WHERE event='operation_recovery_decision' ORDER BY seq", ())
    payload = json.loads(traces[0][0])
    assert payload == {
        "trigger": "test_restart_before_claim_recovery",
        "decision": decision,
        "owner_state": state,
        "operation_id": op_id,
        "kind": "tool_step",
        "detail": "run_tests",
        "owner": operation["owner"],
        "owner_lost_at": operation["owner_lost_at"],
        "admitted_at": operation["admitted_at"],
        "step_instance_id": row["id"],
        "claim_epoch": 1,
    }


def test_release_requires_evidence_and_only_then_allows_retry(tmp_path,
                                                              monkeypatch):
    sf = _engine(tmp_path / "sf.db", tmp_path / "ws", tmp_path / "projects",
                 tmp_path / "repo")
    run_id = sf.create_run("owner_recovery_gate", {"project_id": "p"},
                           project_id="p")
    sf.start_run(run_id)
    with sf._tx() as conn:
        row = conn.execute(
            "SELECT id FROM skillflow_steps WHERE run_id=? AND step_id='gate'",
            (run_id,)).fetchone()
        conn.execute(
            "UPDATE skillflow_steps SET status='claimed', claim_epoch=1, "
            "claimed_at='2026-09-12T21:15:10Z', claimed_by='old owner' "
            "WHERE id=?", (row["id"],))
    op_id = sf._admit_op(
        "tool_step", run_id, step_instance_id=row["id"], claim_epoch=1,
        detail="run_tests")

    import skillflow.core as skillflow_core
    from skillflow import identity

    import core.skillflow_host as host
    monkeypatch.setattr(identity, "owner_is_dead", lambda owner: True)
    monkeypatch.setattr(host, "owner_is_dead", lambda owner: True)
    monkeypatch.setattr(skillflow_core, "owner_is_dead", lambda owner: True)
    sf.reconcile_active_operations(run_id, trigger="restart")

    with pytest.raises(ValueError, match="requires evidence"):
        sf.release_operation(op_id, evidence="")
    assert sf.claim_next_step(run_id) is None

    released = sf.release_operation(
        op_id,
        evidence="REF recovery-test: child process joined; active marker absent")
    assert released["released"] is True
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    scheduler.recover_claims_on_startup()
    reopened = sf._conn.execute(
        "SELECT status FROM skillflow_steps WHERE id=?", (row["id"],)).fetchone()
    assert reopened["status"] == "pending"
    assert sf.unsettled_operations(run_id) == []

    payload = json.loads(sf.trace_query(
        run_id,
        "SELECT payload_json FROM skillflow_trace "
        "WHERE event='op_released_by_operator'", ())[0][0])
    assert payload["owner"]
    assert payload["owner_lost_at"]
    assert payload["evidence"].startswith("REF recovery-test")


def test_recovery_binds_pre_fix_tool_operation_to_claim_identity(tmp_path,
                                                                 monkeypatch):
    sf = _engine(tmp_path / "sf.db", tmp_path / "ws", tmp_path / "projects",
                 tmp_path / "repo")
    run_id = sf.create_run("owner_recovery_gate", {"project_id": "p"},
                           project_id="p")
    sf.start_run(run_id)
    with sf._tx() as conn:
        row = conn.execute(
            "SELECT id FROM skillflow_steps WHERE run_id=? AND step_id='gate'",
            (run_id,)).fetchone()
        conn.execute(
            "UPDATE skillflow_steps SET status='claimed', claim_epoch=4, "
            "claimed_at='2026-09-12T21:15:10Z', claimed_by='legacy owner' "
            "WHERE id=?", (row["id"],))
    # Exact 1.5.75 shape: inline admission preceded claim and recorded neither
    # instance nor epoch.
    op_id = super(AItelierSkillFlow, sf)._admit_op(
        "tool_step", run_id, detail="run_tests")

    from skillflow import identity

    import core.skillflow_host as host
    monkeypatch.setattr(identity, "owner_is_dead", lambda owner: None)
    monkeypatch.setattr(host, "owner_is_dead", lambda owner: None)
    sf.reconcile_active_operations(run_id, trigger="upgrade_cutover")

    operation = sf.unsettled_operations(run_id)[0]
    assert operation["id"] == op_id
    assert operation["step_instance_id"] == row["id"]
    assert operation["claim_epoch"] == 4


@pytest.mark.asyncio
async def test_periodic_recovery_reconciles_operations_before_claims(
        tmp_path, monkeypatch):
    sf = _engine(tmp_path / "sf.db", tmp_path / "ws", tmp_path / "projects",
                 tmp_path / "repo")
    order = []

    def reconcile(*, trigger):
        order.append(("operations", trigger))
        return {"lost": [], "unknown": []}

    def recover(threshold):
        order.append(("claims", threshold))
        return []

    monkeypatch.setattr(sf, "reconcile_active_operations", reconcile)
    monkeypatch.setattr(sf, "recover_stale_claims", recover)
    monkeypatch.setattr(sf, "list_runs", lambda status=None: [])
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)

    await scheduler._check_hung_claims()

    assert order == [
        ("operations", "periodic_before_claim_recovery"),
        ("claims", sf._stale_threshold),
    ]


def test_real_run_tests_restart_never_overlaps_old_effect(tmp_path, monkeypatch):
    """Kill the runtime during the real repo gate, then perform a cutover.

    The bash child deliberately outlives the killed Python owner. A fresh host
    reconciles the durable operation before startup claim recovery and is asked
    to advance repeatedly. A second invocation may start only after the child
    exits and the operator releases that exact operation with evidence.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    active = repo / "active"
    started = repo / "started"
    allow_finish = repo / "allow_finish"
    invocations = repo / "invocations"
    effects = repo / "effects"
    overlap = repo / "OVERLAP"
    gate = repo / "run_tests.sh"
    gate.write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        f"if ! mkdir {active!s}; then touch {overlap!s}; exit 91; fi\n"
        f"trap 'rmdir {active!s}' EXIT\n"
        f"echo start >> {invocations!s}\n"
        f"touch {started!s}\n"
        f"while [ ! -f {allow_finish!s} ]; do sleep 0.02; done\n"
        f"echo effect >> {effects!s}\n",
        encoding="utf-8")
    gate.chmod(0o755)

    db_path, workspace, projects = (tmp_path / "sf.db", tmp_path / "ws",
                                    tmp_path / "projects")
    setup = _engine(db_path, workspace, projects, repo)
    run_id = setup.create_run(
        "owner_recovery_gate", {"project_id": "p"}, project_id="p")
    setup.start_run(run_id)

    proc = multiprocessing.get_context("spawn").Process(
        target=_invoke_long_gate,
        args=(str(db_path), str(workspace), str(projects), str(repo), run_id))
    proc.start()
    try:
        _wait_for(started)
        old = _engine(db_path, workspace, projects, repo).unsettled_operations(run_id)
        assert len(old) == 1
        assert old[0]["step_instance_id"] is not None
        assert old[0]["claim_epoch"] == 1

        proc.kill()
        proc.join(5)
        assert proc.exitcode is not None
        assert active.is_dir(), "the real bash effect did not outlive its owner"

        recovered = _engine(db_path, workspace, projects, repo)
        monkeypatch.setattr(scheduler, "get_skillflow", lambda: recovered)
        scheduler.recover_claims_on_startup()
        claim = recovered._conn.execute(
            "SELECT status, claimed_by FROM skillflow_steps WHERE id=?",
            (old[0]["step_instance_id"],)).fetchone()
        assert claim["status"] == "claimed"

        for _ in range(3):
            assert recovered.advance_run(run_id) is None
        assert invocations.read_text().splitlines() == ["start"]
        assert active.is_dir()
        assert not overlap.exists()

        allow_finish.touch()
        deadline = time.monotonic() + 10
        while active.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not active.exists()
        assert effects.read_text().splitlines() == ["effect"]

        evidence = (
            f"REF integration child-exit={proc.exitcode}: orphan repo gate "
            "exited; active marker absent; first effect settled")
        released = recovered.release_operation(old[0]["id"], evidence=evidence)
        assert released["released"] is True

        scheduler.recover_claims_on_startup()
        recovered.advance_run(run_id)
        assert invocations.read_text().splitlines() == ["start", "start"]
        assert effects.read_text().splitlines() == ["effect", "effect"]
        assert not active.exists()
        assert not overlap.exists(), "two run_tests effects overlapped"
        assert recovered.unsettled_operations(run_id) == []
    finally:
        allow_finish.touch(exist_ok=True)
        if proc.is_alive():
            proc.kill()
            proc.join(5)
