"""Guard the shared bound on the must-complete waits in the wait-contention tests.

``tests/unit/test_state_wait_contention.py`` waits on real SQLite/SkillFlow work,
so a correct-but-slow recovery page must never decide a verdict.  That is why its
must-complete handshakes are bounded by the module constant
``HANDSHAKE_SECONDS`` instead of a number: the constant is the single place to
raise the bound, and raising it is exactly what the slow-page witness needed.
A few sites are deliberate non-must-complete bounds -- the sub-second poll
intervals and the ``budget`` drain.

A must-complete wait has more than one spelling, and every one of them has to
reach the same classification: ``asyncio.wait_for(waiter, 20.0)``, a bare
``wait_for(waiter, 20.0)`` after ``from asyncio import wait_for``, an assignment
alias ``wf = asyncio.wait_for``, ``getattr(asyncio, "wait_for")(waiter, 20.0)``,
and ``asyncio.timeout(20.0)`` / ``asyncio.timeout_at(deadline, 20.0)`` used as an
async context manager.  :func:`audit_wait_bounds` parses the module, resolves
each of those spellings to a wait site, and classifies the site's bound as
``must-complete`` (the Name ``HANDSHAKE_SECONDS``), ``allowed`` (a numeric
literal strictly below 1.0, i.e. a sub-second poll interval, or the Name
``budget``, the drain bound) or ``refused``.  A wait-shaped call the resolver
cannot place is refused as unclassified rather than skipped, so a spelling the
audit has never seen cannot slip past it by being unknown.  Every refusal names
the site: its line number, the form it was written in, and the bound's source
text.

The counts are DERIVED from the audit's own output, never asserted as literals:
the tests check the invariant -- every recorded site is exactly one of the
shared bound or the named deliberate bounds, and no numeric literal bound
reaches 1.0 -- so adding a compliant wait to the guarded module cannot redden
the guard.  A companion witness inserts exactly one compliant
``await asyncio.wait_for(waiter, HANDSHAKE_SECONDS)`` into a ``tmp_path`` copy
and asserts the audit reports one more must-complete site and no refusal.

Run this module directly to print the audit for a reader.
"""
import ast
from pathlib import Path
import shutil
from typing import NamedTuple, Optional

import pytest

# Default target: the real module, resolved relative to this test file so it works
# from any working directory.  A copy (as the witnesses make) is passed explicitly.
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

# The asyncio primitives whose first argument (or ``timeout=`` keyword) is a bound.
WAIT_PRIMITIVES = ("wait", "wait_for", "timeout", "timeout_at")
# ``asyncio.timeout`` / ``asyncio.timeout_at`` take a deadline before an optional
# bound, so their bound is the LAST positional argument.
DEADLINE_FIRST = ("timeout", "timeout_at")

# How a wait site was spelled.  Each refusal carries one of these so the failure
# says WHICH form escaped, not merely that something did.
FORM_ATTRIBUTE = "asyncio.{name}"
FORM_DIRECT_IMPORT = "from asyncio import {name}"
FORM_DIRECT_IMPORT_ALIAS = "from asyncio import {name} as {local}"
FORM_ASSIGNMENT_ALIAS = "alias of asyncio.{name}"
FORM_GETATTR = 'getattr(asyncio, "{name}")'
FORM_GETATTR_DYNAMIC = "getattr(asyncio, <dynamic>)"
FORM_UNCLASSIFIED = "unclassified wait-shaped call"


class WaitSite(NamedTuple):
    """One wait bound found by the audit, with its verdict."""

    line: int
    column: int
    callee: str
    form: str
    bound: str
    classification: str
    message: str


class _Resolution(NamedTuple):
    """What :func:`_resolve_call` decided about one Call node.

    ``primitive`` is the asyncio primitive the call performs, or None when the
    call is wait-shaped but the resolver cannot place it -- in that case ``form``
    carries the reason it could not, and the site is refused rather than skipped.
    """

    primitive: Optional[str]
    form: str
    callee: str


