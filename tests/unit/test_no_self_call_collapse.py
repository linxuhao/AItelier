# tests/unit/test_no_self_call_collapse.py
#
# `py_compile` returns 0 on every file in this repo, so a mangled line can be
# syntactically valid and still reach a delivery unnoticed. One damage shape
# IS unambiguous from the AST: a function body whose ONLY executable statement
# is a call to itself — `def f(): f()`. That is what an observed
# `...():was():was()` collapse reduces to, and no hand-written function has
# that shape.
#
# The OTHER delivery shape — a statement written twice — is NOT visible in the
# AST (it is ordinary code), so it is caught by the line rule in
# `test_no_verbatim_previous_line_dup.py`, not here. This file only guards the
# self-call collapse and runs on its own file too.
import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _python_sources():
    for path in sorted(_ROOT.glob("**/*.py")):
        if set(path.parts) & {"evidence", ".git", "__pycache__", "node_modules"}:
            continue
        yield path


def _mangled_shapes(tree):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = [n for n in node.body if not isinstance(n, ast.Expr)
                or not isinstance(getattr(n, "value", None), ast.Constant)]
        if len(body) == 1 and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Call) \
                and isinstance(body[0].value.func, ast.Name) \
                and body[0].value.func.id == node.name:
            yield node.lineno, f"body is only `{node.name}()`"


def test_no_python_source_is_a_self_call_and_nothing_else():
    offenders = []
    for path in _python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        for number, what in _mangled_shapes(tree):
            offenders.append(f"{path.relative_to(_ROOT)}:{number}: {what}")
    assert offenders == [], (
        "a write went wrong — these functions call only themselves:\n"
        + "\n".join(offenders))


@pytest.mark.parametrize("source,catches", [
    ("def f():\n    f()\n", True),
    ("def f():\n    \"\"\"doc\"\"\"\n    f()\n", True),
    ("def f():\n    return 1\n", False),
    ("def f():\n    g()\n", False),
    ("def f():\n    f\n", False),
    ("def f():\n    return f()\n", False),
])
def test_the_check_catches_the_shape_it_claims(source, catches):
    hits = list(_mangled_shapes(ast.parse(source)))
    assert bool(hits) is catches, hits


def test_the_self_call_pole_is_red_on_this_own_file():
    damaged = Path(__file__).resolve().read_text(encoding="utf-8") + (
        "\n\ndef _seeded_self_call():\n    _seeded_self_call()\n")
    offenders = [f"{n}:{w}" for n, w in _mangled_shapes(ast.parse(damaged))]
    assert any("_seeded_self_call" in o for o in offenders), offenders
