"""Every tool in the live registry, against every keyword the host injects.

Spot-checking cannot answer this one. A host keyword bound to a tool that will
not receive it does not raise: SkillFlow returns ``unrecognised argument(s) ...
No tool action was performed`` and the tool never runs, so downstream reads an
absence of evidence rather than a failure. Run 0cf3c10b lost its whole
commit-history chain that way. The only honest check is an enumeration, and it
is the same enumeration ``scripts/audit_injected_kwargs.py`` prints.
"""

from __future__ import annotations

import inspect

import pytest

from core.skillflow_host import AItelierSkillFlow


@pytest.fixture(scope="module")
def loader():
    from api.dependencies import get_tool_loader
    return get_tool_loader()


@pytest.fixture(scope="module")
def host(loader):
    instance = object.__new__(AItelierSkillFlow)
    instance._tool_loader = loader
    return instance


def _tool_names(loader) -> list[str]:
    return sorted(set(loader.list_tools()) | set(getattr(loader, "_cache", {})))


def _named_parameters(loader, name: str) -> set[str] | None:
    """Named parameters of the callable — the set SkillFlow filters against.

    ``**kwargs`` is deliberately excluded: it is not a member of
    ``sig.parameters`` under the engine's membership test, so it is a refusal.
    """
    try:
        signature = inspect.signature(loader.load_fn(name))
    except Exception:  # noqa: BLE001
        return None
    return {
        parameter.name for parameter in signature.parameters.values()
        if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              inspect.Parameter.KEYWORD_ONLY)
    }


def test_the_guard_never_contradicts_the_signature_it_reads(loader, host):
    """A REVERT DETECTOR, and deliberately not the measurement.

    This asks the guard and then checks the guard's own first clause
    (``keyword in named``), so it cannot come out any other way while that
    clause is there: it goes red if someone restores VAR_KEYWORD-as-acceptance,
    and says nothing at all about whether the engine then runs the tool.

    The evidence that no tool is refused under the new decision is
    ``scripts/sweep_injected_kwargs_execution.py``, which CALLS every tool in
    the live registry through the engine and reads what comes back, with a
    forced-injection control arm so a zero is falsifiable.
    """
    refused: list[str] = []
    for keyword in AItelierSkillFlow.HOST_INJECTED_KEYWORDS:
        for name in _tool_names(loader):
            named = _named_parameters(loader, name)
            if named is None or not host._tool_accepts_keyword(name, keyword):
                continue
            if keyword not in named:
                refused.append(f"{name}: host binds {keyword!r}, signature "
                               f"does not name it — the call would not run")
    assert refused == [], "\n".join(refused)


def test_a_host_owned_tool_declares_the_host_fields_it_consumes(loader, host):
    """Signature and schema must agree for every tool AItelier owns.

    Either the schema declares the field or the signature stops taking it —
    a tool that quietly eats an undeclared host field is how five tools came to
    look refused while they were being served. SkillFlow's own tools are
    excluded: their ``tool.yaml`` is in the wheel and keeps host plumbing off
    the agent-facing schema by its own convention.
    """
    disagreements: list[str] = []
    for keyword in AItelierSkillFlow.HOST_INJECTED_KEYWORDS:
        for name in _tool_names(loader):
            if loader.is_native(name):
                continue
            named = _named_parameters(loader, name)
            if named is None:
                continue
            in_signature = keyword in named
            in_schema = keyword in host._declared_schema_fields(name)
            if in_signature != in_schema:
                disagreements.append(
                    f"{name}: signature={in_signature} schema={in_schema} "
                    f"for {keyword!r} — declare it in tool.yaml or drop it "
                    f"from the signature")
    assert disagreements == [], "\n".join(disagreements)


def test_the_two_confirmed_victims_are_left_alone(loader, host):
    """``semantic_search`` and ``git_history`` were the traced casualties.

    Neither names ``project_id``; both end in ``**kwargs``, which is what the
    old guard mistook for acceptance.
    """
    for name in ("semantic_search", "git_history"):
        named = _named_parameters(loader, name)
        assert named is not None and "project_id" not in named, name
        assert host._tool_accepts_keyword(name, "project_id") is False, name