class _Names(NamedTuple):
    """The module-level identities that let a call be resolved to an asyncio wait."""

    asyncio_names: frozenset
    direct_imports: dict
    aliases: dict
    local_defs: frozenset


def _asyncio_names(tree):
    """Every name bound to the ``asyncio`` module: ``asyncio``, ``import asyncio as``, ``x = asyncio``."""
    names = {"asyncio"}
    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "asyncio":
                    names.add(alias.asname or "asyncio")
        elif (isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
              and isinstance(node.targets[0], ast.Name)):
            assignments.append((node.targets[0].id, node.value.id))
    for _ in range(len(assignments) + 1):
        before = len(names)
        for target, source in assignments:
            if source in names:
                names.add(target)
        if len(names) == before:
            break
    return frozenset(names)


def _direct_imports(tree):
    """``{'wait_for': 'wait_for'}`` for ``from asyncio import wait_for [as ...]``."""
    mapping = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and node.module == "asyncio":
            for alias in node.names:
                if alias.name in WAIT_PRIMITIVES:
                    mapping[alias.asname or alias.name] = alias.name
    return mapping


def _assigned_aliases(tree, asyncio_names):
    """``{'wf': 'wait_for'}`` for ``wf = asyncio.wait_for``."""
    mapping = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Attribute):
            continue
        value = node.value
        if (value.attr in WAIT_PRIMITIVES and isinstance(value.value, ast.Name)
                and value.value.id in asyncio_names):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    mapping[target.id] = value.attr
    return mapping


def _local_defs(tree):
    """Names this module defines itself, so a bare ``wait`` naming one is not asyncio's."""
    return frozenset(
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))


def _names(tree):
    asyncio_names = _asyncio_names(tree)
    return _Names(asyncio_names, _direct_imports(tree),
                  _assigned_aliases(tree, asyncio_names), _local_defs(tree))


def _resolve_getattr(getattr_call, names):
    """Resolve ``getattr(<obj>, "<name>")(...)``, or None when it is not a wait shape.

    *getattr_call* is the inner ``getattr(...)`` Call node, i.e. the callee of the
    call the audit is looking at.
    """
    if not (isinstance(getattr_call.func, ast.Name) and getattr_call.func.id == "getattr"):
        return None
    if len(getattr_call.args) < 2:
        return None
    target, name_node = getattr_call.args[0], getattr_call.args[1]
    on_asyncio = isinstance(target, ast.Name) and target.id in names.asyncio_names
    literal = (name_node.value
               if isinstance(name_node, ast.Constant) and isinstance(name_node.value, str)
               else None)
    if literal is None:
        if on_asyncio:
            return _Resolution(None, FORM_GETATTR_DYNAMIC,
                               f"getattr({ast.unparse(target)}, ...)")
        return None
    if literal not in WAIT_PRIMITIVES:
        return None
    callee = f'getattr({ast.unparse(target)}, "{literal}")'
    if on_asyncio:
        return _Resolution(literal, FORM_GETATTR.format(name=literal), callee)
    if literal == "wait":
        # ``x.wait(...)`` on something else is a different API (asyncio.Event.wait,
        # threading.Event.wait), not an escape from the asyncio bound vocabulary.
        return None
    return _Resolution(None, FORM_UNCLASSIFIED, callee)


