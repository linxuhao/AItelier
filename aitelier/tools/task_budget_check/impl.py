"""task_budget_check — does the PM's task list fit in the run's step budget?

Runs as a tool STEP between the PM's reviewer and the task loop. The arithmetic
it does was always computable the moment the PM emitted its manifest, and
nothing was doing it:

    `boltons`, one benchmark run. The PM listed 33 tasks. The loop body is 4
    steps per task and the linear chain around it is another 13, so 145 steps.
    The 5_review goal loop then appended 21 `fix_*` tasks — 54 tasks, 227 steps
    — against `max_total_steps: 200`. The run died on the STEP CAP, not the
    6-hour wall clock: it could not have finished with unlimited time, and it
    threw away every task the loop had already implemented.

So this gate counts the tasks, computes the steps they need, and either passes
or sends the list back to the PM with the numbers. It is deterministic on
purpose — an LLM asked to check its own list against a budget is exactly the
component that produced the unexecutable list.

**It can only reject, never raise the budget.** `max_total_steps` lives in the
graph's `end_conditions`, which skillflow reads from the registered GRAPH
(`core.py:_get_resolver` → `resolver.graph.end_conditions`), not from the run:
`create_run` snapshots per-node step config and edge counts, and never copies
the end conditions. Raising it at runtime would mean rewriting the graph row
shared by every other run of the same config — not "this run gets more room".

Never fails the run. An unreadable graph, a missing manifest, an empty list:
all pass with a reason. A budget gate that cannot compute has nothing to say.
"""

import json
import math
from pathlib import Path

# How many fix tasks one goal-loop round appends, as a fraction of the planned
# list. Measured: `boltons` planned 33 and the 5_review→PM goal loop appended 21
# (0.64). One round of headroom is budgeted, not two — the second round is what
# `max_loop: 2` on the goal-loop edge is for, and reserving for it would cap the
# pipeline at ~15 tasks.
FIX_TASK_RATIO = 2 / 3

_REPORT = "budget_report.json"


def task_budget_check(*, out_dir: str = "", config_name: str = "",
                      workspace_root: str = "", **kwargs) -> dict:
    """Compare the PM's task list against the run's step budget.

    Returns ``{within_budget, task_count, required_steps, max_total_steps,
    max_tasks, reason}``. ``reason`` is what skillflow puts in the PM's
    "MUST ADDRESS" banner when the reject edge carries ``feedback: true``.
    """
    shape = _graph_shape(config_name)
    if shape is None:
        return _pass(out_dir, reason=f"graph '{config_name}' not resolvable — budget not checked")
    if not shape["budget"]:
        return _pass(out_dir, reason="graph declares no max_total_steps — budget not checked")

    tasks = _read_task_ids(shape["source"], out_dir, workspace_root, config_name)
    if tasks is None:
        return _pass(out_dir, reason="task manifest not readable — budget not checked")
    n = len(tasks)
    if n == 0:
        return _pass(out_dir, reason="task manifest lists no tasks — budget not checked")

    # Stale ids: a goal-loop re-plan that re-lists ids the task loop has ALREADY
    # dispatched runs nothing — skillflow keeps completed_items across re-entry
    # and skips them. R3b (2026-09-02) re-emitted all six of its iteration-2 ids
    # and 3_review passed it (its template forbids this, but it has no view of
    # completed_items). This gate does.
    stale = [t for t in tasks
             if t in _completed_loop_items(kwargs.get("project_id", ""))]
    if stale and len(stale) == n:
        return _write(out_dir, {
            "within_budget": False, "task_count": n, "stale_ids": stale,
            "reason": (
                f"Every task id in the manifest ({', '.join(stale)}) has ALREADY "
                f"been dispatched by this run's task loop (completed_items). The "
                f"engine skips a completed id and never re-runs it, so this "
                f"breakdown would run NOTHING and the run would go straight to "
                f"the final verifier. A fix must be a NEW card with a NEW id "
                f"(e.g. fix_<defect>_2); an old id in execution_order is skipped."
            ),
        })

    budget = shape["budget"]
    required = _required_steps(shape, n)
    if required <= budget:
        note = (f"; {len(stale)} already-dispatched id(s) will be skipped: "
                f"{', '.join(stale)}") if stale else ""
        return _pass(out_dir, task_count=n, required_steps=required,
                     max_total_steps=budget, max_tasks=n, stale_ids=stale,
                     reason=f"{n} tasks need {required} steps of the {budget} budgeted{note}")

    fits = _largest_fitting_count(shape, budget, n)
    return _write(out_dir, {
        "within_budget": False,
        "task_count": n,
        "required_steps": required,
        "max_total_steps": budget,
        "max_tasks": fits,
        "reason": (
            f"Task list too long for the run's step budget. Your {n} tasks need "
            f"{required} pipeline steps ({shape['linear']} for the fixed chain, "
            f"{shape['body']} per task, plus one fix round), and the run is capped "
            f"at {budget} steps — it would be killed mid-loop and lose every task "
            f"already implemented. The budget cannot be raised. Re-emit the "
            f"manifest with at most {fits} tasks: merge the small ones, drop what "
            f"is not required by the goals, and keep each task big enough to be "
            f"worth a plan/implement/review cycle."
        ),
    })


# ── Graph shape ────────────────────────────────────────────────────────────

