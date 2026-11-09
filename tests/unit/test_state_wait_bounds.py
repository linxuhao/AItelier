"""Guard the shared bound on the must-complete waits in the wait-contention tests.

``tests/unit/test_state_wait_contention.py`` waits on real SQLite/SkillFlow work,
so a correct-but-slow recovery page must never decide a verdict.  That is why its
six must-complete handshakes are bounded by the module constant
``HANDSHAKE_SECONDS`` instead of a number: the constant is the single place to
raise the bound, and raising it is exactly what the slow-page witness needed.
Four sites are deliberate non-must-complete bounds -- three sub-second poll
intervals and the ``budget`` drain.

A new must-complete wait written as ``asyncio.wait_for(waiter, 20.0)`` would look
identical today and silently escape that single place tomorrow.  This module is
the guard for that defect class.  :func:`audit_wait_bounds` parses the module and
classifies every ``asyncio.wait`` / ``asyncio.wait_for`` bound as
``must-complete`` (the Name ``HANDSHAKE_SECONDS``), ``allowed`` (a numeric literal
strictly below 1.0, i.e. a sub-second poll interval, or the Name ``budget``, the
drain bound) or ``refused`` -- and a refusal names the site: its line number and
the bound's source text.  Run this module directly to print the audit for a
reader.
"""
import ast
from pathlib import Path
import shutil
from typing import NamedTuple

# Default target: the real module, resolved relative to this test file so it works
# from any working directory.  A copy (as the self-witnessing test makes) is passed
# explicitly.
DEFAULT_MODULE = Path(__file__).resolve().with_name("test_state_wait_contention.py")

MUST_COMPLETE = "must-complete"
ALLOWED = "allowed"
REFUSED = "refused"

# The one shared bound every must-complete handshake must be written against.
MUST_COMPLETE_BOUNDS = {"HANDSHAKE_SECONDS"}
# Deliberate, named non-must-complete bounds: the drain bound of the slow-page test.
ALLOWED_BOUND_NAMES = {"budget"}
# The largest numeric literal that may bound a wait: a sub-second poll interval.
SUB_SECOND_LIMIT = 1.0


class WaitSite(NamedTuple):
    """One ``asyncio.wait_for`` / ``asyncio.wait`` bound, with its verdict."""

    line: int
    column: int
    callee: str
    bound: str
    classification: str
    message: str


