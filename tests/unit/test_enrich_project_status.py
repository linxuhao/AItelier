"""`enrich_project_status` must not throw away the scheduler's enriched status.

The scheduler writes `failed:<why>` into `runs.status` (scheduler.py `_failure_reason`),
but skillflow's own run row only ever says `failed`. The read path used to overwrite
the DB value with the raw one for every status except `running`, so the user saw a
bare "failed" and had to go read container logs to learn why — which is exactly the
diagnosability problem the enrichment was added to solve.

The rule: the DB value wins when it is a REFINEMENT of the raw status (same prefix
before the colon); a prefix mismatch means the DB is stale and skillflow wins.
"""
from __future__ import annotations

import pytest

from api import dependencies as deps


class _FakeSF:
    def __init__(self, run):
        self._run = run

    def get_run_by_project(self, pid):
        return self._run

    def list_runs(self, pid):
        return [self._run] if self._run else []

    def get_steps(self, run_id):
        return []


@pytest.fixture
def patched(monkeypatch):
    def _apply(run):
        monkeypatch.setattr(deps, "get_skillflow", lambda: _FakeSF(run))
        monkeypatch.setattr(deps, "get_config_registry", lambda: None)
    return _apply


def _run(status, node=None):
    return {"id": "r1", "status": status, "current_node": node,
            "graph_name": "pipeline_forge"}


def test_enriched_failure_reason_survives(patched):
    patched(_run("failed"))
    p = {"project_id": "p1",
         "status": "failed:Cycle limit exceeded — v_smoke: no transition matched"}
    out = deps.enrich_project_status(p)
    assert out["status"] == (
        "failed:Cycle limit exceeded — v_smoke: no transition matched")
    # the block really ran (enrich swallows exceptions, which would leave the
    # status untouched and make this assertion pass for the wrong reason)
    assert "completed_project_steps" in out


def test_bare_failed_is_adopted_when_db_has_nothing_richer(patched):
    patched(_run("failed"))
    p = {"project_id": "p1", "status": "running:emit_graph"}
    assert deps.enrich_project_status(p)["status"] == "failed"


def test_stale_enriched_status_loses_to_a_reactivated_run(patched):
    """DB says failed:<old>, skillflow says the run is going again → skillflow wins."""
    patched(_run("running", node="emit_graph"))
    p = {"project_id": "p1", "status": "failed:something old"}
    assert deps.enrich_project_status(p)["status"] == "running:emit_graph"


def test_running_without_a_current_node_falls_back_to_raw(patched):
    patched(_run("running", node=None))
    p = {"project_id": "p1", "status": "planning"}
    assert deps.enrich_project_status(p)["status"] == "running"


def test_completed_is_not_shadowed_by_a_stale_running_status(patched):
    patched(_run("completed"))
    p = {"project_id": "p1", "status": "running:5"}
    assert deps.enrich_project_status(p)["status"] == "completed"


def test_enriched_running_status_from_the_db_is_replaced_by_the_live_node(patched):
    """The live node is fresher than the DB's cached one — AT-15's original case."""
    patched(_run("running", node="7"))
    p = {"project_id": "p1", "status": "running:3"}
    assert deps.enrich_project_status(p)["status"] == "running:7"
