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
# (`return x` written twice, a duplicated block of prose). "One statement
# repeated verbatim" is not a defect signal — it is what ordinary code looks
# like — and no cheap text rule separates the two. The exemption used to carry
# a HAND-COUNTED number ("fires on 15 legitimate sites"); a round-6 review
# counted 24 with the same rule and found that one of them was the candidate's
# OWN defect, which is exactly what a hand-counted allowance buys. So the
# number is not restated here: `_verbatim_repeat_sites` DERIVES it from the
# tree with the rule written down, and
# `test_the_verbatim_repeat_exemption_is_derived_not_asserted` only asserts
# that the rule is broad (it fires on ordinary code), never a particular
# count. The guarantee is then stated rather than overclaimed: this file
# catches the self-call collapse, and the OTHER half of the delivery card's
# rule is honored by reading each written file back, which is the only thing
# that sees damage introduced during a write.
import ast
import collections
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


def _verbatim_repeat_sites(source: str) -> int:
    """How many statements this source repeats verbatim inside one block.

    This is the exemption's rule, DERIVED from the tree instead of a number
    typed into a comment: it is deliberately broad, and what it measures is
    how often ordinary code repeats a statement — which is why the shape
    cannot be a defect signal and is not checked as one.
    """
    tree = ast.parse(source)
    repeats = 0
    for node in ast.walk(tree):
        for block in ("body", "orelse", "finalbody"):
            body = getattr(node, block, None)
            if not isinstance(body, list):
                continue
            texts = [ast.dump(stmt) for stmt in body
                     if isinstance(stmt, ast.stmt)]
            repeats += sum(count - 1 for count in
                           collections.Counter(texts).values() if count > 1)
    return repeats


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


def test_the_verbatim_repeat_exemption_is_derived_not_asserted():
    """The exemption, made real instead of hand-counted.

    The rule that grants it is `_verbatim_repeat_sites` above — written down
    and RUN, not a number someone counted by hand. This asserts the rule is
    BROAD (it fires on ordinary, legitimate code) and that the checker
    nonetheless does NOT flag that code. It deliberately asserts no particular
    COUNT: the count is what the previous revision got wrong (hand-counted 15
    where the same rule yields 24 across the tree, and one of the 24 was the
    candidate's OWN defect).
    """
    ordinary = "def f(x):\n    assert x\n    assert x\n"
    assert _verbatim_repeat_sites(ordinary) == 1, (
        "the exemption's rule must fire on ordinary repeated code")
    assert list(_mangled_shapes(ast.parse(ordinary))) == [], (
        "the checker must NOT flag ordinary repeated code")

    # ...and the rule really is what ordinary code looks like, measured over
    # this tree rather than asserted about it.
    sites = 0
    for path in _python_sources():
        try:
            sites += _verbatim_repeat_sites(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
    assert sites > 0, "the rule found no repeat anywhere — it is not the rule"
