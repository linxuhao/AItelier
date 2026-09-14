"""Host recovery for a confirmed predecessor whose successor cannot be claimed.

The live incident is run 2135847f-d2a9-48d6-a306-a396f1566088.  Its two
successful ``implement`` rows were distinct instances, each followed by a real
``test`` execution; a failed test transition intentionally opened the later
implement instance.  These controls preserve that graph behavior and exercise
the actual missing host boundary: an agent successor returning ``None`` with no
claim or operation must become recovery-required and then fail precisely.
"""

from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

from skillflow import SkillFlow
from skillflow.core import StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition

from core import scheduler


class _Resolver:
    def __init__(self, *, tool=False):
        self.tool = tool

    def get_node(self, _step):
        return SimpleNamespace(step_type="tool" if self.tool else "agent",
                               tool_name="fixture_tool" if self.tool else "")

    def is_gate(self, _step):
        return False

    def is_loop(self, _step):
        return False

    def is_tool(self, _step):
        return self.tool


class _BlockedSuccessor:
    def __init__(self, *, tool=False):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
            CREATE TABLE skillflow_active_ops (id INTEGER, run_id TEXT);
            CREATE TABLE skillflow_steps (
                id INTEGER PRIMARY KEY, run_id TEXT, step_id TEXT, status TEXT,
                completion_seq INTEGER
            );
            INSERT INTO skillflow_steps VALUES (1, 'run1', 'implement', 'completed', 1);
            INSERT INTO skillflow_steps VALUES (2, 'run1', 'test', 'pending', NULL);
        """)
        self.resolver = _Resolver(tool=tool)
        self.payloads = []
        self.failed = []

    def get_run(self, _run_id):
        return {"id": "run1", "project_id": "p1", "status": "running",
                "current_node": "test"}

    def _get_resolver_for_run(self, _run_id):
        return self.resolver

    def _should_delegate_tool(self, _tool_name):
        return False

    @contextmanager
    def _ro(self):
        yield self._conn

    def trace(self, _run_id, _kind, event, payload, **_kwargs):
        assert event == "successor_claim_blocked"
        self.payloads.append(json.dumps(payload))

    def trace_query(self, _run_id, _sql, _params):
        return [[payload] for payload in reversed(self.payloads[-2:])]

    def fail_run(self, run_id, reason):
        self.failed.append((run_id, reason))


def test_unclaimable_successor_enters_recovery_then_fails_precisely(monkeypatch):
    sf = _BlockedSuccessor()
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *_: False)

    first = scheduler._record_unclaimable_successor(sf, "run1", "p1")
    assert first["failed"] is False
    assert first["recovery_required"] is True
    assert first["predecessor"] == "implement"
    assert first["predecessor_instance_id"] == 1
    assert first["successor"] == "test"
    assert first["successor_instance_id"] == 2
    assert first["successor_state"] == "pending"
    assert sf.failed == []

    second = scheduler._record_unclaimable_successor(sf, "run1", "p1")
    assert second["failed"] is True
    assert len(sf.failed) == 1 and sf.failed[0][0] == "run1"
    assert "confirmed predecessor 'implement' (instance 1)" in sf.failed[0][1]
    assert "successor 'test' (instance 2, state pending)" in sf.failed[0][1]
    # The host diagnoses the successor. It never reopens the confirmed maker.
    row = sf._conn.execute(
        "SELECT status FROM skillflow_steps WHERE id = 1").fetchone()
    assert row["status"] == "completed"


def test_inline_tool_none_is_not_misclassified_as_agent_successor(monkeypatch):
    sf = _BlockedSuccessor(tool=True)
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *_: False)
    assert scheduler._record_unclaimable_successor(sf, "run1", "p1") is None
    assert sf.payloads == []
    assert sf.failed == []


def test_scheduler_uses_recovery_result_instead_of_no_claim(monkeypatch):
    sf = MagicMock()
    sf.trace_query.return_value = [[0]]
    sf._get_resolver_for_run.return_value.is_tool.return_value = False
    sf.get_run.return_value = {"status": "running", "current_node": "test"}
    sf.claim_next_step.return_value = None
    monkeypatch.setattr(scheduler, "get_skillflow", lambda: sf)
    monkeypatch.setattr(scheduler, "_get_or_create_skillflow_run", lambda _p: "run1")
    monkeypatch.setattr(scheduler, "_has_active_claim", lambda *_: False)
    monkeypatch.setattr(scheduler, "_advance_recording_crashes", lambda *_: None)
    monkeypatch.setattr(scheduler, "_sync_project_status_to_db", lambda _p: None)
    monkeypatch.setattr(scheduler, "_record_unclaimable_successor", lambda *_: {
        "failed": False, "successor": "test", "predecessor": "implement",
        "reason": "exact recovery reason",
    })
    logged = []
    monkeypatch.setattr(scheduler, "tick_log",
                        lambda _p, outcome, **kw: logged.append((outcome, kw)))

    import asyncio
    asyncio.run(scheduler._run_skillflow_tick("p1", None))

    assert any(outcome == "successor_recovery_required" for outcome, _ in logged)
    assert not any(outcome == "no_claim" for outcome, _ in logged)


def test_confirmed_successor_is_claimed_once_across_host_restart(tmp_path):
    """SkillFlow owns the atomic confirm transition and claim CAS boundary."""
    db = str(tmp_path / "skillflow.db")
    graph = PipelineGraph(
        name="confirmed-successor",
        begin="implement",
        steps=[
            StepNode(id="implement", step_type="agent", agent_config="maker",
                     transitions=[Transition(to="test")]),
            StepNode(id="test", step_type="agent", agent_config="checker",
                     transitions=[Transition(to=None)]),
        ],
    )
    first = SkillFlow(db)
    first.register_agent_config("maker")
    first.register_agent_config("checker")
    first.register_graph(graph)
    run_id = first.create_run(graph.name, {"project_id": "p1"}, project_id="p1")
    first.start_run(run_id)
    assert first.advance_run(run_id) == "implement"
    implement = first.claim_next_step(run_id)
    first.confirm_step(implement.token, StepResult())
    assert first.get_run(run_id)["current_node"] == "test"

    # Re-open the durable database as a restarted host. Two controllers ask for
    # the same successor; the CAS admits exactly one and never reopens implement.
    restarted = SkillFlow(db)
    assert restarted.advance_run(run_id) == "test"
    test_claim = restarted.claim_next_step(run_id)
    duplicate = first.claim_next_step(run_id)
    assert test_claim is not None and test_claim.step_id == "test"
    assert duplicate is None
    rows = restarted.get_steps(run_id)
    assert [(row["step_id"], row["status"]) for row in rows].count(
        ("implement", "completed")) == 1
