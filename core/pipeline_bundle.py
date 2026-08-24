"""Export / import a generated pipeline as one portable document.

A pipeline that cannot leave the machine that made it is a pipeline nobody can
share. AItelier could already GENERATE one (`pipeline_forge`) and EDIT one
(`reload_generated_pipeline`), but its closure was scattered across three places
with no single handle on it:

    ~/.AItelier/configs/<config>.yaml        the graph
    ~/.AItelier/configs/<config>.roles.json  the roles, WITH their prompts inlined
    ~/.AItelier/tools/<tool>/…               any tool the forge built for it

The third is the one that makes this non-trivial. Tools are registered GLOBALLY by
name — nothing records which pipeline built which tool — so the closure has to be
recovered from the graph: every `tool_name`, every `validation.tool`, and every
tool named in a role's tool list, intersected with what actually exists under the
generated-tools directory. A built-in tool (`read_file`, `pytest`) is present
everywhere and is deliberately NOT bundled; shipping a copy would shadow the host's
own with a frozen one.

The bundle is JSON, not a zip, because it travels as an MCP tool result — a string.
Everything in the closure is text (YAML, JSON, Python), so nothing is lost.

── WHAT IMPORT HAS TO GET RIGHT ────────────────────────────────────────────────

**Renaming is not a string replace.** Roles are stored namespaced (`gen_x__author`)
and the graph's `agent_config` refs point at those names. Importing under a new
name has to re-namespace BOTH sides, exactly once. Prefixing without stripping the
old one yields `gen_y__gen_x__author`, which no longer matches the graph — and the
failure is silent: the role lookup misses and every step quietly falls back to the
generic host prompt, so the pipeline runs and produces confident garbage. That is a
live bug this host has already shipped once (`register_forge_pipeline` carries the
same defence for the forge's edit mode).

**An archive tombstone outlives the files.** `archive_generated_pipeline` records a
name in `_archived/archived.json`, and both the boot scan and `ConfigRegistry.build`
consult it. Writing the files back under an archived name produces a pipeline that
works until the next restart and then vanishes. Import calls `_unarchive` for the
same reason `register_forge_pipeline` does.

**A tool collision is not a merge.** Two pipelines can legitimately want a tool
called `fetch_prices`, and they need not be the same code. Import refuses to
overwrite an existing generated tool whose content differs, unless told to — the
alternative is poisoning a shared, globally-registered directory, which this host
has also already done once.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path

import yaml

BUNDLE_VERSION = 1
BUNDLE_KEY = "aitelier_pipeline_bundle"

# Files that make up one generated tool. Anything else in the directory (a
# __pycache__, a stray note) is not part of the contract and is not shipped.
_TOOL_FILES = ("tool.yaml", "impl.py", "README.md")


class BundleError(Exception):
    """The bundle is malformed, or importing it would destroy something."""


# ── Closure discovery ────────────────────────────────────────────────────────

def referenced_tool_names(graph: dict, roles: dict | None = None) -> set[str]:
    """Every tool name this pipeline could invoke, from all four places it can hide.

    Deliberately over-collects: a name that is not a generated tool is dropped
    later by the directory check, whereas a name missed here ships a bundle that
    imports cleanly and fails at the step that needs the tool.
    """
    names: set[str] = set()
    for step in (graph.get("steps") or []):
        if not isinstance(step, dict):
            continue
        if step.get("tool_name"):
            names.add(str(step["tool_name"]))
        for v in (step.get("validation") or []):
            if isinstance(v, dict) and v.get("tool"):
                names.add(str(v["tool"]))
        # `{source: {tool: X}}` context entries invoke a tool to build context.
        for c in (step.get("context") or []):
            src = c.get("source") if isinstance(c, dict) else None
            if isinstance(src, dict) and src.get("tool"):
                names.add(str(src["tool"]))
        for hook in (step.get("lifecycle") or {}).values() if isinstance(
                step.get("lifecycle"), dict) else []:
            for h in (hook if isinstance(hook, list) else [hook]):
                if isinstance(h, dict) and h.get("tool"):
                    names.add(str(h["tool"]))
    for cfg in (roles or {}).values():
        for t in (cfg or {}).get("tools") or []:
            names.add(str(t))
    return {n for n in names if n}


def _generated_tools_dir() -> Path:
    from core import datadir
    return datadir.tools_dir()


def _tool_name_error(name) -> str:
    """Non-empty when *name* is not usable as a generated tool's directory name.

    A tool IS its directory name — the loader keys tools by it — so a name carrying
    a path separator could never be invoked anyway. The reason to check it here
    rather than shrug is `tdir / name`: a bundle arrives from ANOTHER machine (that
    is the whole feature), and a key like `../../AItelier/aitelier/tools/web_search`
    makes the importer write impl.py OUTSIDE the generated-tools directory, on top
    of a built-in tool this host imports and executes. Importing a shared pipeline
    is consent to install a tool, not to overwrite arbitrary files. Same rule
    `edit_tool` already applies to a caller-supplied tool name.
    """
    if not isinstance(name, str) or not name:
        return "a tool name must be a non-empty string"
    if "/" in name or name.startswith("."):
        return (f"invalid tool name {name!r}: a generated tool is one plain "
                f"directory name under the generated-tools directory")
    return ""


def _read_tool(name: str) -> dict[str, str] | None:
    """Read one generated tool's files, or None when it is not a generated tool."""
    if _tool_name_error(name):
        return None                    # a traversing name names no generated tool
    d = _generated_tools_dir() / name
    if not d.is_dir():
        return None
    files = {f: (d / f).read_text(encoding="utf-8")
             for f in _TOOL_FILES if (d / f).is_file()}
    return files or None


