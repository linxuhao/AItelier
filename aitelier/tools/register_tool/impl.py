"""register_tool — persist + live-register a generated tool.

Copies a built tool (tool.yaml + impl.py [+ tests]) into the durable, boot-scanned
generated-tools directory and injects that directory into the running ToolLoader so
`list_tools()`/`load_fn()` resolve it immediately — the mechanism that lets
pipeline_forge reference just-built tools as real primitives before the gate runs.

Two invariants keep a fan-out loop honest:
  * the source is resolved by the tool's OWN identity, never "first tool.yaml found"
    (every loop item shares one source_dir, so a positional match registers item 1's
    code under item 2's name);
  * nothing is published until it imports the way ToolLoader.load_fn will import it,
    so a broken build can never poison the registry for later runs.
"""
from __future__ import annotations

import ast
import importlib.util
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml


def generated_tools_dir() -> Path:
    """The durable, boot-scanned home for generated tools (mirrors the configs dir).

    Resolved through core.datadir so an AITELIER_HOME override (tests) is
    honored — writing to the production dir from a test run is the accident
    the datadir authority exists to prevent.
    """
    from core.datadir import tools_dir

    d = tools_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_yaml(tool_dir: Path) -> dict:
    try:
        data = yaml.safe_load((tool_dir / "tool.yaml").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _declared_name(tool_dir: Path) -> str | None:
    """The name a candidate tool.yaml declares for itself (None if absent)."""
    name = _read_yaml(tool_dir).get("name")
    return str(name).strip() if name else None


def _owner_of(tool_dir: Path) -> str | None:
    """Which pipeline run generated an already-installed tool (None if unknown)."""
    owner = _read_yaml(tool_dir).get("x-generated-by")
    return str(owner).strip() if owner else None


def _candidates(source_dir: Path) -> list[Path]:
    """Every directory under source_dir that holds a tool.yaml, source_dir included."""
    found: list[Path] = []
    if (source_dir / "tool.yaml").exists():
        found.append(source_dir)
    for cand in sorted(source_dir.rglob("tool.yaml")):
        if cand.parent not in found:
            found.append(cand.parent)
    return found


def _resolve_tool_src(source_dir: Path, tool_name: str) -> tuple[Path | None, str]:
    """Find the dir holding THIS tool's files. Returns (dir, diagnostic).

    Matching is by identity — the canonical `<source_dir>/<tool_name>/` layout first,
    then the name a candidate's tool.yaml declares, then its directory name. A lone
    unnamed candidate is accepted (the flat single-tool layout). Anything else is an
    error rather than a guess: silently registering a sibling's code under this name
    is the failure this function exists to prevent.
    """
    nested = source_dir / tool_name
    if (nested / "tool.yaml").exists():
        return nested, ""

    cands = _candidates(source_dir)
    if not cands:
        return None, f"no tool.yaml found under {source_dir}"

    by_name = [c for c in cands if _declared_name(c) == tool_name]
    if by_name:
        return by_name[0], ""

    by_dir = [c for c in cands if c.name == tool_name]
    if by_dir:
        return by_dir[0], ""

    if len(cands) == 1 and _declared_name(cands[0]) is None:
        return cands[0], ""

    declared = [_declared_name(c) or f"<dir:{c.name}>" for c in cands]
    return None, (f"{len(cands)} tool.yaml found under {source_dir} declaring "
                  f"{declared} — none of them is '{tool_name}'")


def _verify_loadable(tool_dir: Path, tool_name: str) -> str:
    """Import impl.py exactly as ToolLoader.load_fn will. Returns "" or the error.

    Catches syntax/import errors AND the wrong-code-under-this-name case: a copied
    sibling exports its own function, not `tool_name`.
    """
    impl = tool_dir / "impl.py"
    try:
        spec = importlib.util.spec_from_file_location(f"_regcheck_{tool_name}", impl)
        if spec is None or spec.loader is None:
            return f"could not create a module spec from {impl}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as e:
        return f"impl.py failed to import: {type(e).__name__}: {e}"
    if getattr(module, tool_name, None) is None:
        exported = [n for n in vars(module) if not n.startswith("_") and callable(getattr(module, n))]
        return (f"impl.py exports no function named '{tool_name}' (ToolLoader.load_fn "
                f"requires it); it exports {sorted(exported)[:6]}")
    return ""


def _derive_fallible(tool_dir: Path, tool_name: str) -> bool:
    """Does this tool's own contract say it can FAIL? (returns `passed` or `error`)

    A tool whose result carries `passed`/`error` needs its failure branch routed in
    the graph, or the engine takes the single unconditional edge and a failure reads
    as success. The gate used to answer this from a hardcoded allowlist of built-in
    names — which by construction cannot see the tools the forge itself generates.
    `skill_package_zip` documented and returned `{"passed": False, "error": ...}` on
    three paths, was routed with one unconditional edge into the COMPLETED terminal,
    and shipped: a failed zip reported a successful run.

    Static, not by import: this runs on staged code that has already been verified
    loadable, and reading the AST cannot execute it. Scoped to the exported function
    (a helper returning `{"error": ...}` says nothing about the tool's own contract).
    """
    try:
        tree = ast.parse((tool_dir / "impl.py").read_text(encoding="utf-8"))
    except Exception:
        return False
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == tool_name), None)
    if fn is None:
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and k.value in ("passed", "error"):
                    return True
    return False


