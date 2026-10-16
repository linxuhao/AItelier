"""Whole-pipeline frozen bundle: graph + resolved roles + template bytes.

(core/pipeline_bundle.py is the portable export/import format and keeps its
name; this module is the immutable RSI unit.) A graph version pin alone never
froze prompts or roles. ``capture`` reads the ACTUAL graph file, the ACTUAL
``sf.agent_registry`` entries the graph's ``agent_config`` names resolve to,
and the ACTUAL template bytes, and hashes all of them as one identity, with
the tools/capabilities it depends on identified as the runtime SERVES them
(``_tool_identity``), and the complete config manifest as hints.
``compile_bundle`` rewrites that unit to private, bundle-specific physical
graph/role names with the template text embedded; ``seal`` persists it beside
the generated configs so the boot scan re-verifies and reloads it. Nothing
here runs a model or a test, or touches the original builtin config.
Adoption (``adopt_bundle``/``reconcile_adoption``) swaps a GENERATED target
onto bundle-specific role names inside ONE registration transaction, guarded by
a durable before-change descriptor that blocks boot/launch of that target
until ``whole_state`` verifies one recorded bundle on every provider (files,
live registries, SQLite rows, manifest values). The settled descriptor also
records each side's manifest, which boot and reload re-apply to that unit.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import io
import json
import os
import re
import types
from pathlib import Path

import yaml

SCHEMA = 1
ROOT = Path(__file__).resolve().parents[1]
PREFIX = "gen_rsi_"
NAME_RE = re.compile(r"^gen_rsi_(.+)_([0-9a-f]{16})$")
_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
# Roles marked ``_frozen`` are registered EXACTLY (no forge defaults, no forced
# host-model substitution, no dropped keys): see pipeline_registry._register_forge_roles.


class BundleRefused(ValueError):
    """An input is missing, symlinked, external, unresolved, changed or forbidden."""


def canonical(obj) -> bytes:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BundleRefused(f"not canonically serialisable: {exc}") from exc


def sha(data) -> str:
    return hashlib.sha256(data if isinstance(data, bytes)
                          else canonical(data)).hexdigest()


def roles_text(roles: dict) -> str:
    return json.dumps(roles, ensure_ascii=False, indent=2)


def safe_file(base: Path, rel: str) -> Path:
    """A regular file strictly inside *base*; no symlink on any component."""
    base = Path(base)
    parts = Path(rel).parts
    if not rel or Path(rel).is_absolute() or ".." in parts:
        raise BundleRefused(f"unsafe path {rel!r}")
    path = base / rel
    cur = path
    while cur != base:
        if cur.is_symlink():
            raise BundleRefused(f"symlink refused: {cur}")
        if cur == cur.parent:
            raise BundleRefused(f"path escapes base: {rel!r}")
        cur = cur.parent
    if not path.is_file():
        raise BundleRefused(f"missing file: {path}")
    if base.resolve() not in path.resolve().parents:
        raise BundleRefused(f"external file refused: {path}")
    return path


def hint_attrs() -> dict:
    """``register_one`` hint key -> ConfigManifest attribute, for EVERY declared
    manifest field (only the derived graph views are not hints). Read from the
    dataclass itself so a manifest field cannot be silently left unfrozen."""
    from dataclasses import fields
    from core.config_registry import ConfigManifest
    return {("labels" if f.name == "label_overrides" else f.name): f.name
            for f in fields(ConfigManifest)
            if f.name not in ("config_name", "graph_provider")}


def manifest_hints(manifest) -> dict:
    """The complete hint set that reproduces *manifest* through register_one."""
    return {k: copy.deepcopy(getattr(manifest, a)) for k, a in hint_attrs().items()}


def agent_refs(node):
    """Every (dict, role) with a string ``agent_config``, nested loops included."""
    if isinstance(node, dict):
        if isinstance(node.get("agent_config"), str) and node["agent_config"]:
            yield node, node["agent_config"]
        for value in node.values():
            yield from agent_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from agent_refs(value)


def _steps(node):
    """Every step mapping of a graph, loop bodies included."""
    for step in node.get("steps") or [] if isinstance(node, dict) else []:
        if isinstance(step, dict):
            yield step
            loop = step.get("loop")
            if isinstance(loop, dict):
                yield from _steps(loop)


def _extra_tool_names(graph: dict) -> set:
    """Tool names the engine grants beyond pipeline_bundle.referenced_tool_names:
    a step's ``config.extra_tools`` (compose ``add_tools``)."""
    names = set()
    for step in _steps(graph):
        cfg = step.get("config")
        extra = cfg.get("extra_tools") if isinstance(cfg, dict) else None
        names |= {str(t) for t in extra or [] if t}
    return names


def _declared_capabilities(graph: dict) -> set:
    """Capability names the graph declares or offers (SkillFlow grants only
    registered ones; ``{from_item: ...}`` names are bounded by the offer list)."""
    names = {str(c) for c in graph.get("capabilities") or [] if c}
    for step in _steps(graph):
        cap = step.get("capability")
        if isinstance(cap, str) and cap:
            names.add(cap)
        elif isinstance(cap, list):
            names |= {str(c) for c in cap if c}
    return names


# Upper bound on the objects one served-code identity may walk; beyond it the
# provider is refused rather than partially identified.
_SERVED_CODE_BUDGET = 200_000


