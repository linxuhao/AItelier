# tests/unit/test_source_lines_are_not_mangled.py
#
# `py_compile` returns 0 on every file in this repo, so a mangled line can be
# SYNTACTICALLY VALID and still reach a delivery unnoticed. That is not
# hypothetical: `aitelier/tools/run_tests/impl.py:663/666` and
# `tests/skillflow/test_coding_impl_gate.py:142/143` were delivered with
# self-repeating fragments, and nothing in the suite could see them because
# every one of them parsed.
#
# The check is AST-based on purpose, and it is deliberately NARROW. A text
# pattern cannot do this job: a regex for "a repeated token" fires on
# legitimate code (`lambda x: x`, a test that QUOTES the damage, a loop whose
# target shadows its iterable), so a text-based version would have to be
# narrowed until it stopped catching the real thing.
#
# What it catches: a function body whose ONLY executable statement is a call
# to the function itself — `def f(): f()`. That is what the observed
# `...():was():was()` collapse reduces to, and no hand-written function has
# that shape.
#
# What it does NOT catch, and why: a statement duplicated verbatim
# (`return x` written twice, a duplicated block of prose). Measured against
# this repo, "one statement repeated verbatim" fires on 15 legitimate sites
# (two identical assertions in a row, a parametrized setup run twice), so it
# is not a defect signal at all — it is what ordinary code looks like. Nothing
# cheap in text can separate the two, so the guarantee here is stated rather
# than overclaimed: this file catches the self-call collapse, and the OTHER
# half of the delivery card's rule is honored by reading each written file
# back, which is the only thing that sees damage introduced during a write.
import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _python_sources():
    for path in _ROOT.glob("**/*.py"):
        if set(path.parts) & {"evidence", ".git", "__pycache__", "node_modules"}:
            continue
        yield path


def _mangled_shapes(tree: ast.AST):
    """The one damage shape that is unambiguous from the AST."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Executable statements only: a docstring is an Expr/Constant and is
        # not a statement for this purpose.
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
            continue          # unparsable is the compiler's job, not this one
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
    """Pole test for the checker itself: a check nobody has seen fail is a
    claim, not a guard. The two `True` rows are the observed delivery damage
    (with and without a docstring); the three `False` rows are ordinary code
    that must NOT be flagged — including the one that merely MENTIONS itself,
    which is what a text-based check would wrongly fire on."""
    hits = list(_mangled_shapes(ast.parse(source)))
    assert bool(hits) is catches, hits
