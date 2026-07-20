"""Config registry — makes skillflow graphs ("configs") first-class, queryable data.

AItelier no longer hardcodes a single DPE pipeline. Every skillflow graph the host
registers (DPE, the requirements conversation, the skill→pipeline converter, and any
config a developer drops into ``configs/*.yaml``) gets a :class:`ConfigManifest` that
the scheduler, API and dashboards read to drive and render runs generically.

A manifest is part DERIVED from the graph (step ids, display labels, checkpoint
locations) and part DECLARED by the config author in an ``x-aitelier:`` block at the
top of the YAML (or, for framework-owned graphs that ship without one, in
:data:`_EXTERNAL_HINTS`). Declared because skillflow's ``StepNode`` carries no notion
of "is this config scheduler-driven?", "does it have a task loop?", or "is this
checkpoint a file review or a conversation?" — those are host concerns.

Declared keys (all optional; defaults in :data:`_DEFAULTS`):
    label            human-readable config name for dashboards
    scheduler_owned  True  → the polling scheduler drives runs of this config
                     False → runs are driven imperatively by the butler
    has_task_loop    True  → DPE-style per-task loop (tasks table, manifest sync)
    seed_file        workspace file the start path writes the seed input into
    output_step      step id whose output is the run's final artifact
    checkpoint_kind  default kind for every checkpoint ("file-review"|"conversational")
    checkpoint_kinds per-step override map {step_id: kind}
    labels           per-step display labels {step_id: label} (overrides node.name)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"

# Defaults for any declared key the config author omits.
_DEFAULTS: dict = {
    "label": None,
    "scheduler_owned": True,
    "has_task_loop": False,
    "seed_file": None,
    "output_step": None,
    "registers_generated_pipeline": False,
    "registers_generated_addon": False,
    "repo_mode": "code",
    "preamble_steps": [],
    "checkpoint_kind": "file-review",
    "checkpoint_kinds": {},
    "labels": {},
}  # NB: steps/labels/checkpoints/description are NOT here — derived from the graph.

# Host-declared hints for framework-owned graphs registered from the skillflow
# package (their YAML lives in the library and carries no x-aitelier block).
_EXTERNAL_HINTS: dict[str, dict] = {
    "skill_converter": {
        "label": "Skill → Pipeline",
        "scheduler_owned": False,
        "seed_file": "skill_description.md",
        "output_step": "done",
        "registers_generated_pipeline": True,
        "repo_mode": "none",
    },
    "addon_converter": {
        "label": "Capability → Addon",
        "scheduler_owned": False,
        "seed_file": "addon_description.md",
        "output_step": "done",
        "registers_generated_addon": True,
        "repo_mode": "none",
    },
}


@dataclass
class ConfigManifest:
    """Everything the host needs to schedule and render runs of one config.

    DECLARED fields (``label``, ``scheduler_owned``, ``seed_file`` …) come from
    the config's ``x-aitelier:`` block and are stored here. DERIVED views
    (``steps``, ``labels``, ``checkpoints``, ``description``) are computed live
    from the graph via ``graph_provider`` — never cached — so a re-registered
    graph is reflected immediately and there is no duplicated step/label state
    that can fall stale.
    """

    config_name: str
    graph_provider: Callable[[], Any]   # returns the live PipelineGraph for this config
    label: str = ""
    has_task_loop: bool = False
    scheduler_owned: bool = True
    seed_file: str | None = None
    output_step: str | None = None
    # Self-description for the butler's pipeline catalog: what SEED this config
    # expects, in what shape. A human "what it does" line is derived from the
    # graph's `description:`; this is the machine-actionable input contract, so
    # the driver doesn't guess the seed (e.g. code_review needs the verbatim
    # git diff, not a summary of it). Declared in the x-aitelier block.
    input_hint: str = ""
    # When True, a completed run of this config emits a pipeline YAML the host
    # should register as a runnable config (skill_converter). Lets the meta agent's
    # completion handler stay generic instead of special-casing the graph name.
    registers_generated_pipeline: bool = False
    # When True, a completed run of this config emits an addon OVERLAY the host
    # should register (addon_converter). Parallel to registers_generated_pipeline,
    # so the meta agent's completion handler stays generic.
    registers_generated_addon: bool = False
    # Does a run of this config produce/modify code in a git repo? "code" (the
    # default) gets the usual repo workspace; "none" gets a repo-less one (no
    # repo_path, no throwaway projects/<id>/.git). DECLARED, not inferred from
    # the config's identity: authoring converters and generated pipelines are
    # both often repo-less, but for unrelated reasons — conflating them is what
    # gave test-drives of a generated pipeline a fake empty repo.
    repo_mode: str = "code"
    # Step ids whose outputs are project-global & stable (e.g. SOTA/architecture).
    # The host hoists these into the shared system preamble for prompt caching.
    preamble_steps: list[str] = field(default_factory=list)
    # Declared rendering hints — inputs used to DERIVE labels/checkpoints below.
    label_overrides: dict[str, str] = field(default_factory=dict)   # step_id -> label
    checkpoint_kind: str = "file-review"
    checkpoint_kinds: dict[str, str] = field(default_factory=dict)  # step_id -> kind

    @property
    def _graph(self):
        return self.graph_provider()

    @property
    def description(self) -> str:
        return self._graph.description or ""

    @property
    def steps(self) -> list[str]:
        return [node.id for node in self._graph.steps]

    @property
    def labels(self) -> dict[str, str]:
        """step_id -> display label (x-aitelier override, else node name, else id)."""
        return {
            node.id: str(self.label_overrides.get(node.id) or node.name or node.id)
            for node in self._graph.steps
        }

    @property
    def checkpoints(self) -> dict[str, dict]:
        """step_id -> {label, reject_to, kind} for every checkpoint node."""
        out: dict[str, dict] = {}
        for node in self._graph.steps:
            if node.checkpoint:
                out[node.id] = {
                    "label": node.checkpoint_label or node.name or node.id,
                    "reject_to": node.checkpoint_reject_to or "",
                    "kind": self.checkpoint_kinds.get(node.id, self.checkpoint_kind),
                }
        return out

    def label_for(self, step_id: str) -> str:
        """Display label for a step, falling back to the raw id (graceful for
        unknown/loop-instance steps)."""
        return self.labels.get(step_id, step_id)

    def to_dict(self) -> dict:
        return {
            "config_name": self.config_name,
            "label": self.label,
            "description": self.description,
            "steps": self.steps,
            "labels": self.labels,
            "checkpoints": self.checkpoints,
            "has_task_loop": self.has_task_loop,
            "scheduler_owned": self.scheduler_owned,
            "seed_file": self.seed_file,
            "output_step": self.output_step,
            "preamble_steps": self.preamble_steps,
        }


def _read_host_hints() -> dict[str, dict]:
    """Read the ``x-aitelier:`` block from every host config (keyed by graph name).

    skillflow strips unknown top-level keys on load and does not round-trip them
    through ``skillflow_graphs.yaml_text``, so the declared hints must come from the
    source files directly.
    """
    hints: dict[str, dict] = {}
    if not CONFIGS_DIR.exists():
        return hints
    for f in sorted(CONFIGS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        name = data.get("name")
        if name:
            block = data.get("x-aitelier") or {}
            if isinstance(block, dict):
                hints[name] = block
    return hints


class ConfigRegistry:
    """Builds and holds a :class:`ConfigManifest` for every registered graph."""

    def __init__(self) -> None:
        self._manifests: dict[str, ConfigManifest] = {}

    @staticmethod
    def _make_manifest(sf, name: str, host_hints: dict,
                       hint_overrides: dict | None = None) -> "ConfigManifest | None":
        """Build one manifest for an already-registered graph, or None if it
        doesn't resolve. Derived views (steps/labels/checkpoints/description) are
        NOT computed here — the manifest reads them from the live graph on access,
        so a re-registered graph is reflected without rebuilding the registry."""
        try:
            sf._get_resolver(name)  # ensure the graph resolves; skip if broken
        except Exception:
            # A broken graph silently vanishing from the registry is hard to
            # diagnose (e.g. a generated pipeline the butler can't then run).
            import logging
            logging.getLogger("aitelier").warning(
                "config %r did not resolve; skipped from registry", name,
                exc_info=True)
            return None
        hints = {**_DEFAULTS, **_EXTERNAL_HINTS.get(name, {}),
                 **host_hints.get(name, {}), **(hint_overrides or {})}
        return ConfigManifest(
            config_name=name,
            graph_provider=(lambda n=name: sf._get_resolver(n).graph),
            label=hints.get("label") or name,
            has_task_loop=bool(hints.get("has_task_loop")),
            scheduler_owned=bool(hints.get("scheduler_owned")),
            seed_file=hints.get("seed_file"),
            output_step=hints.get("output_step"),
            input_hint=hints.get("input_hint") or "",
            registers_generated_pipeline=bool(hints.get("registers_generated_pipeline")),
            registers_generated_addon=bool(hints.get("registers_generated_addon")),
            repo_mode=hints.get("repo_mode") or "code",
            preamble_steps=list(hints.get("preamble_steps") or []),
            label_overrides=hints.get("labels") or {},
            checkpoint_kind=hints.get("checkpoint_kind") or "file-review",
            checkpoint_kinds=hints.get("checkpoint_kinds") or {},
        )

    @classmethod
    def build(cls, sf) -> "ConfigRegistry":
        """Construct manifests for every graph ``sf`` knows about. Layers declared
        hints (host ``x-aitelier`` block, then framework defaults) on top."""
        reg = cls()
        host_hints = _read_host_hints()  # read once, not per-graph
        for g in sf.list_graphs():
            m = cls._make_manifest(sf, g["name"], host_hints)
            if m is not None:
                reg._manifests[g["name"]] = m
        return reg

    def register_one(self, sf, name: str, *, hint_overrides: dict | None = None,
                     host_hints: dict | None = None) -> "ConfigManifest | None":
        """Add (or replace) a single manifest for an already-registered graph, so a
        graph registered at runtime (e.g. a generated ``gen_*`` pipeline) becomes
        runnable without rebuilding the registry or restarting. ``hint_overrides``
        lets the caller apply policy the registry shouldn't hard-code (the
        generated-pipeline hints live in core.pipeline_registry). Idempotent.
        Returns None if the graph doesn't resolve."""
        m = self._make_manifest(
            sf, name, host_hints if host_hints is not None else _read_host_hints(),
            hint_overrides=hint_overrides)
        if m is not None:
            self._manifests[name] = m
        return m

    def list(self) -> list[ConfigManifest]:
        return list(self._manifests.values())

    def get(self, config_name: str) -> ConfigManifest | None:
        return self._manifests.get(config_name)

    def names(self) -> list[str]:
        return list(self._manifests.keys())

    def catalog(self, full: bool = False) -> list[dict]:
        """The butler's pipeline catalog, generated from the live registry.

        ``full=False`` (compact — pushed into the system context up front):
        name, one-line description, and drive-mode. ``full=True`` (pulled via
        list_pipelines when the butler needs to choose): adds the input_hint
        (seed contract), seed_file and task-loop flags. Registry-generated, so
        gen_* and later-added pipelines appear automatically — no per-config
        prompt text to maintain.
        """
        out = []
        for m in sorted(self._manifests.values(), key=lambda x: x.config_name):
            out.append(self._entry(m, full=full))
        return out

    @staticmethod
    def _entry(m: "ConfigManifest", full: bool = False) -> dict:
        """One catalog row for a manifest (compact, or full with the contract)."""
        try:
            desc = m.description
        except Exception:
            desc = ""
        entry = {
            "config_name": m.config_name,
            "description": desc,
            # scheduler-owned → fire-and-poll; else butler-driven inline.
            "drive": "background" if m.scheduler_owned else "inline",
        }
        if full:
            entry["input_hint"] = m.input_hint
            entry["takes_seed"] = bool(m.seed_file)
            entry["has_task_loop"] = bool(m.has_task_loop)
        return entry

    def describe(self, query: str) -> list[dict]:
        """Targeted lookup for the butler's describe_pipeline tool.

        An exact ``config_name`` match returns just that pipeline's full
        contract; otherwise the query is matched (case-insensitive substring)
        against config_name and description, returning every match's full
        contract. Lets the butler get one pipeline's seed shape without pulling
        the whole catalog.
        """
        q = (query or "").strip()
        if not q:
            return []
        exact = self._manifests.get(q)
        if exact is not None:
            return [self._entry(exact, full=True)]
        ql = q.lower()
        return [self._entry(m, full=True)
                for m in sorted(self._manifests.values(), key=lambda x: x.config_name)
                if ql in m.config_name.lower() or ql in (m.description or "").lower()]
