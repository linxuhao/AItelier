"""A capability may not grant a tool whose guard lives in a package we deploy.

`tests/unit/test_tool_root_guard_inventory.py` classifies every tool under
`aitelier/tools/` that takes a root. It cannot see the other half: skillflow's
NATIVE tools. Their guards ship in the `skillflow-py` wheel PyPI serves, not in
this repo, so nothing in this checkout can assert what the engine that actually
runs a step does with a missing root — the container installs whatever version
the pin resolved to, and it lags this checkout by however long a release takes.

That gap is not hypothetical. The container runs skillflow 1.5.46, whose `pytest`
is, in full::

    def pytest(file: str, *, workspace_root: str = "") -> dict:
        full = (Path(workspace_root) / file).resolve()

— no guard. `_execute_tool_impl` fills the argument with
``kwargs.setdefault("workspace_root", project_root or "")``, and
`core/dpe_pipeline.py:_exec_tool` sends ``project_root=""`` for a run that
declares no code repository. `Path("")` is `Path(".")`, so the root becomes the
process CWD: in the container, `/app` — the bind-mounted AItelier checkout.
`tool_creation` granted `pytest`, `pipeline_forge`'s `t_tool_impl` holds that
capability, and `pipeline_forge` is `repo_mode: none`.

Nothing is known to have executed out of `/app` this way: the tool still requires
``full.exists()``, so a hit needs the generated tool's own relative path to
collide with a real path in the checkout, and no such collision was constructed.
The defect is that the ROOT moved from the run's own directory to the server's
repository while `_exec_tool`'s comment states as an invariant that it cannot.

The fix is the grant, not a guard: a guard written here would sit in a file the
container does not install. So the rule below — no capability grants a native
root-resolving tool — and, because a rule is only worth its evidence, the
composed reproduction of what the deployed engine does with what this host sends.
"""
import ast
import inspect
from pathlib import Path

import pytest as _pytest


# ── What the deployed engine does ─────────────────────────────────────────

def _deployed_pytest_1_5_46(file: str, *, workspace_root: str = ""):
    """skillflow 1.5.46's `pytest`, first line — verbatim.

    Reproduced rather than imported: the copy in `~/stepflow` grew a guard in
    this same series of changes, and that copy is not what the container runs.
    """
    return (Path(workspace_root) / file).resolve()


def _deployed_kwargs(params: dict, project_root: str) -> dict:
    """skillflow 1.5.46 `_execute_tool_impl`, the two lines that fill the roots."""
    kwargs = dict(params)
    kwargs.setdefault("workspace_root", project_root or "")
    kwargs.setdefault("project_root", project_root or "")
    return kwargs


def _host_project_root_for_a_repoless_run() -> str:
    """What `_exec_tool` actually sends when the run declares no repository.

    Driven through the real method rather than restated, so this stops being
    true the moment the host starts sending something else.
    """
    from core.dpe_pipeline import PipelineEngine

    seen = {}

    class _RecordingSkillflow:
        def execute_tool(self, name, params, **kw):
            seen.update(name=name, params=params, **kw)
            return {}

    engine = PipelineEngine.__new__(PipelineEngine)
    engine._code_path = None          # get_code_path's answer for repo_type=none
    engine._current_step = "t_tool_impl"

    import api.dependencies as deps
    real = deps.get_skillflow
    deps.get_skillflow = lambda: _RecordingSkillflow()
    try:
        engine._exec_tool({"tool": "pytest", "params": {"file": "x/test_x.py"}})
    finally:
        deps.get_skillflow = real
    return seen["project_root"]


def test_the_host_sends_no_project_root_for_a_repoless_run():
    """The premise of everything below. Not an assertion about what is right —
    "" is the correct thing to send, it means "no opinion, ask your resolver";
    only the 1.5.46 reading of it is wrong."""
    assert _host_project_root_for_a_repoless_run() == ""