def _resolve_call(call, names):
    """Resolve *call* to an asyncio wait primitive, or None when it is not a wait shape.

    A wait-shaped call whose callee cannot be placed returns a resolution with
    ``primitive is None``: the audit refuses those instead of skipping them.
    """
    func = call.func
    if isinstance(func, ast.Call):
        return _resolve_getattr(func, names)
    if isinstance(func, ast.Attribute):
        if func.attr not in WAIT_PRIMITIVES:
            return None
        if isinstance(func.value, ast.Name) and func.value.id in names.asyncio_names:
            return _Resolution(func.attr, FORM_ATTRIBUTE.format(name=func.attr),
                               f"asyncio.{func.attr}")
        if func.attr == "wait":
            return None
        return _Resolution(None, FORM_UNCLASSIFIED, ast.unparse(func))
    if isinstance(func, ast.Name):
        if func.id in names.direct_imports:
            imported = names.direct_imports[func.id]
            form = (FORM_DIRECT_IMPORT.format(name=imported) if func.id == imported
                    else FORM_DIRECT_IMPORT_ALIAS.format(name=imported, local=func.id))
            return _Resolution(imported, form, func.id)
        if func.id in names.aliases:
            primitive = names.aliases[func.id]
            return _Resolution(primitive, FORM_ASSIGNMENT_ALIAS.format(name=primitive),
                               func.id)
        if func.id in WAIT_PRIMITIVES and func.id not in names.local_defs:
            return _Resolution(None, FORM_UNCLASSIFIED, func.id)
        return None
    return None


def _bound_node(call, primitive):
    """The AST node that bounds *call*, or None when the site declares no bound."""
    for keyword in call.keywords:
        if keyword.arg == "timeout":
            return keyword.value
    if primitive == "wait_for" and len(call.args) >= 2:
        return call.args[1]
    if primitive in DEADLINE_FIRST and call.args:
        return call.args[-1]
    return None


def _numeric_value(node):
    """*node*'s value when it is a plain numeric literal, else None.

    ``True``/``False`` are ints in Python but are not bounds, so bools are skipped.
    """
    if isinstance(node, ast.Constant) and not isinstance(node.value, bool):
        if isinstance(node.value, (int, float)):
            return float(node.value)
    return None