def _served_code(root) -> list:
    """Canonical identity of a live host callable or class AS SERVED by this
    process: its code objects (bytecode, constants, names, line table), defaults,
    closure cells, and every global it names that its own module defines,
    walked recursively through that module (functions, classes, constants,
    route tables). Objects of other modules are named, not walked.

    Read from the live objects only: nothing is called, imported or executed,
    and no source file is consulted (a file edited after import is not what
    the process serves)."""
    inner = (root.func if isinstance(root, functools.partial) else
             root.__func__ if isinstance(root, types.MethodType) else root)
    home = getattr(inner, "__module__", None)
    seen: set = set()
    budget = [_SERVED_CODE_BUDGET]

    def name_of(obj):
        return f"{getattr(obj, '__module__', None)}:{getattr(obj, '__qualname__', None)}"

    def code_names(code):
        out = set(code.co_names)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                out |= code_names(const)
        return out

    def code(c, stack):
        return ["code", c.co_qualname, c.co_code.hex(), [val(k, stack) for k in c.co_consts],
                list(c.co_names), list(c.co_varnames), list(c.co_freevars),
                list(c.co_cellvars), [c.co_argcount, c.co_posonlyargcount,
                                      c.co_kwonlyargcount, c.co_flags, c.co_firstlineno],
                c.co_linetable.hex(), c.co_exceptiontable.hex()]

    def val(v, stack):
        budget[0] -= 1
        if budget[0] < 0:
            raise BundleRefused(f"served code of {name_of(root)} is too large to identify")
        if v is None or v is Ellipsis or isinstance(v, (bool, int, float, complex, str)):
            return [type(v).__name__, repr(v)]
        if isinstance(v, bytes):
            return ["bytes", v.hex()]
        if isinstance(v, types.CodeType):
            return code(v, stack)
        if isinstance(v, types.ModuleType):
            return ["module", v.__name__]
        if isinstance(v, re.Pattern):
            return ["pattern", v.pattern if isinstance(v.pattern, str) else v.pattern.hex(),
                    v.flags]
        if id(v) in stack:
            return ["cycle"]
        stack = stack | {id(v)}
        if isinstance(v, (tuple, list)):
            return [type(v).__name__, [val(x, stack) for x in v]]
        if isinstance(v, (set, frozenset)):
            return [type(v).__name__, sorted((val(x, stack) for x in v), key=canonical)]
        if isinstance(v, dict):
            return ["dict", sorted(([val(k, stack), val(x, stack)] for k, x in v.items()),
                                   key=lambda kv: canonical(kv[0]))]
        if isinstance(v, (staticmethod, classmethod)):
            return [type(v).__name__, val(v.__func__, stack)]
        if isinstance(v, property):
            return ["property", val(v.fget, stack), val(v.fset, stack), val(v.fdel, stack)]
        if isinstance(v, functools.partial):
            return ["partial", val(v.func, stack), val(v.args, stack), val(v.keywords, stack)]
        if isinstance(v, types.MethodType):
            return ["method", val(v.__func__, stack), ["object", name_of(type(v.__self__))]]
        if isinstance(v, (types.FunctionType, type)):
            if v.__module__ != home:
                return ["ref", name_of(v)]
            if id(v) in seen:
                return ["seen", name_of(v)]
            seen.add(id(v))
            if isinstance(v, type):
                return ["class", name_of(v), [name_of(b) for b in v.__bases__],
                        [[k, val(x, stack)] for k, x in sorted(vars(v).items())
                         if k not in ("__dict__", "__weakref__")]]
            cells = []
            for cell in v.__closure__ or ():
                try:
                    cells.append(val(cell.cell_contents, stack))
                except ValueError:
                    cells.append(["empty"])
            refs = v.__globals__
            return ["function", name_of(v), code(v.__code__, stack),
                    val(v.__defaults__, stack), val(v.__kwdefaults__, stack), cells,
                    [[n, val(refs[n], stack)] for n in sorted(code_names(v.__code__))
                     if n in refs]]
        return ["object", name_of(type(v))]

    return val(root, frozenset())


def _served_identity(obj, what: str) -> dict:
    """Identity of a live in-process provider: its served code, never its file.
    A provider whose code cannot be walked (a callable instance, a builtin) is
    refused rather than identified by its type name alone."""
    inner = (obj.func if isinstance(obj, functools.partial) else
             obj.__func__ if isinstance(obj, types.MethodType) else obj)
    if not isinstance(inner, (types.FunctionType, type)):
        raise BundleRefused(f"{what} has no served code to identify "
                            f"({type(obj).__qualname__})")
    return {"qualname": getattr(obj, "__qualname__", type(obj).__qualname__),
            "sha256": sha(_served_code(obj))}


def _parse_tool_schema(data: bytes):
    """``tool.yaml`` parsed exactly as ``ToolLoader.load_schema`` parses it
    (text mode, universal newlines, ``yaml.safe_load``)."""
    try:
        return yaml.safe_load(io.TextIOWrapper(io.BytesIO(data), encoding="utf-8").read())
    except (UnicodeError, yaml.YAMLError) as exc:
        raise BundleRefused(f"tool schema is not readable YAML: {exc}") from exc


def _tool_identity(sf, name):
    """(identity, location, served schema) of *name* as ``sf._tool_loader``
    SERVES it now.

    The engine resolves agent tools, tool steps, validation/lifecycle tools and
    ``{source: {tool: X}}`` context providers alike through that loader
    (``load_fn``/``load_schema``): its cache first, else the FIRST tools
    directory holding ``<name>/tool.yaml``. A directory tool's identity is its
    files' bytes, and only when what the loader already holds is provably
    those bytes:

    * a cached schema must equal the current ``tool.yaml`` as the loader
      parses it;
    * a cached callable must be one the loader recorded compiling from the
      current ``impl.py`` bytes (``AItelierToolLoader.loaded_impl``). A cached
      callable from that file with no such record (the base SDK loader keeps
      none) is unprovable: its file name or the file's current bytes are not
      what was compiled. Refused, never assumed;
    * a callable registered dynamically (from another file) is served instead
      of the directory's: its live served code and its registered schema are
      the identity.

    Nothing is imported or called, and no loader cache is cleared or reloaded."""
    loader = getattr(sf, "_tool_loader", None)
    if loader is None:
        raise BundleRefused(f"unresolved tool {name!r}: no tool provider")
    cached_schema, cached_fn = getattr(loader, "_cache", {}).get(name, (None, None))
    tool_dir = loader._find_tool_dir(name)
    native = bool(loader.is_native(name))
    if tool_dir is not None:
        tdir = Path(tool_dir) / name
        raw = {f: safe_file(tdir, f).read_bytes()
               for f in ("tool.yaml", "impl.py", "README.md")
               if (tdir / f).exists() or (tdir / f).is_symlink()}
        if "tool.yaml" not in raw:
            raise BundleRefused(f"unresolved tool {name!r}: no tool.yaml at {tdir}")
        files = {f: sha(b) for f, b in raw.items()}
        ident = {"native": native, "files": files}
        impl = tdir / "impl.py"
        dynamic = False
        if cached_fn is not None:
            loaded = getattr(loader, "loaded_impl", lambda _n: None)(name)
            if loaded is not None:
                if Path(loaded[0]) != impl or loaded[1] != files.get("impl.py"):
                    raise BundleRefused(
                        f"tool {name!r}: the loader serves a callable compiled from "
                        f"different bytes than {impl} holds now (sha256 "
                        f"{loaded[1][:12]}); it must be reloaded before capture")
            elif _defined_in(cached_fn, impl):
                raise BundleRefused(
                    f"tool {name!r}: the loader serves a cached callable from {impl} "
                    f"but recorded no bytes it was compiled from; its identity "
                    f"cannot be proven from the file")
            else:
                dynamic = True
                ident["callable"] = _served_identity(cached_fn, f"tool {name!r}")
        if dynamic and cached_schema is not None:
            ident["schema_sha256"] = sha(cached_schema)
            schema = cached_schema
        else:
            schema = _parse_tool_schema(raw["tool.yaml"])
            if cached_schema is not None and cached_schema != schema:
                raise BundleRefused(
                    f"tool {name!r}: the loader serves a cached schema that differs "
                    f"from {tdir / 'tool.yaml'}; it must be reloaded before capture")
        if "impl.py" not in files and cached_fn is None:
            raise BundleRefused(f"unresolved tool {name!r}: no impl.py at {tdir}")
        return ident, {"dir": str(tdir), "kind": "native" if native else "dir"}, schema
    if cached_fn is not None:
        return ({"native": native, "files": {},
                 "callable": _served_identity(cached_fn, f"tool {name!r}"),
                 "schema_sha256": sha(cached_schema) if cached_schema is not None else None},
                {"dir": None, "kind": "dynamic"}, cached_schema)
    raise BundleRefused(f"unresolved tool {name!r}: no provider serves it")


