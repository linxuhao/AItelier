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
    from api import run_routers

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

    response = run_routers.list_project_runs("project-1", db=FakeDB())
    assert response["runs"][0]["active_step"] == {
        "step_id": "t_impl",
        "status": "claimed",
        "instance_id": 4513,
        "loop_item": "map_task",
        "source": "step_instance",
    }