def _literal_value(text):
    """The float a bound's source text spells, or None when it is not a literal."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def classify_bound(bound, line, callee, form):
    """Classify one bound: ``(classification, source text, message)``.

    The message is empty unless the site is refused, in which case it names the
    site -- its form, line number and bound text -- so the failure says which
    wait escaped and how it was written.
    """
    if bound is None:
        return (REFUSED, "<no bound>",
                f"line {line}: {form} -- {callee}(...) declares no bound, so the audit "
                f"cannot classify it; a wait must be bounded by "
                f"{', '.join(sorted(MUST_COMPLETE_BOUNDS))}, a sub-second literal, or the "
                f"`budget` drain")
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
            f"line {line}: {form} -- {callee}(..., {text}) is refused -- {reason}; "
            f"must-complete waits use {', '.join(sorted(MUST_COMPLETE_BOUNDS))}, and only a "
            f"numeric literal bound < {SUB_SECOND_LIMIT:.1f} or the `budget` drain may be a "
            f"literal")


def _unresolved_message(resolution, line):
    return (f"line {line}: {resolution.form} -- the audit cannot resolve "
            f"{resolution.callee} to an asyncio wait primitive, so it refuses the call "
            f"instead of silently skipping it (fail-closed); a wait must be written so the "
            f"audit can read which bound it uses")


def audit_wait_bounds(module_path=DEFAULT_MODULE):
    """Every wait bound in *module_path*, in source order.

    Returns a list of :class:`WaitSite` records; that list is the classification
    the tests assert on, and :func:`print_audit` renders the same records.
    """
    path = Path(module_path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _names(tree)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolution = _resolve_call(node, names)
        if resolution is None:
            continue
        if resolution.primitive is None:
            sites.append(WaitSite(node.lineno, node.col_offset, resolution.callee,
                                  resolution.form, "<unresolved>", REFUSED,
                                  _unresolved_message(resolution, node.lineno)))
            continue
        bound = _bound_node(node, resolution.primitive)
        classification, text, message = classify_bound(
            bound, node.lineno, resolution.callee, resolution.form)
        sites.append(WaitSite(node.lineno, node.col_offset, resolution.callee,
                              resolution.form, text, classification, message))
    sites.sort(key=lambda site: (site.line, site.column))
    return sites


def audit_lines(sites, module_path=DEFAULT_MODULE):
    """The printed form: one line per site, then a summary line of the counts."""
    lines = []
    for site in sites:
        line = (f"{site.classification:<13} line {site.line:>4}  {site.form:<34} "
                f"{site.callee}(..., {site.bound})")
        if site.message:
            line += f"  | {site.message}"
        lines.append(line)
    counts = {name: 0 for name in (MUST_COMPLETE, ALLOWED, REFUSED)}
    for site in sites:
        counts[site.classification] += 1
    lines.append(
        f"summary: {counts[MUST_COMPLETE]} must-complete, {counts[ALLOWED]} allowed, "
        f"{counts[REFUSED]} refused ({len(sites)} wait bounds in "
        f"{Path(module_path).name})")
    return lines


def print_audit(module_path=DEFAULT_MODULE):
    """Print the audit and return the records (what a reader runs to count sites)."""
    sites = audit_wait_bounds(module_path)
    for line in audit_lines(sites, module_path):
        print(line)
    return sites


def _audit_synthetic(tmp_path, source, name):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return audit_wait_bounds(path)


def test_every_must_complete_wait_uses_the_shared_bound():
    sites = audit_wait_bounds()
    refused = [site for site in sites if site.classification == REFUSED]
    assert not refused, "the audit refused these wait bounds:\n" + "\n".join(
        site.message for site in refused)
    must_complete = [site for site in sites
                     if site.classification == MUST_COMPLETE]
    assert must_complete, ("no must-complete wait was found: the audit is not looking "
                           "at the wait-contention module")


def test_every_site_is_the_shared_bound_or_a_named_deliberate_bound():
    """The invariant, derived from the audit -- no expected site count is written down.

    Adding a fully compliant wait to the guarded module adds one must-complete
    site and changes nothing else, so this stays green; a literal count would
    redden the guard for a change it must accept.
    """
    sites = audit_wait_bounds()
    assert sites, "the audit found no wait sites at all; it is not looking at the module"
    refused = [site for site in sites if site.classification == REFUSED]
    assert not refused, "the audit refused these wait bounds:\n" + "\n".join(
        site.message for site in refused)
    assert {site.classification for site in sites} == {MUST_COMPLETE, ALLOWED}, (
        f"unexpected classifications present: "
        f"{sorted({site.classification for site in sites})}")

    for site in sites:
        if site.classification == MUST_COMPLETE:
            assert site.bound in MUST_COMPLETE_BOUNDS, (
                f"line {site.line} ({site.form}) is classified must-complete but its bound "
                f"is {site.bound!r}, not one of {sorted(MUST_COMPLETE_BOUNDS)}")
        else:
            value = _literal_value(site.bound)
            assert (site.bound in ALLOWED_BOUND_NAMES
                    or (value is not None and value < SUB_SECOND_LIMIT)), (
                f"line {site.line} ({site.form}) is classified allowed but its bound "
                f"{site.bound!r} is neither a named deliberate bound "
                f"{sorted(ALLOWED_BOUND_NAMES)} nor a sub-second literal")

    literal_sites = [(site.line, site.bound, _literal_value(site.bound)) for site in sites]
    offending = [(line, bound) for line, bound, value in literal_sites
                 if value is not None and value >= SUB_SECOND_LIMIT]
    assert not offending, (
        f"a numeric literal bound >= {SUB_SECOND_LIMIT:.1f} is recorded as a wait bound: "
        f"{offending}; must-complete waits use {sorted(MUST_COMPLETE_BOUNDS)}")

    allowed = [site for site in sites if site.classification == ALLOWED]
    drains = [site for site in allowed if site.bound in ALLOWED_BOUND_NAMES]
    polls = [site for site in allowed if site.bound not in ALLOWED_BOUND_NAMES]
    assert drains, ("no named deliberate drain bound is audited, so the `budget` site "
                    "either moved or stopped being recognised")
    assert polls, ("no sub-second poll interval is audited, so the polls either moved or "
                   "are being read as something else")
    assert all(site.bound in ALLOWED_BOUND_NAMES for site in drains), (
        f"a drain site carries an unnamed bound: {[(s.line, s.bound) for s in drains]}")
    assert all(_literal_value(site.bound) is not None
               and _literal_value(site.bound) < SUB_SECOND_LIMIT for site in polls), (
        f"a poll interval is not a sub-second literal: "
        f"{[(site.line, site.bound) for site in polls]}")
    assert {site.bound for site in drains} == ALLOWED_BOUND_NAMES, (
        f"the drain sites do not cover the named deliberate bounds "
        f"{sorted(ALLOWED_BOUND_NAMES)}: {sorted({site.bound for site in drains})}")


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
    assert DEFAULT_MODULE.read_text(encoding="utf-8") == original, (
        "the witness must not write to the guarded module itself")


# The four spellings a must-complete wait can escape through, each with a tiny
# synthetic module carrying it and the sites the audit must report.  ``{bound}``
# is filled with a literal (which must be refused) and with the shared constant
# (which must be classified, proving the spelling resolves to a normal wait site
# rather than merely being refused as unknown).
SPELLINGS = [
    pytest.param(
        "direct-import",
        "from asyncio import wait_for\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await wait_for(waiter, {bound})\n",
        [(5, "from asyncio import wait_for")],
        id="a-from-asyncio-import",
    ),
    pytest.param(
        "module-alias",
        "import asyncio\n"
        "\n"
        "wf = asyncio.wait_for\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await wf(waiter, {bound})\n",
        [(7, "alias of asyncio.wait_for")],
        id="b-module-level-alias",
    ),
    pytest.param(
        "getattr",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await getattr(asyncio, \"wait_for\")(waiter, {bound})\n",
        [(5, 'getattr(asyncio, "wait_for")')],
        id="c-getattr-asyncio",
    ),
    pytest.param(
        "timeout-context-manager",
        "import asyncio\n"
        "\n"
        "\n"
        "async def via_timeout():\n"
        "    async with asyncio.timeout({bound}):\n"
        "        return None\n"
        "\n"
        "\n"
        "async def via_timeout_at(deadline):\n"
        "    async with asyncio.timeout_at(deadline, {bound}):\n"
        "        return None\n",
        [(5, "asyncio.timeout"), (10, "asyncio.timeout_at")],
        id="d-asyncio-timeout",
    ),
]


@pytest.mark.parametrize("spelling, template, expected", SPELLINGS)
def test_each_spelling_resolves_and_its_literal_bound_is_refused(
        tmp_path, spelling, template, expected):
    literal = _audit_synthetic(tmp_path, template.format(bound="20.0"),
                               f"literal_{spelling}.py")
    assert len(literal) == len(expected), (
        f"the {spelling} spelling must be audited as exactly {len(expected)} wait "
        f"site(s), audited "
        f"{[(s.line, s.form, s.bound, s.classification) for s in literal]}")
    refused = [site for site in literal if site.classification == REFUSED]
    assert len(refused) == len(expected), (
        f"the {spelling} spelling must be refused at every site, audited "
        f"{[(s.line, s.form, s.bound, s.classification) for s in literal]}")
    for line, fragment in expected:
        matches = [site for site in refused if site.line == line]
        assert matches, (
            f"line {line} of the {spelling} module was not refused, audited "
            f"{[(s.line, s.form, s.classification) for s in literal]}")
        site = matches[0]
        assert fragment in site.form, (
            f"the refusal must name the form it read ({fragment!r} in {site.form!r})")
        assert fragment in site.message, (
            f"the refusal message must name the form it read ({fragment!r}): "
            f"{site.message}")
        assert "20.0" in site.bound, (
            f"the refusal must carry the literal it read, got {site.bound!r}")
        assert "literal" in site.message and "1.0" in site.message, (
            f"the refusal must say the literal escaped the shared bound: {site.message}")

    compliant = _audit_synthetic(tmp_path, template.format(bound="HANDSHAKE_SECONDS"),
                                 f"compliant_{spelling}.py")
    assert not [site for site in compliant if site.classification == REFUSED], (
        f"the {spelling} spelling written against the shared bound must not be refused: "
        f"{[(s.line, s.form, s.bound) for s in compliant if s.classification == REFUSED]}")
    classified = [site for site in compliant
                  if site.classification == MUST_COMPLETE]
    assert [(site.line, site.bound) for site in classified] == [
        (line, "HANDSHAKE_SECONDS") for line, _ in expected], (
        f"the {spelling} spelling must be classified by the bound it carries, audited "
        f"{[(s.line, s.form, s.bound, s.classification) for s in compliant]}")
    for site, (_, fragment) in zip(classified, expected):
        assert fragment in site.form, (
            f"the classified site must name its form ({fragment!r} in {site.form!r})")


# A wait-shaped call the resolver cannot place must be refused, not skipped: an
# unknown spelling is exactly how a literal bound escapes the shared constant.
UNRESOLVABLE = [
    pytest.param(
        "method-on-a-non-asyncio-object",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await asyncio.get_event_loop().wait_for(waiter, 20.0)\n",
        5,
        "unclassified wait-shaped call",
        "cannot resolve",
        id="wait_for-method-on-a-non-asyncio-object",
    ),
    pytest.param(
        "dynamic-getattr",
        "import asyncio\n"
        "\n"
        "NAME = \"wait_for\"\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await getattr(asyncio, NAME)(waiter, 20.0)\n",
        7,
        "getattr(asyncio, <dynamic>)",
        "cannot resolve",
        id="getattr-with-a-dynamic-name",
    ),
    pytest.param(
        "bare-name-from-another-module",
        "from concurrent.futures import wait\n"
        "\n"
        "\n"
        "def spelled(futures):\n"
        "    return wait(futures, 20.0)\n",
        5,
        "unclassified wait-shaped call",
        "cannot resolve",
        id="bare-wait-imported-from-another-module",
    ),
    pytest.param(
        "declares-no-bound",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await asyncio.wait_for(waiter)\n",
        5,
        "asyncio.wait_for",
        "declares no bound",
        id="resolved-spelling-with-no-bound",
    ),
]


@pytest.mark.parametrize("shape, source, line, fragment, phrase", UNRESOLVABLE)
def test_a_wait_shaped_call_the_audit_cannot_resolve_is_refused_not_skipped(
        tmp_path, shape, source, line, fragment, phrase):
    sites = _audit_synthetic(tmp_path, source, f"unresolvable_{shape}.py")
    refused = [site for site in sites if site.classification == REFUSED]
    assert len(refused) == 1, (
        f"the {shape} call must be refused rather than skipped, audited "
        f"{[(s.line, s.form, s.bound, s.classification) for s in sites]}")
    assert refused[0].line == line, (
        f"the audit refused line {refused[0].line}, not line {line}")
    assert fragment in refused[0].form, (
        f"the refusal must name the form it read ({fragment!r} in {refused[0].form!r})")
    assert fragment in refused[0].message and phrase in refused[0].message, (
        f"the refusal must say why it could not classify the site ({phrase!r} and "
        f"{fragment!r} in {refused[0].message})")


INSERTED_WAIT = "await asyncio.wait_for(waiter, HANDSHAKE_SECONDS)"


def _own_awaits(func):
    """How many ``await`` expressions *func*'s own body holds (nested defs excluded)."""
    class Counter(ast.NodeVisitor):
        def __init__(self):
            self.count = 0

        def visit_Await(self, node):
            self.count += 1
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            pass

        def visit_AsyncFunctionDef(self, node):
            pass

    counter = Counter()
    for statement in func.body:
        counter.visit(statement)
    return counter.count