def _defined_in(fn, path: Path) -> bool:
    code = getattr(fn, "__code__", None)
    filename = getattr(code, "co_filename", None)
    try:
        return bool(filename) and Path(filename).resolve() == path.resolve()
    except OSError:
        return True


def _role_schema_drift(sf, roles, schemas: dict) -> None:
    """A role offers its tools through the schemas SkillFlow resolved into it
    (``AgentConfig.tool_schemas``) at its last registration; those copies are
    what a model is shown. Refuse when one differs from the loader's."""
    for role in roles:
        cfg = sf.agent_registry.get(role) if role in sf.agent_registry else None
        for tool, schema in (getattr(cfg, "tool_schemas", None) or {}).items():
            if tool in schemas and schema != schemas[tool]:
                raise BundleRefused(
                    f"role {role!r} serves a stale schema for tool {tool!r} "
                    f"(resolved before the loader's current one); re-register it first")


def _capability_identity(sf, name):
    cap = (getattr(sf, "_capabilities", None) or {}).get(name)
    if not isinstance(cap, dict):
        raise BundleRefused(f"unresolved capability {name!r}: not registered")
    provider = cap.get("context_provider")
    return {"tools": list(cap.get("tools") or []), "owner": cap.get("owner"),
            "briefing_sha256": sha((cap.get("briefing") or "").encode("utf-8")),
            "context_provider": (None if provider is None else
                                 _served_identity(provider, f"capability {name!r}"))}


def _loader_identity(sf):
    """The loader class as served: its methods and everything they reach in
    their module, including the host route table and route callables."""
    loader = getattr(sf, "_tool_loader", None)
    if loader is None:
        return None
    return _served_identity(type(loader), "tool loader")


def resolve_providers(sf, graph: dict, roles: dict):
    """Resolve every tool/capability the unit depends on through the ACTUAL
    providers. Returns (tools, capabilities, tool_locations)."""
    from core.pipeline_bundle import referenced_tool_names
    caps = {n: _capability_identity(sf, n)
            for n in sorted(_declared_capabilities(graph))}
    names = referenced_tool_names(graph, {r: {"tools": c["tools"]}
                                          for r, c in roles.items()})
    names |= _extra_tool_names(graph)
    names |= {t for c in caps.values() for t in c["tools"]}
    tools, where, schemas = {}, {}, {}
    for n in sorted(names):
        tools[n], where[n], schemas[n] = _tool_identity(sf, n)
    _role_schema_drift(sf, roles, schemas)
    return tools, caps, where


def verify_providers(sf, unit: dict) -> None:
    """Refuse when a tool/capability/loader the unit froze is now missing,
    resolves to different bytes or served code, or is served from a cache that
    cannot be proven to hold them (checked before any registration)."""
    for name, want in (unit.get("tools") or {}).items():
        if _tool_identity(sf, name)[0] != want:
            raise BundleRefused(f"tool {name!r} changed since capture")
    for name, want in (unit.get("capabilities") or {}).items():
        if _capability_identity(sf, name) != want:
            raise BundleRefused(f"capability {name!r} changed since capture")
    if unit.get("tool_loader") is not None and _loader_identity(sf) != unit["tool_loader"]:
        raise BundleRefused("tool loader changed since capture")


