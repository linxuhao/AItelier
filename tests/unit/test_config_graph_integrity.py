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


# ── Task-loop reviewers must never dead-end the run ─────────────────────────
# `t_plan_review` / `t_impl_review` route on `passed` read out of
# review_verdict.json. When the reviewer (an LLM) writes no verdict at all, or
# an unparseable one, or a non-bool `passed`, none of the value-matching edges
# fire and skillflow fails the RUN: "No matching transition from 't_impl_review'
# with flags {}". That happened live in the 104-task benchmark sweep
# (nl2repo-asteval / core_astutils: the reviewer burned every tool turn on
# reasoning and committed "0 file(s)"), throwing away every task the loop had
# already implemented. These two steps sit inside the task loop, so the blast
# radius is the whole project — which is why they, unlike the preamble
# reviewers, carry an explicit bounded fallback chain.

DPE_CONFIG = CONFIG_DIR / "dpe_default.yaml"

# step id → the node the fallback chain must reach once the retries are spent.
# Both are forward: `task_loop` credits the current item and dispatches the
# next, `t_impl` continues the current one. Neither can re-run the same item.
LOOP_REVIEWER_ESCAPES = {"t_plan_review": "t_impl", "t_impl_review": "task_loop"}


def _dpe_resolver():
    from skillflow.graph import GraphResolver, PipelineGraph
    return GraphResolver(PipelineGraph.from_yaml(DPE_CONFIG))


def _walk(resolver, step, file_reader, counts=None, limit=10):
    """Follow the transitions out of `step`, counting edges as skillflow does,
    until they leave the node. Returns the visited targets."""
    counts = dict(counts or {})
    seen = []
    for _ in range(limit):
        target = resolver.next_node(step, {}, counts, file_reader=file_reader)
        seen.append(target)
        if target is None or target != step:
            return seen
        counts[(step, target)] = counts.get((step, target), 0) + 1
    return seen


def _no_verdict(path):
    raise FileNotFoundError(path)


@pytest.mark.parametrize("step,escape", sorted(LOOP_REVIEWER_ESCAPES.items()))
def test_loop_reviewer_routes_when_the_verdict_is_missing(step, escape):
    """A verdict that routes nowhere must not kill the run."""
    chain = _walk(_dpe_resolver(), step, _no_verdict)
    assert None not in chain, (
        f"{step} dead-ends on a missing review_verdict.json — the run fails with "
        f"\"No matching transition from '{step}' with flags {{}}\" and every task "
        f"already completed by the loop is lost. Add an unconditional fallback edge."
    )
    assert chain[-1] == escape, f"{step} fallback chain ended at {chain[-1]}, not {escape}"


@pytest.mark.parametrize("step,escape", sorted(LOOP_REVIEWER_ESCAPES.items()))
def test_loop_reviewer_retry_is_bounded(step, escape):
    """The retry edge re-runs the reviewer, but only finitely often.

    An unconditional self-edge would spin forever on a systematically broken
    reviewer, so the retries must carry max_loop and hand off to the escape.
    """
    chain = _walk(_dpe_resolver(), step, _no_verdict)
    retries = [t for t in chain if t == step]
    assert retries, f"{step} does not retry the reviewer before giving up"
    assert chain[-1] == escape, (
        f"{step} retries unboundedly — the chain never leaves the node: {chain}"
    )


def test_loop_reviewer_routes_when_the_reject_edge_is_spent():
    """max_loop on the reject edge used to be its own dead end.

    With every edge exhausted skillflow raises CycleLimitExceeded and fails the
    run; the fallback has to absorb that too.
    """
    import json
    resolver = _dpe_resolver()
    chain = _walk(resolver, "t_impl_review",
                  lambda p: json.dumps({"passed": False}),
                  counts={("t_impl_review", "t_impl"): 3})
    assert chain[-1] == "task_loop", chain


def test_loop_reviewer_verdict_routing_is_unchanged():
    """The fallback must not shadow a verdict that DOES route."""
    import json
    resolver = _dpe_resolver()
    passed = lambda p: json.dumps({"passed": True})       # noqa: E731
    rejected = lambda p: json.dumps({"passed": False})    # noqa: E731
    assert resolver.next_node("t_impl_review", {}, {}, file_reader=passed) == "task_loop"
    assert resolver.next_node("t_impl_review", {}, {}, file_reader=rejected) == "t_impl"
    assert resolver.next_node("t_plan_review", {}, {}, file_reader=passed) == "t_impl"
    assert resolver.next_node("t_plan_review", {}, {}, file_reader=rejected) == "t_plan"


def test_loop_reviewer_fallbacks_stay_inside_the_loop_body():
    """The fallback targets must keep the task loop's topology intact.

    skillflow scopes per-item retry budgets to the nodes that can reach back to
    the loop (graph.loop_body_map); a fallback that left the body would give the
    reviewer a run-wide budget instead of a per-task one.
    """
    resolver = _dpe_resolver()
    body = set(resolver.loop_bodies().get("task_loop", ()))
    assert {"t_plan", "t_plan_review", "t_impl", "t_impl_review"} <= body, body


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


def _dpe_node(step):
    return _dpe_resolver().get_node(step)


@pytest.mark.parametrize("step,required_file", sorted(MUST_PROMOTE.items()))
def test_task_loop_steps_validate_their_load_bearing_output(step, required_file):
    """Without this the step completes green on zero files."""
    specs = _dpe_node(step).validation or []
    guarded = [s for s in specs if required_file in (s.get("files") or [])]
    assert guarded, (
        f"{step} declares no validation for {required_file}: an empty staging dir "
        f"commits as a success and the step completes, so downstream reviews an "
        f"empty workspace instead of the step being retried."
    )


@pytest.mark.parametrize("step", sorted(MUST_PROMOTE))
def test_validation_exhaustion_routes_instead_of_killing_the_run(step):
    """Validation exhaustion goes through `_fail_step_in_tx(retryable=False)`.

    That path consults `find_error_transition` and, finding none, fails the RUN
    — discarding every task the loop already implemented. Adding validation to a
    task-loop step therefore has to come with an `_error` edge, or it re-opens
    the dead end the fallback chain above was added to close.
    """
    resolver = _dpe_resolver()
    target = resolver.find_error_transition(step)
    assert target is not None, (
        f"{step} is validated but has no `match: {{_error: true}}` transition: "
        f"once its retry budget is spent the whole run fails."
    )
    body = set(resolver.loop_bodies().get("task_loop", ()))
    assert target in body | {"task_loop"}, (
        f"{step} escapes validation failure to {target}, outside the task loop"
    )
    assert target != step, f"{step} routes validation failure back to itself"


def test_error_edge_does_not_shadow_t_plans_forward_edge():
    """`match: {_error: true}` must never fire on an ordinary completion.

    (The reviewers' own routing is pinned by
    `test_loop_reviewer_verdict_routing_is_unchanged` above.)
    """
    resolver = _dpe_resolver()
    assert resolver.next_node("t_plan", {}, {}, file_reader=_no_verdict) == "t_plan_review"


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
