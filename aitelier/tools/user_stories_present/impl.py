"""user_stories_present — validation tool for the meta_conversation gather step.

Enforces that a FINALIZED project brief carries at least one user story. The
gather step writes a single ``gather_state.json``; this tool only enforces the
rule when the agent finalizes (``need_input == false``). On a question turn
(``need_input == true``) or when the file/brief is absent, it passes — so the
agent is free to keep asking clarifying questions.

Validation-tool contract (see skillflow/step_validation.py): takes ``files`` +
``workspace_root`` (the step's draft dir), returns ``{all_passed: bool,
results: [{passed, error}]}``.
"""

import json
from pathlib import Path


def user_stories_present(*, files=None, workspace_root: str = "", **kwargs) -> dict:
    base = Path(workspace_root or ".")
    target = None
    for pattern in (files or ["gather_state.json"]):
        candidate = base / pattern
        if candidate.is_file():
            target = candidate
            break
    # Nothing written yet → nothing to enforce.
    if target is None:
        return {"all_passed": True}

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        return {"all_passed": False, "results": [
            {"passed": False, "error": f"gather_state.json is not valid JSON: {e}"}]}

    # Question turn — the agent is still gathering; no brief to validate.
    if data.get("need_input"):
        return {"all_passed": True}

    brief = data.get("brief") or {}
    stories = brief.get("user_stories")
    if isinstance(stories, list) and any(str(s).strip() for s in stories):
        return {"all_passed": True}

    return {"all_passed": False, "results": [{
        "passed": False,
        "error": ("The project brief must contain at least one user story "
                  "(brief.user_stories, e.g. 'As a <role>, I want <action>, so "
                  "that <benefit>'). Either ask the user for one or draft one "
                  "from the conversation, then finalize."),
    }]}
