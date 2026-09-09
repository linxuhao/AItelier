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


# ── Task-loop delivery and reviews fail closed ─────────────────────────────
# A task may be credited only after its implementation passed validation, was
# delivered, and received a boolean passing verdict. Missing/malformed verdicts,
# exhausted reject loops, validation exhaustion, and execution errors stay failed
# for operator recovery; none may advance the loop to its next item.

DPE_CONFIG = CONFIG_DIR / "dpe_default.yaml"


def _dpe_resolver():
    from skillflow.graph import GraphResolver, PipelineGraph
    return GraphResolver(PipelineGraph.from_yaml(DPE_CONFIG))


def _no_verdict(path):
    raise FileNotFoundError(path)


def _dpe_node(step):
    return _dpe_resolver().get_node(step)


def _dpe_step_dict(step):
    import yaml
    graph = yaml.safe_load(DPE_CONFIG.read_text(encoding="utf-8"))
    return next(item for item in graph["steps"] if item.get("id") == step)


@pytest.mark.parametrize("step", ["t_plan", "t_plan_review", "t_impl",
                                   "t_impl_review"])
def test_task_loop_validation_exhaustion_is_fail_closed(step):
    node = _dpe_node(step)
    assert node.validation, f"{step} has no validation to protect"
    assert _dpe_step_dict(step).get("validation_on_exhaustion") == "fail", (
        f"{step} must not promote invalid output after its validation retry budget "
        f"is spent")


@pytest.mark.parametrize("step", ["t_plan", "t_plan_review", "t_impl",
                                   "t_impl_review"])
def test_task_loop_errors_do_not_route_forward(step):
    assert _dpe_resolver().find_error_transition(step) is None, (
        f"{step} routes an execution/validation error forward instead of leaving "
        f"the run failed for recovery")


def test_implementation_reaches_review_only_after_normal_completion():
    transitions = _dpe_node("t_impl").transitions
    assert len(transitions) == 1
    assert transitions[0].to == "t_impl_review"
    assert not transitions[0].match


def test_only_a_passing_implementation_review_credits_the_task():
    transitions = _dpe_node("t_impl_review").transitions
    to_next = [t for t in transitions if t.to == "task_loop"]
    assert len(to_next) == 1
    assert to_next[0].match == {
        "from_file": "review_verdict.json", "field": "passed", "value": True}
    assert all(t.match for t in transitions), (
        "an unconditional reviewer fallback can credit an unreviewed task")


def test_missing_implementation_verdict_has_no_forward_route():
    assert _dpe_resolver().next_node(
        "t_impl_review", {}, {}, file_reader=_no_verdict) is None


def test_exhausted_reject_loop_cannot_fall_through_to_next_task():
    import json
    from skillflow.exceptions import CycleLimitExceeded

    rejected = lambda p: json.dumps({"passed": False})  # noqa: E731
    with pytest.raises(CycleLimitExceeded):
        _dpe_resolver().next_node(
            "t_impl_review", {},
            {("t_impl_review", "t_impl"): 3}, file_reader=rejected)


# ── A step that promotes nothing must fail, not complete ────────────────────
# `_step_commit` returns {"passed": True, "files": []} for an empty staging dir
# — an explicit success — and the engine's zero-file warning is gated on
# `output_mode == "write"`, which none of these content-mode steps is. So a
# `t_plan` that wrote no file at all completed green, handed an empty workspace
# to `t_plan_review`, and burned two of the three replan retries on a rejection
# that had nothing to do with the real problem. `output.fixed` is not a
# contract (it only generates the per-slot write tools), so the only mechanism
# that makes a slot mandatory is `validation:`.

# step id → the file whose absence must fail the step.
MUST_PROMOTE = {
    "t_plan": "task_plan.md",
    "t_plan_review": "review_verdict.json",
    "t_impl_review": "review_verdict.json",
}


@pytest.mark.parametrize("step,required_file", sorted(MUST_PROMOTE.items()))
def test_task_loop_steps_validate_their_load_bearing_output(step, required_file):
    """Without this the step completes green on zero files."""
    specs = _dpe_node(step).validation or []
    guarded = [spec for spec in specs
               if required_file in (spec.get("files") or [])]
    assert guarded, (
        f"{step} declares no validation for {required_file}: an empty staging dir "
        f"could complete and reach downstream work")


def test_reviewer_verdict_schema_only_gates_the_field_that_routes():
    """`passed` is what both edges read; anything stricter is the step-"3" trap.

    The comment at step "3" records why strict schema validation was removed
    there: LLM JSON formatting varies and a strict schema retried forever. A
    schema that requires only the boolean the transitions already read cannot
    reject a verdict that would have routed.
    """
    for step in ("t_plan_review", "t_impl_review"):
        specs = [s for s in (_dpe_node(step).validation or [])
                 if "review_verdict.json" in (s.get("files") or [])]
        for spec in specs:
            if spec.get("tool") != "json_schema":
                continue
            schema = spec.get("inline_schema") or {}
            assert schema.get("required") == ["passed"], (
                f"{step} requires {schema.get('required')} — only `passed` is read "
                f"by the transitions, so anything more can fail a routable verdict"
            )
            assert schema.get("additionalProperties") is not False, (
                f"{step} forbids extra keys; the reviewers legitimately emit more"
            )