def capture(sf, registry, name: str, *, template_base=None, configs_dirs=None,
            tools_dir=None) -> dict:
    """Freeze the complete logical unit of registered graph *name*.

    Tools and capabilities resolve through ``sf``'s own providers (see
    ``_tool_identity``); *tools_dir* only labels which of the loader's
    directories is the generated-tools one."""
    from core import datadir, pipeline_registry as pr
    from core.config_registry import ConfigManifest
    from skillflow.core import graph_digest

    base = Path(template_base or ROOT / "templates")
    tools_dir = Path(tools_dir or datadir.tools_dir())
    dirs = [Path(d) for d in (configs_dirs or
                              [ROOT / "configs", pr.generated_configs_dir()])]
    gpath = next((d / f"{name}.yaml" for d in dirs
                  if (d / f"{name}.yaml").exists()
                  or (d / f"{name}.yaml").is_symlink()), None)
    if gpath is None:
        raise BundleRefused(f"no graph file for {name!r}")
    raw = safe_file(gpath.parent, gpath.name).read_bytes()
    graph = yaml.safe_load(raw)
    if not isinstance(graph, dict) or graph.get("name") != name:
        raise BundleRefused("graph file is not a mapping named " + repr(name))
    graph.pop("x-aitelier", None)
    live_graph = sf._graphs.get(name)
    if live_graph is None:
        raise BundleRefused(f"graph {name!r} is not registered")
    digest = graph_digest(live_graph.to_dict())
    if graph_digest(pr._pipeline_graph()._from_dict(
            copy.deepcopy(graph)).to_dict()) != digest:
        raise BundleRefused(f"registered graph {name!r} differs from its file")
    roles, prompts = {}, {}
    sources = {"graph": {"path": gpath.name, "sha256": sha(raw)}}
    for _node, role in agent_refs(graph):
        if role in roles:
            continue
        if role not in sf.agent_registry:
            raise BundleRefused(f"unresolved role {role!r}")
        d = sf.agent_registry.get(role).to_dict()
        cfg = copy.deepcopy(d.get("config") or {})
        model = cfg.pop("model", d.get("model"))
        tools = cfg.pop("tools", d.get("tools") or [])
        template, inline = cfg.pop("template", ""), cfg.pop("system_prompt", "")
        key = None
        if template:
            key = f"template:{template}"
            tbytes = safe_file(base, template).read_bytes()
            try:
                prompts[key] = tbytes.decode("utf-8")
            except UnicodeError as exc:
                raise BundleRefused(f"template {template!r} not UTF-8") from exc
            sources[key] = {"path": template, "sha256": sha(tbytes)}
        elif inline:
            key = f"inline:{role}"
            prompts[key] = inline
            sources[key] = {"path": None, "sha256": sha(inline.encode("utf-8"))}
        roles[role] = {"model": model, "tools": list(tools), "config": cfg,
                       "prompt": key}
    tools, caps, where = resolve_providers(sf, graph, roles)
    for loc in where.values():
        if loc["dir"] and Path(loc["dir"]).parent.resolve() == tools_dir.resolve():
            loc["kind"] = "generated"
    manifest = registry.get(name) if registry is not None else None
    hints = manifest_hints(manifest) if isinstance(manifest, ConfigManifest) else {}
    rows = sf.list_graph_versions(name)
    parent = {"version": rows[0]["version"] if rows else None, "digest": digest}
    steps = [s for s in graph.get("steps") or [] if isinstance(s, dict)]
    body = {"schema": SCHEMA, "logical_name": name, "graph": graph,
            "roles": roles, "prompts": prompts, "sources": sources,
            "tools": tools, "capabilities": caps,
            "tool_loader": _loader_identity(sf), "hints": hints,
            "parent": parent, "derived_from": None,
            "inventory": {"steps": [s.get("id") for s in steps],
                          "outputs": {str(s.get("id")): s.get("output")
                                      for s in steps},
                          "capabilities": graph.get("capabilities"),
                          "tools": sorted(tools)}}
    return {**body, "identity": sha(body),
            "locations": {"graph": str(gpath), "template_base": str(base),
                          "tools_dir": str(tools_dir), "tools": where}}


def body_of(bundle: dict) -> dict:
    return {k: v for k, v in bundle.items() if k not in ("identity", "locations")}


def verify_identity(bundle: dict) -> None:
    if sha(body_of(bundle)) != bundle.get("identity"):
        raise BundleRefused("bundle bytes do not match its identity")


def verify_inputs(bundle: dict) -> None:
    """Re-read every actual input; refuse any byte changed since capture."""
    verify_identity(bundle)
    loc = bundle["locations"]
    for key, src in bundle["sources"].items():
        want = src.get("base_sha256") or src["sha256"]
        if key == "graph":
            gp = Path(loc["graph"])
            have = sha(safe_file(gp.parent, gp.name).read_bytes())
        elif src.get("path"):
            have = sha(safe_file(Path(loc["template_base"]),
                                 src["path"]).read_bytes())
        else:
            continue
        if have != want:
            raise BundleRefused(f"input {key!r} changed since capture")
    for name, ident in bundle["tools"].items():
        where = (loc.get("tools") or {}).get(name) or {}
        if ident["files"] and not where.get("dir"):
            raise BundleRefused(f"tool {name!r} has no recorded provider location")
        if where.get("dir"):
            tdir = Path(where["dir"])
            have = {f: sha(safe_file(tdir, f).read_bytes()) for f in ident["files"]}
            if have != ident["files"]:
                raise BundleRefused(f"tool {name!r} changed since capture")


# ── Candidate whitelist ────────────────────────────────────────────────────

# Word stems that DECLARE an evaluating step/role/output. Matched at the start
# of whole identifier words, never as substrings: "gate" is not a word of
# "investigate".
_CHECKER_STEMS = ("review", "verif", "check", "judg", "gate", "audit", "validat",
                  "critic", "critiq", "grader", "grading", "evaluat", "verdict",
                  "approv", "score")
_EDIT_KEYS = {"graph", "prompts"}
MAX_TEXT = 200_000


def _movable(entry) -> bool:
    """A PUBLIC cross-config output source that may change position."""
    src = entry.get("source") if isinstance(entry, dict) else None
    return (isinstance(src, dict) and "config" in src and "tool" not in src
            and set(entry) == {"source"} and not src.get("required"))


def _normalise(graph) -> dict:
    if not isinstance(graph, dict) or not isinstance(graph.get("steps"), list):
        raise BundleRefused("graph edit is not a mapping with steps")
    g = copy.deepcopy(graph)
    g.pop("description", None)
    for step in g["steps"]:
        if not isinstance(step, dict):
            continue
        step.pop("description", None)
        ctx = step.get("context")
        if isinstance(ctx, list):
            step["context"] = ["\0movable" if _movable(c) else c for c in ctx]
            step["\0order"] = sorted(canonical(c).decode()
                                     for c in ctx if _movable(c))
    return g


def _words(*texts) -> list:
    out = []
    for text in texts:
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text or ""))
        out += [w for w in re.split(r"[^a-z]+", spaced.lower()) if w]
    return out


def _output_names(step: dict) -> list:
    out = step.get("output")
    fixed = (out.get("fixed") if isinstance(out, dict) else None) or step.get("output_fixed")
    if not isinstance(fixed, dict):
        return []
    return [*fixed, *(v for v in fixed.values() if isinstance(v, str))]


