"""Compatibility forward: the todo templates moved to `dsh_todo.templates` (integrations/dsh-director/todo/).

This module IS that module (`core.todo_templates is dsh_todo.templates`): one implementation, and a
monkeypatch through either name lands on it. Kept for the backend and the Python tests for the
compatibility week (design dsh-director section 9 MF8), then deleted.
"""
import sys

from core.dsh_todo_hooks import install

install()
from dsh_todo import templates as _module  # noqa: E402 - importable only after install()

sys.modules[__name__] = _module
