"""Exercise the public tool's declared pytest wall and honest incomplete outcomes.

The transport is substituted explicitly. The last two checks use the real executor
entry and real pytest inside the director's disposable CPU test capsule; these do
not independently validate Docker/launcher wiring or the repository's full suite.
UNRUN by the source author.
"""
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from aitelier.gate_evidence import release_disposition, report_state
from aitelier.tools.run_tests import impl
from core import repository_executor_entry as entry


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    state = tmp_path / "state"
    baseline = Path(impl._baseline_dir(str(state), repo.resolve())) / impl.BASELINE_FILE
    return SimpleNamespace(repo=repo, out=tmp_path / "out", state=state,
                           baseline=baseline, calls=[], monkeypatch=monkeypatch)


def _run(env, **kwargs):
    result = impl.run_tests(project_root=str(env.repo), out_dir=str(env.out),
                            state_dir=str(env.state), run_id="pytest-wall-test", **kwargs)
    assert result["written"] == "test_report.json", result
    return result, json.loads((env.out / "test_report.json").read_text())


def _seed(env):
    env.baseline.parent.mkdir(parents=True)
    content = b'{"failures": ["test_sample.py::test_old_red"]}\n'
    env.baseline.write_bytes(content)
    return content


@pytest.mark.parametrize("params,expected", [
    ({}, 1800), ({"pytest_wall_seconds": 3600}, 3600),
    ({"pytest_wall_seconds": 1}, 1), ({"pytest_wall_seconds": 7200}, 7200),
], ids=["unchanged-default", "coding-impl", "minimum", "executor-ceiling"])
def test_public_tool_forwards_the_declared_wall_without_narrowing_discovery(env, params, expected):
    def unavailable(repo, args, timeout, **kwargs):
        env.calls.append((repo, args, timeout, kwargs))
        raise impl.repository_executor.IsolationUnavailable("test transport unavailable")

    env.monkeypatch.setattr(impl.repository_executor, "execute", unavailable)
    result, report = _run(env, **params)
    assert len(env.calls) == 1
    repo, args, timeout, kwargs = env.calls[0]
    assert repo == env.repo.resolve() and timeout == expected
    assert args[:4] == ["python3", "-m", "pytest", str(env.repo.resolve())]
    assert "--rootdir" in args and kwargs["pytest_timeout"] is True
    assert report["pytest_wall_seconds"] == timeout
    assert report["skipped_because"] == "cpu_executor_unavailable"
    assert result["passed"] is False and result["passed_relative"] is False


@pytest.mark.parametrize("wall", [True, False, "3600", "", None, 0, -1, 7201,
                                  3600.0, float("inf"), [], {}])
def test_invalid_wall_refuses_every_execution_leg_and_preserves_baseline(env, wall):
    previous = _seed(env)

    def forbidden(label):
        def call(*args, **kwargs):
            env.calls.append(label)
            raise AssertionError("invalid wall started " + label)
        return call

    env.monkeypatch.setattr(impl.repository_executor, "execute", forbidden("pytest"))
    env.monkeypatch.setattr(impl, "_run_node_checks", forbidden("node"))
    env.monkeypatch.setattr(impl, "_acquire_repo_gate", forbidden("repo gate"))
    from core import coding_impl_outcome as coding
    env.monkeypatch.setattr(coding, "current_binding", forbidden("prechange"))
    from aitelier import gate_evidence
    env.monkeypatch.setattr(gate_evidence, "first_upstream_blocker", forbidden("fail-fast"))

    result, report = _run(env, pytest_wall_seconds=wall, prechange_capture=True,
                          fail_fast_gates="[]")
    assert env.calls == []
    assert report["skipped_because"] == "invalid_pytest_wall_seconds"
    assert report["measurement_not_taken"] is True
    assert "integer from 1 to 7200" in report["summary"]
    assert result["passed"] is False and result["passed_relative"] is False
    assert report["baseline_state"] == "unmeasured" and result["new_failures"] == []
    assert release_disposition(report) == "unresolved"
    assert env.baseline.read_bytes() == previous


