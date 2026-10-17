"""Finite execution policy of the RSI evaluator pilot.

Compiles what a cell run WOULD actually execute -- the physical config's served
graph, its served role, its manifest, the context sources and their tool
providers, the write/read/host tools the engine will advertise, hooks,
templates, capabilities and implicit read roots -- before registration, launch,
claim or advance can have an effect. Only the single-maker ``investigate``
shape is supported; every other shape refuses (``PolicyRefused``) rather than
being approximated by a general sandbox.

The result is the contract's ``CompiledPolicy``. ``admits`` answers the host's
claim/advance ingress for an owned cell run (pinned graph and served role must
still be the compiled ones), ``grant`` answers each real tool dispatch: a
missing run, step, class or an empty grant denies every invocation.
"""
from __future__ import annotations

import copy
from pathlib import Path

from contracts.rsi.evaluator.contract import Selection, digest
from contracts.rsi.evaluator.providers import CompiledPolicy, InvocationGrant, ProviderIdentity

# Engine-injected host-level step-control tools (core/dpe_pipeline.py
# _run_native_step): advertised to the model, answered by the engine itself.
HOST_TOOLS = ("ask_more_turns", "recall_observation")
CLASSES = ("agent", "context_source", "tool_node", "validation", "after_deliver", "capability")
_ROLE_KEYS = {"model", "tools", "system_prompt", "temperature", "max_output_tokens",
              "max_tool_turns", "native_tool_calling", "fallback_to_json_mode", "thinking",
              "_frozen"}
_STEP_KEYS = {"id", "step_type", "agent_config", "timeout_seconds", "context", "output_mode",
              "output", "transitions"}
_GRAPH_KEYS = {"name", "begin", "steps", "description", "end_conditions"}
# The SDK's normalized context source (PipelineGraph.to_dict) with its
# defaults; a source must equal one of the pilot forms exactly.
_SOURCE_DEFAULTS = {"files": [], "path": "", "order": [], "step_id": "", "config_name": "",
                    "required": False, "scope": "task"}


class PolicyRefused(ValueError):
    """The effective configuration is outside the finite pilot policy."""


def _need(condition, message):
    if not condition:
        raise PolicyRefused(message)


def sdk_digest(value: str) -> str:
    """SDK graph digests are already ``sha256:<hex>``; never re-prefix one."""
    value = value or ""
    return value if value.startswith("sha256:") else "sha256:" + value


def _overlaps(a: Path, b: Path) -> bool:
    a, b = Path(a).resolve(), Path(b).resolve()
    return a == b or a in b.parents or b in a.parents


def _role_config(sf, name: str) -> dict:
    _need(name in sf.agent_registry, f"role {name!r} is not served")
    d = sf.agent_registry.get(name).to_dict()
    cfg = copy.deepcopy(d.get("config") or {})
    cfg.setdefault("model", d.get("model"))
    cfg.setdefault("tools", list(d.get("tools") or []))
    return cfg


def _served_graph(sf, phys: str) -> tuple[dict, int, str]:
    graph = sf._graphs.get(phys)
    _need(graph is not None, f"{phys!r} is not a registered graph")
    rows = sf.list_graph_versions(phys)
    _need(bool(rows), f"{phys!r} has no registered SDK graph version")
    return graph.to_dict(), rows[0]["version"], rows[0]["digest"]


def _context_forms(step: dict, phys: str, seed_file: str, context_tools) -> list:
    forms = []
    for src in step.get("context") or []:
        _need(isinstance(src, dict), "context source must be a mapping")
        kind = src.get("source_type")
        if kind == "repository":
            want = {**_SOURCE_DEFAULTS, "from": "repository", "mode": "tool",
                    "source_type": "repository"}
            _need(src == want, f"unsupported repository source {src!r}")
            forms.append(("repository", None))
        elif kind == "config":
            want = {**_SOURCE_DEFAULTS, "config": phys, "config_name": phys, "output": seed_file,
                    "files": [seed_file], "mode": "both", "source_type": "config"}
            _need(src == want, f"cross-config/run or non-seed source refused {src!r}")
            forms.append(("seed", seed_file))
        elif kind == "tool":
            _need(src.get("tool") in context_tools, f"context source tool {src.get('tool')!r} refused")
            want = {**_SOURCE_DEFAULTS, "tool": src.get("tool"), "mode": "inline",
                    "source_type": "tool"}
            _need(src == want, f"unsupported tool source {src!r}")
            forms.append(("tool", src["tool"]))
        else:
            raise PolicyRefused(f"unsupported context source {src!r}")
    _need(len(set(forms)) == len(forms), "duplicate context source")
    _need({k for k, _ in forms} == {"repository", "seed", "tool"},
          "the pilot needs exactly its public repository, seed and source-tool context")
    return forms