def _stamp_provenance(tool_dir: Path, owner: str, tool_name: str = "") -> None:
    """Record provenance + the fail-routing contract, so both are inspectable later.

    Extra top-level keys are inert — ToolLoader.load_schema returns the parsed dict
    as-is — so this rides along in tool.yaml rather than in a sidecar file.
    `x-fallible` is what `forge_registry_check` reads to decide whether a step
    running this tool needs a failure edge; an explicit declaration already in the
    yaml (the tool-build agent's own call) always wins over the derivation.
    """
    path = tool_dir / "tool.yaml"
    data = _read_yaml(tool_dir)
    if not data:
        return
    if owner:
        data["x-generated-by"] = owner
        data["x-generated-at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if "x-fallible" not in data and tool_name:
        data["x-fallible"] = _derive_fallible(tool_dir, tool_name)
    try:
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
    except Exception:  # best-effort; never fail a good build over a stamp
        pass


# One concept, six spellings across the registry: `path` (read_file, forge_lint,
# list_tree), `file` (write, pytest), `files`, `filename`, `file_path`, `graph_path`.
# The two most-used tools disagree, so an agent that just called `write(file=…)` and
# reads the file back with `read_file(file=…)` is following the most recent example it
# saw. That is where the fatal-typo class comes from. Renaming the existing tools would
# break every config's `tool_params` and every role prompt, so instead: stop the
# divergence growing. Advisory only — a tool with an unusual name still registers.
_CANONICAL = {"path": ("file", "filename", "file_path", "filepath", "fname"),
              "paths": ("files", "file_list")}


def _nonstandard_param_names(tool_dir: Path) -> list[str]:
    notes = []
    params = (_read_yaml(tool_dir).get("parameters") or {})
    if not isinstance(params, dict):
        return notes
    for name in params:
        for canonical, variants in _CANONICAL.items():
            if name in variants:
                notes.append(
                    f"parameter '{name}' — the registry's usual name for this is "
                    f"'{canonical}'. Mixed spellings are why agents mistype file "
                    f"arguments; prefer '{canonical}' in new tools.")
    return notes


def register_tool(tool_name: str = "", source_dir: str = "", task_name: str = "",
                  owner: str = "", **kwargs) -> dict:
    # A loop var like "$current_tool" is NOT interpolated in tool_params (only in
    # context paths), so inside a loop body take the tool name from the injected
    # `task_name` (the loop's current_item). Fall back to an explicit tool_name.
    name = (tool_name or "").strip()
    if not name or name.startswith("$"):
        name = (task_name or "").strip()
    tool_name = name
    if not tool_name:
        return {"registered": False, "error": "tool_name is required (no task_name injected)"}
    src_root = Path(source_dir) if source_dir else None
    if not src_root or not src_root.exists():
        return {"registered": False, "error": f"source_dir not found: {source_dir}"}

    src, why = _resolve_tool_src(src_root, tool_name)
    if src is None:
        return {"registered": False, "error": f"cannot resolve '{tool_name}': {why}"}
    if not (src / "impl.py").exists():
        return {"registered": False, "error": f"impl.py missing in {src}"}

    # Stage → verify → swap. Staging lives OUTSIDE the scanned tools dir so a
    # half-built tool is never visible to list_tools(), and a rejected build leaves
    # any previously-registered version of the name untouched.
    dest = generated_tools_dir() / tool_name
    staging = generated_tools_dir().parent / ".tool_staging" / tool_name
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, staging)

    err = _verify_loadable(staging, tool_name)
    if err:
        shutil.rmtree(staging, ignore_errors=True)
        return {"registered": False, "error": f"'{tool_name}' rejected — {err}"}

    # Generated tools share one flat namespace, so replacing a tool another pipeline
    # generated is reported rather than silent. It is not blocked: a re-generation of
    # the same pipeline is a new run with a new id, and refusing that would be worse.
    owner = (owner or kwargs.get("project_id") or kwargs.get("config_name") or "").strip()
    prior_owner = _owner_of(dest) if dest.exists() else None
    _stamp_provenance(staging, owner, tool_name)
    if dest.exists():
        shutil.rmtree(dest)
    staging.rename(dest)

    result = {"registered": True, "tool_name": tool_name, "path": str(dest)}
    naming = _nonstandard_param_names(dest)
    if naming:
        result["param_naming"] = naming
    if prior_owner and owner and prior_owner != owner:
        result["replaced_owner"] = prior_owner
        result["warning"] = (f"'{tool_name}' was generated by {prior_owner}; it has been "
                             f"replaced by {owner}. Generated tools share one namespace.")

    # Live-register: ensure the generated-tools dir is on the loader's scan path and
    # invalidate its cache so the new tool is discoverable this session.
    try:
        from api.dependencies import get_skillflow
        loader = get_skillflow()._tool_loader
        loader.add_tools_dir(generated_tools_dir())  # add_tools_dir clears the cache
        result["live"] = tool_name in loader.list_tools()
    except Exception as e:  # pragma: no cover - defensive
        result["live"] = False
        result["warning"] = f"persisted but live-register failed: {e}"
    return result
