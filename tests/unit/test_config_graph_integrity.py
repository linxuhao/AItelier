"""Every shipped config must be RUNNABLE, not merely parseable.

`create_run` seeds `skillflow_edge_counts` for every transition that declares
`max_loop` (core.py: `if trans.max_loop is not None`), and that table is UNIQUE on
(run_id, from_step, to_step). So the invariant is precise: **two edges may share a
(from, to) pair only if at most one of them carries `max_loop`.** Parallel edges
distinguished purely by `match` are fine — `meta_conversation` has always had two
`intent_detect → gather` edges and runs happily.

Violating it produces a config that parses, lints clean, and is then impossible to
run: every attempt dies with an IntegrityError inside the scheduler, which the user
sees only as a run stuck in 'planning' with no explanation. That is exactly how a
routing change to pipeline_forge shipped broken, so it is checked here now.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"
CONFIGS = sorted(p for p in CONFIG_DIR.glob("*.yaml"))


def _steps(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [s for s in (data.get("steps") or []) if isinstance(s, dict)]


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.stem)
def test_at_most_one_max_loop_edge_per_pair(config):
    """Two `max_loop` edges sharing a (from, to) pair make the config un-runnable."""
    offenders = []
    for step in _steps(config):
        counted = Counter(t.get("to") for t in (step.get("transitions") or [])
                          if isinstance(t, dict) and t.get("to") is not None
                          and t.get("max_loop") is not None)
        offenders += [f"{step.get('id')} → {target} ×{n}"
                      for target, n in counted.items() if n > 1]
    assert not offenders, (
        f"{config.name} declares more than one max_loop edge for the same (from, to) "
        f"pair ({'; '.join(offenders)}). create_run inserts one skillflow_edge_counts "
        f"row per max_loop edge and the table is UNIQUE on (run_id, from_step, "
        f"to_step), so this config cannot create a run at all. Keep one bounded edge "
        f"per pair and distinguish the cases with `match`."
    )


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.stem)
def test_transitions_point_at_real_steps(config):
    """A typo'd target is a run that dies at the first branch, not at load."""
    steps = _steps(config)
    known = {s.get("id") for s in steps}
    bad = []
    for step in steps:
        for t in step.get("transitions") or []:
            if not isinstance(t, dict):
                continue
            target = t.get("to")
            if target is not None and target not in known:
                bad.append(f"{step.get('id')} → {target}")
    assert not bad, f"{config.name} has transitions to unknown steps: {bad}"


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.stem)
def test_checkpoint_reject_targets_are_real_steps(config):
    """`reject_checkpoint` writes `current_node = redirect_to` with no validation.

    A typo therefore parks the run on a node that will never be claimed — the user
    presses "Request Changes" and the run silently stops. (An EMPTY reject target is
    legal and means "re-run the checkpoint step itself", which is right whenever the
    checkpoint step is also the step that produced the artifact under review.)
    """
    steps = _steps(config)
    known = {s.get("id") for s in steps}
    bad = [f"{s.get('id')} → {s['checkpoint_reject_to']}" for s in steps
           if s.get("checkpoint_reject_to")
           and s["checkpoint_reject_to"] not in known]
    assert not bad, f"{config.name} rejects to unknown steps: {bad}"


@pytest.mark.parametrize("config", CONFIGS, ids=lambda p: p.stem)
def test_end_conditions_name_real_nodes(config):
    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    known = {s.get("id") for s in _steps(config)}
    missing = [c.get("node") for c in
               ((data.get("end_conditions") or {}).get("conditions") or [])
               if isinstance(c, dict) and c.get("type") == "node_reached"
               and c.get("node") not in known]
    assert not missing, f"{config.name} end_conditions name unknown nodes: {missing}"
