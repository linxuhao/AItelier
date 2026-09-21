"""Compatibility of host-owned tool kwargs with SkillFlow 1.5.77.

The host binds ``project_id`` into a tool call's caller params. SkillFlow drops
any caller param the callable does not NAME and returns ``unrecognised
argument(s) ... No tool action was performed`` — the tool never runs, and the
agent sees a hole instead of an error. These tests pin the two surfaces that
have to agree before the host binds anything, using the four shapes the live
registry actually contains.
"""

from __future__ import annotations

from pathlib import Path

from skillflow.tool_loader import ToolLoader

from core.skillflow_host import AItelierSkillFlow


def _write_tool(root: Path, name: str, implementation: str,
                *, declares_project_id: bool = False) -> None:
    tool = root / name
    tool.mkdir()
    parameters = "  q:\n    type: string\n    required: false\n"
    if declares_project_id:
        parameters += "  project_id:\n    type: string\n    required: false\n"
    (tool / "tool.yaml").write_text(
        f"name: {name}\ndescription: test\nparameters:\n{parameters}",
        encoding="utf-8",
    )
    (tool / "impl.py").write_text(implementation, encoding="utf-8")


def _host_owned_loader(tmp_path: Path) -> tuple[ToolLoader, Path]:
    """A loader shaped like production: the wheel's dir first, AItelier's next.

    Nativeness is "lives in the FIRST tools dir", so a single-directory loader
    would make every fixture tool look wheel-owned and skip the schema half of
    the decision entirely.
    """
    wheel = tmp_path / "wheel"
    wheel.mkdir()
    host_tools = tmp_path / "aitelier_tools"
    host_tools.mkdir()
    loader = ToolLoader(wheel)
    loader.add_tools_dir(host_tools)
    return loader, host_tools


def _host(loader: ToolLoader) -> AItelierSkillFlow:
    host = object.__new__(AItelierSkillFlow)
    host._tool_loader = loader
    host._workspace = None
    host._step_scoped_names = set()
    host._step_tool_fns = {}
    host._get_project_id = lambda _run_id: "owned-project"
    return host


def test_host_identity_is_only_bound_when_tool_accepts_it(tmp_path: Path) -> None:
    loader, tools = _host_owned_loader(tmp_path)
    _write_tool(
        tools,
        "native_read",
        "def native_read():\n    return {'ok': True}\n",
    )
    _write_tool(
        tools,
        "forge_tool",
        "def forge_tool(project_id=''):\n    return {'project_id': project_id}\n",
        declares_project_id=True,
    )
    host = _host(loader)

    native_result = host._execute_tool_impl("native_read", {}, run_id="run-1")
    forge_result = host._execute_tool_impl("forge_tool", {}, run_id="run-1")

    assert native_result == {"ok": True}
    assert forge_result == {"project_id": "owned-project"}


def test_var_keyword_is_not_acceptance_and_the_tool_still_runs(
        tmp_path: Path) -> None:
    """``**kwargs`` is the shape that silenced ``semantic_search``.

    SkillFlow's caller-param filter tests membership in ``sig.parameters``, and
    a VAR_KEYWORD is not a member under that test. A guard that read
    ``**kwargs`` as "this tool accepts anything" therefore bound a keyword the
    engine would refuse, and the refusal cancelled the whole call. The tool must
    run — without the keyword — instead.
    """
    loader, tools = _host_owned_loader(tmp_path)
    _write_tool(
        tools,
        "semantic_like",
        "def semantic_like(q='', **kwargs):\n"
        "    return {'ran': True, 'saw': sorted(kwargs)}\n",
    )
    # Declaring the keyword in the schema does NOT rescue a ``**kwargs``
    # signature: the engine refuses on the signature, so the second tool here
    # proves the schema alone is not the deciding surface.
    _write_tool(
        tools,
        "declared_but_kwargs",
        "def declared_but_kwargs(q='', **kwargs):\n"
        "    return {'ran': True, 'saw': sorted(kwargs)}\n",
        declares_project_id=True,
    )
    host = _host(loader)

    assert host._tool_accepts_keyword("semantic_like", "project_id") is False
    assert host._tool_accepts_keyword("declared_but_kwargs", "project_id") is False

    for name in ("semantic_like", "declared_but_kwargs"):
        result = host._execute_tool_impl(name, {"q": "hi"}, run_id="run-1")
        assert result == {"ran": True, "saw": []}, result
        assert "error" not in result


def test_a_named_parameter_the_schema_omits_is_not_bound(tmp_path: Path) -> None:
    """A host-owned tool has to DECLARE what the host feeds it.

    The engine would accept the keyword here — it is a named parameter — so this
    is not about avoiding a refusal. It is about the contract: five tools
    consumed ``project_id`` while no reader of their ``tool.yaml`` could tell,
    which is why an audit read them as refused when they were being served.
    """
    loader, tools = _host_owned_loader(tmp_path)
    _write_tool(
        tools,
        "undeclared_consumer",
        "def undeclared_consumer(q='', project_id=''):\n"
        "    return {'project_id': project_id}\n",
    )
    host = _host(loader)

    assert host._tool_accepts_keyword("undeclared_consumer", "project_id") is False
    result = host._execute_tool_impl(
        "undeclared_consumer", {"q": "hi"}, run_id="run-1")
    assert result == {"project_id": ""}


def test_the_host_value_wins_over_a_caller_supplied_one(tmp_path: Path) -> None:
    """The field is public now, so a caller can name it — and must not win.

    ``project_id`` reaches commit owner identity and the code-path resolver.
    Whoever owns the run owns that identity.
    """
    loader, tools = _host_owned_loader(tmp_path)
    _write_tool(
        tools,
        "identity_tool",
        "def identity_tool(q='', project_id=''):\n"
        "    return {'project_id': project_id}\n",
        declares_project_id=True,
    )
    host = _host(loader)

    result = host._execute_tool_impl(
        "identity_tool", {"q": "hi", "project_id": "someone-elses"},
        run_id="run-1")
    assert result == {"project_id": "owned-project"}


def test_a_skillflow_owned_tool_keeps_its_wheel_side_convention(
        tmp_path: Path) -> None:
    """Native tools are held to the signature half only.

    ``repo_apply`` and ``draft_commit`` name ``project_id`` beside
    ``workspace_root``/``run_id`` and declare none of them: that ``tool.yaml``
    lives in the wheel, where host plumbing is deliberately kept off the
    agent-facing schema. The host cannot edit it, so requiring a declaration
    there would stop feeding two working native tools.
    """
    loader, _tools = _host_owned_loader(tmp_path)
    _write_tool(
        tmp_path / "wheel",
        "wheel_tool",
        "def wheel_tool(q='', *, project_id=''):\n"
        "    return {'project_id': project_id}\n",
    )
    host = _host(loader)

    assert loader.is_native("wheel_tool") is True
    assert host._execute_tool_impl(
        "wheel_tool", {"q": "hi"}, run_id="run-1") == {
            "project_id": "owned-project"}
