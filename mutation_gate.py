"""Pytest plugin behind the write-opening mutation gate.

Activated ONLY when the environment sets STATE_MUTATION_ACTION (see
tests/unit/test_write_opening_coverage.py). It then infects the ONE callable
that `core.state_commands._handlers` binds to that write action, so that every
time the action runs it ALSO opens the project it touched — the exact mutation
criterion 2 demands ("this write action opens the project as a side effect").

It records how many times the mutation actually FIRED (a green run with
`fired == 0` proves nothing), and writes that count to STATE_MUTATION_FIRE.

WHY THIS FILE SITS AT THE REPOSITORY ROOT: it is neither application code nor a
test — it is a pytest PLUGIN, loaded by NAME (`-p mutation_gate`) by the
mutation subprocesses that `tests/unit/test_write_opening_coverage.py` spawns
with `cwd=<repository root>`. `python -m pytest` puts the current directory on
`sys.path`, so the repository root is the one place from which that name
resolves without the test editing `sys.path` or installing a package.
"""
import json
import os
import tempfile
from pathlib import Path

_STATE = {"action": None, "target": None, "fired": 0, "error": None}


def pytest_configure(config):
    action = os.environ.get("STATE_MUTATION_ACTION")
    if not action:
        return
    _STATE["action"] = action
    from core.state_commands import WRITE_REQUESTS, _handlers
    from core.state_database import StateDatabase
    from core.state_graph import StateGraphStore
    from core.state_service import StateService
    if action not in WRITE_REQUESTS:
        raise RuntimeError(f"'{action}' is not a write action in WRITE_REQUESTS")
    with tempfile.TemporaryDirectory() as td:
        probe = StateService(StateDatabase(str(Path(td) / "probe.sqlite")),
                             project_read_trusted=True)
        handler = _handlers(probe)[action]
    owner = type(handler.__self__)
    name = handler.__name__
    original = getattr(owner, name)

    def wrap(fn):
        def inner(self, *args, **kwargs):
            # Open AFTER the handler runs (also on its failure path): the
            # mutation asserts "this action LEAVES the project open as a side
            # effect", so an action that legitimately closes must not be able
            # to undo the mutation, and an action that fails must not escape it.
            try:
                result = fn(self, *args, **kwargs)
            except Exception:
                _open(self, args, kwargs)
                raise
            _open(self, args, kwargs)
            return result

        def _open(self, args, kwargs):
            try:
                model = WRITE_REQUESTS.get(action)
                positional = dict(zip(model.model_fields, args)) if model else {}
                values = {**positional, **kwargs}
                pids = [values[name] for name in
                        ("project_id", "sender_project_id", "target_project_id")
                        if values.get(name)]
                if not pids and values.get("attempt_id"):
                    store = self if isinstance(self, StateGraphStore) else self.store
                    resolved = store.project_for_attempt(values["attempt_id"])
                    pids = [resolved] if resolved else []
                if pids:
                    store = self if isinstance(self, StateGraphStore) else self.store
                    for pid in pids:
                        store.set_project_access(pid, "public", "mutation-gate")
                        if store.is_project_public(pid):
                            _STATE["fired"] += 1
                        _STATE["last_pid"] = str(pid)
                else:
                    _STATE["last_values"] = {k: str(v)[:40] for k, v in values.items()}
            except Exception as exc:
                _STATE["error"] = f"{type(exc).__name__}: {exc}"
        return inner

    setattr(owner, name, wrap(original))
    _STATE["target"] = f"{owner.__module__}.{owner.__qualname__}.{name}"


def pytest_sessionfinish(session, exitstatus):
    fire = os.environ.get("STATE_MUTATION_FIRE")
    if fire:
        Path(fire).write_text(json.dumps(_STATE))
