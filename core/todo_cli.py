"""Compatibility forward: the todo CLI moved to `dsh_todo.cli` (integrations/dsh-director/todo/).

This module IS that module (`core.todo_cli is dsh_todo.cli`): one implementation, and a
monkeypatch through either name lands on it. Kept for the backend and the Python tests for the
compatibility week (design dsh-director section 9 MF8), then deleted.
"""
import sys

from core.dsh_todo_hooks import install

install()
from dsh_todo import cli as _module  # noqa: E402 - importable only after install()

if __name__ == "__main__":   # `python -m core.todo_cli`, as before: the same entry as `python -m dsh_todo`
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(_module.main())

sys.modules[__name__] = _module
