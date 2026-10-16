"""SkillFlow's native ``pytest`` is served by the owned CPU executor on every route.

The executor transport is substituted in this suite: untrusted test jobs get no
Docker or launcher socket. The ``entry`` cases run REAL pytest through the real
container entry inside this disposable test job, which is entry/adapter
evidence, not a host-launcher measurement.
"""
from __future__ import annotations

import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest
import skillflow as skillflow_package
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader

from core import repository_executor
from core import tool_routes
from core.tool_routes import AItelierToolLoader, native_pytest

NATIVE = Path(skillflow_package.__file__).parent / "tools"
ROOT = Path(__file__).resolve().parents[2]

PASSING_JUNIT = ('<testsuites><testsuite name="pytest" tests="2" failures="0" '
                 'errors="0" skipped="1"/></testsuites>')
SKIPPED_JUNIT = ('<testsuites><testsuite name="pytest" tests="2" failures="0" '
                 'errors="0" skipped="2"/></testsuites>')


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "test_probe.py").write_text("def test_probe():\n    assert 1 + 1 == 2\n")
    return repo


def _loader() -> AItelierToolLoader:
    loader = AItelierToolLoader(NATIVE)
    loader.add_tools_dir(ROOT / "aitelier" / "tools")
    return loader


class Recorder:
    """A substitute executor that records the request and answers like one."""

    def __init__(self, returncode=0, junit=PASSING_JUNIT, stdout="1 passed\n"):
        self.calls, self.returncode, self.junit, self.stdout = [], returncode, junit, stdout

    def __call__(self, root, args, timeout, **kwargs):
        self.calls.append((Path(root), list(args), timeout, kwargs))
        if self.junit is not None:
            junit = next(a for a in args if a.startswith("--junitxml="))
            Path(junit.split("=", 1)[1]).write_text(self.junit)
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, "")


