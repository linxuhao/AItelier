"""Thin r2 binding over the accepted native frozen-bundle provider.

Every effect goes through existing native code: ``capture`` (registered graph,
live AgentRegistry roles, actual prompt/template bytes, ToolLoader identities),
``compile_bundle``/``seal``/``verify_sealed`` (immutable filesystem seals),
``adopt_bundle`` (expected-current whole-unit CAS, before-change descriptor,
one RegistrationTransaction, retained old roles) and ``verify_providers``.

This module only projects native units to the logical content of README.md,
names seals by logical id, and maps native refusals to contract codes. It holds
no state besides the host objects it is given: no version, pin, run or
sequence ledger. The catalog is the set of verified seals on disk; run pins are
the SDK's own.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

import yaml

from . import contract as c
from core import pipeline_frozen_bundle as fb
from core import pipeline_registry as pr

_TAG_RE = re.compile(r"rsi[0-9a-f]{16}_")
_BUNDLE_SUFFIX = ".bundle.json"


def logical_role(own: str, physical: str) -> str:
    """Physical role name -> logical role name.

    Native naming is deterministic: registration namespaces ``<config>__<role>``,
    a seal uses ``<seal>__<role>`` and adoption ``<target>__rsi<16 hex>_<role>``
    (repeatedly, when an adopted unit is captured and sealed again)."""
    name = physical
    if name.startswith(own + pr._ROLE_SEP):
        name = name[len(own) + len(pr._ROLE_SEP):]
    while _TAG_RE.match(name):
        name = name[len("rsi") + 16 + 1:]
    return name


def _mentions_self(node: Any) -> bool:
    if isinstance(node, dict):
        return any(_mentions_self(k) or _mentions_self(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_mentions_self(v) for v in node)
    return node == c.SELF


def project(own: str, graph: Dict[str, Any], role_of: Callable[[str], Dict[str, Any]],
            tools: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """(logical content, logical id) of one native unit named *own*.

    Removes only physical naming: the graph's own name and its own-config
    context references (the scope ``pr._rewrite_self_config_refs`` renames)
    become ``$self``, and agent roles their logical names."""
    if not isinstance(graph, dict) or graph.get("name") != own:
        raise c.UnsupportedContent(f"unit graph is not named {own!r}")
    if _mentions_self(graph):
        raise c.UnsupportedContent(f"graph already uses reserved {c.SELF!r}")
    g = copy.deepcopy(graph)
    g.pop("x-aitelier", None)
    g["name"] = c.SELF
    for step in g.get("steps") or []:
        for ctx in (step.get("context") or []) if isinstance(step, dict) else []:
            inner = ctx.get("source", ctx) if isinstance(ctx, dict) else None
            if isinstance(inner, dict) and inner.get("config") == own:
                inner["config"] = c.SELF
    names: Dict[str, str] = {}
    for node, physical in list(fb.agent_refs(g)):
        logical = logical_role(own, physical)
        if not logical or names.setdefault(logical, physical) != physical:
            raise c.UnsupportedContent(f"role {physical!r} has no unique logical name")
        node["agent_config"] = logical
    content = {"graph": g, "roles": {n: role_of(p) for n, p in names.items()},
               "tools": copy.deepcopy(tools)}
    return content, c.version_id_of(content)


def _captured_role(bundle: Dict[str, Any]) -> Callable[[str], Dict[str, Any]]:
    def role_of(physical: str) -> Dict[str, Any]:
        r = bundle["roles"][physical]
        return {"model": r["model"], "tools": list(r["tools"]),
                "settings": copy.deepcopy(r["config"]),
                "prompt": bundle["prompts"][r["prompt"]] if r["prompt"] else None}
    return role_of


def _frozen_role(entries: Dict[str, Any]) -> Callable[[str], Dict[str, Any]]:
    """A ``_frozen`` role entry as registration then capture resolve it."""
    def role_of(physical: str) -> Dict[str, Any]:
        e = copy.deepcopy(entries[physical])
        if e.pop("_frozen", None) is not True or "template" in e:
            raise c.UnsupportedContent(f"role {physical!r} is not a frozen inline role")
        model, tools = e.pop("model", ""), e.pop("tools", None) or []
        prompt = e.pop("system_prompt", "") or None
        return {"model": model, "tools": list(tools), "settings": e, "prompt": prompt}
    return role_of


def project_capture(bundle: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    return project(bundle["logical_name"], bundle["graph"], _captured_role(bundle),
                   bundle["tools"])


def project_sealed(sealed: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    return project(sealed["physical_name"], yaml.safe_load(sealed["graph_yaml"]),
                   _frozen_role(sealed["roles"]), sealed["tools"])


def project_unit(target: str, unit: Dict[str, Any], tools: Dict[str, Any]):
    roles = json.loads(unit["roles_text"]) if unit["roles_text"] else {}
    return project(target, yaml.safe_load(unit["graph_yaml"]), _frozen_role(roles), tools)


def _classify(exc: fb.BundleRefused) -> c.VersioningError:
    """Native refusal -> contract code. Unclassified refusals stay loud."""
    msg = str(exc)
    if msg.startswith("not canonically serialisable"):
        err = c.MalformedContent(msg)
    elif msg.startswith(("unresolved tool", "unresolved capability")):
        err = c.ProviderUnavailable(msg)
    elif (msg.startswith(("tool ", "capability ")) or "serves a stale schema" in msg):
        err = c.ProviderChanged(msg)
    else:
        err = c.NativeRefused(msg)
    err.native = msg
    return err


class NativeVersions:
    """The r2 versioning API over one host (SkillFlow + ConfigRegistry) and the
    generated-config directory that holds its owned targets and seals."""

    def __init__(self, sf, registry, config_dir):
        self._sf = sf
        self._registry = registry
        self._dir = Path(config_dir)

    canonical_bytes = staticmethod(c.canonical_bytes)
    version_id_of = staticmethod(c.version_id_of)

    def _target(self, target: Any) -> str:
        c.check_target(target)
        gp = self._dir / f"{target}.yaml"
        if gp.is_symlink() or not gp.is_file() or target not in self._sf._graphs:
            raise c.UnknownTarget(f"{target!r} is not a registered generated target")
        return target

    def _capture_live(self, target: str):
        try:
            bundle = fb.capture(self._sf, self._registry, target, configs_dirs=[self._dir])
        except fb.BundleRefused as exc:
            raise _classify(exc) from exc
        return (bundle, *project_capture(bundle))

    def _live_id(self, target: str):
        try:
            return self._capture_live(target)[2]
        except c.VersioningError:
            return None

    def _verified(self, physical: str):
        try:
            sealed = fb.verify_sealed(self._dir, physical)
            content, vid = project_sealed(sealed)
        except (fb.BundleRefused, c.VersioningError) as exc:
            raise c.TamperedVersion(f"seal {physical} does not verify: {exc}") from exc
        if not c.physical_matches(physical, vid):
            raise c.TamperedVersion(f"seal {physical} holds {vid}, not its named version")
        return sealed, content, vid

    def _sealed(self, version_id: Any):
        c.check_version_id(version_id)
        prefix = c.SEAL_PREFIX + c.seal_slug(version_id) + "_"
        names = sorted(p.name[:-len(_BUNDLE_SUFFIX)]
                       for p in self._dir.glob(prefix + "*" + _BUNDLE_SUFFIX))
        names = [n for n in names if c.physical_matches(n, version_id)]
        if not names:
            raise c.UnknownVersion(f"no seal holds logical version {version_id}")
        found = [self._verified(n) for n in names]
        for sealed, _, vid in found:
            if vid != version_id:
                raise c.TamperedVersion(f"seal {sealed['physical_name']} holds {vid}")
        return names[0], found[0][0], found[0][1]

    def capture(self, target: Any) -> Dict[str, Any]:
        self._target(target)
        with fb.exclusion(self._sf):
            bundle, _, vid = self._capture_live(target)
            try:
                physical = self._sealed(vid)[0]
                return {"target": target, "version_id": vid, "created": False,
                        "physical_name": physical}
            except c.UnknownVersion:
                pass
            slug = c.seal_slug(vid)
            if project_sealed(fb.compile_bundle(bundle, slug))[1] != vid:
                raise c.UnsupportedContent("the sealed form would not reproduce this content")
            try:
                sealed = fb.seal(bundle, slug, self._dir)
            except fb.BundleRefused as exc:
                raise _classify(exc) from exc
            return {"target": target, "version_id": vid, "created": True,
                    "physical_name": sealed["physical_name"]}

    def list_versions(self):
        ids = set()
        for path in sorted(self._dir.glob(c.SEAL_PREFIX + "v*" + _BUNDLE_SUFFIX)):
            physical = path.name[:-len(_BUNDLE_SUFFIX)]
            if c.PHYSICAL_RE.fullmatch(physical):
                ids.add(self._verified(physical)[2])
        return sorted(ids)

    def read(self, version_id: Any) -> Dict[str, Any]:
        physical, sealed, content = self._sealed(version_id)
        native = {k: copy.deepcopy(sealed.get(k)) for k in (
            "physical_name", "bundle_identity", "digest", "logical_name", "target",
            "derived_from", "parent", "sources", "hints")}
        return {"version_id": version_id, "content": content, "native": native}

    def current(self, target: Any) -> Dict[str, Any]:
        self._target(target)
        rows = self._sf.list_graph_versions(target)
        return {"target": target,
                "unit_identity": fb.unit_identity(fb.read_unit(self._dir, target)),
                "graph_version": rows[0]["version"] if rows else None}

    def restore(self, target: Any, version_id: Any, *, expected_current: Any) -> Dict[str, Any]:
        c.check_version_id(version_id)
        c.check_target(target)
        c.check_unit_identity(expected_current)
        self._target(target)
        with fb.exclusion(self._sf):
            physical, sealed, _ = self._sealed(version_id)
            previous = fb.unit_identity(fb.read_unit(self._dir, target))
            if previous != expected_current:
                raise c.Conflict(f"target {target!r} is {previous[:12]}, "
                                 f"not expected {expected_current[:12]}")
            # Every tool, capability and the tool loader the version recorded
            # must still be served as recorded, for a no-op as for a change:
            # their native identities are outside the logical id, so an equal
            # live id says nothing about them.
            try:
                fb.verify_providers(self._sf, sealed)
            except fb.BundleRefused as exc:
                err = (c.ProviderUnavailable if str(exc).startswith("unresolved ")
                       else c.ProviderChanged)(str(exc))
                raise err from exc
            if self._live_id(target) == version_id:
                return {"target": target, "version_id": version_id, "changed": False,
                        "previous_unit_identity": previous, "unit_identity": previous}
            try:
                unit = fb.adopted_unit(self._dir, target, physical)
            except fb.BundleRefused as exc:
                raise _classify(exc) from exc
            if project_unit(target, unit, sealed["tools"])[1] != version_id:
                raise c.UnsupportedContent("adoption would not serve this logical content")
            try:
                done = fb.adopt_bundle(self._sf, self._registry, target, physical,
                                       expected_current=expected_current,
                                       config_dir=self._dir)
            except fb.BundleRefused as exc:
                if str(exc).startswith("target changed"):
                    raise c.Conflict(str(exc)) from exc
                raise _classify(exc) from exc
        return {"target": target, "version_id": version_id, "changed": True,
                "previous_unit_identity": done["previous"], "unit_identity": done["identity"]}