def compile_selection(sf, registry, selection: Selection, sealed: dict, *,
                      requested_model: str, role_tools=("list_tree",),
                      context_tools=("dir_tree",)) -> dict:
    """Compile one arm's served physical config. Raises PolicyRefused."""
    from skillflow.read_tools import get_read_tool_names
    from skillflow.write_tools import generate_write_tool_schemas
    from core import pipeline_frozen_bundle as fb

    phys = selection.physical_config
    graph, version, gdigest = _served_graph(sf, phys)
    manifest = registry.get(phys) if registry is not None else None
    _need(manifest is not None, f"{phys!r} has no served manifest")
    # A generated config's manifest is DERIVED by the host (pipeline_registry
    # GEN_HINTS/_gen_hints): repo_mode "code" for any repository source,
    # scheduler_owned, seed "seed_input.md". The pilot neither writes code nor
    # takes another driver, so both are enforced by the evaluator instead: no
    # code-target output below, read-snapshot isolation at launch, and a single
    # coordinator thread at the host ingress. The seed the evaluator publishes
    # is the file the graph's own seed source reads.
    _need(getattr(manifest, "repo_mode", None) in ("none", "code"), "unknown repo mode")

    _need(set(graph) <= _GRAPH_KEYS, f"graph fields outside the pilot: {sorted(set(graph) - _GRAPH_KEYS)}")
    steps = list(graph.get("steps") or [])
    _need(len(steps) == 1, "the pilot is exactly one step")
    step = steps[0]
    _need(set(step) <= _STEP_KEYS,
          f"step fields outside the finite pilot: {sorted(set(step) - _STEP_KEYS)}")
    _need(step.get("step_type") == "agent", "the pilot step is an agent step")
    _need(graph.get("begin") == step["id"], "the pilot begins at its step")
    _need(graph.get("end_conditions") == {"combinator": "or", "conditions": [
        {"type": "node_reached", "node": step["id"], "require_completed": True}]},
        "the pilot ends when its step completes")
    _need(step.get("output_mode") == "content", "pilot output is one content-mode artifact slot")
    out = step.get("output") or {}
    _need(set(out) <= {"fixed", "target"} and out.get("target", "artifact") == "artifact",
          "pilot output targets its artifact slot only")
    fixed = out.get("fixed") or {}
    _need(isinstance(fixed, dict) and len(fixed) == 1, "exactly one output slot")
    slot, filename = next(iter(fixed.items()))
    _need(filename == "findings.md", "the pilot output interface is findings.md")
    _need(step.get("transitions") == [{"to": None}], "single terminal transition")
    seeds = [src.get("output") for src in step.get("context") or []
             if isinstance(src, dict) and src.get("source_type") == "config"]
    _need(len(seeds) == 1 and isinstance(seeds[0], str) and "/" not in seeds[0]
          and seeds[0].endswith(".md"), "the pilot reads exactly one public markdown seed")
    seed_file = seeds[0]
    forms = _context_forms(step, phys, seed_file, context_tools)

    role_name = step["agent_config"]
    role = _role_config(sf, role_name)
    _need(set(role) <= _ROLE_KEYS, f"role settings outside the pilot: {sorted(set(role) - _ROLE_KEYS)}")
    _need(not role.get("template") and isinstance(role.get("system_prompt"), str),
          "role must be an inline (frozen) role")
    _need(role.get("model") == requested_model, "role model is not the frozen profile model")
    _need(set(role.get("tools") or []) <= set(role_tools), "role tools outside the grantable set")
    _need(role.get("native_tool_calling") is True, "native tool calling required")

    write_names = [w["name"] for w in generate_write_tool_schemas(
        "content", fixed, allow_full_write=False, carry_forward=False, output_target="artifact")]
    read_names = sorted(get_read_tool_names(step.get("context") or []))
    agent_tools = sorted(set(role.get("tools") or []) | set(write_names) | set(read_names)
                         | set(HOST_TOOLS))
    source_tools = sorted(t for k, t in forms if k == "tool")

    loader_tools = {}
    for name in sorted(set(role.get("tools") or []) | set(source_tools)):
        ident = fb._tool_identity(sf, name)[0]
        want = (sealed.get("tools") or {}).get(name)
        if want is not None and ident != want:
            raise PolicyRefused(f"served tool {name!r} differs from the sealed provider")
        loader_tools[name] = digest(ident)
    return {
        "selection": selection, "physical": phys, "graph": graph,
        "graph_version": version, "graph_digest": sdk_digest(gdigest),
        "seed_file": seed_file, "manifest": {"repo_mode": manifest.repo_mode,
                                             "scheduler_owned": manifest.scheduler_owned,
                                             "seed_file": manifest.seed_file,
                                             "output_step": manifest.output_step},
        "step": step["id"], "role": role_name, "role_digest": digest(role),
        "slot": slot, "output": filename, "agent_tools": agent_tools,
        "source_tools": source_tools, "loader_tools": loader_tools,
        "sdk_tools": sorted(set(write_names) | set(read_names)),
    }


