"""Whole-pipeline frozen bundle: graph + resolved roles + template bytes.

(core/pipeline_bundle.py is the portable export/import format and keeps its
name; this module is the immutable RSI unit.) A graph version pin alone never
froze prompts or roles. ``capture`` reads the ACTUAL graph file, the ACTUAL
``sf.agent_registry`` entries the graph's ``agent_config`` names resolve to,
and the ACTUAL template bytes, and hashes all of them as one identity.
``compile_bundle`` rewrites that unit to private, bundle-specific physical
graph/role names with the template text embedded; ``seal`` persists it beside
the generated configs so the boot scan re-verifies and reloads it. Nothing
here runs a model or a test, or touches the original builtin config.
Adoption (``adopt_bundle``/``reconcile_adoption``) swaps a GENERATED target
onto bundle-specific role names inside ONE registration transaction, guarded by
a durable before-change descriptor that blocks boot/launch of that target
until reconciled.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
from pathlib import Path

import yaml

SCHEMA = 1
ROOT = Path(__file__).resolve().parents[1]
PREFIX = "gen_rsi_"
NAME_RE = re.compile(r"^gen_rsi_(.+)_([0-9a-f]{16})$")
_TARGET_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,31}$")
# ConfigRegistry.register_one hint key -> ConfigManifest attribute.
HINT_ATTRS = {
    "label": "label", "has_task_loop": "has_task_loop",
    "scheduler_owned": "scheduler_owned", "seed_file": "seed_file",
    "seed_default": "seed_default", "output_step": "output_step",
    "input_hint": "input_hint", "repo_mode": "repo_mode",
    "preamble_steps": "preamble_steps", "labels": "label_overrides",
    "checkpoint_kind": "checkpoint_kind",
    "manual_checkpoints_only": "manual_checkpoints_only",
    "checkpoint_kinds": "checkpoint_kinds"}
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


def _context_tools(node) -> set:
    """Tools named only as ``context: [{source: {tool: X}}]`` providers."""
    found = set()
    if isinstance(node, dict):
        src = node.get("source")
        if isinstance(src, dict) and isinstance(src.get("tool"), str):
            found.add(src["tool"])
        for value in node.values():
            found |= _context_tools(value)
    elif isinstance(node, list):
        for value in node:
            found |= _context_tools(value)
    return found


def _tool_identity(sf, name, tools_dir, host_names, context_only=False):
    gdir = Path(tools_dir) / name
    if gdir.exists() or gdir.is_symlink():
        files = {}
        for fname in ("tool.yaml", "impl.py", "README.md"):
            if (gdir / fname).exists() or (gdir / fname).is_symlink():
                files[fname] = sha(safe_file(gdir, fname).read_bytes())
        if not files:
            raise BundleRefused(f"generated tool {name!r} has no files")
        return {"kind": "generated", "files": files}
    loader = getattr(sf, "_tool_loader", None)
    if loader is not None and name in set(loader.list_tools()):
        digest = None
        try:
            src = inspect.getsourcefile(loader.load_fn(name))
            digest = sha(Path(src).read_bytes()) if src else None
        except Exception:                                   # noqa: BLE001
            digest = None
        return {"kind": "builtin", "sha256": digest}
    if name in host_names:
        return {"kind": "host-role-tool", "sha256": None}
    if context_only:
        return {"kind": "context-source", "sha256": None}
    raise BundleRefused(f"unresolved tool {name!r}")


def capture(sf, registry, name: str, *, template_base=None, configs_dirs=None,
            tools_dir=None) -> dict:
    """Freeze the complete logical unit of registered graph *name*."""
    from core import datadir, pipeline_registry as pr
    from core.config_registry import ConfigManifest
    from core.pipeline_bundle import referenced_tool_names

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
    roles, prompts = {}, {}
    sources = {"graph": {"path": gpath.name, "sha256": sha(raw)}}
    host_names = set()
    for _node, role in agent_refs(graph):
        if role in roles:
            continue
        if role not in sf.agent_registry:
            raise BundleRefused(f"unresolved role {role!r}")
        live = sf.agent_registry.get(role)
        d = live.to_dict()
        host_names |= set(getattr(live, "tool_schemas", None) or {})
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
    role_tools = {t for c in roles.values() for t in c["tools"]}
    ctx_only = _context_tools(graph) - role_tools
    tools = {n: _tool_identity(sf, n, tools_dir, host_names, n in ctx_only)
             for n in sorted(referenced_tool_names(graph, {
                 r: {"tools": c["tools"]} for r, c in roles.items()}))}
    manifest = registry.get(name) if registry is not None else None
    hints = ({k: copy.deepcopy(getattr(manifest, a))
              for k, a in HINT_ATTRS.items()}
             if isinstance(manifest, ConfigManifest) else {})
    parent = {"version": None, "digest": None}
    try:
        from skillflow.core import graph_digest
        rows = sf.list_graph_versions(name)
        parent = {"version": rows[0]["version"] if rows else None,
                  "digest": graph_digest(sf._graphs[name].to_dict())}
    except Exception:                                       # noqa: BLE001
        pass
    steps = [s for s in graph.get("steps") or [] if isinstance(s, dict)]
    body = {"schema": SCHEMA, "logical_name": name, "graph": graph,
            "roles": roles, "prompts": prompts, "sources": sources,
            "tools": tools, "hints": hints, "parent": parent,
            "derived_from": None,
            "inventory": {"steps": [s.get("id") for s in steps],
                          "outputs": {str(s.get("id")): s.get("output")
                                      for s in steps},
                          "capabilities": graph.get("capabilities"),
                          "tools": sorted(tools)}}
    return {**body, "identity": sha(body),
            "locations": {"graph": str(gpath), "template_base": str(base),
                          "tools_dir": str(tools_dir)}}


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
        if ident["kind"] == "generated":
            gdir = Path(loc["tools_dir"]) / name
            have = {f: sha(safe_file(gdir, f).read_bytes()) for f in ident["files"]}
            if have != ident["files"]:
                raise BundleRefused(f"tool {name!r} changed since capture")


# ── Candidate whitelist ────────────────────────────────────────────────────

_NON_MAKER = ("review", "verif", "check", "judge", "gate", "audit", "validat")
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


def _maker_roles(graph: dict) -> set:
    makers = set()
    for node, role in agent_refs(graph):
        label = (role + " " + str(node.get("id", ""))).lower()
        if not any(t in label for t in _NON_MAKER):
            makers.add(role)
    return makers


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
    makers = _maker_roles(body["graph"])
    for key, text in (edit.get("prompts") or {}).items():
        if key not in body["prompts"]:
            raise BundleRefused(f"unknown prompt {key!r}")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise BundleRefused(f"prompt {key!r} must be bounded nonempty text")
        users = [r for r, c in body["roles"].items() if c["prompt"] == key]
        if not users or any(r not in makers for r in users):
            raise BundleRefused(f"prompt {key!r} is not exclusively a maker prompt")
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
            "tools": bundle["tools"], "parent": bundle["parent"],
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
    graph, hints = pr._validated_registration(
        phys, sealed["graph_yaml"], roles=sealed["roles"], sf=sf)
    pr._commit_registration(sf, registry, phys, graph, sealed_hints(hints, sealed),
                            roles=sealed["roles"], require_manifest=True)
    return registry.get(phys)


def sealed_hints(hints: dict, sealed: dict) -> dict:
    """Frozen manifest hints win over generated-pipeline derivation."""
    return {**hints, **{k: v for k, v in (sealed.get("hints") or {}).items()
                        if v is not None}}


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
        sf, registry, target, graph, {**base, **{k: v for k, v in (hints or {}).items()
                                                if v is not None}},
        roles=roles, paths=[gp, rp], persist=persist, require_manifest=True)


def adopt_bundle(sf, registry, target: str, phys: str, *, expected_current: str,
                 config_dir=None) -> dict:
    """Swap generated *target* onto sealed bundle *phys*, all-or-nothing.

    Under the registration lock (also taken by the launcher): compare the
    CURRENT whole-unit identity with ``expected_current``, fsync a before-change
    descriptor holding old+new bytes, publish through the existing validated
    registration transaction, verify, then record the outcome. A crash leaves a
    ``pending`` descriptor that blocks only this target at boot/launch.
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
        old_roles = json.loads(old["roles_text"]) if old["roles_text"] else None
        live_graph, _ = pr._validated_registration(
            target, old["graph_yaml"], roles=old_roles, sf=sf)
        from skillflow.core import graph_digest
        live = sf._graphs.get(target)
        if live is None or graph_digest(live.to_dict()) != graph_digest(live_graph.to_dict()):
            raise BundleRefused("target is registered mixed/unregistered; reconcile first")
        record = {"schema": SCHEMA, "target": target, "sealed": phys,
                  "state": "pending", "expected_current": expected_current,
                  "old": {"identity": current, **old},
                  "new": {"identity": new_id, "graph_yaml": new["graph_yaml"],
                          "roles_text": new["roles_text"], "hints": new["hints"]}}
        write_descriptor(d, target, record)          # durable BEFORE any change
        try:
            _publish_unit(sf, registry, d, target, new, new["hints"])
        except BaseException:
            # RegistrationTransaction already restored files + live state; only
            # a VERIFIED restoration may close the descriptor.
            try:
                restored = unit_identity(read_unit(d, target)) == current
            except BundleRefused:
                restored = False
            if restored:
                write_descriptor(d, target, {**record, "state": "rolled_back"})
            raise
        write_descriptor(d, target, {**record, "state": "adopted"})
        return {"target": target, "identity": new_id, "previous": current}


def reconcile_adoption(sf, registry, target: str, *, to: str, config_dir=None) -> dict:
    """Restore a crashed/mixed target to the RECORDED old or new whole bundle
    (files + live registration, verified) and only then clear the block."""
    from core import pipeline_registry as pr
    if to not in ("old", "new"):
        raise BundleRefused("reconcile target must be 'old' or 'new'")
    d = Path(config_dir or pr.generated_configs_dir())
    with sf._lock:
        desc = read_descriptor(d, target)
        if desc is None or desc.get("state") != "pending":
            raise BundleRefused(f"no pending adoption for {target!r}")
        side = desc[to]
        unit = {"graph_yaml": side["graph_yaml"], "roles_text": side["roles_text"]}
        _publish_unit(sf, registry, d, target, unit, side.get("hints"))
        if unit_identity(read_unit(d, target)) != side["identity"]:
            raise BundleRefused("reconciled bytes do not match the recorded identity")
        write_descriptor(d, target, {k: v for k, v in desc.items() if k != "digest"}
                         | {"state": f"reconciled_{to}"})
        return {"target": target, "identity": side["identity"], "to": to}