def _checker_evidence(graph: dict, roles: dict) -> dict:
    """role -> reasons it judges or routes work (empty list = a maker).

    Evidence comes from the actual graph and frozen role, not one name:
    * a ``match`` transition: the step's own output decides the route;
    * a ``feedback`` transition, or a transition/``checkpoint_reject_to`` that
      is a back edge from ``begin``: it sends work upstream for rework;
    * its step id, role name or declared output slots are evaluation words;
    * the host prompt assembler's own checker test (PromptAssembler._is_checker).
    A role with any evidence on any of its steps stays protected."""
    from core.prompt_assembler import PromptAssembler
    steps = list(_steps(graph))
    edges = {}
    for s in steps:
        targets = [t.get("to") for t in s.get("transitions") or [] if isinstance(t, dict)]
        targets.append(s.get("checkpoint_reject_to"))
        edges[str(s.get("id"))] = [str(t) for t in targets if t]
    # Back edges of a depth-first walk from ``begin`` (then any unreached step,
    # e.g. loop bodies, in file order): the edges that return work upstream.
    back, state = set(), {}
    for root in [str(graph.get("begin"))] + list(edges):
        if root not in edges or root in state:
            continue
        state[root] = "open"
        stack = [(root, iter(edges[root]))]
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                state[node] = "done"
                stack.pop()
            elif state.get(nxt) == "open":
                back.add((node, nxt))
            elif nxt in edges and nxt not in state:
                state[nxt] = "open"
                stack.append((nxt, iter(edges[nxt])))

    evidence = {}
    for s in steps:
        role = s.get("agent_config")
        if not isinstance(role, str) or not role:
            continue
        sid = str(s.get("id"))
        why = evidence.setdefault(role, [])
        transitions = [t for t in s.get("transitions") or [] if isinstance(t, dict)]
        if any(t.get("match") for t in transitions):
            why.append(f"step {sid!r} routes on its own output")
        if any(t.get("feedback") for t in transitions):
            why.append(f"step {sid!r} feeds its output back as feedback")
        for target in edges[sid]:
            if target != sid and (sid, target) in back:
                why.append(f"step {sid!r} sends work back to {target!r}")
        if any(w.startswith(_CHECKER_STEMS)
               for w in _words(sid, role, *_output_names(s))):
            why.append(f"step {sid!r} declares an evaluating name or output")
        if PromptAssembler._is_checker((roles.get(role) or {}).get("tools") or [], sid):
            why.append(f"step {sid!r} is a checker to the prompt assembler")
    return evidence


def derive_candidate(bundle: dict, edit: dict) -> dict:
    """Apply a PARSED edit document through the whitelist; refuse everything else.

    Allowed: maker prompt text, graph/step ``description`` text, and reordering
    of public cross-config context sources. Topology, schema/output, tools,
    capabilities, models/routes/limits, checkers, required sources and any
    unknown key are refused before anything is sealed or registered.
    """
    verify_identity(bundle)
    if not isinstance(edit, dict) or not set(edit) <= _EDIT_KEYS:
        raise BundleRefused(f"unknown edit keys: {sorted(set(edit or {}) - _EDIT_KEYS)}")
    body = copy.deepcopy(body_of(bundle))
    evidence = _checker_evidence(body["graph"], body["roles"])
    for key, text in (edit.get("prompts") or {}).items():
        if key not in body["prompts"]:
            raise BundleRefused(f"unknown prompt {key!r}")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise BundleRefused(f"prompt {key!r} must be bounded nonempty text")
        users = [r for r, c in body["roles"].items() if c["prompt"] == key]
        why = [w for r in users for w in evidence.get(r, ["role used by no step"])]
        if not users or why:
            raise BundleRefused(f"prompt {key!r} is not exclusively a maker prompt: "
                                + "; ".join(why or ["no role uses it"]))
        body["prompts"][key] = text
        src = body["sources"][key]
        body["sources"][key] = {**src, "base_sha256": src.get("base_sha256") or src["sha256"],
                                "sha256": sha(text.encode("utf-8"))}
    if edit.get("graph") is not None:
        new_graph = edit["graph"]
        if _normalise(new_graph) != _normalise(body["graph"]):
            raise BundleRefused("graph edit changes more than descriptions/public context order")
        for key in ("description",):
            if key in new_graph and not isinstance(new_graph[key], str):
                raise BundleRefused("description must be text")
        src = body["sources"]["graph"]
        body["graph"] = copy.deepcopy(new_graph)
        body["sources"]["graph"] = {**src, "base_sha256": src.get("base_sha256") or src["sha256"],
                                    "sha256": sha(body["graph"])}
    body["derived_from"] = bundle["identity"]
    return {**body, "identity": sha(body), "locations": bundle["locations"]}


# ── Compile / seal / boot ──────────────────────────────────────────────────

def compile_bundle(bundle: dict, target: str) -> dict:
    """Deterministic private physical form; one trusted path for every arm."""
    from core import pipeline_registry as pr
    verify_identity(bundle)
    if not isinstance(target, str) or not _TARGET_RE.match(target):
        raise BundleRefused(f"invalid target slug {target!r}")
    logical = bundle["logical_name"]
    phys = f"{PREFIX}{target}_{bundle['identity'][:16]}"
    data = copy.deepcopy(bundle["graph"])
    pr._rewrite_self_config_refs(data, phys)
    data["name"] = phys
    sep = pr._ROLE_SEP
    pre = logical + sep
    role_map = {r: f"{phys}{sep}{r[len(pre):] if r.startswith(pre) else r}"
                for r in bundle["roles"]}
    for node, role in list(agent_refs(data)):
        if role not in role_map:
            raise BundleRefused(f"role {role!r} missing from frozen roles")
        node["agent_config"] = role_map[role]
    roles, role_prompt = {}, {}
    for role, cfg in bundle["roles"].items():
        entry = {**cfg["config"], "model": cfg["model"], "tools": list(cfg["tools"])}
        if cfg["prompt"] is not None:
            entry["system_prompt"] = bundle["prompts"][cfg["prompt"]]
        entry["_frozen"] = True
        roles[role_map[role]] = entry
        role_prompt[role_map[role]] = cfg["prompt"]
    body = {"schema": SCHEMA, "target": target, "physical_name": phys,
            "logical_name": logical, "bundle_identity": bundle["identity"],
            "derived_from": bundle.get("derived_from"),
            "role_map": role_map, "role_prompt": role_prompt,
            "graph_yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            "roles": roles, "prompts": bundle["prompts"],
            "hints": bundle["hints"], "sources": bundle["sources"],
            "tools": bundle["tools"], "capabilities": bundle["capabilities"],
            "tool_loader": bundle["tool_loader"], "parent": bundle["parent"],
            "inventory": bundle["inventory"]}
    return {**body, "digest": sha(body)}


def sealed_paths(config_dir, phys):
    d = Path(config_dir)
    return (d / f"{phys}.yaml", d / f"{phys}.roles.json", d / f"{phys}.bundle.json")


