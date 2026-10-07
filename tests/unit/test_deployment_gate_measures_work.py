"""The deployment gate blocks on work, not on the shape of bystanders.

Journal event c5cfa3fea34847818d4cf99503cceed1 (2026-10-06 23:31Z): an
owner-authorized redeploy was refused on (a) the gate's own invoking bash
wrapper, flagged twice as an unregistered evaluator because every Claude Code
command runs through `bash -c "source <snapshot> && eval '<command>'"`, and
(b) two runs with active_operations=0 and audit.alive=0, which the driver
rebuilds from their trace after a restart.

Each claim has its opposite pole, so the fix cannot pass by switching the
gate off: a real unregistered evaluator, and a run that owns a live process,
still block.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from core import deployment_quiescence as dq


def _probe(*lines):
    def runner(command):
        out = "" if command[0] == "docker" else "\n".join(lines) + "\n"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    return runner


def _stat(root, pid, ppid, comm="bash"):
    entry = root / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "stat").write_text(f"{pid} ({comm}) S {ppid} 1 1 0 -1 4194560 0\n")


# ===========================================================================
# 1. the measurer's own pid chain is not an owner
# ===========================================================================

def test_the_gate_does_not_measure_its_own_shell(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    me = os.getpid()
    # Independently derived parent chain: this process -> 777 -> 778 -> init(1) -> 0.
    # pid 1 is the legitimate init ancestor of the invocation, not a bystander.
    chain = [(me, 777, "python3"), (777, 778, "bash"),
             (778, 1, "bash"), (1, 0, "init")]
    for pid, ppid, comm in chain:
        _stat(root, pid, ppid, comm=comm)
    monkeypatch.setattr(dq, "PROC_ROOT", root)
    expected_own = {str(pid) for pid, _, _ in chain}
    assert dq.measurer_pids() == expected_own
    wrapper = "/bin/bash -c source snap.sh && eval 'python -m evaluator_worker'"
    owners, errors = dq.external_owners(runner=_probe(
        f"{me} 777 {wrapper}", f"777 778 {wrapper}", f"778 1 {wrapper}",
        f"1 0 {wrapper}", f"4321 1 {wrapper}"))
    # Byte-identical argv on an unrelated pid is still an unknown evaluator.
    assert [o["command"].split()[0] for o in owners] == ["4321"]
    assert owners[0]["ownership"] == "unregistered" and owners[0]["active"] is True
    assert len(errors) == 1 and "4321 1" in errors[0]


def test_an_unreadable_proc_chain_still_excludes_the_process_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "PROC_ROOT", tmp_path / "nowhere")
    assert dq.measurer_pids() == {str(os.getpid())}


# ===========================================================================
# 2. the shell builtin `eval` is not an evaluator
# ===========================================================================

def test_a_claude_code_shell_wrapper_is_not_an_evaluator(tmp_path, monkeypatch):
    monkeypatch.setattr(dq, "PROC_ROOT", tmp_path / "proc")
    line = ("9001 1 /bin/bash -c source /home/x/.claude/shell-snapshots/snapshot-bash-1.sh "
            "2>/dev/null || true && eval 'date; cd ~/AItelier && .venv/bin/python guarded.py'")
    owners, errors = dq.external_owners(runner=_probe(line))
    # The pre-existing "aitelier" needle still lists the row as a resident,
    # inactive process; what matters is that nothing is active or in error.
    assert not any(o["active"] for o in owners)
    assert all(o.get("ownership") is None for o in owners)
    assert errors == []


@pytest.mark.parametrize("line", [
    "9002 1 /bin/bash -c python evaluator.py --repo /tmp/x",
    "9003 1 python -m eval_job --task 7",
    "9004 1 node evaluation.js",
    "9005 1 /usr/local/bin/eval_job --queue a",
    "9006 1 /bin/bash -c source snap.sh && eval 'python -m grader_worker'",
])
def test_real_evaluator_shapes_still_block(tmp_path, monkeypatch, line):
    monkeypatch.setattr(dq, "PROC_ROOT", tmp_path / "proc")
    owners, errors = dq.external_owners(runner=_probe(line))
    assert owners and owners[0]["active"] is True
    assert owners[0]["resource"] == "external_measurement"
    assert owners[0]["ownership"] == "unregistered"
    assert errors


def test_the_pattern_lost_exactly_the_bare_eval_alternative():
    """Pinned so the list cannot shrink quietly: every other alternative the
    gate matched before 2026-10-06 is still here, and bare `eval` is not."""
    assert dq.MEASUREMENT_NAME_PATTERN.pattern == (
        r"(?:^|[^a-z0-9])(measurement|evaluation|evaluator|eval[_-]?job|"
        r"benchmark|playtest|judge|grader|grading|scor(?:e|ing)|"
        r"assessment|assessor|rater|review|quality[_-]?check|"
        r"metrics?|"
        r"[a-z0-9]+[_-](?:worker|job)|[a-z0-9]+(?:worker|job))"
        r"(?:[^a-z0-9]|$)")
    assert dq.MEASUREMENT_NAME_PATTERN.search(" eval 'date'") is None
    for word in ("eval_job", "evaluation", "evaluator", "metrics", "review",
                 "foo_worker", "fooworker", "playtest"):
        assert dq.MEASUREMENT_NAME_PATTERN.search(f" {word} "), word


# ===========================================================================
# 3. a run with nothing admitted is resumable, not a blocker
# ===========================================================================

class _SkillFlow:
    def __init__(self, status="running", audit=None, raise_audit=False):
        self.status, self.audit_value, self.raise_audit = status, audit, raise_audit

    def list_runs(self):
        return [{"id": "run-1", "project_id": "sg-1", "status": self.status}]

    def audit_operation_owners(self, run_id):
        if self.raise_audit:
            raise RuntimeError("audit down")
        return self.audit_value if self.audit_value is not None else {
            "alive": 0, "lost": [], "unknown": []}


def _measure(sf):
    return dq.measure(skillflow=sf, db=None, sidecar_db=None, external_probe=lambda: [])


def test_a_running_run_with_nothing_admitted_is_resumable_and_does_not_block():
    obs = _measure(_SkillFlow())
    assert obs["runs"][0]["resumable"] is True
    # The literal public projection names exactly the runs a restart resumes.
    assert obs["resumable_runs"] == [
        {"run_id": "run-1", "project_id": "sg-1", "status": "running"}]
    assert obs["blockers"]["active_runs"] == []
    assert obs["blockers"]["active_operations"] == []
    assert obs["errors"] == [] and obs["quiescent"] is True
    assert dq._validate_observation(obs) is None


@pytest.mark.parametrize("sf", [
    _SkillFlow(audit={"alive": 1, "lost": [], "unknown": []}),
    _SkillFlow(audit={"alive": 0, "lost": ["op-9"], "unknown": []}),
    _SkillFlow(audit={"alive": 0, "lost": [], "unknown": ["op-9"]}),
], ids=["alive", "lost", "unknown"])
def test_a_run_that_owns_a_process_still_blocks(sf):
    obs = _measure(sf)
    assert obs["runs"][0]["resumable"] is False
    assert obs["resumable_runs"] == []
    assert [r["run_id"] for r in obs["blockers"]["active_runs"]] == ["run-1"]
    assert obs["quiescent"] is False
    assert dq._validate_observation(obs) is None


def test_a_run_whose_audit_failed_still_blocks():
    obs = _measure(_SkillFlow(raise_audit=True))
    assert obs["runs"][0]["resumable"] is False
    assert [r["run_id"] for r in obs["blockers"]["active_runs"]] == ["run-1"]
    assert any("operation audit failed" in e for e in obs["errors"])
    assert obs["quiescent"] is False


def test_a_terminal_run_is_neither_resumable_nor_a_blocker():
    obs = _measure(_SkillFlow(status="completed"))
    assert obs["runs"][0]["resumable"] is False
    assert obs["blockers"]["active_runs"] == [] and obs["quiescent"] is True


def test_a_forged_resumable_flag_is_refused_by_validation():
    obs = _measure(_SkillFlow(audit={"alive": 1, "lost": [], "unknown": []}))
    obs["runs"][0]["resumable"] = True
    obs["blockers"]["active_runs"] = []
    obs["quiescent"] = True
    obs["digest"] = dq._observation_digest(obs)
    assert "resumable contradicts" in dq._validate_observation(obs)


def test_a_forged_top_level_resumable_runs_projection_is_refused():
    """observation.resumable_runs is public output but never an authorization
    input: forging the list so a genuinely blocked run looks resumable fails."""
    obs = _measure(_SkillFlow(audit={"alive": 1, "lost": [], "unknown": []}))
    assert obs["resumable_runs"] == []
    obs["resumable_runs"] = [
        {"run_id": "run-1", "project_id": "sg-1", "status": "running"}]
    obs["digest"] = dq._observation_digest(obs)
    reason = dq._validate_observation(obs)
    assert reason is not None and "resumable_runs contradicts" in reason


def test_the_journal_records_which_runs_the_restart_will_resume(tmp_path):
    obs = _measure(_SkillFlow())
    clearance = dq.authorize("restart", obs, journal=tmp_path / "journal.json")
    assert clearance["allowed"] is True
    event = clearance["event"]
    assert event["status"] == "authorized"
    assert event["resumable_runs"] == [
        {"run_id": "run-1", "project_id": "sg-1", "status": "running"}]
    assert event["blockers"]["active_runs"] == []