@pytest.mark.parametrize("wall", [1, 3600])
@pytest.mark.parametrize("existing", [False, True], ids=["no-baseline", "known-red-baseline"])
def test_actual_timeout_branch_uses_requested_wall_and_cannot_seed_or_relative_pass(env, wall, existing):
    previous = _seed(env) if existing else None

    def timeout(repo, args, limit, **kwargs):
        env.calls.append(limit)
        raise subprocess.TimeoutExpired(args, limit, output=b"partial pytest progress\n",
                                        stderr=b"partial diagnostic\n")

    env.monkeypatch.setattr(impl.repository_executor, "execute", timeout)
    result, report = _run(env, pytest_wall_seconds=wall)
    assert env.calls == [wall]
    assert report["pytest_wall_seconds"] == wall
    assert f"within {wall}s" in report["summary"]
    assert any(f"timed out after {wall}s" in f for f in report["failures"])
    assert report["stdout"] == "partial pytest progress\n"
    assert report["stderr"] == "partial diagnostic\n"
    assert report["timed_out"] is True and report["returncode"] is None
    assert report["skipped_because"] == "pytest_timeout"
    assert report_state(report) == "skipped" and result["release_evidence"] == "unresolved"
    assert result["passed"] is False and result["passed_relative"] is False
    assert result["baseline_state"] == "unmeasured" and result["new_failures"] == []
    if existing:
        assert env.baseline.read_bytes() == previous
    else:
        assert not env.baseline.exists()


def _real_entry_transport(env):
    """Only the Docker I/O boundary is substituted, within the outer test capsule."""
    observations = []

    def execute(repo, args, timeout, **kwargs):
        env.calls.append(timeout)
        measured = entry.run({"repo": str(repo), "args": args, "timeout": timeout,
                              "pytest_timeout": kwargs["pytest_timeout"],
                              "import_module": kwargs["import_module"]})
        observations.append(measured)
        if measured.get("timed_out"):
            raise subprocess.TimeoutExpired(args, timeout, measured["stdout"], measured["stderr"])
        return SimpleNamespace(**measured)

    env.monkeypatch.setattr(impl.repository_executor, "execute", execute)
    return observations


def test_real_tiny_pytest_completes_under_requested_wall(env):
    observations = _real_entry_transport(env)
    result, report = _run(env, pytest_wall_seconds=20)
    assert env.calls == [20] and len(observations) == 1
    assert observations[0]["timed_out"] is False
    assert result["passed"] is True and report["returncode"] == 0
    assert "1 passed" in report["summary"] and report["pytest_wall_seconds"] == 20
    assert "timed_out" not in report


def test_real_pytest_timeout_keeps_partial_output_without_a_baseline(env):
    (env.repo / "pytest.ini").write_text("[pytest]\naddopts = -s\n")
    (env.repo / "test_sample.py").write_text(
        "import time\ndef test_hang():\n"
        "    print('WALL_PARTIAL_OUTPUT', flush=True)\n"
        "    time.sleep(30)\n"
        "    assert False, 'should have been killed'\n")
    observations = _real_entry_transport(env)
    result, report = _run(env, pytest_wall_seconds=5)
    assert env.calls == [5] and observations[0]["timed_out"] is True
    assert "WALL_PARTIAL_OUTPUT" in observations[0]["stdout"]
    assert "WALL_PARTIAL_OUTPUT" in report["stdout"]
    assert report["timed_out"] is True and report["pytest_wall_seconds"] == 5
    assert "within 5s" in report["summary"]
    assert result["passed"] is False and result["passed_relative"] is False
    assert result["release_evidence"] == "unresolved"
    assert result["baseline_state"] == "unmeasured" and not env.baseline.exists()


def test_coding_impl_baseline_and_test_declare_the_same_inner_wall():
    root = Path(__file__).resolve().parents[2]
    graph = yaml.safe_load((root / "configs" / "coding_impl.yaml").read_text())
    steps = {step["id"]: step for step in graph["steps"]}
    for name in ("baseline", "test"):
        step = steps[name]
        assert step["tool_name"] == "run_tests" and step["capability"] == "stateful"
        assert step["tool_params"]["pytest_wall_seconds"] == 3600
        assert step["timeout_seconds"] == 4200
        assert step["tool_params"]["pytest_wall_seconds"] < step["timeout_seconds"]
    assert steps["baseline"]["tool_params"]["prechange_capture"] is True
    assert steps["test"]["tool_params"]["prechange_from"] == "baseline"
