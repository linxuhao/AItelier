from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from core.dpe_pipeline import PipelineEngine


ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = ROOT / "tests/fixtures/coding_impl_no_runner_20260914.json"


def _focused_check():
    from aitelier.tools.focused_check.impl import focused_check

    return focused_check


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "attempt-worktree"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


def test_live_no_runner_shape_is_fixed_by_discoverable_tool_at_reviewed_budget() -> None:
    observed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert observed["reported_constraint"] == "no command runner"
    assert observed["turns_used"] == observed["max_tool_turns_observed"] == 40
    assert observed["finish_step_reached"] is False
    assert observed["test_step_reached"] is False
    assert "focused_check" not in observed["available_tools"]

    agent = yaml.safe_load((ROOT / "agent_configs/coding_impl.yaml").read_text())[
        "offload_implementer"
    ]
    prompt = (ROOT / "templates/coding_impl.md").read_text(encoding="utf-8")
    assert "focused_check" in agent["tools"]
    # The reviewed budget; pinned in tests/unit/test_implementer_turn_budgets.py
    assert agent["max_tool_turns"] == 200
    assert "available from turn 1" in prompt
    assert "before `finish_step`" in prompt


def test_focused_pytest_is_installed_in_the_runtime_image() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert any(item.startswith("pytest>=") for item in project["project"]["dependencies"])


def test_pytest_check_is_scoped_scrubbed_bounded_and_identified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    test_file = repo / "tests/test_probe.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "import os\n"
        "def test_probe():\n"
        "    assert 'PROBE_API_KEY' not in os.environ\n"
        "    print('😀' * 10000)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PROBE_API_KEY", "must-not-reach-child")

    result = _focused_check()(
        kind="pytest",
        targets=["tests/test_probe.py::test_probe"],
        timeout_seconds=30,
        project_root=str(repo),
        run_id="run-identity",
        step_id="implement",
    )

    assert result["passed"] is True
    assert result["exit_status"] == 0
    assert result["timed_out"] is False
    assert result["cwd"] == str(repo.resolve())
    assert result["command"][-1] == "tests/test_probe.py::test_probe"
    assert result["timeout_seconds"] == 30
    assert result["output_truncated"] is True
    assert len(result["output"].encode()) <= result["output_limit_bytes"]
    assert result["output_bytes"] > result["output_limit_bytes"]
    assert result["run_id"] == "run-identity"
    assert result["step_id"] == "implement"
    assert result["attempt_scope"] == "run-identity:implement"


def test_timeout_kills_the_focused_process_group(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    target = repo / "tests/test_slow.py"
    target.parent.mkdir()
    target.write_text(
        "import time\n"
        "def test_slow():\n"
        "    time.sleep(10)\n",
        encoding="utf-8",
    )
    result = _focused_check()(
        kind="pytest", targets=["tests/test_slow.py"], timeout_seconds=1,
        project_root=str(repo), run_id="r", step_id="implement"
    )
    assert result["passed"] is False
    assert result["timed_out"] is True
    assert result["exit_status"] == -9


def test_game_probe_uses_the_same_bounds_and_trace_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "project.godot").write_text("[application]\n", encoding="utf-8")
    from aitelier.tools.focused_check import impl as focused_impl
    monkeypatch.setattr(focused_impl, "_PROCESS_START_METHOD", "fork")

    def fake_scenario(**kwargs):
        assert kwargs["_timeout_seconds"] == 17
        assert kwargs["project_root"] == str(repo.resolve())
        return {
            "all_passed": True,
            "hard_passed": True,
            "timed_out": False,
            "report": "界" * 20_000,
        }

    monkeypatch.setattr(focused_impl, "_run_godot_scenario", fake_scenario)
    result = focused_impl.focused_check(
        kind="godot_scenario", scenario="one_smoke", timeout_seconds=17,
        project_root=str(repo), run_id="game-run", step_id="implement"
    )
    assert result["passed"] is True
    assert result["command"] == ["godot_playtest_scenario", "one_smoke"]
    assert result["cwd"] == str(repo.resolve())
    assert result["timeout_seconds"] == 17
    assert result["exit_status"] == 0
    assert result["output_truncated"] is True
    assert len(result["output"].encode()) <= result["output_limit_bytes"]
    assert result["attempt_scope"] == "game-run:implement"


