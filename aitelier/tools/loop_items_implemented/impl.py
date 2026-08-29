"""loop_items_implemented — no task may be reviewed green without being built.

The task loop hands each item to t_plan → t_impl → t_impl_review. When an item
arrives at its reviewer with an EMPTY implementation, the reviewer's `t_impl`
context source resolves to nothing and it reviews the repository instead — the
accumulated work of every OTHER card — and passes. dpe_default already carries
that lesson one step earlier, on t_plan:

    # A planner that promotes NOTHING used to complete green ... t_plan_review
    # then reviewed an empty workspace, fell back to reviewing the repository

That fix is a validation on the PRODUCING step, which only works when the step
ran at all. Live on jinyong-hud, 2026-08-27, one did not: after the previous
item's review completed, the loop claimed a t_plan and then, three minutes
later, claimed a t_impl_review for a DIFFERENT item — one with no t_plan row and
no t_impl row anywhere in the run. Its verdict:

    "The fix_battle_hud_overlap_readability implementation is sound ...
     Verified by direct read: 1. HP-number readability ... 3. Skill-button
     label layout ... 5. Contract wiring ..."

— five other cards, reviewed and signed off under this card's name. The two
observables it was meant to add (`hint_nameplate_overlap`,
`nameplate_pairwise_overlap`) were absent, so the playtest asserts written
against them failed with `Invalid named index`, and the on-screen defect it was
meant to fix was untouched.

One occurrence in every run on the box, so this is a race in loop progression,
not a systematic break — and the race belongs to the engine. What belongs HERE
is that a round must not be able to end green while a card it was given has no
implementation. This reports the fact; the round's reviewer decides, which keeps
the judgement where a judgement belongs and costs the loop nothing when all is
well.
"""

import json
from pathlib import Path

# The loop body step whose per-item output proves the item was built. Named
# rather than derived: skillflow's loop node declares its ITEM source, not which
# body step is the implementer, and every config that uses this calls it t_impl.
_IMPL_STEP = "t_impl"


def loop_items_implemented(*, out_dir: str = "", workspace_root: str = "",
                           config_name: str = "", project_id: str = "",
                           **_ignored) -> dict:
    graph_dir = _graph_dir(out_dir, workspace_root, config_name, project_id)
    if graph_dir is None:
        # Say so instead of reporting "nothing missing": an unlocatable
        # workspace is the one case where silence reads exactly like health.
        return {"complete": None, "planned": 0, "implemented": 0, "missing": [],
                "summary": "workspace not locatable — implementation coverage "
                           "NOT checked. Do not read this as a pass.",
                "content": "IMPLEMENTATION COVERAGE: workspace not locatable — "
                           "NOT checked. Do not read this as a pass."}

    items = _loop_items(graph_dir, config_name)
    if items is None:
        return {"complete": None, "planned": 0, "implemented": 0, "missing": [],
                "summary": "task manifest not readable — implementation "
                           "coverage NOT checked. Do not read this as a pass.",
                "content": "IMPLEMENTATION COVERAGE: task manifest not readable "
                           "— NOT checked. Do not read this as a pass."}

    impl_dir = graph_dir / _IMPL_STEP
    built = {p.name for p in impl_dir.iterdir() if p.is_dir()} if impl_dir.is_dir() else set()
    missing = [i for i in items if i not in built]

    if not missing:
        ok = f"all {len(items)} task(s) have {_IMPL_STEP} output"
        return {"complete": True, "planned": len(items),
                "implemented": len(items), "missing": [], "summary": ok,
                "content": f"IMPLEMENTATION COVERAGE: {ok}."}

    result = {
        "complete": False,
        "planned": len(items),
        "implemented": len(items) - len(missing),
        "missing": missing,
        "summary": (
            f"{len(missing)} of {len(items)} task(s) have NO {_IMPL_STEP} output "
            f"on disk: {', '.join(missing)}. These cards were never implemented "
            f"— any review that passed them reviewed the repository, not the "
            f"card. Their deliverables are absent from this round regardless of "
            f"what their verdicts say. NOTE for re-planning: the loop records a "
            f"skipped item as completed, so re-issuing the SAME id is skipped "
            f"again — re-issue under a new id."
        ),
    }
    # `_resolve_tool` renders `content` when present and str(dict) otherwise, so
    # the reviewer reads a sentence rather than a repr.
    result["content"] = "IMPLEMENTATION COVERAGE: " + result["summary"]
    return result


def _graph_dir(out_dir: str, workspace_root: str, config_name: str,
               project_id: str) -> Path | None:
    """Directory holding the per-step output dirs.

    Same locator as task_budget_check / knowledge_sync: $STEP_DIR's parent is
    the graph dir. Duplicated rather than imported — tools are loaded as
    isolated modules and cannot import each other.
    """
    if out_dir:
        p = Path(out_dir).parent
        if p.exists():
            return p
    if workspace_root and config_name:
        d = Path(workspace_root) / config_name
        if d.exists():
            return d
    if project_id and config_name:
        try:
            from api.dependencies import get_workspace_manager
            base = Path(get_workspace_manager().get_workspace_path(project_id))
        except Exception:
            return None
        d = base / config_name
        if d.exists():
            return d
    return None


def _loop_items(graph_dir: Path, config_name: str) -> list | None:
    """The loop's own item list, read where the loop node says it lives.

    Falls back to dpe's `3/tasks_manifest.json` + `execution_order` only when
    the graph cannot be resolved, so a config that keeps its manifest elsewhere
    is still read correctly.
    """
    step, file, field = "3", "tasks_manifest.json", "execution_order"
    try:
        from api.dependencies import get_skillflow
        # KNOWN GAP, not a clean exemption: this is a GATE, and a config edited
        # mid-run can change which loop `source` it reads — a gate reading the
        # wrong source passes or fails a step silently. It cannot be pinned
        # because skillflow hands this tool no run_id at all; closing it means
        # widening the tool-invocation contract.
        # by-name-ok: gate tool, no run_id available — see KNOWN GAP above
        resolver = get_skillflow()._get_resolver(config_name)
        for node in resolver.graph.steps:
            src = getattr(node, "source", None)
            if getattr(node, "step_type", "") == "loop" and isinstance(src, dict):
                step = src.get("step", step)
                file = src.get("file", file)
                field = src.get("field", field)
                break
    except Exception:
        pass

    path = graph_dir / step / file
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    groups = data.get(field, [])
    if not isinstance(groups, list):
        return None
    flat: list[str] = []
    for g in groups:
        if isinstance(g, list):
            flat.extend(x for x in g if isinstance(x, str))
        elif isinstance(g, str):
            flat.append(g)
    return list(dict.fromkeys(flat))