def fsync_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_sealed(config_dir, phys: str) -> dict:
    """Re-verify the three persisted files against the sidecar's own digest."""
    gp, rp, sp = sealed_paths(config_dir, phys)
    for p in (gp, rp, sp):
        if p.is_symlink() or not p.is_file():
            raise BundleRefused(f"sealed file missing or symlinked: {p.name}")
    try:
        sealed = json.loads(sp.read_text(encoding="utf-8"))
        body = {k: v for k, v in sealed.items() if k != "digest"}
        ok = (sha(body) == sealed["digest"] and sealed["physical_name"] == phys
              and bool(NAME_RE.match(phys))
              and phys.endswith(sealed["bundle_identity"][:16])
              and gp.read_text(encoding="utf-8") == sealed["graph_yaml"]
              and rp.read_text(encoding="utf-8") == roles_text(sealed["roles"]))
        for role, key in sealed["role_prompt"].items():
            if key is not None and (sealed["roles"][role].get("system_prompt")
                                    != sealed["prompts"][key]):
                ok = False
    except (OSError, ValueError, KeyError, TypeError, BundleRefused) as exc:
        raise BundleRefused(f"sealed bundle {phys!r} unreadable: {exc}") from exc
    if not ok:
        raise BundleRefused(f"sealed bundle {phys!r} bytes were altered")
    return sealed


def seal(bundle: dict, target: str, config_dir=None) -> dict:
    """Verify ACTUAL input bytes, recompile here (never trust a supplied
    payload), persist immutably. Same physical name + different bytes refuses.
    Nothing is registered or executed."""
    from core import pipeline_registry as pr
    verify_inputs(bundle)
    compiled = compile_bundle(bundle, target)
    d = Path(config_dir or pr.generated_configs_dir())
    gp, rp, sp = sealed_paths(d, compiled["physical_name"])
    if any(p.exists() or p.is_symlink() for p in (gp, rp, sp)):
        existing = verify_sealed(d, compiled["physical_name"])
        if existing["digest"] != compiled["digest"]:
            raise BundleRefused("same bundle identity with different bytes refused")
        return existing
    fsync_write(gp, compiled["graph_yaml"])
    fsync_write(rp, roles_text(compiled["roles"]))
    fsync_write(sp, json.dumps(compiled, ensure_ascii=False, indent=2))  # marker last
    return verify_sealed(d, compiled["physical_name"])


def is_sealed_name(stem: str) -> bool:
    return bool(NAME_RE.match(stem or ""))


# ── Registration, adoption and recovery ────────────────────────────────────

def register_sealed(sf, registry, phys: str, config_dir=None):
    """Verify a sealed bundle's bytes, then register it through the existing
    validated registration path (one RegistrationTransaction). Returns manifest."""
    from core import pipeline_registry as pr
    d = Path(config_dir or pr.generated_configs_dir())
    sealed = verify_sealed(d, phys)
    verify_providers(sf, sealed)
    graph, hints = pr._validated_registration(
        phys, sealed["graph_yaml"], roles=sealed["roles"], sf=sf)
    pr._commit_registration(sf, registry, phys, graph, sealed_hints(hints, sealed),
                            roles=sealed["roles"], require_manifest=True)
    return registry.get(phys)


def frozen_hints(hints: dict, frozen: dict | None) -> dict:
    """Recorded manifest hints replace generated-pipeline derivation EXACTLY,
    an explicit ``None`` (no seed file, no output step) included: the frozen
    manifest is the captured one, not a blend with what a generated graph
    would derive."""
    return {**hints, **(frozen or {})}


def sealed_hints(hints: dict, sealed: dict) -> dict:
    return frozen_hints(hints, sealed.get("hints"))


# Settled descriptor state -> the recorded side the target now IS.
_SETTLED_SIDE = {"adopted": "new", "reconciled_new": "new",
                 "rolled_back": "old", "reconciled_old": "old"}


def recorded_hints(config_dir, target: str, unit: dict | None = None) -> dict | None:
    """Manifest hints a settled adoption descriptor recorded for *target*'s
    CURRENT whole unit, or None when the target's files are not that recorded
    unit (never adopted, or since replaced by an ordinary edit). Boot, reload,
    publication and ``whole_state`` all apply these, so a recorded bundle's
    scheduler ownership, checkpoint policy and labels survive a restart."""
    desc = read_descriptor(config_dir, target)
    side = desc and desc.get(_SETTLED_SIDE.get(desc.get("state"), ""))
    if not isinstance(side, dict) or not isinstance(side.get("hints"), dict):
        return None
    if unit_identity(unit or read_unit(config_dir, target)) != side.get("identity"):
        return None
    return side["hints"]


def boot_gate(config_dir, stem: str):
    """Boot/launch gate for one generated stem: (sealed_or_None, refusal_or_None).

    A sealed bundle must re-verify; a target with a pending/unreadable adoption
    descriptor is blocked until reconciled. Only that stem is affected."""
    try:
        block = adoption_block(config_dir, stem)
        if block:
            return None, block
        if is_sealed_name(stem):
            return verify_sealed(config_dir, stem), None
    except BundleRefused as exc:
        return None, str(exc)
    return None, None


def descriptor_path(config_dir, target: str) -> Path:
    return Path(config_dir) / f"{target}.adoption.json"


def read_descriptor(config_dir, target: str) -> dict | None:
    p = descriptor_path(config_dir, target)
    if not p.exists() and not p.is_symlink():
        return None
    if p.is_symlink() or not p.is_file():
        raise BundleRefused(f"adoption descriptor for {target!r} is unreadable")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        body = {k: v for k, v in data.items() if k != "digest"}
        if sha(body) != data["digest"]:
            raise ValueError("digest mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BundleRefused(
            f"adoption descriptor for {target!r} is corrupt: {exc}") from exc
    return data


def write_descriptor(config_dir, target: str, body: dict) -> dict:
    data = {**body, "digest": sha(body)}
    fsync_write(descriptor_path(config_dir, target),
                json.dumps(data, ensure_ascii=False, indent=2))
    return data


def adoption_block(config_dir, target: str) -> str | None:
    """Why *target* may not boot/launch, or None. A crash mid-adoption leaves a
    ``pending`` descriptor; it stays blocking until reconcile_adoption."""
    desc = read_descriptor(config_dir, target)
    if desc is not None and desc.get("state") == "pending":
        return (f"adoption of {target!r} is unreconciled (old "
                f"{desc['old']['identity'][:12]} / new {desc['new']['identity'][:12]}); "
                f"reconcile to the recorded old or new bundle first")
    return None