def insert_compliant_wait(source):
    """*source* with one compliant wait prepended to its first awaiting coroutine.

    Returns ``(mutated source, line number of the added wait)``.  Position and
    indentation are read from the parsed source, so the witness needs only
    ``HANDSHAKE_SECONDS`` and ``asyncio.wait_for`` to still exist somewhere in a
    coroutine body -- never the guarded module's statements themselves.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        first = node.body[0]
        if first.lineno <= node.lineno or not _own_awaits(node):
            continue
        lines = source.splitlines(keepends=True)
        index = first.lineno - 1
        line = lines[index]
        indent = line[:len(line) - len(line.lstrip())]
        assert indent, f"line {first.lineno} is not indented, so it is not a body line"
        lines.insert(index, f"{indent}{INSERTED_WAIT}\n")
        return "".join(lines), first.lineno
    raise AssertionError(
        "no awaiting coroutine body was found to add a compliant wait to: the witness "
        "needs a function that already awaits")


def test_one_compliant_addition_is_one_more_must_complete_site_and_no_refusal(tmp_path):
    """The counts must be derived: a compliant wait added to a copy changes only the total.

    This is what a hard-coded site count got wrong -- it reddened the guard for an
    edit the guard is supposed to accept.  The copy is the only thing written.
    """
    baseline = audit_wait_bounds()
    base_must = [site for site in baseline if site.classification == MUST_COMPLETE]
    base_allowed = [site for site in baseline if site.classification == ALLOWED]
    assert not [site for site in baseline if site.classification == REFUSED], (
        "the real module must already audit clean for this witness to mean anything")

    original = DEFAULT_MODULE.read_text(encoding="utf-8")
    copy = tmp_path / "test_state_wait_contention.py"
    shutil.copyfile(DEFAULT_MODULE, copy)
    mutated, line_number = insert_compliant_wait(original)
    assert mutated.count("\n") == original.count("\n") + 1, (
        "the witness must add exactly one line")
    copy.write_text(mutated, encoding="utf-8")
    assert mutated.splitlines()[line_number - 1].strip() == INSERTED_WAIT, (
        f"line {line_number} of the copy is not the added wait: "
        f"{mutated.splitlines()[line_number - 1]!r}")

    added = audit_wait_bounds(copy)
    refused = [site for site in added if site.classification == REFUSED]
    assert not refused, (
        f"one compliant {INSERTED_WAIT!r} added at line {line_number} must not be "
        f"refused: {[(s.line, s.form, s.bound, s.message) for s in refused]}")
    must_complete = [site for site in added if site.classification == MUST_COMPLETE]
    allowed = [site for site in added if site.classification == ALLOWED]
    assert len(added) == len(baseline) + 1, (
        f"the copy must audit exactly one more site than the real module, audited "
        f"{len(added)} against {len(baseline)}")
    assert len(must_complete) == len(base_must) + 1, (
        f"the copy must audit exactly one more must-complete site than the real module "
        f"({len(base_must)}), audited {len(must_complete)}")
    assert len(allowed) == len(base_allowed), (
        f"the added wait is bounded by HANDSHAKE_SECONDS, so the allowed sites must not "
        f"move: {len(base_allowed)} before, {len(allowed)} after")
    assert any(site.line == line_number and site.bound == "HANDSHAKE_SECONDS"
               and site.form == "asyncio.wait_for" for site in must_complete), (
        f"the added site at line {line_number} was not classified as a must-complete "
        f"asyncio.wait_for bound: {[(s.line, s.form, s.bound, s.classification) for s in added]}")
    assert all(site.bound in MUST_COMPLETE_BOUNDS for site in must_complete), (
        f"a must-complete site carries a foreign bound: "
        f"{[(s.line, s.bound) for s in must_complete]}")
    assert DEFAULT_MODULE.read_text(encoding="utf-8") == original, (
        "the witness must not write to the guarded module itself")


if __name__ == "__main__":
    print_audit()
