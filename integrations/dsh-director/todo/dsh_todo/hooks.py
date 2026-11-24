"""The host's half of the todo runtime: the one injection point of this package.

Five names are the host's to supply, because they are policy the host already owns:

* `DeferralLedger` - the bounded absence EPISODE (wait, ceiling, the sentence an expired
  absence ends with). The runtime hands every unmeasurable predicate to it and invents no
  second state machine (runtime.py), so a host whose gate pipeline already has a ledger
  supplies that one.
* `read_absence(report_path) -> dict | None` - the absence a gate's own report declares, read
  exactly as the host's gate pipeline reads it.
* `candidate_rejection(report_bytes, status, artifact) -> str | None` - the host's judgement of
  a candidate report before it is written: None accepts it, a string is the reason `finish`
  refuses with (`report_rejected`). Any exception is the CLI's `internal` refusal.
* `TEMPLATES_DIR` - where the host keeps its reviewed templates, or None for the package's own
  `todo_templates/` beside `dsh_todo/`. The TEMPLATES setting still overrides it (templates.py).
  Either replaces the package's set, so the host's directory also carries the package's
  `cost_audit.yaml` (copy or link it) and a `find_verify` batch template: director.md tells
  every director to dispatch both.
* `setting(name) -> str | None` - the package's three settings, STATE_DIR (the plan store's
  parent), TEMPLATES (a template directory) and CLOCK_OVERRIDE ("1" lets `--now` move the
  clock). The default reads DSH_TODO_<name>; a host may also honour names of its own.

DSH_TODO_HOOKS names the module that supplies them (a host that imports dsh_todo in-process may
set `dsh_todo.HOOKS` instead); DSH_TODO_HOOKS_PATH, if set, is appended to sys.path first so that
module can be imported from a cwd outside the host's checkout. Neither set, the package's own
`dsh_todo.default_hooks` is used. Read once, at import: a CLI call is one process.
"""
from __future__ import annotations

import importlib
import os
import sys

import dsh_todo


def _host():
    name = os.environ.get("DSH_TODO_HOOKS") or dsh_todo.HOOKS
    if not name:
        from dsh_todo import default_hooks
        return default_hooks
    path = os.environ.get("DSH_TODO_HOOKS_PATH")
    if path and path not in sys.path:
        sys.path.append(path)
    return importlib.import_module(name)


_HOST = _host()
DeferralLedger = _HOST.DeferralLedger
read_absence = _HOST.read_absence
candidate_rejection = _HOST.candidate_rejection
TEMPLATES_DIR = _HOST.TEMPLATES_DIR
if getattr(_HOST, "setting", None) is not None:
    setting = _HOST.setting
else:
    from dsh_todo.default_hooks import setting  # noqa: E402 - a host that predates the hook