def test_an_unguarded_native_tool_resolves_the_process_cwd(tmp_path,
                                                           monkeypatch):
    """Composed reproduction: the host's argument, the deployed engine's fill,
    the deployed tool's body. This is why the rule below exists."""
    monkeypatch.chdir(tmp_path)

    kwargs = _deployed_kwargs({"file": "conftest.py"},
                              _host_project_root_for_a_repoless_run())
    resolved = _deployed_pytest_1_5_46(**{
        k: v for k, v in kwargs.items()
        if k in inspect.signature(_deployed_pytest_1_5_46).parameters})

    assert resolved == tmp_path / "conftest.py", (
        "1.5.46's pytest no longer resolves the CWD — if the deployed engine "
        "has moved on, re-read it and rewrite this file's premise")


# ── The rule ──────────────────────────────────────────────────────────────

# Names `_execute_tool_impl` claims BEFORE it reaches ToolLoader, so the tool
# directory of the same name is never loaded for them (skillflow core.py, the
# write/create/edit/delete dispatch and `finish_step`). `write` is on this list
# and that matters: `skillflow/tools/write/impl.py` declares `workspace_root`,
# but no grant of the name `write` ever reaches it.
ENGINE_INTERCEPTED = {"create", "edit", "write", "finish_step"}
ENGINE_INTERCEPTED_PREFIXES = ("write_", "create_", "edit_", "delete_")


def _native_tools_dir() -> Path:
    import skillflow
    return Path(skillflow.__file__).resolve().parent / "tools"


def _native_tools_declaring_a_root() -> set[str]:
    """Every native tool whose entry point takes a root — complete by
    construction, exactly like the AItelier inventory's scan."""
    found = set()
    for d in sorted(_native_tools_dir().iterdir()):
        impl = d / "impl.py"
        if not impl.is_file():
            continue
        try:
            tree = ast.parse(impl.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a broken tool is its own bug
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != d.name:
                continue
            args = node.args
            names = {a.arg for a in
                     args.posonlyargs + args.args + args.kwonlyargs}
            if names & {"project_root", "workspace_root"}:
                found.add(d.name)
    return found


def _reaches_the_tool_loader(name: str) -> bool:
    return (name not in ENGINE_INTERCEPTED
            and not name.startswith(ENGINE_INTERCEPTED_PREFIXES))


def test_the_scan_finds_the_native_tools_that_take_a_root():
    """Without this the rule below could pass by finding nothing at all."""
    found = _native_tools_declaring_a_root()
    assert {"pytest", "repo_apply", "repo_validate"} <= found, found


def test_no_capability_grants_a_native_tool_that_resolves_its_own_root():
    """The rule. A native tool's guard is in the wheel, not in this repo — so a
    grant that depends on one is a grant this deployment cannot verify.

    Guard the tool in skillflow, release it, raise the pin, rebuild — and only
    then add it back here. Until then the grant is the control."""
    from api.dependencies import get_skillflow

    native = {n for n in _native_tools_declaring_a_root()
              if _reaches_the_tool_loader(n)}
    offending = {}
    for cap_name, cap in (get_skillflow()._capabilities or {}).items():
        bad = sorted(set(cap.get("tools") or ()) & native)
        if bad:
            offending[cap_name] = bad

    assert not offending, (
        f"these capabilities grant native skillflow tools that resolve a root "
        f"from an argument the host cannot guarantee: {offending}. The guard "
        f"would live in the skillflow wheel, which this checkout does not "
        f"deploy — the container runs whatever version the pin resolved to.")


def test_tool_creation_still_grants_what_the_forge_needs():
    """The rule above is satisfiable by granting nothing; this is the control."""
    from api.dependencies import get_skillflow

    grants = set((get_skillflow()._capabilities.get("tool_creation") or {})
                 .get("tools") or ())
    assert {"write", "register_tool", "register_capability"} <= grants, grants


def test_the_tool_build_template_promises_no_test_runner():
    """A grant removed while the prompt still tells the agent to use it spends a
    turn per generation on a tool that is not there — the offered-then-denied
    shape skillflow's own allowlist comment was written for."""
    from api.dependencies import get_skillflow

    text = (Path(__file__).resolve().parents[2] / "templates"
            / "forge_tool_impl.md").read_text(encoding="utf-8")
    grants = set((get_skillflow()._capabilities.get("tool_creation") or {})
                 .get("tools") or ())

    assert "pytest" not in grants
    assert "run it on your" not in text.lower(), text
    assert "CANNOT run" in text, text


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_pytest.main([__file__]))