def compile_policy(sf, registry, selections, sealed_by_arm: dict, *, grants, requested_model,
                   public_root, private_roots, public_fixture: str) -> tuple[CompiledPolicy, dict]:
    """Compile every selected arm, intersect with host grants, check roots."""
    _need(grants is not None, "missing evaluator grants: every tool is refused")
    agent_grant = set(grants.get("agent") or ())
    source_grant = set(grants.get("context_source") or ())
    _need(agent_grant and source_grant, "empty evaluator grants: every tool is refused")
    for cls in CLASSES[2:]:
        _need(not grants.get(cls), f"invocation class {cls} is never granted in the pilot")
    for root in private_roots:
        _need(not _overlaps(public_root, root),
              f"public fixture root {public_root} overlaps private checker material")
    compiled = {}
    for selection in selections:
        arm = compile_selection(sf, registry, selection, sealed_by_arm[selection.arm],
                                requested_model=requested_model)
        missing = set(arm["agent_tools"]) - agent_grant
        _need(not missing, f"tools {sorted(missing)} would be dispatched without a grant")
        missing = set(arm["source_tools"]) - source_grant
        _need(not missing, f"context tools {sorted(missing)} would run without a grant")
        compiled[selection.arm] = arm

    grant_rows = []
    for arm in compiled.values():
        providers = tuple(ProviderIdentity(name=n, digest=arm["loader_tools"].get(n)
                                           or digest(["skillflow-step-scoped-or-host", n]))
                          for n in arm["agent_tools"])
        grant_rows.append(InvocationGrant(step=f"{arm['physical']}:{arm['step']}", path="agent",
                                          providers=providers,
                                          public_read_roots=(public_fixture,),
                                          artifact_write_slots=(arm["slot"],)))
        grant_rows.append(InvocationGrant(step=f"{arm['physical']}:{arm['step']}",
                                          path="context_source",
                                          providers=tuple(ProviderIdentity(
                                              name=n, digest=arm["loader_tools"][n])
                                              for n in arm["source_tools"]),
                                          public_read_roots=(public_fixture,),
                                          artifact_write_slots=()))
        for cls in CLASSES[2:]:
            grant_rows.append(InvocationGrant(step=f"{arm['physical']}:{arm['step']}", path=cls,
                                              providers=(), public_read_roots=(),
                                              artifact_write_slots=()))
    body = dict(graph=digest([[a, c["graph_digest"]] for a, c in sorted(compiled.items())]),
                selections=tuple(selections), grants=tuple(grant_rows),
                effective_templates=tuple(digest(sealed_by_arm[s.arm].get("prompts") or {})
                                          for s in selections),
                implicit_self_roots=(digest(["own-step-staging-and-promoted-output-only"]),),
                implicit_repo_roots=(public_fixture,), public_fixture=public_fixture,
                output_interface=("findings.md",))
    policy = CompiledPolicy(identity=digest(body), **body)
    return policy, compiled


class CellPolicy:
    """Ingress/dispatch answers for one owned cell run (trusted, in-process)."""

    def __init__(self, policy: CompiledPolicy, arm: dict):
        self.policy = policy
        self.arm = arm

    def grant(self, step_id: str, path: str) -> frozenset:
        key = f"{self.arm['physical']}:{step_id}"
        for row in self.policy.grants:
            if row.step == key and row.path == path:
                return frozenset(p.name for p in row.providers)
        return frozenset()

    def dispatch(self, sf, step_id: str, name: str) -> str | None:
        """Why this agent tool call may not run now, or None: exact membership
        of (run, step, agent) and, for a loader-served tool, its provider
        identity re-checked at the call itself."""
        from core import pipeline_frozen_bundle as fb
        if name not in self.grant(step_id, "agent"):
            return "not granted"
        want = self.arm["loader_tools"].get(name)
        if want is not None:
            try:
                if digest(fb._tool_identity(sf, name)[0]) != want:
                    return f"provider of {name!r} changed since compilation"
            except fb.BundleRefused as exc:
                return f"provider of {name!r} unavailable: {exc}"
        return None

    def admits(self, sf, run: dict) -> str | None:
        """Why this owned run may not be claimed/advanced now, or None."""
        from core import pipeline_frozen_bundle as fb
        if run.get("graph_name") != self.arm["physical"]:
            return "cell run is not of the compiled physical config"
        if run.get("graph_version") != self.arm["graph_version"]:
            return "cell run is pinned to a graph version other than the compiled one"
        if sdk_digest(run.get("graph_digest")) != self.arm["graph_digest"]:
            return "cell run is pinned to graph content other than the compiled one"
        try:
            role = _role_config(sf, self.arm["role"])
        except PolicyRefused as exc:
            return str(exc)
        if digest(role) != self.arm["role_digest"]:
            return "served role differs from the compiled role"
        for name, want in self.arm["loader_tools"].items():
            try:
                if digest(fb._tool_identity(sf, name)[0]) != want:
                    return f"tool provider {name!r} changed since compilation"
            except fb.BundleRefused as exc:
                return f"tool provider {name!r} unavailable: {exc}"
        return None