def launch_block(config_name: str, config_dir=None) -> str | None:
    from core import pipeline_registry as pr
    if not str(config_name).startswith(pr.GEN_PREFIX):
        return None                     # adopt_bundle only targets generated configs
    try:
        return adoption_block(config_dir or pr.generated_configs_dir(), config_name)
    except BundleRefused as exc:
        return str(exc)


def read_unit(config_dir, target: str) -> dict:
    """The persisted whole unit of a generated target: graph + roles bytes."""
    d = Path(config_dir)
    gp, rp = d / f"{target}.yaml", d / f"{target}.roles.json"
    if gp.is_symlink() or not gp.is_file() or rp.is_symlink():
        raise BundleRefused(f"target {target!r} has no regular graph file")
    return {"graph_yaml": gp.read_text(encoding="utf-8"),
            "roles_text": rp.read_text(encoding="utf-8") if rp.is_file() else None}


def unit_identity(unit: dict) -> str:
    return sha({"graph_yaml": unit["graph_yaml"], "roles_text": unit["roles_text"]})


def adopted_unit(config_dir, target: str, phys: str) -> dict:
    """Target's next whole unit: the sealed graph under the target name, bundle
    specific immutable role names, old roles RETAINED beside them."""
    from core import pipeline_registry as pr
    sealed = verify_sealed(config_dir, phys)
    sep, tag = pr._ROLE_SEP, f"rsi{sealed['bundle_identity'][:16]}_"
    role_map = {}
    for role in sealed["roles"]:
        bare = role[len(phys + sep):] if role.startswith(phys + sep) else role
        role_map[role] = f"{target}{sep}{tag}{bare}"
    data = yaml.safe_load(sealed["graph_yaml"])
    pr._rewrite_self_config_refs(data, target)
    data["name"] = target
    for node, role in list(agent_refs(data)):
        node["agent_config"] = role_map[role]
    old = read_unit(config_dir, target)
    roles = json.loads(old["roles_text"]) if old["roles_text"] else {}
    for role, cfg in sealed["roles"].items():
        roles[role_map[role]] = copy.deepcopy(cfg)
    return {"graph_yaml": yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            "roles_text": roles_text(roles), "hints": sealed.get("hints") or {}}


def _publish_unit(sf, registry, config_dir, target, unit, hints=None):
    """Write+register one whole unit in ONE registration transaction, then
    prove files AND live graph equal it (any failure rolls the unit back)."""
    from core import pipeline_registry as pr
    d = Path(config_dir)
    gp, rp = d / f"{target}.yaml", d / f"{target}.roles.json"
    roles = json.loads(unit["roles_text"]) if unit["roles_text"] else None
    graph, base = pr._validated_registration(
        target, unit["graph_yaml"], roles=roles, sf=sf)

    def persist():
        fsync_write(gp, unit["graph_yaml"])
        if unit["roles_text"] is None:
            rp.unlink(missing_ok=True)
        else:
            fsync_write(rp, unit["roles_text"])
        fsync_dir(d)
        if unit_identity(read_unit(d, target)) != unit_identity(unit):
            raise BundleRefused("persisted unit differs from the intended bundle")
        from skillflow.core import graph_digest
        live = sf._graphs.get(target)
        if live is None or graph_digest(live.to_dict()) != graph_digest(graph.to_dict()):
            raise BundleRefused("live graph differs from the intended bundle")

    pr._commit_registration(
        sf, registry, target, graph, frozen_hints(base, hints),
        roles=roles, paths=[gp, rp], persist=persist, require_manifest=True)


def _expected_roles(target: str, roles: dict | None) -> dict:
    """What ``_register_forge_roles`` registers for *roles*, computed into a
    scratch AgentRegistry (no live effect)."""
    from core import pipeline_registry as pr
    from skillflow.agent_registry import AgentRegistry

    class _Scratch:
        agent_registry = AgentRegistry()

        def register_agent_config_from_dict(self, name, d):
            self.agent_registry.register_dict(name, d)

    scratch = _Scratch()
    pr._register_forge_roles(scratch, target, roles or {})
    return scratch.agent_registry._configs


def whole_state(sf, registry, config_dir, target: str, unit: dict) -> list:
    """Every provider where *target* differs from whole *unit* ([] = verified).

    Covers each place a registration lands: the graph/roles files, the live
    graph and resolver, the persisted SQLite graph row and its version-history
    row (what a restart or an uncached resolver reads), the live role configs
    and the config manifest, value by value: the unit's recorded hints (else
    the durable record of this unit, else what a generated config derives),
    built through the real ``ConfigRegistry._make_manifest``."""
    from core import pipeline_registry as pr
    from core.config_registry import ConfigRegistry, _read_host_hints
    from skillflow.core import canonical_graph_json, graph_digest
    problems = []
    try:
        if unit_identity(read_unit(config_dir, target)) != unit_identity(unit):
            problems.append("files")
    except BundleRefused as exc:
        problems.append(f"files: {exc}")
    roles = json.loads(unit["roles_text"]) if unit["roles_text"] else None
    graph, base = pr._validated_registration(target, unit["graph_yaml"], roles=roles, sf=sf)
    want, text = graph_digest(graph.to_dict()), canonical_graph_json(graph.to_dict())
    live = sf._graphs.get(target)
    if live is None or graph_digest(live.to_dict()) != want:
        problems.append("live graph")
    resolver = sf._resolvers.get(target)
    if resolver is None or graph_digest(resolver.graph.to_dict()) != want:
        problems.append("live resolver")
    with sf._lock:
        row = sf._conn.execute("SELECT yaml_text, version FROM skillflow_graphs "
                               "WHERE name=?", (target,)).fetchone()
        hist = row and sf._conn.execute(
            "SELECT yaml_text, digest FROM skillflow_graph_versions "
            "WHERE name=? AND version=?", (target, row["version"])).fetchone()
    if row is None or row["yaml_text"] != text:
        problems.append("persisted graph row")
    elif hist is None or hist["digest"] != want or hist["yaml_text"] != text:
        problems.append("persisted version history")
    for role, cfg in _expected_roles(target, roles).items():
        have = sf.agent_registry.get(role)
        if have is None or (have.model, list(have.tools), have.config) != (
                cfg.model, list(cfg.tools), cfg.config):
            problems.append(f"live role {role}")
    for node in graph.steps:
        if node.agent_config and node.agent_config not in sf.agent_registry:
            problems.append(f"live role {node.agent_config}")
    if registry is not None:
        manifest = registry.get(target)
        hints = unit.get("hints")
        if hints is None:
            try:
                hints = recorded_hints(config_dir, target, unit)
            except BundleRefused as exc:
                problems.append(f"config manifest: {exc}")
        expected = ConfigRegistry._make_manifest(
            sf, target, _read_host_hints(), hint_overrides=frozen_hints(base, hints))
        if manifest is None or expected is None:
            problems.append("config manifest")
        else:
            have, need = manifest_hints(manifest), manifest_hints(expected)
            changed = sorted(k for k in need if have.get(k) != need[k])
            if changed:
                problems.append(f"config manifest {', '.join(changed)}")
    return problems