# ── Export ───────────────────────────────────────────────────────────────────

def export_pipeline(config_name: str) -> dict:
    """Collect a generated pipeline's whole closure into one JSON-able document."""
    from core import pipeline_registry as pr

    cdir = pr.generated_configs_dir()
    gpath = cdir / f"{config_name}.yaml"
    if not gpath.is_file():
        raise BundleError(
            f"'{config_name}' is not a generated pipeline (no {gpath.name}). Only "
            f"generated pipelines can be exported; a built-in config lives in the "
            f"repo and travels with it.")

    graph_yaml = gpath.read_text(encoding="utf-8")
    graph = yaml.safe_load(graph_yaml) or {}

    rpath = cdir / f"{config_name}.roles.json"
    roles = {}
    if rpath.is_file():
        try:
            roles = json.loads(rpath.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError as e:
            raise BundleError(f"{rpath.name} is not valid JSON: {e}") from e

    tools: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for name in sorted(referenced_tool_names(graph, roles)):
        files = _read_tool(name)
        if files:
            tools[name] = files
    # A tool the graph names, that is neither built-in nor generated, would import
    # as a pipeline that cannot run. Say so at export time, where the author can
    # still fix it, rather than at the importer's first run.
    from api.dependencies import get_tool_loader
    try:
        known = set(get_tool_loader().list_tools())
    except Exception:
        known = set()
    if known:
        missing = sorted(n for n in referenced_tool_names(graph, roles)
                         if n not in known and n not in tools)

    return {
        BUNDLE_KEY: BUNDLE_VERSION,
        "config_name": config_name,
        "exported_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "graph_yaml": graph_yaml,
        "roles": roles,
        "tools": tools,
        "unresolved_tools": missing,
    }


# ── Import ───────────────────────────────────────────────────────────────────

def _renamespace(graph: dict, roles: dict, old: str, new: str) -> tuple[dict, dict]:
    """Move roles and their graph references from `old`'s namespace into `new`'s.

    Strip-then-prefix, never prefix-alone: see the module docstring. Idempotent, so
    re-importing under the same name is a no-op rather than a second prefix.
    """
    from core.pipeline_registry import _ROLE_SEP
    old_p, new_p = old + _ROLE_SEP, new + _ROLE_SEP

    def rebase(role: str) -> str:
        bare = role[len(old_p):] if role.startswith(old_p) else role
        bare = bare[len(new_p):] if bare.startswith(new_p) else bare
        return new_p + bare

    new_roles = {rebase(str(r)): cfg for r, cfg in (roles or {}).items()}
    for step in (graph.get("steps") or []):
        if isinstance(step, dict) and isinstance(step.get("agent_config"), str):
            step["agent_config"] = rebase(step["agent_config"])
    return graph, new_roles


def _digest(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode()); h.update(b"\0")
        h.update(files[name].encode()); h.update(b"\0")
    return h.hexdigest()[:16]


def validate_bundle(bundle: dict) -> None:
    """Reject a document that is not a bundle, before it touches the filesystem."""
    if not isinstance(bundle, dict):
        raise BundleError("bundle must be a JSON object")
    version = bundle.get(BUNDLE_KEY)
    if version is None:
        raise BundleError(f"not an AItelier pipeline bundle (no '{BUNDLE_KEY}' key)")
    if version != BUNDLE_VERSION:
        raise BundleError(
            f"bundle format v{version}, this host reads v{BUNDLE_VERSION}")
    if not isinstance(bundle.get("graph_yaml"), str) or not bundle["graph_yaml"].strip():
        raise BundleError("bundle carries no graph_yaml")
    if not isinstance(bundle.get("roles", {}), dict):
        raise BundleError("bundle 'roles' is not an object")
    if not isinstance(bundle.get("tools", {}), dict):
        raise BundleError("bundle 'tools' is not an object")
    try:
        graph = yaml.safe_load(bundle["graph_yaml"])
    except yaml.YAMLError as e:
        raise BundleError(f"graph_yaml is not valid YAML: {e}") from e
    if not isinstance(graph, dict) or not graph.get("steps"):
        raise BundleError("graph_yaml has no steps")


def import_pipeline(sf, registry, bundle: dict, *, name: str | None = None,
                    overwrite_tools: bool = False) -> dict:
    """Write a bundle's closure to disk and register it live.

    `name` renames on the way in (the bundle's own name is used otherwise).
    Nothing is written until every check has passed: a half-imported pipeline —
    graph on disk, tool refused — is worse than a refused one, because it looks
    installed.
    """
    from core import pipeline_registry as pr
    from skillflow.graph import PipelineGraph

    validate_bundle(bundle)
    src_name = str(bundle.get("config_name") or "")
    config_name = pr.config_name_for(name) if name else src_name
    if not config_name:
        raise BundleError("bundle has no config_name and no name was given")
    # `register_graph` is INSERT OR REPLACE by name, so a bundle that simply calls
    # itself `dpe_default_v2` replaces the flagship built-in graph — live, and on
    # disk in the generated dir where the boot scan finds it again. The whole
    # registry rests on "generated and built-in keyspaces are disjoint by
    # construction, so there is no reserved-name blocklist"; export, edit and
    # archive all hold that line, and import is the one door left open. Renaming
    # (`name=`) already forces `gen_<slug>`; this is the same rule for the
    # bundle's own claim.
    if not config_name.startswith(pr.GEN_PREFIX):
        raise BundleError(
            f"this bundle calls itself '{config_name}', which is not a generated "
            f"pipeline name. Importing it would replace a built-in config of that "
            f"name. Re-run with name=<something> to install it as "
            f"{pr.GEN_PREFIX}<slug>.")

    graph = yaml.safe_load(bundle["graph_yaml"]) or {}
    roles = dict(bundle.get("roles") or {})
    if src_name and config_name != src_name:
        pr._rewrite_self_config_refs(graph, config_name)
    # Namespacing runs on EVERY import, not only on a rename. Roles are registered
    # GLOBALLY by key: a bundle written by hand (or by a host that never namespaced)
    # carrying a bare `researcher` would overwrite DPE's own researcher agent config
    # for the whole process. Idempotent, so an already-namespaced bundle imported
    # under its own name is unchanged.
    graph, roles = _renamespace(graph, roles, src_name or config_name, config_name)
    graph["name"] = config_name

    # ── Decide every tool BEFORE writing anything ──
    tdir = _generated_tools_dir()
    to_write: dict[str, dict[str, str]] = {}
    conflicts: list[str] = []
    for tname, files in (bundle.get("tools") or {}).items():
        err = _tool_name_error(tname)
        if err:
            raise BundleError(err)
        if not isinstance(files, dict) or not files:
            raise BundleError(f"tool '{tname}' carries no files")
        # Checked HERE, not in the commit loop: a bad filename found while writing
        # leaves half a tool directory on disk, and the retry then fails the digest
        # check ("already exist with DIFFERENT content") against the debris of the
        # first attempt — bricking the import even with overwrite_tools=true.
        for fname in files:
            if fname not in _TOOL_FILES:
                raise BundleError(f"tool '{tname}' carries unexpected file {fname!r}")
        existing = _read_tool(tname)
        if existing is None:
            to_write[tname] = files
        elif _digest(existing) == _digest(files):
            continue                       # identical, already installed
        elif overwrite_tools:
            to_write[tname] = files
        else:
            conflicts.append(tname)
    if conflicts:
        raise BundleError(
            "these tools already exist with DIFFERENT content: "
            + ", ".join(sorted(conflicts))
            + ". Tools are registered globally by name, so importing would change "
              "them for every pipeline that uses them. Re-run with "
              "overwrite_tools=true only if that is what you mean.")

    yaml_text = yaml.safe_dump(graph, sort_keys=False, allow_unicode=True)
    try:
        parsed = PipelineGraph._from_dict(yaml.safe_load(yaml_text))
    except Exception as e:
        raise BundleError(f"bundle's graph is not a valid pipeline: {e}") from e
    # `_from_dict` only BUILDS the object — a dangling transition target, a missing
    # begin node, an unreachable step all survive it. The check that catches those
    # is `validate()`, and it used to run only inside `sf.register_graph` below,
    # i.e. after the graph, the roles and the tools were already on disk and the
    # archive tombstone was lifted. That left a broken pipeline installed, which
    # every boot scan then picked up, and the raw GraphValidationError escaped past
    # the MCP layer's `except BundleError`.
    issues = parsed.validate()
    if issues:
        raise BundleError("bundle's graph is not a valid pipeline: "
                          + "; ".join(issues))

    existed = registry.get(config_name) is not None
    # Registering live BEFORE writing: this is where the remaining checks live
    # (`register_graph` re-validates and rejects unresolved agent_config refs), and
    # a failure here must leave the disk untouched. The reverse half-state — live
    # but not persisted — costs nothing: it is gone at the next restart, whereas a
    # half-written config outlives every restart.
    try:
        pr._register_forge_roles(sf, config_name, roles)
        pr.ensure_host_agents(sf, parsed)
        sf.register_graph(parsed)
    except Exception as e:
        raise BundleError(f"bundle's graph was rejected: {e}") from e

    # ── Commit ──
    for tname, files in to_write.items():
        d = tdir / tname
        d.mkdir(parents=True, exist_ok=True)
        for fname, text in files.items():
            (d / fname).write_text(text, encoding="utf-8")

    cdir = pr.generated_configs_dir()
    (cdir / f"{config_name}.yaml").write_text(yaml_text, encoding="utf-8")
    if roles:
        (cdir / f"{config_name}.roles.json").write_text(
            json.dumps(roles, ensure_ascii=False, indent=2), encoding="utf-8")
    # Writing the files IS the intent to have this pipeline; a stale tombstone
    # would delete it again at the next boot scan.
    pr._unarchive(config_name)
    registry.register_one(sf, config_name,
                          hint_overrides=pr._gen_hints(parsed, roles, config_name))

    return {
        "config_name": config_name,
        "action": "updated" if existed else "created",
        "renamed_from": src_name if src_name != config_name else None,
        "roles": sorted(roles),
        "tools_installed": sorted(to_write),
        "path": str(cdir / f"{config_name}.yaml"),
    }