@pytest.fixture
def forbid_local(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("native pytest route started a local process")
    monkeypatch.setattr(tool_routes.subprocess, "Popen", refuse)
    monkeypatch.setattr(tool_routes.subprocess, "run", refuse)


@pytest.fixture
def recorder(monkeypatch, forbid_local):
    rec = Recorder()
    monkeypatch.setattr(repository_executor, "execute", rec)
    return rec


# ── the route itself ────────────────────────────────────────────────────────

def test_native_name_resolves_to_the_host_route_and_others_are_untouched():
    loader = _loader()
    assert loader.is_native("pytest")
    assert loader.load_fn("pytest") is native_pytest
    plain = ToolLoader(NATIVE)
    assert loader.load_fn("lint") is not native_pytest
    lint = loader.load_fn("lint")
    assert lint.__wrapped__.__code__.co_filename == plain.load_fn("lint").__code__.co_filename
    assert __import__("inspect").signature(lint) == __import__("inspect").signature(plain.load_fn("lint"))
    assert loader.load_fn("run_tests").__module__.endswith("run_tests")


def test_route_survives_every_cache_reset_and_dynamic_reregistration():
    loader = _loader()
    loader.load_fn("pytest")
    for _ in range(2):
        loader.add_tools_dir(ROOT / "aitelier" / "tools")   # register_tool / edit_tool reload
        assert loader.load_fn("pytest") is native_pytest
    loader.register_dynamic_tool("pytest", {"name": "pytest"}, lambda **k: {"verdict": "passed"})
    assert loader.load_fn("pytest") is native_pytest


def test_negative_control_a_dynamic_override_is_erased_by_reload():
    """Why the route is a subclass: the dynamic slot does not survive a reload."""
    loader = ToolLoader(NATIVE)
    loader.register_dynamic_tool("pytest", {"name": "pytest"}, native_pytest)
    assert loader.load_fn("pytest") is native_pytest
    loader.add_tools_dir(ROOT / "aitelier" / "tools")
    assert loader.load_fn("pytest") is not native_pytest
    assert loader.load_fn("pytest").__code__.co_filename.endswith("skillflow/tools/pytest/impl.py")


def test_a_generated_tool_named_pytest_cannot_displace_the_route(tmp_path):
    gen = tmp_path / "tools" / "pytest"
    gen.mkdir(parents=True)
    (gen / "tool.yaml").write_text("name: pytest\n")
    (gen / "impl.py").write_text("def pytest(**k):\n    return {'verdict': 'passed'}\n")
    loader = _loader()
    loader.add_tools_dir(tmp_path / "tools")
    assert loader.load_fn("pytest") is native_pytest


def test_the_host_composes_the_routed_loader(monkeypatch):
    import api.dependencies as deps
    monkeypatch.setattr(deps, "_tool_loader_instance", None)
    loader = deps.get_tool_loader()
    try:
        assert isinstance(loader, AItelierToolLoader)
        assert loader.load_fn("pytest") is native_pytest
    finally:
        monkeypatch.setattr(deps, "_tool_loader_instance", None)


# ── adapter semantics (shape kept, false greens removed) ───────────────────

def test_measured_pass_goes_through_the_executor_with_a_run_owned_ticket(tmp_path, recorder):
    from core import datadir
    repo = _repo(tmp_path)
    result = native_pytest("test_probe.py", workspace_root=str(repo), run_id="run-a")
    assert result["verdict"] == "passed" and result["passed"] is True and result["feedback"] == ""
    assert result["measured"] is True and result["counts"]["passed"] == 1
    [(root, args, timeout, kwargs)] = recorder.calls
    assert root == repo.resolve() and timeout == 60 and kwargs["run_id"] == "run-a"
    [ticket] = kwargs["writable_dirs"]
    owned = datadir.aitelier_home() / "gate-reports" / hashlib.sha256(b"run-a").hexdigest()
    assert Path(ticket).parent == owned
    assert args[:4] == ["python3", "-m", "pytest", "test_probe.py"]
    assert f"--junitxml={Path(ticket) / 'junit.xml'}" in args


@pytest.mark.parametrize("rc,junit,reason", [
    (5, None, "no_tests"),
    (0, SKIPPED_JUNIT, "all_skipped"),
    (0, None, "no_record"),
    (0, "<not-xml", "no_record"),
    (2, None, "session_incomplete"),
    (4, None, "session_incomplete"),
])
def test_unmeasured_outcomes_never_pass(tmp_path, monkeypatch, forbid_local, rc, junit, reason):
    monkeypatch.setattr(repository_executor, "execute", Recorder(rc, junit))
    result = native_pytest("test_probe.py", workspace_root=str(_repo(tmp_path)), run_id="r")
    assert result["verdict"] == "failed" and result["passed"] is False
    assert result["measured"] is False and result["unmeasured_reason"] == reason
    assert result["error"]


def test_measured_failure_is_failed_with_output(tmp_path, monkeypatch, forbid_local):
    junit = ('<testsuite tests="1" failures="1" errors="0" skipped="0"/>')
    monkeypatch.setattr(repository_executor, "execute", Recorder(1, junit, "FAILED test_probe.py::t"))
    result = native_pytest("test_probe.py", workspace_root=str(_repo(tmp_path)), run_id="r")
    assert result["verdict"] == "failed" and result["measured"] is True
    assert "FAILED" in result["feedback"]


def test_the_run_owned_code_root_wins_over_the_workspace(tmp_path, recorder):
    repo = _repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    native_pytest("test_probe.py", workspace_root=str(workspace), project_root=str(repo), run_id="r")
    assert recorder.calls[0][0] == repo.resolve()


@pytest.mark.parametrize("kwargs,reason", [
    ({"run_id": ""}, "no_run_identity"),
    ({"run_id": "   "}, "no_run_identity"),
    ({"run_id": "r", "workspace_root": "relative"}, "no_root"),
    ({"run_id": "r", "file": "../outside.py"}, "bad_target"),
    ({"run_id": "r", "file": "--co"}, "bad_target"),
    ({"run_id": "r", "file": "missing.py"}, "bad_target"),
    ({"run_id": "r", "file": "notes.md"}, "not_python"),
])
def test_refusals_happen_before_any_transport(tmp_path, monkeypatch, forbid_local, kwargs, reason):
    repo = _repo(tmp_path)
    (repo / "notes.md").write_text("# not a test\n")
    (tmp_path / "outside.py").write_text("def test_x(): pass\n")
    monkeypatch.setattr(repository_executor, "execute", lambda *a, **k: pytest.fail("transport used"))
    args = {"file": "test_probe.py", "workspace_root": str(repo), **kwargs}
    result = native_pytest(args.pop("file"), **args)
    assert result["verdict"] == "failed" and result["passed"] is False
    assert result["unmeasured_reason"] == reason


@pytest.mark.parametrize("failure", [repository_executor.IsolationUnavailable("launcher refused"),
                                     OSError("socket gone")])
def test_unavailable_executor_is_unmeasured_without_fallback(tmp_path, monkeypatch, forbid_local, failure):
    def refuse(*a, **k):
        raise failure
    monkeypatch.setattr(repository_executor, "execute", refuse)
    result = native_pytest("test_probe.py", workspace_root=str(_repo(tmp_path)), run_id="r")
    assert result["verdict"] == "failed" and result["infrastructure_unavailable"] is True
    assert result["returncode"] is None and result["measured"] is False


def test_timeout_has_no_invented_exit_code(tmp_path, monkeypatch, forbid_local):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(["python3"], 60, b"partial", b"")
    monkeypatch.setattr(repository_executor, "execute", slow)
    result = native_pytest("test_probe.py", workspace_root=str(_repo(tmp_path)), run_id="r")
    assert result["timed_out"] is True and result["returncode"] is None
    assert result["verdict"] == "failed" and "partial" in result["feedback"]


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_cancellation_propagates(tmp_path, monkeypatch, forbid_local, failure):
    calls = []
    def cancel(*a, **k):
        calls.append(1)
        raise failure
    monkeypatch.setattr(repository_executor, "execute", cancel)
    with pytest.raises(type(failure)):
        native_pytest("test_probe.py", workspace_root=str(_repo(tmp_path)), run_id="r")
    assert calls == [1]


# ── real pytest through the real container entry (inside this disposable job) ─

@pytest.fixture
def entry(monkeypatch):
    from core import repository_executor_entry

    def execute(root, args, timeout, *, writable_dirs=(), run_id=""):
        assert run_id and len(writable_dirs) == 1
        result = repository_executor_entry.run({"repo": str(root), "args": args, "timeout": timeout})
        if result["timed_out"]:
            raise subprocess.TimeoutExpired(args, timeout, result["stdout"], result["stderr"])
        return subprocess.CompletedProcess(args, result["returncode"], result["stdout"], result["stderr"])
    monkeypatch.setattr(repository_executor, "execute", execute)


@pytest.mark.parametrize("source,verdict,reason", [
    ("def test_ok():\n    assert sum([1, 2]) == 3\n", "passed", None),
    ("import pytest\ndef test_raises():\n    with pytest.raises(ZeroDivisionError):\n        1 / 0\n",
     "passed", None),
    ("import unittest\nclass T(unittest.TestCase):\n    def test_eq(self):\n        self.assertEqual(2, 2)\n",
     "passed", None),
    ("def test_bad():\n    assert 1 == 2\n", "failed", None),
    ("VALUE = 1\n", "failed", "no_tests"),
    ("import pytest\n@pytest.mark.skip(reason='x')\ndef test_a():\n    pass\n"
     "@pytest.mark.skip(reason='y')\ndef test_b():\n    pass\n", "failed", "all_skipped"),
])
def test_real_pytest_outcomes(tmp_path, entry, source, verdict, reason):
    repo = _repo(tmp_path)
    (repo / "test_case.py").write_text(source)
    result = native_pytest("test_case.py", workspace_root=str(repo), run_id="real-run")
    assert result["verdict"] == verdict, result
    assert result.get("unmeasured_reason") == reason
    if verdict == "passed":
        assert result["counts"]["passed"] >= 1 and result["measured"] is True


# ── all five SkillFlow dispatch routes reach the route ─────────────────────

def _engine(tmp_path, repo):
    from core.skillflow_host import AItelierSkillFlow
    sf = AItelierSkillFlow(
        str(tmp_path / "sf.db"), tool_loader=_loader(),
        workspace_base=str(tmp_path / "ws"), projects_base=str(tmp_path / "projects"),
        code_path_resolver=lambda project_id, run_id=None: repo)
    sf.agent_registry.register("prober", model="flash", tools=["pytest"])
    return sf


def test_inline_tool_step_route(tmp_path, recorder):
    repo = _repo(tmp_path)
    sf = _engine(tmp_path, repo)
    sf.register_graph(PipelineGraph(
        name="native_inline", begin="check",
        steps=[StepNode(id="check", step_type="tool", tool_name="pytest",
                        tool_params={"file": "test_probe.py"},
                        transitions=[Transition(to=None)])]))
    run_id = sf.create_run("native_inline", project_id="p1")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    [(root, _args, _t, kwargs)] = recorder.calls
    assert root == repo.resolve() and kwargs["run_id"] == run_id


def _claimed_agent(tmp_path, repo, lifecycle=None):
    sf = _engine(tmp_path, repo)
    sf.register_graph(PipelineGraph(
        name="native_agent", begin="work",
        steps=[StepNode(id="work", step_type="agent", agent_config="prober",
                        lifecycle=lifecycle or {}, transitions=[Transition(to=None)])]))
    run_id = sf.create_run("native_agent", project_id="p1")
    sf.start_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert claimed is not None
    return sf, run_id, claimed.token


def test_agent_route_replaces_spoofed_identity_and_root(tmp_path, recorder):
    repo = _repo(tmp_path)
    foreign = _repo(tmp_path / "foreign")
    sf, run_id, token = _claimed_agent(tmp_path, repo)
    sf.execute_tool("pytest", {"file": "test_probe.py", "run_id": "foreign-run",
                               "workspace_root": str(foreign), "project_root": str(foreign)},
                    run_id=run_id, step_id="work", step_instance_id=token.step_instance_id,
                    claim_epoch=token.claim_epoch, project_root=str(repo))
    [(root, _args, _t, kwargs)] = recorder.calls
    assert root == repo.resolve() and kwargs["run_id"] == run_id


def test_lifecycle_hook_route(tmp_path, recorder):
    repo = _repo(tmp_path)
    hook = {"tool": "pytest", "params": {"file": "test_probe.py"}}
    sf, run_id, token = _claimed_agent(tmp_path, repo, {"on_deliver": hook})
    node = sf._get_resolver_for_run(run_id).graph.steps[0]
    result = sf._execute_lifecycle_hook(token, node, "on_deliver", hook)
    assert result["passed"] is True
    [(root, _args, _t, kwargs)] = recorder.calls
    assert root == repo.resolve() and kwargs["run_id"] == run_id


def test_lifecycle_hook_counts_a_failed_verdict_as_failed(tmp_path, monkeypatch, forbid_local):
    """The native {"verdict": "failed"} had no `passed`/`error`, which the hook path read as a pass."""
    monkeypatch.setattr(repository_executor, "execute", Recorder(5, None))
    repo = _repo(tmp_path)
    hook = {"tool": "pytest", "params": {"file": "test_probe.py"}}
    sf, run_id, token = _claimed_agent(tmp_path, repo, {"on_deliver": hook})
    node = sf._get_resolver_for_run(run_id).graph.steps[0]
    assert sf._execute_lifecycle_hook(token, node, "on_deliver", hook)["passed"] is False


def test_validation_route_fails_closed_without_run_identity(tmp_path, monkeypatch, forbid_local):
    from skillflow.step_validation import StepValidator
    monkeypatch.setattr(repository_executor, "execute", lambda *a, **k: pytest.fail("unbound execution"))
    repo = _repo(tmp_path)
    result = StepValidator(_loader(), repo).validate([{"tool": "pytest", "files": ["test_probe.py"]}])
    assert result["passed"] is False
    assert "no run identity" in str(result["errors"])


def test_context_source_route_reaches_the_route(tmp_path, monkeypatch):
    from skillflow.context import ContextResolver
    seen = []
    def route(**kwargs):
        seen.append(kwargs)
        return {"content": "routed"}
    monkeypatch.setitem(tool_routes.NATIVE_ROUTES, "pytest", route)
    resolver = ContextResolver(tmp_path, tool_loader=_loader(), code_root=tmp_path)
    assert resolver._resolve_tool({"tool": "pytest"}) == ("[pytest]", "routed")
    assert seen and seen[0]["project_root"] == str(tmp_path)