def test_game_probe_enforces_wall_clock_and_kills_late_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (repo / "project.godot").write_text("[application]\n", encoding="utf-8")
    outside = tmp_path / "late-mutation"
    from aitelier.tools.focused_check import impl as focused_impl
    monkeypatch.setattr(focused_impl, "_GODOT_WORKER", (
        "import sys,time\n"
        "for _ in range(30):\n"
        " sys.stdout.write('x'); sys.stdout.flush(); time.sleep(.1)\n"
        f"open({str(outside)!r}, 'w').write('too late')\n"
    ))
    started = time.monotonic()
    result = focused_impl.focused_check(
        kind="godot_scenario", scenario="slow", timeout_seconds=1,
        project_root=str(repo), run_id="game-run", step_id="implement"
    )
    elapsed = time.monotonic() - started
    time.sleep(1.2)

    assert elapsed < 1.8
    assert result["passed"] is False
    assert result["timed_out"] is True
    assert result["exit_status"] == -9
    assert not outside.exists()


def test_agent_cannot_spoof_focused_check_attempt_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import api.dependencies as dependencies

    captured = {}

    class FakeSkillFlow:
        def execute_tool(self, name, params, **host):
            captured.update(name=name, params=params, host=host)
            return {"ok": True}

    monkeypatch.setattr(dependencies, "get_skillflow", lambda: FakeSkillFlow())
    with patch("core.agents.AgentFactory.__init__", return_value=None):
        engine = PipelineEngine()
    engine._tool_schemas = {"focused_check": {"name": "focused_check"}}
    engine._run_id = "host-run"
    engine._current_step = "implement"
    engine._step_instance_id = 9
    engine._output_target = "code"
    engine._output_fixed = {}
    engine._write_scope = None
    engine._project_id = "host-project"

    result = engine._exec_tool({
        "tool": "focused_check",
        "params": {
            "kind": "pytest", "targets": ["tests/test_ok.py"],
            "run_id": "spoof", "step_id": "spoof", "project_id": "spoof",
            "operation_id": "spoof", "project_root": "/tmp/spoof",
        },
    })
    assert result == {"ok": True}
    assert captured["params"] == {
        "kind": "pytest", "targets": ["tests/test_ok.py"]
    }
    assert captured["host"]["run_id"] == "host-run"
    assert captured["host"]["step_id"] == "implement"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"kind": "shell", "targets": ["tests/test_ok.py"]}, "unsupported check kind"),
        ({"kind": "container", "targets": ["tests/test_ok.py"]}, "unsupported check kind"),
        ({"kind": "pytest", "targets": ["../outside_test.py"]}, "escapes"),
        ({"kind": "pytest", "targets": ["/tmp/outside_test.py"]}, "relative"),
        ({"kind": "pytest", "targets": ["--collect-only"]}, "option"),
        ({"kind": "pytest", "targets": ["tests/test_ok.py"],
          "command": ["docker", "restart", "aitelier"]}, "unsupported host/control"),
    ],
)
def test_escape_and_host_control_requests_fail_closed(
    tmp_path: Path, kwargs: dict, message: str
) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "outside-sentinel"
    outside.write_text("unchanged", encoding="utf-8")
    result = _focused_check()(
        project_root=str(repo), run_id="r", step_id="implement", **kwargs
    )
    assert message in result["error"]
    assert outside.read_text(encoding="utf-8") == "unchanged"


def test_one_focused_check_cannot_hide_unrelated_full_suite_regression(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_focused.py").write_text("def test_focused(): assert True\n")
    (tests / "test_unrelated.py").write_text("def test_unrelated(): assert False\n")

    focused = _focused_check()(
        kind="pytest", targets=["tests/test_focused.py"], timeout_seconds=30,
        project_root=str(repo), run_id="r", step_id="implement"
    )
    later = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"], cwd=repo,
        capture_output=True, text=True, timeout=30, check=False,
    )
    graph = yaml.safe_load((ROOT / "configs/coding_impl.yaml").read_text())
    test_step = next(step for step in graph["steps"] if step["id"] == "test")

    assert focused["passed"] is True
    assert later.returncode != 0
    assert "test_unrelated" in later.stdout
    assert test_step["tool_name"] == "run_tests"
    assert graph["end_conditions"]["conditions"][0]["node"] == "done"


