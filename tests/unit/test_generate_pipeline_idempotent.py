"""One `generate_pipeline` request must produce one pipeline_forge run.

A duplicate tool call (observed: two calls 6s apart from a single user request)
launched a second forge run for the same slug. Both then designed, built tools for,
and raced to register the same `gen_<slug>` — double the cost, and a trace split
across two run ids so neither tells the whole story.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.meta_agent import MetaAgent


def _sf(runs_by_status):
    sf = MagicMock()
    sf.list_runs.side_effect = lambda project_id=None, status=None: \
        runs_by_status.get(status, [])
    return sf


def _run(rid, pid, graph="pipeline_forge"):
    return {"id": rid, "project_id": pid, "graph_name": graph}


class TestInflightLookup:
    def test_finds_a_running_run_for_the_same_slug(self):
        sf = _sf({"running": [_run("r1", "forge-todo-app-abc123")]})
        assert MetaAgent._inflight_forge_run(sf, "todo-app") == "r1"

    def test_finds_a_paused_run_too(self):
        """A forge run parked at its Design Review checkpoint is still in flight."""
        sf = _sf({"paused": [_run("r2", "forge-todo-app-abc123")]})
        assert MetaAgent._inflight_forge_run(sf, "todo-app") == "r2"

    def test_ignores_a_different_slug(self):
        sf = _sf({"running": [_run("r1", "forge-other-thing-abc123")]})
        assert MetaAgent._inflight_forge_run(sf, "todo-app") is None

    def test_ignores_a_prefix_collision(self):
        """`forge-todo-app-x` must not be matched by the slug `todo`."""
        sf = _sf({"running": [_run("r1", "forge-todo-app-abc123")]})
        assert MetaAgent._inflight_forge_run(sf, "todo") is None

    def test_ignores_another_config_that_happens_to_share_the_pid_shape(self):
        sf = _sf({"running": [_run("r1", "forge-todo-app-abc", graph="dpe_default_v2")]})
        assert MetaAgent._inflight_forge_run(sf, "todo-app") is None

    def test_a_terminal_run_does_not_block_a_regeneration(self):
        """Completed/failed runs are not queried — regenerating must still work."""
        sf = _sf({"completed": [_run("r1", "forge-todo-app-abc123")]})
        assert MetaAgent._inflight_forge_run(sf, "todo-app") is None

    def test_a_broken_skillflow_never_blocks_a_generation(self):
        sf = MagicMock()
        sf.list_runs.side_effect = RuntimeError("db locked")
        assert MetaAgent._inflight_forge_run(sf, "todo-app") is None


@pytest.mark.asyncio
async def test_duplicate_call_relays_the_existing_run_and_launches_nothing(monkeypatch):
    agent = MetaAgent(MagicMock(), MagicMock(), owner_email="test@local")

    sf = _sf({"running": [_run("r1", "forge-todo-app-abc123")]})
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)
    sf.get_run.return_value = {"id": "r1", "project_id": "forge-todo-app-abc123"}

    launched = []
    import core.run_launcher as rl
    monkeypatch.setattr(rl, "start_config_run",
                        lambda *a, **k: launched.append(a) or {"status": "ok",
                                                              "run_id": "r2"})

    async def _fake_poll(self, run_id):
        return {"status": "checkpoint", "polled": run_id}
    monkeypatch.setattr(MetaAgent, "_poll_pipeline_until_checkpoint", _fake_poll)

    out = await agent._tool_generate_pipeline(
        {"description": "a todo app pipeline", "name": "todo app"})

    assert launched == [], "a second pipeline_forge run was started"
    assert out["reused_existing_run"] is True
    assert out["run_id"] == "r1"
    assert out["polled"] == "r1"
    assert out["project_id"] == "forge-todo-app-abc123"


# ── The poll must not outlive the turn that is waiting on it ──────────────────
# Three timeouts disagreed: the CLI kills a silent turn at 420s
# (cli/tui/chat.py `_STALL_RELEASE_SECONDS`), the server's SSE guard fires at 600s
# (`api/agent_routers.py:_STREAM_IDLE_TIMEOUT`), and this poll waited up to 1800s
# emitting nothing. A 22-minute generation therefore always ended with the user
# reading "the turn looks dead" while the run was perfectly healthy.

def _source_constant(rel_path: str, pattern: str) -> int:
    """Read a timeout literal straight from the source.

    Importing the modules would be more natural, but another test module patches
    `cli.tui.chat`, so the constant comes back as a MagicMock depending on test
    order. The assertion is about the numbers in the files agreeing anyway.
    """
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / rel_path).read_text(encoding="utf-8")
    m = re.search(pattern, text)
    assert m, f"{pattern} no longer found in {rel_path} — did the guard move?"
    return int(m.group(1))


def test_the_poll_budget_stays_under_the_cli_watchdog():
    stall_release = _source_constant("cli/tui/chat.py",
                                     r"_STALL_RELEASE_SECONDS\s*=\s*(\d+)")
    assert MetaAgent._POLL_BUDGET_S < stall_release


def test_the_poll_budget_stays_under_the_server_sse_guard():
    sse_idle = _source_constant(
        "api/agent_routers.py",
        r'_STREAM_IDLE_TIMEOUT\s*=\s*int\(os\.getenv\([^,]+,\s*"(\d+)"\)\)')
    assert MetaAgent._POLL_BUDGET_S < sse_idle


@pytest.mark.asyncio
async def test_a_still_running_run_returns_instead_of_blocking(monkeypatch):
    agent = MetaAgent(MagicMock(), MagicMock(), owner_email="test@local")
    sf = MagicMock()
    sf.get_run.return_value = {"id": "r1", "status": "running", "graph_name": "pipeline_forge"}
    sf.get_steps.return_value = [{"step_id": "emit_graph", "status": "completed"}]
    monkeypatch.setattr("api.dependencies.get_skillflow", lambda: sf)

    slept = []

    async def _sleep(n):
        slept.append(n)
    monkeypatch.setattr("asyncio.sleep", _sleep)

    out = await agent._poll_pipeline_until_checkpoint("r1")

    assert out["still_running"] is True
    assert out["status"] == "running"
    assert "END YOUR TURN" in out["message"]
    assert sum(slept) <= MetaAgent._POLL_BUDGET_S


# ── A test-drive is driven by the butler, not the scheduler ──────────────────
# Generated pipelines are `scheduler_owned: false`, so `drive_pipeline` runs the
# whole claim/advance/confirm loop itself and no poller tick ever reaches the run.
# Without an explicit sync the project row sat at 'planning' forever while skillflow
# said `failed` — a dead test-drive looked like one that had not started.

def test_drive_pipeline_syncs_the_project_status():
    import inspect
    from core.meta_agent import MetaAgent
    src = inspect.getsource(MetaAgent._tool_drive_pipeline)
    assert "_sync_project_status_to_db" in src, (
        "drive_pipeline must sync its own run status — nothing else will")


def test_every_run_launching_butler_tool_syncs():
    """generate_pipeline already did; drive_pipeline was the odd one out."""
    import inspect
    from core.meta_agent import MetaAgent
    for name in ("_tool_generate_pipeline", "_tool_drive_pipeline"):
        src = inspect.getsource(getattr(MetaAgent, name))
        assert "_sync_project_status_to_db" in src, f"{name} does not sync"
