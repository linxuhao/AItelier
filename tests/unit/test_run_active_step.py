"""Current-step API projection prefers execution evidence over graph position."""
from api.run_routers import _active_step_snapshot


def _run(status="running", node="old_graph_node"):
    return {"id": "run-1", "status": status, "current_node": node}


def test_newest_active_instance_overrides_stale_current_node():
    steps = [
        {"id": 40, "step_id": "old_active", "status": "running",
         "updated_at": "2026-09-09 10:00:00"},
        {"id": 41, "step_id": "t_impl", "status": "claimed",
         "loop_item": "fix_map_travel_ending_scenarios",
         "updated_at": "2026-09-09 10:01:00"},
    ]

    assert _active_step_snapshot(_run(), steps) == {
        "step_id": "t_impl",
        "status": "claimed",
        "instance_id": 41,
        "loop_item": "fix_map_travel_ending_scenarios",
        "source": "step_instance",
    }


def test_terminal_run_never_exposes_a_stale_step():
    assert _active_step_snapshot(
        _run("completed", "t_impl"),
        [{"id": 41, "step_id": "t_impl", "status": "claimed"}],
    ) is None


def test_paused_run_never_claims_a_step_is_executing():
    assert _active_step_snapshot(
        _run("paused", "t_impl"),
        [{"id": 41, "step_id": "t_impl", "status": "claimed"}],
    ) is None


def test_active_run_uses_current_node_only_between_step_instances():
    assert _active_step_snapshot(_run("running", "t_plan"), []) == {
        "step_id": "t_plan",
        "status": "running",
        "instance_id": None,
        "loop_item": None,
        "source": "current_node",
    }


def test_project_runs_endpoint_attaches_the_snapshot(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api import run_routers
    from api.dependencies import get_db_manager

    run = _run("running", "stale_node")
    steps = [{"id": 4513, "step_id": "t_impl", "status": "claimed",
              "loop_item": "map_task"}]

    class FakeSF:
        def list_runs(self, project_id, status=None):
            assert project_id == "project-1"
            return [dict(run)]

        def get_steps(self, run_id):
            assert run_id == "run-1"
            return steps

    class FakeDB:
        def get_project(self, project_id):
            return {"project_id": project_id}

    monkeypatch.setattr(run_routers, "get_skillflow", lambda: FakeSF())
    monkeypatch.setattr(run_routers, "compute_cache_stats_batch", lambda ids: {})

    # Through the real route, not a direct call: the body now projects the row
    # through `execution_progress(request, row)` for whoever asked, so the
    # identity-bearing Request has to exist. With no Cloudflare Access gate
    # configured (this process) the verdict is trusted, exactly as for any other
    # read, and the row passes through unchanged.
    app = FastAPI()
    app.include_router(run_routers.router)
    app.dependency_overrides[get_db_manager] = lambda: FakeDB()
    response = TestClient(app).get("/api/projects/project-1/runs")
    assert response.status_code == 200, response.text
    response = response.json()
    assert response["runs"][0]["active_step"] == {
        "step_id": "t_impl",
        "status": "claimed",
        "instance_id": 4513,
        "loop_item": "map_task",
        "source": "step_instance",
    }
