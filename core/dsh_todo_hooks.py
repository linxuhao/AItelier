"""AItelier's side of the dsh_todo package (integrations/dsh-director/todo/dsh_todo).

dsh_todo imports nothing from AItelier; it asks its host for four hooks (dsh_todo/hooks.py).
This module is that host module, so the todo runtime runs exactly the policy it ran as
core/todo_*.py:

* `DeferralLedger`, `read_absence` - the gate pipeline's own (core.gate_deferral), including its
  AITELIER_GATE_DEFERRAL_* tunables.
* `candidate_rejection` - core.state_report_integrity.validate_external_semantics, imported
  lazily at `finish` as before; its StateConflict becomes the `report_rejected` reason, and any
  other exception stays the CLI's `internal` refusal.
* `TEMPLATES_DIR` - the repository's todo_templates/, the directory core/todo_templates.py
  defaulted to (the templates are not moved in this step).

Two ways in. In-process (the core.todo_* forwards): `install()`. The driver's
`python -m dsh_todo`, run from `TODO_ROOT`: the environment `CLI_ENV` (core/dsh_driver.py).
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

from core.gate_deferral import DeferralLedger, read_absence  # noqa: F401 - the hooks dsh_todo reads

REPO_ROOT = Path(__file__).resolve().parents[1]
#: The directory that holds the dsh_todo package: the CLI's cwd, and what the forwards import from.
TODO_ROOT = REPO_ROOT / "integrations" / "dsh-director" / "todo"
#: DSH_TODO_HOOKS_PATH lets the CLI import this module from TODO_ROOT, outside the repository root.
CLI_ENV = {"DSH_TODO_HOOKS": __name__, "DSH_TODO_HOOKS_PATH": str(REPO_ROOT)}
TEMPLATES_DIR = REPO_ROOT / "todo_templates"


#: This deployment's older names for dsh_todo's settings, honoured after the package's own
#: DSH_TODO_<name> so existing tests and drivers that set them keep working.
LEGACY_SETTINGS = {"STATE_DIR": "AITELIER_DSH_STATE_DIR", "TEMPLATES": "AITELIER_TODO_TEMPLATES",
                   "CLOCK_OVERRIDE": "AITELIER_TODO_CLOCK_OVERRIDE"}


def setting(name: str) -> str | None:
    """dsh_todo's settings hook: DSH_TODO_<name>, else this deployment's legacy name."""
    return os.environ.get("DSH_TODO_" + name) or os.environ.get(LEGACY_SETTINGS.get(name, ""), "") or None


def candidate_rejection(report_bytes: bytes, status: str, artifact: str | None) -> str | None:
    from core.state_graph import StateConflict
    from core.state_report_integrity import validate_external_semantics
    try:
        validate_external_semantics(report_bytes, status, artifact)
    except StateConflict as exc:
        return str(exc)
    return None


def install() -> None:
    """Make `dsh_todo` importable and select these hooks, before any dsh_todo module is imported.

    sys.path, not a pip install: the production venv stays as it is, and the bind-mounted /app
    and the host checkout see the package as soon as the checkout has it. Appended, so it
    shadows nothing. Not through os.environ, which every subprocess would inherit."""
    if str(TODO_ROOT) not in sys.path:
        sys.path.append(str(TODO_ROOT))
    import dsh_todo
    if dsh_todo.HOOKS is None:
        dsh_todo.HOOKS = __name__
