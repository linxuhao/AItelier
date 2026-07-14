"""apply_state — books the chapter into the durable novel state.

chapter_events.json (written by the finalize agent, schema-validated by the
graph):

  {
    "chapter": int, "title": str,
    "summary": str,                    # 2-3 句，只记录对后续有影响的关键信息
    "events": [{"entity_type": "character|protagonist|faction|world_setting",
                "entity_name": str, "create": bool?, "changes": {...},
                "reason": str}],
    "appearances": [{"name": str, "importance": int?}],
    "thread_updates": [{"name": str, "action": "hint|resolve|register|abandon",
                        "detail": str, ...}],
    "arc_updates": [{"name": str, "milestone_completed": str?,
                     "current_phase": str?, "status": str?}]
  }

Everything durable lives OUTSIDE the config/step dirs (which the next run's
promotion overwrites): novel/chapters/ is the permanent record, novel/bible/
the balances. Step outputs remain recoverable via artifact history.
"""

import json
from pathlib import Path

from aitelier import novel_state as ns


def apply_state(*, project_root: str = "", workspace_root: str = "",
                out_dir: str = "", finalize_step_dir: str = "",
                prose_step_dir: str = "", **kwargs) -> dict:
    # novel/ tree lives in the code repo (project_root); agent step outputs
    # (finalize/humanize) live in the skillflow workspace (workspace_root). In
    # unit tests both point at the same tmp dir.
    ws = Path(project_root or workspace_root or ".")           # novel/ tree
    stage = Path(workspace_root or project_root or ".")        # step outputs

    fin_dir = Path(finalize_step_dir) if finalize_step_dir \
        else stage / "novel_chapter" / "finalize"
    events_path = fin_dir / "chapter_events.json"
    if not events_path.is_file():
        raise ValueError(f"apply_state: chapter_events.json not found at {events_path}")
    try:
        record = json.loads(events_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise ValueError(f"apply_state: chapter_events.json is not valid JSON: {e}")

    prose_dir = Path(prose_step_dir) if prose_step_dir \
        else stage / "novel_chapter" / "humanize"
    prose_path = prose_dir / "chapter_final.md"
    if not prose_path.is_file():
        raise ValueError(f"apply_state: chapter_final.md not found at {prose_path}")
    prose = prose_path.read_text(encoding="utf-8")

    n = ns.next_chapter_number(ws)
    declared = record.get("chapter")
    if declared != n:
        raise ValueError(
            f"apply_state: chapter_events.json declares chapter {declared!r} but "
            f"the next unwritten chapter is {n} — refusing to book a mismatched "
            "journal (stale finalize output?)")
    summary = str(record.get("summary") or "").strip()
    if not summary:
        raise ValueError("apply_state: summary is empty — the digest and future "
                         "context depend on it")

    # ── 1. Immutable chapter record ──
    ch_dir = ns.chapter_dir(ws, n)
    if ch_dir.exists():
        raise ValueError(f"apply_state: {ch_dir} already exists — chapters are "
                         "append-only (rollback goes through git)")
    ch_dir.mkdir(parents=True)
    (ch_dir / "prose.md").write_text(prose, encoding="utf-8")
    title = str(record.get("title") or "").strip()
    (ch_dir / "summary.md").write_text(
        (f"# 第{n}章：{title}\n\n" if title else "") + summary + "\n",
        encoding="utf-8")
    ns.dump_yaml(ch_dir / "events.yaml", {
        "chapter": n, "title": title,
        "word_count": ns.char_count(prose),
        "events": record.get("events") or [],
        "appearances": record.get("appearances") or [],
        "thread_updates": record.get("thread_updates") or [],
        "arc_updates": record.get("arc_updates") or [],
    })

    # ── 2. Post to balances ──
    warnings: list[str] = []
    warnings += ns.apply_events(ws, record.get("events") or [], n)
    warnings += ns.log_appearances(ws, record.get("appearances") or [], n)
    warnings += ns.apply_thread_updates(ws, record.get("thread_updates") or [], n)
    warnings += ns.apply_arc_updates(ws, record.get("arc_updates") or [], n)

    # ── 3. Derived state + audit ──
    ns.rebuild_digest(ws)
    ns.rebuild_index(ws)
    drift = ns.reconcile(ws)
    warnings += drift

    # ── 4. Commit the chapter into the code repo (git history + download) ──
    committed = ns.git_commit(ws, f"第{n}章：{title}" if title else f"第{n}章")

    report = {"applied": True, "chapter": n, "title": title, "committed": committed,
              "warnings": warnings, "reconcile_drift": drift}
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "apply_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"applied": True, "chapter": n, "committed": committed, "warnings": warnings}
