"""tasks_manifest_complete — the manifest and the cards on disk must agree.

Step 3 is `output.mode: content`: promotion `rmtree`s the whole step directory
and renames staging over it, so a file that is not in staging is DELETED. The
agent's own prompt describes the opposite mental model —

    2. Step staging (.tmp) — files you just wrote go here FIRST. They are
       promoted to the step output dir when the step completes.
    3. Step output — files from previous retries of this step (if any).
    read_file and list_tree search in order: project root -> staging -> output.

— a LAYERED read with nothing about a destructive write. An agent that has just
been rejected reads that, sees its nine cards in step output, and re-emits only
the ones it changed. That is the reasonable reading, and it silently destroys
the rest.

Live, 2026-08-26 (jinyong-hud): a re-plan wrote 2 of 9 cards. Promotion deleted
the other 8 — git-confirmed in the artifact history:

    + backlog_closure.json          11 +++++++++++
    - contract_wiring.json          12 ------------
    - health_bar_numbers.json        9 ---------
    - hud_derivation.json           12 ------------
    ... 8 deletions in one commit

`tasks_manifest.json` still named all eight. The step's only validation was
`file_exists` on the manifest, so this passed, and the break would not have
surfaced until the task loop reached the first missing card — by then the cards
were gone from the step dir and only recoverable from artifact history.

This turns that into a step-local validation failure the agent can fix on the
spot, with the reason spelled out.
"""

import json
from pathlib import Path


def tasks_manifest_complete(files: list[str] | None = None, *,
                            workspace_root: str = "", step_dir: str = "",
                            out_dir: str = "", **_ignored) -> dict:
    # A VALIDATION tool is handed `workspace_root` — StepValidator sets it to
    # this step's staging dir (see skillflow file_exists, the same contract).
    # Written first against `step_dir`, which nothing passes, so the tool looked
    # in "." and failed every run with "tasks_manifest.json not found at
    # tasks_manifest.json" — a validation that blocks the step it was added to
    # protect. The other two names are kept so the same function also works when
    # called as an ordinary tool step.
    root = Path(workspace_root or step_dir or out_dir or ".")
    manifest_path = root / "tasks_manifest.json"
    if not manifest_path.is_file():
        return {"passed": False,
                "error": f"tasks_manifest.json not found at {manifest_path}"}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"passed": False, "error": f"tasks_manifest.json unreadable: {e}"}

    named: list[str] = []
    for group in manifest.get("execution_order") or []:
        if isinstance(group, str):
            named.append(group)
        else:
            named.extend(str(t) for t in (group or []))

    cards_dir = root / "tasks"
    on_disk = {p.stem for p in cards_dir.glob("*.json")} if cards_dir.is_dir() else set()

    missing = [t for t in named if t not in on_disk]
    orphan = sorted(on_disk - set(named))

    if not missing and not orphan:
        return {"passed": True, "tasks": len(named),
                "summary": f"{len(named)} task(s), every card present"}

    parts = []
    if missing:
        parts.append(
            f"tasks_manifest.json names {len(missing)} task(s) with NO card on "
            f"disk: {', '.join(missing)}. This step is output.mode=content — "
            f"promotion REPLACES the whole step directory, so a card you did "
            f"not write into staging this run is DELETED, even though you can "
            f"still read it from the previous run's step output. Re-emit EVERY "
            f"card named in the manifest, not only the ones you changed.")
    if orphan:
        parts.append(
            f"{len(orphan)} card(s) exist but are not in execution_order: "
            f"{', '.join(orphan)} — add them to the manifest or delete them.")
    return {"passed": False, "error": " ".join(parts),
            "missing": missing, "orphan": orphan,
            "named": len(named), "on_disk": len(on_disk)}