def _callee(node):
    """The bare ``asyncio.<name>`` callee of *node*, or None.

    Awaiting is irrelevant here: ``ast.walk`` yields the Call node of an awaited
    expression and of its un-awaited form alike, so ``asyncio.wait`` is found
    whether or not it is awaited.
    """
    func = node.func
    if (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"):
        return func.attr
    return None


def _bound_node(call, callee):
    """The AST node that bounds *call*, or None when the site declares no bound."""
    if callee == "wait_for" and len(call.args) >= 2:
        return call.args[1]
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return keyword.value
    return None


def _numeric_value(node):
    """*node*'s value when it is a plain numeric literal, else None.

    ``True``/``False`` are ints in Python but are not bounds, so bools are skipped.
    """
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
        if isinstance(node.value, (int, float)):
            return float(node.value)
    return None


def classify_bound(bound, line, callee):
    """Classify one bound: ``(classification, source text, message)``.

    The message is empty unless the site is refused, in which case it names the
    site -- line number and bound text -- so the failure says which wait escaped.
    """
    if bound is None:
        text = "<no bound>"
        return (REFUSED, text,
                f"line {line}: {callee}(...) declares no bound, so the audit cannot "
                f"classify it; a wait must be bounded by HANDSHAKE_SECONDS, a "
                f"sub-second literal, or the `budget` drain")
    text = ast.unparse(bound)
    if isinstance(bound, ast.Name) and bound.id in MUST_COMPLETE_BOUNDS:
        return MUST_COMPLETE, text, ""
    if isinstance(bound, ast.Name) and bound.id in ALLOWED_BOUND_NAMES:
        return ALLOWED, text, ""
    value = _numeric_value(bound)
    if value is not None and value < SUB_SECOND_LIMIT:
        return ALLOWED, text, ""
    if value is not None:
        reason = (f"a numeric literal bound >= {SUB_SECOND_LIMIT:.1f} silently escapes "
                  f"the shared bound the slow-page witness raised")
    else:
        reason = "the audit cannot classify this bound expression"
    return (REFUSED, text,
            f"line {line}: {callee}(..., {text}) is refused -- {reason}; must-complete "
            f"waits use HANDSHAKE_SECONDS, and only a numeric bound < "
            f"{SUB_SECOND_LIMIT:.1f} or the `budget` drain may be a literal")


def audit_wait_bounds(module_path=DEFAULT_MODULE):
    """Every wait bound in *module_path*, in source order.

    Returns a list of :class:`WaitSite` records; that list is the classification
    the tests assert on, and :func:`print_audit` renders the same records.
    """
    path = Path(module_path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee(node)
        if callee not in ("wait_for", "wait"):
            continue
        bound = _bound_node(node, callee)
        classification, text, message = classify_bound(bound, node.lineno, callee)
        sites.append(WaitSite(node.lineno, node.col_offset, callee, text,
                              classification, message))
    sites.sort(key=lambda site: (site.line, site.column))
    return sites


def audit_lines(sites, module_path=DEFAULT_MODULE):
    """The printed form: one line per site, then a summary line of the counts."""
    lines = []
    for site in sites:
        line = (f"{site.classification:<13} line {site.line:>4}  "
                f"{site.callee}(..., {site.bound})")
        if site.message:
            line += f"  | {site.message}"
        lines.append(line)
    counts = {name: 0 for name in (MUST_COMPLETE, ALLOWED, REFUSED)}
    for site in sites:
        counts[site.classification] += 1
    lines.append(
        f"summary: {counts[MUST_COMPLETE]} must-complete, {counts[ALLOWED]} allowed, "
        f"{counts[REFUSED]} refused ({len(sites)} asyncio.wait/wait_for bounds in "
        f"{Path(module_path).name})")
    return lines


def print_audit(module_path=DEFAULT_MODULE):
    """Print the audit and return the records (what a reader runs to count sites)."""
    sites = audit_wait_bounds(module_path)
    for line in audit_lines(sites, module_path):
        print(line)
    return sites


def test_every_must_complete_wait_uses_the_shared_bound():
    sites = audit_wait_bounds()
    refused = [site for site in sites if site.classification == REFUSED]
    assert not refused, "the audit refused these wait bounds:\n" + "\n".join(
        site.message for site in refused)
    must_complete = [site for site in sites
                     if site.classification == MUST_COMPLETE]
    assert must_complete, ("no must-complete wait was found: the audit is not looking "
                           "at the wait-contention module")


def test_the_four_deliberate_bounds_are_allowed_and_the_rest_use_the_constant():
    sites = audit_wait_bounds()
    allowed = [site for site in sites if site.classification == ALLOWED]
    must_complete = [site for site in sites if site.classification == MUST_COMPLETE]
    classifications = {site.classification for site in sites}

    assert len(sites) == 10, (
        f"expected the ten known wait sites, audited {len(sites)}: "
        + ", ".join(f"line {site.line} {site.bound}" for site in sites))
    assert len(allowed) == 4, (
        "the deliberate bounds are the three sub-second poll intervals plus the "
        f"`budget` drain, audited {[(site.line, site.bound) for site in allowed]}")
    assert len(must_complete) == 6, (
        f"every remaining site must use HANDSHAKE_SECONDS, audited "
        f"{[(site.line, site.bound) for site in must_complete]}")
    assert classifications == {ALLOWED, MUST_COMPLETE}, (
        f"unexpected classifications present: {sorted(classifications)}")
    assert all(site.bound == "HANDSHAKE_SECONDS" for site in must_complete), (
        "a must-complete site is not written against the shared bound: "
        f"{[(site.line, site.bound) for site in must_complete]}")

    polls = [site for site in allowed if site.bound != "budget"]
    drains = [site for site in allowed if site.bound == "budget"]
    assert len(polls) == 3, (
        f"expected three sub-second poll intervals, audited "
        f"{[(site.line, site.bound) for site in polls]}")
    assert len(drains) == 1, (
        f"expected exactly one `budget` drain bound, audited "
        f"{[(site.line, site.bound) for site in drains]}")
    assert all(float(site.bound) < SUB_SECOND_LIMIT for site in polls), (
        f"a poll interval is not sub-second: {[(site.line, site.bound) for site in polls]}")


def test_the_audit_refuses_a_literal_must_complete_bound(tmp_path):
    target = "asyncio.wait_for(waiter, HANDSHAKE_SECONDS)"
    replacement = "asyncio.wait_for(waiter, 20.0)"
    original = DEFAULT_MODULE.read_text(encoding="utf-8")
    index = original.find(target)
    assert index != -1, f"the module no longer contains {target!r}"
    line_number = original.count("\n", 0, index) + 1

    copy = tmp_path / "test_state_wait_contention.py"
    shutil.copyfile(DEFAULT_MODULE, copy)
    mutated = original[:index] + replacement + original[index + len(target):]
    assert mutated.count("\n") == original.count("\n"), (
        "the rewrite must not change the line count")
    copy.write_text(mutated, encoding="utf-8")

    refused = [site for site in audit_wait_bounds(copy)
               if site.classification == REFUSED]
    assert len(refused) == 1, (
        f"rewriting line {line_number} ({target!r} -> {replacement!r}) must be "
        f"refused exactly once, audited {[(s.line, s.bound) for s in refused]}")
    assert refused[0].line == line_number, (
        f"the audit refused line {refused[0].line}, not line {line_number}, the line "
        f"it was written {replacement!r} on")
    assert "20.0" in refused[0].bound, (
        f"the refusal must name the bound it read, got {refused[0].bound!r}")


if __name__ == "__main__":
    print_audit()