def _completed_loop_items(project_id: str) -> set[str]:
    """Ids the project's live run has already dispatched through any loop.

    A tool node receives project_id, not run_id, so the run is found through
    the runs table (non-terminal status). Any failure reads as "nothing
    completed" — the gate can only pass on error, never fail spuriously.
    """
    if not project_id:
        return set()
    try:
        from api.dependencies import get_skillflow
        conn = get_skillflow()._conn
        rows = conn.execute(
            "SELECT ls.completed_items FROM skillflow_loop_state ls "
            "JOIN skillflow_runs r ON r.id = ls.run_id "
            "WHERE r.project_id = ? AND r.status NOT IN ('completed', 'failed')",
            (project_id,)).fetchall()
    except Exception:
        return set()
    done: set[str] = set()
    for row in rows:
        try:
            done.update(x for x in json.loads(row[0] or "[]") if isinstance(x, str))
        except Exception:
            pass
    return done


def _graph_shape(config_name: str) -> dict | None:
    """Step accounting derived from the live graph, not hardcoded.

    ``linear`` — steps that run once per pass and leave a row skillflow counts
    (`status IN ('completed','failed')`): every agent/tool node outside a loop
    body, plus the loop node itself, which is marked completed when it drains.
    Gate nodes leave no row. ``body`` — the loop body, 4 for dpe_default
    (t_plan, t_plan_review, t_impl, t_impl_review).

    Derived rather than written down because the numbers move: the game_harness
    addon splices extra steps into this same graph, and a hardcoded 11 would go
    quietly wrong there.
    """
    graph = _live_graph(config_name)
    if graph is None:
        return None
    try:
        from skillflow.graph import loop_body_map

        bodies = loop_body_map(graph.steps)
        loops = [s for s in graph.steps if s.step_type == "loop"]
        if not loops:
            return None
        loop = loops[0]
        body_ids = set(bodies.get(loop.id, ()))
        linear = len([s for s in graph.steps
                      if s.step_type in ("agent", "tool") and s.id not in body_ids])
        return {
            "linear": linear + len(loops),
            "body": len(body_ids),
            "source": dict(loop.loop.source),
            "budget": _step_budget(graph),
        }
    except Exception:
        return None


def _live_graph(config_name: str):
    """The registered PipelineGraph for this run's config, or None."""
    if not config_name:
        return None
    try:
        from api.dependencies import get_config_registry

        manifest = get_config_registry().get(config_name)
        return manifest.graph_provider() if manifest else None
    except Exception:
        return None


def _step_budget(graph) -> int:
    """The graph's max_total_steps limit, or 0 when it declares none."""
    ec = getattr(graph, "end_conditions", None)
    for cond in getattr(ec, "conditions", None) or []:
        if getattr(cond, "type", "") == "max_total_steps" and cond.limit:
            return int(cond.limit)
    return 0


# ── Arithmetic ─────────────────────────────────────────────────────────────

def _required_steps(shape: dict, n: int) -> int:
    """Steps a list of ``n`` tasks needs: one clean pass plus one fix round.

    The fix round re-runs the linear chain and a body per appended fix task.
    Counting the WHOLE linear chain there over-estimates (the goal loop re-enters
    at the PM, so the research/architecture head does not re-run) — deliberately:
    a list that overshoots costs the whole run, a list one round too cautious
    costs one extra planning round.
    """
    fix_tasks = math.ceil(n * FIX_TASK_RATIO)
    return (shape["linear"] + shape["body"] * n
            + shape["linear"] + shape["body"] * fix_tasks)


def _largest_fitting_count(shape: dict, budget: int, n: int) -> int:
    """Biggest task count that fits, never more than what the PM asked for."""
    fits = 0
    for k in range(1, n + 1):
        if _required_steps(shape, k) > budget:
            break
        fits = k
    return fits


# ── Manifest ───────────────────────────────────────────────────────────────

def _read_task_ids(source: dict, out_dir: str, workspace_root: str,
                   config_name: str) -> list | None:
    """The loop's own item list, read the way skillflow reads it.

    Location comes from the loop's `source` ({step, file, field}), so this gate
    never hardcodes "step 3 / tasks_manifest.json". A list of lists (waves) is
    flattened and deduped, exactly as `core.py:_read_loop_items` does.
    """
    graph_dir = _graph_dir(out_dir, workspace_root, config_name)
    if graph_dir is None:
        return None
    path = graph_dir / source.get("step", "") / source.get("file", "")
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = data.get(source.get("field", ""), [])
    except (ValueError, OSError, AttributeError):
        return None
    if not isinstance(items, list):
        return None
    flat = []
    for item in items:
        if isinstance(item, list):
            flat.extend(item)
        else:
            flat.append(item)
    return list(dict.fromkeys(i for i in flat if isinstance(i, str)))


def _graph_dir(out_dir: str, workspace_root: str, config_name: str) -> Path | None:
    """Directory holding the per-step output dirs (same locator as knowledge_sync:
    $STEP_DIR's parent is the graph dir; workspace_root is not injected for tool
    steps, so it is only a fallback for explicit callers)."""
    if out_dir:
        p = Path(out_dir).parent
        if p.exists():
            return p
    if workspace_root and config_name:
        d = Path(workspace_root) / config_name
        if d.exists():
            return d
    return None


# ── Result ─────────────────────────────────────────────────────────────────

def _pass(out_dir: str, **fields) -> dict:
    return _write(out_dir, {"within_budget": True, "task_count": 0,
                            "required_steps": 0, "max_total_steps": 0,
                            "max_tasks": 0, **fields})


def _write(out_dir: str, result: dict) -> dict:
    """Persist the numbers next to the step so the PM's context can read them."""
    if out_dir:
        try:
            d = Path(out_dir)
            d.mkdir(parents=True, exist_ok=True)
            (d / _REPORT).write_text(json.dumps(result, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        except OSError:
            pass
    return result
