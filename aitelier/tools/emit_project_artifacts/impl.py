"""emit_project_artifacts — the meta_conversation ``finalize`` tool step.

Promotes the approved requirements into the project's skillflow artifacts so the
DPE pipeline consumes them through the declared data-flow rather than out-of-band
host glue:

  - ``project/project_brief.md`` + ``project/spec.md`` → skillflow's project
    brief slot (``get_project_brief_dir`` == ``<workspace>/project``), read by
    every DPE step's prompt assembly.
  - ``step1_goals.json`` → the finalize step's own output dir (``$STEP_DIR``),
    consumed by DPE step 1 via a ``{config: meta_conversation, step: finalize,
    output: step1_goals.json}`` cross-config context source.

Pure, deterministic transform (no LLM): the brief comes from the gather step's
``gather_state.json`` and the spec is the verbatim ``meta/conversation.md`` — so
detailed requirements (e.g. game rules) survive un-summarized.

Runs INLINE during ``advance_run`` (framework mode); the graph's
``node_reached: finalize`` end condition then completes the meta run.
"""

import json
from pathlib import Path

from core.meta_conversation import (
    format_brief_as_markdown,
    build_spec_markdown,
    brief_to_step1_goals,
)


def emit_project_artifacts(*, workspace_root: str = "", out_dir: str = "",
                           **kwargs) -> dict:
    """Write project_brief.md + spec.md + step1_goals.json. Returns {written, emitted}."""
    ws = Path(workspace_root or ".")
    written: list[str] = []

    # ── 1. Recover the approved brief from the gather step's committed output ──
    # FAIL LOUD on an unrecoverable brief: this tool is the SOLE producer of the
    # project artifacts, so emitting empty ones would let the require_completed
    # end-condition complete the meta run on garbage and start DPE brief-less.
    # Raising fails the step → the run cannot complete → the error surfaces.
    gs_path = ws / "meta_conversation" / "gather" / "gather_state.json"
    if not gs_path.is_file():
        raise ValueError(f"emit_project_artifacts: gather_state.json not found at {gs_path}")
    try:
        data = json.loads(gs_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        raise ValueError(f"emit_project_artifacts: gather_state.json is not valid JSON: {e}")
    brief = data.get("brief") or {}
    stories = brief.get("user_stories")
    if not (isinstance(stories, list) and any(str(s).strip() for s in stories)):
        raise ValueError("emit_project_artifacts: brief has no user stories — refusing to "
                         "emit an empty brief (the run must not complete on an incomplete brief).")

    # ── 2. project brief slot (skillflow get_project_brief_dir == <ws>/project) ──
    project_dir = ws / "project"
    project_dir.mkdir(parents=True, exist_ok=True)

    brief_md = format_brief_as_markdown(brief)
    (project_dir / "project_brief.md").write_text(brief_md, encoding="utf-8")
    written.append("project/project_brief.md")

    # ── 3. spec.md — the verbatim requirements conversation (un-summarized) ──
    convo = ws / "meta" / "conversation.md"
    if convo.is_file():
        try:
            spec_md = build_spec_markdown(convo.read_text(encoding="utf-8"))
        except Exception:
            spec_md = ""
        if spec_md:
            (project_dir / "spec.md").write_text(spec_md, encoding="utf-8")
            written.append("project/spec.md")

    # ── 4. step1_goals.json → finalize step dir (live cross-config source) ──
    if out_dir:
        od = Path(out_dir)
        od.mkdir(parents=True, exist_ok=True)
        (od / "step1_goals.json").write_text(
            json.dumps(brief_to_step1_goals(brief), indent=2, ensure_ascii=False),
            encoding="utf-8")
        written.append("step1_goals.json")

    return {"written": written, "emitted": True}