def _annotate_pending(config_dir, target, record, key, exc, problems):
    """Keep the descriptor ``pending`` and record why the outcome is unverified.
    The existing fsynced pending record already blocks; a failed annotation is
    attached to *exc*, never substituted for it."""
    try:
        write_descriptor(config_dir, target, {**record, key: {
            "error": f"{type(exc).__name__}: {exc}" if exc else None,
            "mismatches": problems}})
    except Exception as write_exc:                          # noqa: BLE001
        if exc is not None:
            exc.add_note(f"pending adoption annotation not written: {write_exc}")


def adopt_bundle(sf, registry, target: str, phys: str, *, expected_current: str,
                 config_dir=None) -> dict:
    """Swap generated *target* onto sealed bundle *phys*, all-or-nothing.

    Under the registration lock (also taken by run creation): compare the
    CURRENT whole-unit identity with ``expected_current`` and verify the target
    is registered exactly as that unit on every provider, fsync a before-change
    descriptor holding old+new bytes, publish through the existing validated
    registration transaction, then record an outcome only once ``whole_state``
    verifies it. Any unverified outcome (crash, failed rollback, mixed state)
    leaves the descriptor ``pending``: it blocks only this target at
    boot/launch until ``reconcile_adoption`` verifies a recorded whole bundle.
    """
    from core import pipeline_registry as pr
    from core.tool_guards import bad_config_name
    d = Path(config_dir or pr.generated_configs_dir())
    if (bad_config_name(target) or not target.startswith(pr.GEN_PREFIX)
            or is_sealed_name(target)):
        raise BundleRefused(f"adoption target {target!r} must be a generated config")
    with sf._lock:
        block = adoption_block(d, target)
        if block:
            raise BundleRefused(block)
        old = read_unit(d, target)
        current = unit_identity(old)
        if current != expected_current:
            raise BundleRefused("target changed: expected whole-bundle identity "
                                f"{expected_current[:12]} but current is {current[:12]}")
        new = adopted_unit(d, target, phys)
        new_id = unit_identity(new)
        if new_id == current:
            raise BundleRefused("adoption would not change the target")
        verify_providers(sf, verify_sealed(d, phys))
        old = {**old, "hints": recorded_hints(d, target, old)}
        mixed = whole_state(sf, registry, d, target, old)
        if mixed:
            raise BundleRefused(f"target is registered mixed ({', '.join(mixed)}); "
                                "reconcile first")
        # The old side's COMPLETE manifest, as verified just above: a rollback,
        # reconciliation or boot restores exactly this, not a re-derivation.
        if registry is not None:
            old["hints"] = manifest_hints(registry.get(target))
        record = {"schema": SCHEMA, "target": target, "sealed": phys,
                  "state": "pending", "expected_current": expected_current,
                  "old": {"identity": current, **old},
                  "new": {"identity": new_id, "graph_yaml": new["graph_yaml"],
                          "roles_text": new["roles_text"], "hints": new["hints"]}}
        write_descriptor(d, target, record)          # durable BEFORE any change
        try:
            _publish_unit(sf, registry, d, target, new, new["hints"])
        except BaseException as exc:
            # RegistrationTransaction attempted to restore memory, SQLite and
            # files and re-raises its own failure. Only the WHOLE old unit,
            # verified on every provider, may close the descriptor.
            try:
                problems = whole_state(sf, registry, d, target, old)
            except Exception as verify_exc:                 # noqa: BLE001
                problems = [f"verification failed: {type(verify_exc).__name__}: {verify_exc}"]
            if problems:
                _annotate_pending(d, target, record, "rollback_unverified", exc, problems)
            else:
                write_descriptor(d, target, {**record, "state": "rolled_back"})
            raise
        problems = whole_state(sf, registry, d, target, new)
        if problems:
            error = BundleRefused(f"adoption of {target!r} not verified "
                                  f"({', '.join(problems)}); reconcile first")
            _annotate_pending(d, target, record, "adoption_unverified", error, problems)
            raise error
        write_descriptor(d, target, {**record, "state": "adopted"})
        return {"target": target, "identity": new_id, "previous": current}


def reconcile_adoption(sf, registry, target: str, *, to: str, config_dir=None) -> dict:
    """Restore a crashed/mixed target to the RECORDED old or new whole bundle
    and clear the block only once ``whole_state`` verifies every provider."""
    from core import pipeline_registry as pr
    if to not in ("old", "new"):
        raise BundleRefused("reconcile target must be 'old' or 'new'")
    d = Path(config_dir or pr.generated_configs_dir())
    with sf._lock:
        desc = read_descriptor(d, target)
        if desc is None or desc.get("state") != "pending":
            raise BundleRefused(f"no pending adoption for {target!r}")
        side = desc[to]
        unit = {"graph_yaml": side["graph_yaml"], "roles_text": side["roles_text"],
                "hints": side.get("hints")}
        if unit_identity(unit) != side["identity"]:
            raise BundleRefused("recorded bundle bytes do not match their identity")
        _publish_unit(sf, registry, d, target, unit, side.get("hints"))
        problems = whole_state(sf, registry, d, target, unit)
        if problems:
            raise BundleRefused(f"reconciled {target!r} not verified "
                                f"({', '.join(problems)}); still pending")
        write_descriptor(d, target, {k: v for k, v in desc.items() if k != "digest"}
                         | {"state": f"reconciled_{to}"})
        return {"target": target, "identity": side["identity"], "to": to}
