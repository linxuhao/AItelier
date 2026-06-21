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

import yaml

CONFIGS_DIR = Path(__file__).resolve().parent.parent / "configs"

# Defaults for any declared key the config author omits.
_DEFAULTS: dict = {
    "label": None,
    "scheduler_owned": True,
    "has_task_loop": False,
    "seed_file": None,
    "output_step": None,
    "preamble_steps": [],
    "checkpoint_kind": "file-review",
    "checkpoint_kinds": {},
    "labels": {},
}

# Host-declared hints for framework-owned graphs registered from the skillflow
# package (their YAML lives in the library and carries no x-aitelier block).
_EXTERNAL_HINTS: dict[str, dict] = {
    "skill_converter": {
        "label": "Skill → Pipeline",
        "scheduler_owned": False,
        "seed_file": "skill_description.md",
        "output_step": "done",
    },
}


@dataclass
class ConfigManifest:
    """Everything the host needs to schedule and render runs of one config."""

    config_name: str
    label: str = ""
    description: str = ""
    steps: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)        # step_id -> display label
    checkpoints: dict[str, dict] = field(default_factory=dict)  # step_id -> {label, reject_to, kind}
    has_task_loop: bool = False
    scheduler_owned: bool = True
    seed_file: str | None = None
    output_step: str | None = None
    # Step ids whose outputs are project-global & stable (e.g. SOTA/architecture).
    # The host hoists these into the shared system preamble for prompt caching.
    preamble_steps: list[str] = field(default_factory=list)

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

    @classmethod
    def build(cls, sf) -> "ConfigRegistry":
        """Construct manifests for every graph ``sf`` knows about.

        Derives step ids / labels / checkpoint locations from the graph; layers
        declared hints (host ``x-aitelier`` block, then framework defaults) on top.
        """
        reg = cls()
        host_hints = _read_host_hints()
        for g in sf.list_graphs():
            name = g["name"]
            try:
                graph = sf._get_resolver(name).graph
            except Exception:
                continue
            hints = {**_DEFAULTS, **_EXTERNAL_HINTS.get(name, {}), **host_hints.get(name, {})}
            label_overrides: dict = hints.get("labels") or {}
            per_step_kind: dict = hints.get("checkpoint_kinds") or {}
            default_kind: str = hints.get("checkpoint_kind") or "file-review"

            steps: list[str] = []
            labels: dict[str, str] = {}
            checkpoints: dict[str, dict] = {}
            for node in graph.steps:
                steps.append(node.id)
                labels[node.id] = str(label_overrides.get(node.id) or node.name or node.id)
                if node.checkpoint:
                    checkpoints[node.id] = {
                        "label": node.checkpoint_label or node.name or node.id,
                        "reject_to": node.checkpoint_reject_to or "",
                        "kind": per_step_kind.get(node.id, default_kind),
                    }

            reg._manifests[name] = ConfigManifest(
                config_name=name,
                label=hints.get("label") or name,
                description=g.get("description") or graph.description or "",
                steps=steps,
                labels=labels,
                checkpoints=checkpoints,
                has_task_loop=bool(hints.get("has_task_loop")),
                scheduler_owned=bool(hints.get("scheduler_owned")),
                seed_file=hints.get("seed_file"),
                output_step=hints.get("output_step"),
                preamble_steps=list(hints.get("preamble_steps") or []),
            )
        return reg

    def list(self) -> list[ConfigManifest]:
        return list(self._manifests.values())

    def get(self, config_name: str) -> ConfigManifest | None:
        return self._manifests.get(config_name)

    def names(self) -> list[str]:
        return list(self._manifests.keys())