class _Workspace:
    def __init__(self, base: Path, repo: Path):
        self.base_path = base
        self.projects_base = base / "projects"
        self.repo = repo

    def _get_secure_path(self, project_id: str) -> Path:
        path = self.base_path / project_id
        path.mkdir(exist_ok=True)
        return path

    def get_code_path(self, project_id: str, run_id: str | None = None) -> Path:
        return self.repo

    def clean_draft_dir(self, *args, **kwargs) -> None:
        return None


def _turn(*calls: tuple[str, dict, str]):
    return SimpleNamespace(
        text="", reasoning_content="", truncated=False,
        tool_calls=[
            {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}
            for name, args, call_id in calls
        ],
    )


def test_representative_native_implement_traces_repo_and_game_checks_before_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_probe.py").write_text("def test_probe(): assert True\n")
    (repo / "project.godot").write_text("[application]\n", encoding="utf-8")
    playtest = repo / "playtest"
    playtest.mkdir()
    (playtest / "_common.yaml").write_text(
        "scene: res://main.tscn\nscenario_order: [smoke]\n", encoding="utf-8"
    )
    (playtest / "smoke.yaml").write_text(
        "name: smoke\ntimeline:\n  - at: 1\n    assert: {ready: 'ready == true'}\n",
        encoding="utf-8",
    )

    from aitelier.tools.focused_check import impl as focused_impl
    monkeypatch.setattr(focused_impl, "_PROCESS_START_METHOD", "fork")
    sidecar = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({
                "passed": True, "spec_used": True, "frames": 2, "errors": [],
                "summary": "focused runtime passed", "behavior": {
                    "all_passed": True, "scenarios": [{
                        "name": "smoke", "passed": True, "errors": [],
                        "asserts": [{"name": "ready", "passed": True}],
                    }],
                },
            }).encode()

    def fake_urlopen(request, timeout=0):
        sidecar["payload"] = json.loads(request.data)
        sidecar["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "aitelier.tools.godot_playtest.impl.urllib.request.urlopen", fake_urlopen
    )

    with patch("core.agents.AgentFactory.__init__", return_value=None):
        engine = PipelineEngine()
    engine.factory = MagicMock()
    engine.factory.is_native.return_value = True
    engine.factory.get_fallback_to_json.return_value = False
    engine.factory.get_max_retries.return_value = 1
    engine.factory.get_max_tool_turns.return_value = 32
    native = engine.factory.get_native_agent.return_value
    native.gateway.litellm_model = "mock"
    native.gateway.last_usage = {}
    native.turn.side_effect = [
        _turn(("focused_check", {"kind": "pytest", "targets": ["tests/test_probe.py"]}, "c1")),
        _turn(("focused_check", {"kind": "godot_scenario", "scenario": "smoke"}, "c2")),
        _turn(("finish_step", {}, "c3")),
    ]
    traces: list[tuple[str, str, dict]] = []
    engine._trace_cb = lambda category, event, payload: traces.append(
        (category, event, payload)
    )

    def execute(action: dict) -> dict:
        if action["tool"] == "finish_step":
            return {"status": "completed"}
        return focused_impl.focused_check(
            **action["params"], project_root=str(repo), run_id="run-representative",
            step_id="implement"
        )

    engine._exec_tool = execute
    ok = engine.run_step(
        task_id=1, step_id="implement", workspace=_Workspace(tmp_path, repo),
        project_id="p", agent_config_name="offload_implementer",
        tool_schemas={"focused_check": {"name": "focused_check", "parameters": {}}},
        output_target="code", run_id="run-representative", step_instance_id=77,
        config_name="coding_impl",
    )

    assert ok is True
    tool_rows = [
        json.loads(payload["content"])
        for category, event, payload in traces
        if event == "prompt_delta" and payload.get("role") == "tool"
        and payload.get("name") == "focused_check"
    ]
    assert [row["kind"] for row in tool_rows] == ["pytest", "godot_scenario"]
    assert all(row["run_id"] == "run-representative" for row in tool_rows)
    assert all(row["step_id"] == "implement" for row in tool_rows)
    assert all(row["cwd"] == str(repo.resolve()) for row in tool_rows)
    assert all(row["timeout_seconds"] > 0 for row in tool_rows)
    assert all(isinstance(row["exit_status"], int) for row in tool_rows)
    assert all(row["output_bytes"] <= row["output_limit_bytes"] or row["output_truncated"]
               for row in tool_rows)
