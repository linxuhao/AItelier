"""Enumerate every tool in the live registry against every host-injected keyword.

The host injects keywords into a tool call's ``params`` before SkillFlow builds
the call.  SkillFlow then computes ``dropped = [k for k in params
if k not in sig.parameters]`` and, if anything is dropped, returns
``unrecognised argument(s): ...  No tool action was performed.`` — the call
never runs.  So an injection is only safe when the keyword survives BOTH
surfaces that can refuse it:

  * the tool's declared schema (``tool.yaml``'s ``parameters``) — the public
    contract an agent and a reviewer read, and
  * the callable's NAMED signature parameters — what the engine filters on,
    and the only surface that decides whether the call happens at all.
    ``**kwargs`` is not a named parameter, so it is not acceptance.

Run inside the container::

    docker exec aitelier python3 /app/scripts/audit_injected_kwargs.py
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def injected_keywords() -> list[str]:
    """Every keyword the host binds into a tool call's caller params."""
    from core.skillflow_host import AItelierSkillFlow
    # Declared on the host once the fix landed; before it, the single keyword
    # the host bound was hard-coded in _execute_tool_impl.
    return sorted(getattr(AItelierSkillFlow, "HOST_INJECTED_KEYWORDS",
                          ("project_id",)))


def _is_native(loader, name: str) -> bool:
    """True when the tool ships in the SkillFlow wheel, whose tool.yaml we
    cannot edit and which keeps host plumbing off its agent-facing schema."""
    try:
        return bool(loader.is_native(name))
    except Exception:
        return False


def schema_fields(loader, name: str) -> set[str] | None:
    try:
        schema = loader.load_schema(name)
    except Exception:
        return None
    if not isinstance(schema, dict):
        return None
    params = schema.get("parameters")
    if isinstance(params, dict) and isinstance(params.get("properties"), dict):
        params = params["properties"]          # JSON-Schema shape
    if not isinstance(params, dict):
        return set()
    return {k for k in params if isinstance(k, str)}


def signature_facts(loader, name: str):
    """(named parameter set, has **kwargs) or None when the fn cannot load."""
    try:
        sig = inspect.signature(loader.load_fn(name))
    except Exception:
        return None
    named = {
        p.name for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.KEYWORD_ONLY)
    }
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                 for p in sig.parameters.values())
    return named, var_kw


def main() -> int:
    from api.dependencies import get_tool_loader
    from core.skillflow_host import AItelierSkillFlow

    loader = get_tool_loader()
    host = object.__new__(AItelierSkillFlow)
    host._tool_loader = loader

    names = sorted(set(loader.list_tools()) | set(getattr(loader, "_cache", {})))
    keywords = injected_keywords()

    report: dict[str, dict] = {}
    for keyword in keywords:
        would_inject_and_be_refused = []
        would_inject_and_run = []
        not_injected = []
        undeclared = []
        unloadable = []
        for name in names:
            guard = host._tool_accepts_keyword(name, keyword)
            facts = signature_facts(loader, name)
            if facts is None:
                unloadable.append(name)
                continue
            named, var_kw = facts
            fields = schema_fields(loader, name)
            # The engine drops any caller param that is not a NAMED signature
            # parameter, and a dropped param cancels the whole call.  Verified
            # through the real call path, 2026-09-21: a schema that DECLARES the
            # keyword is still refused when the signature only has **kwargs, and
            # a signature that names it runs even when the schema omits it.  So
            # the named set alone decides whether the tool runs.
            engine_would_refuse = keyword not in named
            schema_declares = bool(fields) and keyword in fields
            row = {
                "guard_says_accepts": guard,
                "schema_declares": schema_declares,
                "signature_named": keyword in named,
                "has_var_kwargs": var_kw,
                "native": _is_native(loader, name),
            }
            if not guard:
                not_injected.append(name)
            elif engine_would_refuse:
                would_inject_and_be_refused.append((name, row))
            else:
                would_inject_and_run.append((name, row))
                # Executes, but no reader of the tool.yaml could know the field
                # is consumed.  The wheel's own tools keep host plumbing off the
                # agent-facing schema by their convention and are not counted.
                if not schema_declares and not row["native"]:
                    undeclared.append((name, row))
        report[keyword] = {
            "total_tools": len(names),
            "unloadable": unloadable,
            "injected_and_refused": would_inject_and_be_refused,
            "injected_and_ran": would_inject_and_run,
            "undeclared": undeclared,
            "not_injected_count": len(not_injected),
        }

    for keyword, data in report.items():
        refused = data["injected_and_refused"]
        ran = data["injected_and_ran"]
        print(f"=== keyword: {keyword} ===")
        print(f"tools in live registry: {data['total_tools']}  "
              f"(unloadable: {len(data['unloadable'])})")
        print(f"INJECTED + REFUSED (tool never runs): {len(refused)}")
        for name, row in refused:
            why = []
            if not row["schema_declares"]:
                why.append("schema omits it")
            if not row["signature_named"]:
                why.append("no named signature param")
            if row["has_var_kwargs"]:
                why.append("**kwargs made the old guard say yes")
            print(f"  - {name}: {'; '.join(why)}")
        print(f"INJECTED + EXECUTES: {len(ran)}")
        for name, row in ran:
            tag = " [skillflow-owned]" if row["native"] else ""
            print(f"  + {name}{tag}")
        undeclared = data["undeclared"]
        print(f"INJECTED + EXECUTES but the tool.yaml never says so "
              f"(AItelier-owned only): {len(undeclared)}")
        for name, _row in undeclared:
            print(f"  ? {name}")
        print(f"NOT INJECTED: {data['not_injected_count']}")
        print()

    print("SUMMARY_JSON " + json.dumps(
        {k: {"refused": len(v["injected_and_refused"]),
             "tolerate": len(v["injected_and_ran"]),
             "undeclared": len(v["undeclared"]),
             "total": v["total_tools"]} for k, v in report.items()},
        sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
