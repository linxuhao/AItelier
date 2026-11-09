"""Guard the shared bound on the must-complete waits in the wait-contention tests.

``tests/unit/test_state_wait_contention.py`` waits on real SQLite/SkillFlow work,
so a correct-but-slow recovery page must not decide a verdict.  That is why its
must-complete handshakes are bounded by the module constant
``HANDSHAKE_SECONDS`` instead of a number: the constant is the single place to
raise the bound, and raising it is exactly what the slow-page witness needed.
A few sites are deliberate non-must-complete bounds -- the sub-second poll
intervals and the ``budget`` drain.

A must-complete wait has more than one spelling, and the spellings this resolver
reads reach the same classification: ``asyncio.wait_for(waiter, 20.0)``, a bare
``wait_for(waiter, 20.0)`` after ``from asyncio import wait_for``, name aliases
(``wf = asyncio.wait_for``, its annotated form ``wf: object = asyncio.wait_for``,
a ``functools.partial(asyncio.wait_for)``,
``getattr(asyncio, "wait_for")``, and the mapping subscripts
``asyncio.__dict__["wait_for"]`` / ``vars(asyncio)["wait_for"]``), an
attribute-target alias (``A.wf = asyncio.wait_for`` awaited as ``A.wf(...)``,
where ``A`` is a class or a second name for the module, ``A = asyncio``), the
CHAINED spellings (``wf = wait_for`` off a direct import, ``wf2 = wf`` -- an alias
of an alias --, a default-parameter binding
``async def spelled(waiter, wf=asyncio.wait_for)`` and its own chain
``async def spelled(waiter, *, wf=P)`` off ``P = asyncio.wait_for``, a
``functools.partial(asyncio.wait_for)`` left unnamed in callee position, and
``asyncio.wait_for.__call__``), ``getattr(asyncio, "wait_for")(waiter, 20.0)``
inlined, and ``asyncio.timeout(20.0)`` / ``asyncio.timeout_at(deadline, 20.0)``
used as an async context manager.  :func:`audit_wait_bounds` parses the module,
resolves each spelling above to a wait site, and classifies the site's bound as
``must-complete`` (the Name ``HANDSHAKE_SECONDS``), ``allowed`` (a numeric
literal strictly below 1.0, i.e. a sub-second poll interval, or the Name
``budget``, the drain bound) or ``refused``.  A wait-shaped call the resolver
cannot place -- a primitive's bare name it does not know, or one of ``wait_for``
/ ``timeout`` / ``timeout_at`` used as a method of an object it cannot identify --
is refused as unclassified rather than skipped and assumed harmless.  The one
exception is the different API BOUNDARY (i) describes, ``x.wait(...)`` on
something that is not ``asyncio``, and it has two sides, not one: a numeric
literal bound of 1.0 or more is refused, while a bound read from a name, no
argument at all, and a sub-second literal all carry no record -- the same
threshold as everywhere else in this file.  Each refusal names the
site: its line number, the form it was written in, and the bound's source text.

FAIL-CLOSED ON AN UNRESOLVED AWAIT.  A chained spelling is closed by resolution,
and the shapes left unresolved are caught by one rule of last resort: an
``await`` whose callee the resolver cannot place, and whose arguments carry a
numeric literal of 1.0 or more, is recorded and refused as
:data:`FORM_UNRESOLVED_LITERAL` -- the site names the unresolved callee and the
literal it read.  A name built by ``exec``, a callee returned from another
module, or any chained spelling nobody has written a resolver branch for is
therefore refused as soon as its arguments carry such a literal, rather than
skipped.  Measured on the guarded module with the rule in place, 0 refused sites
are recorded -- i.e. no awaited call the resolver cannot place there carries a
numeric literal of 1.0 or more -- and
``test_must_complete_waits_are_bounded_by_the_shared_bound`` asserts that count.
The module's own inventory of unresolved callees is deliberately NOT restated
here: the executed count is what has to stay true when that module changes, and a
list in this prose is a second copy of it that only rots.

BOUNDARY.  This is a reading of one module's syntax, not a proof that no wait
escapes it.  Two measured edges, each carried by an executed probe in
``SILENT_SHAPES`` below: a claim of silence in this prose is backed by a case
that runs the audit on exactly that snippet and gets zero records, and that case
also requires the shape's phrase to still be here, so prose and probe cannot
drift apart.
(i) ``x.wait(...)`` on something that is not ``asyncio`` is a DIFFERENT API
(``threading.Event.wait``, ``asyncio.Event.wait``) recorded ONLY when one of its
arguments is a numeric literal of 1.0 or more; below that threshold it carries no
record.  The ``handshake`` helper in the guarded module reads
``observed = event.wait(HANDSHAKE_SECONDS)``, and neither that name-bound form nor
``await event.wait()`` nor a sub-second ``event.wait(0.5)`` appears in the audit.
So the exclusion is for the API above that one threshold: the same shape carrying
``event.wait(20.0)``, ``event.wait(timeout=5)`` or
``getattr(event, "wait")(20.0)`` is refused as
unclassified rather than skipped, which
``test_a_wait_shaped_call_outside_the_asyncio_vocabulary_refuses_a_literal_bound``
measures.
(ii) The resolver follows what this module's own syntax binds and it can read:
direct imports and their aliases, name aliases and their chains, attribute-target
aliases, the mapping subscripts ``asyncio.__dict__[...]`` / ``vars(asyncio)[...]``,
parameter defaults, ``functools.partial``, and ``asyncio.timeout(...)``.  It does
not follow a value through ``exec`` (``exec("wf = asyncio.wait_for")``, then
``await wf(...)``), a callee returned from another module's function
(``wf = helpers.pick()``), a ``getattr`` whose target is already an attribute
(``getattr(asyncio.wait_for, "__call__")(...)``), a tuple-unpacked alias,
a list subscript, or a conditional expression picking the primitive.  Nor does it
follow a ``getattr`` whose name is not a literal.  ``SILENT_SHAPES`` below is this
audit's census of exactly
those shapes, each measured to produce zero records: a census of the classes this
audit KNOWS it skips, not a proof that no other spelling escapes -- which is why
the fail-closed rule exists for the shapes nobody wrote down.
THE FAIL-CLOSED RULE READS NUMERIC LITERALS, NOT VALUES.  An unresolved ``await``
is refused when its arguments carry a literal of 1.0 or more; a bound that is a
name or an expression -- a bound read from a name (``bound = 20.0``, then
``await wf(waiter, bound)``), a bound computed from a name (``H * 0.5``), or
a bound from a lambda call (``(lambda: 20.0)()``) -- is NOT refused, because the
rule reads the argument subtree for numeric literals and not what they evaluate
to.  Those three are in ``SILENT_SHAPES`` too, so this edge is a measurement
rather than a hedge.

The counts are DERIVED from the audit's own output, never asserted as literals:
the tests check the invariant over the records the audit returned -- each such
record is the shared bound or one of the named deliberate bounds, and none
reaches a numeric literal bound of 1.0 -- so a compliant addition to the guarded
module stays green.  A derived invariant cannot see a DELETION, though (a census
one site smaller satisfies it just as well), so the audit's ``(enclosing function,
primitive)`` keys are also held to a superset of ``KNOWN_MUST_COMPLETE_SITES``.
That table is keyed by the guarded module's TEST-FUNCTION NAME (``held_recovery``
is a fixture, the rest are test functions), so renaming or moving a guarded test
-- or moving its wait into a different function -- requires updating those keys;
a stale key shows up as a missing known site, never as silence.  Two companions
make both directions visible on ``tmp_path`` copies: one inserts a compliant
``await asyncio.wait_for(waiter, HANDSHAKE_SECONDS)`` and asserts one more
must-complete site with no refusal, the other deletes the ``held_recovery`` wait
and asserts the superset check goes false with the census one site smaller.

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

# The one shared bound the must-complete handshakes are written against.
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
FORM_ANNOTATED_ALIAS = "annotated alias: asyncio.{name}"
FORM_GETATTR = 'getattr(asyncio, "{name}")'
FORM_GETATTR_ALIAS = 'getattr(asyncio, "{name}") alias'
FORM_ATTRIBUTE_TARGET_ALIAS = "class-attribute alias: asyncio.{name}"
FORM_PARTIAL = "functools.partial of asyncio.{name}"
FORM_DICT_SUBSCRIPT = 'asyncio.__dict__["{name}"] alias'
FORM_VARS_SUBSCRIPT = 'vars(asyncio)["{name}"] alias'
FORM_GETATTR_DYNAMIC = "getattr(asyncio, <dynamic>)"
FORM_UNCLASSIFIED = "unclassified wait-shaped call"
# Chained spellings: a name bound to a name that already names a primitive, so the
# refusal says which link of the chain it read.
FORM_CHAINED_ALIAS = "chained alias via {source}: asyncio.{name}"
FORM_CHAINED_DEFAULT = "default-parameter binding via {source}: asyncio.{name}"
FORM_DEFAULT_PARAMETER = "default-parameter binding: asyncio.{name}"
# ``asyncio.wait_for.__call__(...)``: the primitive's own dunder, in callee position.
FORM_DUNDER_CALL = "asyncio.{name}.__call__"
# The rule of last resort: an await whose callee the resolver cannot place, and
# whose arguments carry a numeric literal bound of SUB_SECOND_LIMIT or more.
FORM_UNRESOLVED_LITERAL = "unresolved await, numeric literal bound"
# ``x.wait(...)`` on something that is not ``asyncio``: a different API, recorded
# only when one of its arguments is a numeric literal of SUB_SECOND_LIMIT or more.
# Below that threshold it carries no record -- the guarded module's name-bound
# handshake, a call with no argument at all, and ``event.wait(0.5)`` are all
# unrecorded, each carried by a probe in ``SILENT_SHAPES``.
FORM_WAIT_OTHER_API = "wait() on a non-asyncio object, numeric literal bound"

# The must-complete waits the guarded module is known to hold, as
# ``(enclosing function or fixture, primitive)``.  The derived invariant cannot see
# a DELETION -- a census one site smaller both satisfy "each site is the shared bound
# or a named deliberate bound" -- so the audit's site keys are held to a superset of
# this set instead, and adding a compliant wait stays green.  The keys are the guarded
# module's TEST-FUNCTION names (``held_recovery`` is a fixture), so renaming or
# moving a guarded test requires updating its key here; a stale key fails the
# superset check rather than passing quietly.
KNOWN_MUST_COMPLETE_SITES = {
    ("held_recovery", "wait_for"),
    ("test_external_only_wait_survives_other_recovery_and_replays_own_event", "wait_for"),
    ("test_quiet_recovery_release_allows_fresh_disposition", "wait_for"),
    ("test_busy_nonzero_recovery_cursor_waits_without_hotspin_and_reaches_last_page",
     "wait_for"),
    ("test_reentrant_observation_does_not_deadlock_or_lose_outer_result", "wait_for"),
    ("test_a_slow_but_correct_page_does_not_decide_the_wait_verdict", "wait"),
}


class WaitSite(NamedTuple):
    """One wait bound found by the audit, with its verdict."""

    line: int
    column: int
    callee: str
    form: str
    bound: str
    classification: str
    message: str
    # The enclosing function or fixture.  Trailing and defaulted so the printed
    # form and the older positional constructions keep working.
    function: str = ""


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
    # local name -> (primitive, form) for the spellings that bind one to a name
    aliases: dict
    local_defs: frozenset
    # (receiver name, attribute) -> (primitive, form) for ``A.wf = asyncio.wait_for``
    attribute_aliases: dict
    # local name -> primitive, the names a chain can be followed from: the direct
    # imports plus the resolved aliases, chained ones included.
    bound_names: dict


def _asyncio_names(tree):
    """The names bound to the ``asyncio`` module: ``asyncio``, ``import asyncio as``, ``x = asyncio``."""
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


def _literal_key(subscript):
    """The string literal a ``[...]`` subscript uses as its key, or None."""
    key = subscript.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    return None


def _is_mapping_of_asyncio(node, asyncio_names):
    """``asyncio.__dict__`` or ``vars(asyncio)`` -- a mapping a primitive can be read from."""
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return isinstance(node.value, ast.Name) and node.value.id in asyncio_names
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "vars" and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in asyncio_names)


def _value_resolves_to_wait(value, asyncio_names, bound_names=None):
    """``(primitive, form)`` when *value* spells an asyncio wait primitive, else None.

    The right-hand sides a name can be given a primitive through: the attribute
    itself, ``functools.partial(<that attribute>)``,
    ``getattr(asyncio, "<literal>")``, the mapping subscripts
    ``asyncio.__dict__["<name>"]`` / ``vars(asyncio)["<name>"]``, and -- when
    *bound_names* is given -- a bare name that already resolves to one
    (``wf = wait_for`` off a direct import, ``wf2 = wf``, a parameter default
    naming another alias), which is the chained spelling.
    """
    if isinstance(value, ast.Attribute):
        if (value.attr in WAIT_PRIMITIVES and isinstance(value.value, ast.Name)
                and value.value.id in asyncio_names):
            return value.attr, FORM_ASSIGNMENT_ALIAS.format(name=value.attr)
        return None
    if isinstance(value, ast.Call):
        if (isinstance(value.func, ast.Attribute) and value.func.attr == "partial"
                and value.args):
            inner = _value_resolves_to_wait(value.args[0], asyncio_names, bound_names)
            if inner is None:
                return None
            return inner[0], FORM_PARTIAL.format(name=inner[0])
        resolution = _resolve_getattr(value, asyncio_names)
        if resolution is None or resolution.primitive is None:
            return None
        return (resolution.primitive,
                FORM_GETATTR_ALIAS.format(name=resolution.primitive))
    if isinstance(value, ast.Subscript):
        key = _literal_key(value)
        if key not in WAIT_PRIMITIVES or not _is_mapping_of_asyncio(
                value.value, asyncio_names):
            return None
        template = (FORM_VARS_SUBSCRIPT if isinstance(value.value, ast.Call)
                    else FORM_DICT_SUBSCRIPT)
        return key, template.format(name=key)
    if isinstance(value, ast.Name) and bound_names is not None:
        primitive = bound_names.get(value.id)
        if primitive is not None:
            return (primitive,
                    FORM_CHAINED_ALIAS.format(name=primitive, source=value.id))
    return None


def _parameter_defaults(tree):
    """``(parameter name, default node)`` for the defaults of each def and lambda.

    A default that binds a primitive is how ``async def spelled(waiter,
    wf=asyncio.wait_for)`` escapes a resolver that only reads assignments.
    """
    pairs = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        args = node.args
        positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
        pairs.extend((argument.arg, default)
                     for argument, default in zip(
                         positional[len(positional) - len(args.defaults):],
                         args.defaults))
        pairs.extend((argument.arg, default)
                     for argument, default in zip(args.kwonlyargs, args.kw_defaults)
                     if default is not None)
    return pairs


def _assigned_aliases(tree, asyncio_names, direct_imports=()):
    """The names this module binds an asyncio wait primitive to, and to what.

    Returns ``({name: (primitive, form)}, {(receiver, attribute): (primitive, form)})``.
    The binding shapes :func:`_value_resolves_to_wait` reads are covered -- a plain
    assignment, an annotated one (``wf: object = asyncio.wait_for``), a
    ``functools.partial``, a ``getattr``, a ``__dict__``/``vars`` subscript, an
    attribute target (``A.wf = asyncio.wait_for``, awaited as ``A.wf(...)``), and
    the chained name bindings (``wf = wait_for``, ``wf2 = wf``), which are followed
    to a fixed point, so a chain resolves link by link to the primitive at its
    head.  A default-parameter binding is followed too; a parameter name is
    recorded module-wide, which refuses more call sites rather than fewer.
    """
    aliases = {}
    attribute_aliases = {}
    pending = []

    def record(targets, primitive, form):
        for target in targets:
            if isinstance(target, ast.Name):
                aliases[target.id] = (primitive, form)
            elif (isinstance(target, ast.Attribute)
                  and isinstance(target.value, ast.Name)):
                attribute_aliases[(target.value.id, target.attr)] = (
                    primitive, FORM_ATTRIBUTE_TARGET_ALIAS.format(name=primitive))

    def bind(targets, value, template, plain_form=None):
        """Record *value* for *targets*, or queue it when it names another name.

        *template* names the chain link the binding came through; *plain_form*
        renames the plain ``alias of ...`` reading when the binding's own shape
        deserves to be in the refusal (an annotation, a parameter default).
        """
        resolved = _value_resolves_to_wait(value, asyncio_names)
        if resolved is not None:
            primitive, form = resolved
            if (plain_form is not None
                    and form == FORM_ASSIGNMENT_ALIAS.format(name=primitive)):
                form = plain_form.format(name=primitive)
            record(targets, primitive, form)
            return
        if isinstance(value, ast.Name):
            pending.append((targets, value.id, template))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bind(node.targets, node.value, FORM_CHAINED_ALIAS)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            bind([node.target], node.value, FORM_CHAINED_ALIAS,
                 plain_form=FORM_ANNOTATED_ALIAS)
    for parameter, default in _parameter_defaults(tree):
        bind([ast.Name(id=parameter)], default, FORM_CHAINED_DEFAULT,
             plain_form=FORM_DEFAULT_PARAMETER)

    known = dict(direct_imports)
    known.update({name: primitive for name, (primitive, _form) in aliases.items()})
    for _ in range(len(pending) + 1):
        progressed = False
        for entry in list(pending):
            targets, source, template = entry
            primitive = known.get(source)
            if primitive is None:
                continue
            record(targets, primitive, template.format(name=primitive, source=source))
            for target in targets:
                if isinstance(target, ast.Name):
                    known[target.id] = primitive
            pending.remove(entry)
            progressed = True
        if not progressed:
            break
    return aliases, attribute_aliases


def _local_defs(tree):
    """Names this module defines itself, so a bare ``wait`` naming one is not asyncio's."""
    return frozenset(
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))


def _names(tree):
    asyncio_names = _asyncio_names(tree)
    direct_imports = _direct_imports(tree)
    aliases, attribute_aliases = _assigned_aliases(tree, asyncio_names, direct_imports)
    bound_names = dict(direct_imports)
    bound_names.update({name: primitive
                        for name, (primitive, _form) in aliases.items()})
    return _Names(asyncio_names, direct_imports, aliases, _local_defs(tree),
                  attribute_aliases, bound_names)


def _refused_wait_literal(call, func):
    """Refuse a deliberately-unaudited ``*.wait(...)`` call carrying a big literal.

    ``x.wait(...)`` on something that is not ``asyncio`` is a different API
    (``threading.Event.wait``, ``asyncio.Event.wait``) and no site is recorded for
    its name-bound form -- the guarded module's handshake is exactly that shape --
    nor for one with no argument at all or a sub-second literal: the trigger is a
    numeric literal of 1.0 or more, which is the defect this audit exists to see, so
    that form is refused as unclassified instead of skipped.  The exclusion is for
    the other API, not for large numbers.
    """
    if _argument_literal(call) is None:
        return None
    return _Resolution(None, FORM_WAIT_OTHER_API, ast.unparse(func))


def _resolve_getattr(getattr_call, asyncio_names, outer_call=None):
    """Resolve ``getattr(<obj>, "<name>")(...)``, or None when it is not a wait shape.

    *getattr_call* is the inner ``getattr(...)`` Call node, i.e. the callee of the
    call the audit is looking at.
    """
    if not (isinstance(getattr_call.func, ast.Name) and getattr_call.func.id == "getattr"):
        return None
    if len(getattr_call.args) < 2:
        return None
    target, name_node = getattr_call.args[0], getattr_call.args[1]
    on_asyncio = isinstance(target, ast.Name) and target.id in asyncio_names
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
        # Its name-bound form stays silent; a literal of 1.0 or more is refused.
        return (_refused_wait_literal(outer_call, getattr_call)
                if outer_call is not None else None)
    return _Resolution(None, FORM_UNCLASSIFIED, callee)


def _resolve_call(call, names):
    """Resolve *call* to an asyncio wait primitive, or None when it is not a wait shape.

    A wait-shaped call whose callee cannot be placed returns a resolution with
    ``primitive is None``: the audit refuses those instead of skipping them.
    """
    func = call.func
    if isinstance(func, ast.Call):
        resolution = _resolve_getattr(func, names.asyncio_names, call)
        if resolution is not None:
            return resolution
        # A ``functools.partial(<primitive>)`` left unnamed in callee position.
        resolved = _value_resolves_to_wait(func, names.asyncio_names, names.bound_names)
        if resolved is None:
            return None
        return _Resolution(resolved[0], resolved[1], ast.unparse(func))
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            known = names.attribute_aliases.get((func.value.id, func.attr))
            if known is not None:
                primitive, form = known
                return _Resolution(primitive, form, ast.unparse(func))
        if func.attr == "__call__":
            # ``asyncio.wait_for.__call__(...)``: the primitive's own dunder.
            resolved = _value_resolves_to_wait(func.value, names.asyncio_names,
                                               names.bound_names)
            if resolved is not None:
                return _Resolution(resolved[0],
                                   FORM_DUNDER_CALL.format(name=resolved[0]),
                                   ast.unparse(func))
            return None
        if func.attr not in WAIT_PRIMITIVES:
            return None
        if isinstance(func.value, ast.Name) and func.value.id in names.asyncio_names:
            return _Resolution(func.attr, FORM_ATTRIBUTE.format(name=func.attr),
                               f"asyncio.{func.attr}")
        if func.attr == "wait":
            # Another API (see :func:`_refused_wait_literal`): silent when its bound is
            # a name, refused when its bound is a literal of 1.0 or more.
            return _refused_wait_literal(call, func)
        return _Resolution(None, FORM_UNCLASSIFIED, ast.unparse(func))
    if isinstance(func, ast.Name):
        if func.id in names.direct_imports:
            imported = names.direct_imports[func.id]
            form = (FORM_DIRECT_IMPORT.format(name=imported) if func.id == imported
                    else FORM_DIRECT_IMPORT_ALIAS.format(name=imported, local=func.id))
            return _Resolution(imported, form, func.id)
        if func.id in names.aliases:
            primitive, form = names.aliases[func.id]
            return _Resolution(primitive, form, func.id)
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
        reason = (f"a numeric literal bound >= {SUB_SECOND_LIMIT:.1f} would escape "
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


def _wait_other_api_message(call, resolution, line):
    literal = _argument_literal(call)
    text = ast.unparse(literal) if literal is not None else "<no bound>"
    return (f"line {line}: {resolution.form} -- {resolution.callee}(..., {text}) is "
            f"refused: ``x.wait(...)`` on something that is not ``asyncio`` is a "
            f"different API, and its name-bound form is deliberately not audited, but a "
            f"numeric literal bound >= {SUB_SECOND_LIMIT:.1f} is refused rather than "
            f"skipped (fail-closed); a wait must be written so the audit can read which "
            f"bound it uses")


def _awaited_calls(tree):
    """``id(node)`` for each ``Call`` that is the value of an ``await``."""
    return {id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)}


def _argument_literal(call):
    """The first numeric literal bound of 1.0 or more among *call*'s arguments.

    The walk covers each argument's subtree, so a literal wrapped in a keyword or a
    helper call is still read; a comprehension's or lambda's own numbers are not
    arguments of this call and are skipped.
    """
    def literals(node):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp, ast.Lambda)):
            return []
        found = []
        value = _numeric_value(node)
        if value is not None and value >= SUB_SECOND_LIMIT:
            found.append(node)
        for child in ast.iter_child_nodes(node):
            found.extend(literals(child))
        return found

    for argument in list(call.args) + [keyword.value for keyword in call.keywords]:
        found = literals(argument)
        if found:
            return found[0]
    return None


def _unresolved_literal_message(callee, literal, line):
    return (f"line {line}: {FORM_UNRESOLVED_LITERAL} -- the audit cannot resolve {callee} "
            f"to an asyncio wait primitive, and the awaited call passes the numeric "
            f"literal {literal} (>= {SUB_SECOND_LIMIT:.1f}), so it refuses the call "
            f"instead of silently skipping it (fail-closed); a wait must be written so the "
            f"audit can read which bound it uses")


def _enclosing_functions(tree):
    """Map ``id(node)`` -> the name of the innermost function/fixture enclosing it.

    ``ast.walk`` loses parents, so the walk here carries the enclosing name on an
    explicit stack: a site inside a nested helper reports the helper, a site in the
    fixture body around it reports the fixture.
    """
    enclosing = {}
    stack = [(child, "") for child in ast.iter_child_nodes(tree)]
    while stack:
        node, current = stack.pop()
        name = (node.name
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current)
        enclosing[id(node)] = name
        for child in ast.iter_child_nodes(node):
            stack.append((child, name))
    return enclosing


def _must_complete_site_keys(sites):
    """``{(enclosing function or fixture, primitive)}`` for each must-complete site.

    The primitive is the callee's last dotted segment, so ``asyncio.wait_for`` gives
    ``wait_for``.  A site spelled through an alias reports its local name in that
    position, which no known key contains, so an alias site is never mistaken for a
    known one.
    """
    return {(site.function, site.callee.rsplit(".", 1)[-1])
            for site in sites if site.classification == MUST_COMPLETE}


def audit_wait_bounds(module_path=DEFAULT_MODULE):
    """The wait bounds this audit resolves in *module_path*, in source order.

    Returns a list of :class:`WaitSite` records; that list is the classification
    the tests assert on, and :func:`print_audit` renders the same records.

    Not every wait in the file: see BOUNDARY in the module docstring.  The
    ``threading.Event().wait(...)`` handshake in the guarded module's ``handshake``
    helper is a threading primitive and is deliberately outside this vocabulary, and
    the call shapes named there resolve to no site -- each of them carried by a case
    in ``SILENT_SHAPES``.
    One rule of last resort is applied here: an ``await`` whose callee this resolver
    cannot place, and whose arguments carry a numeric literal of 1.0 or more, is
    recorded and refused, so an unresolved spelling that carries a large literal is
    refused rather than passed over.
    """
    path = Path(module_path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _names(tree)
    enclosing = _enclosing_functions(tree)
    awaited = _awaited_calls(tree)
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolution = _resolve_call(node, names)
        function = enclosing.get(id(node), "")
        if resolution is None:
            literal = _argument_literal(node) if id(node) in awaited else None
            if literal is None:
                continue
            sites.append(WaitSite(
                node.lineno, node.col_offset, ast.unparse(node.func),
                FORM_UNRESOLVED_LITERAL, ast.unparse(literal), REFUSED,
                _unresolved_literal_message(ast.unparse(node.func),
                                            ast.unparse(literal), node.lineno),
                function))
            continue
        if resolution.primitive is None:
            message = (_wait_other_api_message(node, resolution, node.lineno)
                       if resolution.form == FORM_WAIT_OTHER_API
                       else _unresolved_message(resolution, node.lineno))
            sites.append(WaitSite(node.lineno, node.col_offset, resolution.callee,
                                  resolution.form, "<unresolved>", REFUSED,
                                  message, function))
            continue
        bound = _bound_node(node, resolution.primitive)
        classification, text, message = classify_bound(
            bound, node.lineno, resolution.callee, resolution.form)
        sites.append(WaitSite(node.lineno, node.col_offset, resolution.callee,
                              resolution.form, text, classification, message, function))
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


def test_must_complete_waits_are_bounded_by_the_shared_bound():
    sites = audit_wait_bounds()
    refused = [site for site in sites if site.classification == REFUSED]
    assert not refused, "the audit refused these wait bounds:\n" + "\n".join(
        site.message for site in refused)
    must_complete = [site for site in sites
                     if site.classification == MUST_COMPLETE]
    assert must_complete, ("no must-complete wait was found: the audit is not looking "
                           "at the wait-contention module")


def test_each_audited_site_is_the_shared_bound_or_a_named_deliberate_bound():
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


def test_each_known_must_complete_site_is_still_audited():
    """The audit's site keys must still cover the must-complete waits we know of.

    A SUPERSET check, not an equality and not a total count: a compliant addition
    adds a key and stays green, while deleting a known wait -- which the derived
    invariants above accept -- reddens this.
    """
    sites = audit_wait_bounds()
    keys = _must_complete_site_keys(sites)
    missing = KNOWN_MUST_COMPLETE_SITES - keys
    assert not missing, (
        f"the audit no longer reports these known must-complete waits "
        f"{sorted(missing)}; it reports {sorted(keys)}")
    assert any(site.function == "held_recovery" for site in sites), (
        "no site names the held_recovery fixture, so the enclosing-function map is "
        "not reading fixtures")


DELETED_WAIT = "        result = await asyncio.wait_for(owner_wait, HANDSHAKE_SECONDS)"
DELETED_WAIT_REPLACEMENT = "        result = await owner_wait"


def test_a_deleted_must_complete_wait_is_visible_as_a_missing_known_site(tmp_path):
    """Deleting one must-complete wait must be visible, and this witnesses that it is.

    The derived invariant accepts a shorter module, so the detector is the named-key
    superset check.  The guarded module is copied to ``tmp_path`` and its
    ``held_recovery`` wait replaced by the bare await it was bounding: the
    known-site check must become false and the census must lose exactly one site.
    Without this, a reader would have to take both claims on faith.
    """
    baseline = audit_wait_bounds()
    base_keys = _must_complete_site_keys(baseline)
    assert not (KNOWN_MUST_COMPLETE_SITES - base_keys), (
        f"the real module does not audit the known must-complete sites "
        f"{sorted(KNOWN_MUST_COMPLETE_SITES - base_keys)}, so this witness cannot mean "
        f"anything")

    original = DEFAULT_MODULE.read_text(encoding="utf-8")
    assert original.count(DELETED_WAIT) == 1, (
        "the guarded module must contain the held_recovery wait exactly once, "
        f"found {original.count(DELETED_WAIT)}")
    line_number = original.count("\n", 0, original.index(DELETED_WAIT)) + 1
    mutated = original.replace(DELETED_WAIT, DELETED_WAIT_REPLACEMENT)
    assert mutated.count("\n") == original.count("\n"), (
        "the rewrite must not change the line count")
    assert mutated.count(DELETED_WAIT_REPLACEMENT) == 1, (
        f"the replacement line must exist exactly once: {DELETED_WAIT_REPLACEMENT!r}")
    copy = tmp_path / "test_state_wait_contention.py"
    shutil.copyfile(DEFAULT_MODULE, copy)
    copy.write_text(mutated, encoding="utf-8")

    after = audit_wait_bounds(copy)
    lost = KNOWN_MUST_COMPLETE_SITES - _must_complete_site_keys(after)
    assert lost == {("held_recovery", "wait_for")}, (
        f"deleting the held_recovery wait (copy line {line_number}) must make the "
        f"known-site check false for exactly that key, lost {sorted(lost)} of "
        f"{sorted(KNOWN_MUST_COMPLETE_SITES)}")
    assert len(after) == len(baseline) - 1, (
        f"the census must lose exactly one wait site ({len(baseline)} -> "
        f"{len(baseline) - 1}), audited {len(after)}")
    assert not any(site.line == line_number for site in after), (
        f"line {line_number} still audits as a wait site: "
        f"{[(s.line, s.callee, s.bound) for s in after if s.line == line_number]}")
    assert DEFAULT_MODULE.read_text(encoding="utf-8") == original, (
        "the witness must not write to the guarded module itself")


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


# The spellings a must-complete wait can be written in, each with a tiny synthetic
# module carrying it and the sites the audit must report.  ``{bound}`` is filled
# with a literal (which must be refused) and with the shared constant (which must
# be classified, proving the spelling resolves to a normal wait site rather than
# merely being refused as unknown) -- so a chained spelling that resolves is
# refused on a big literal and still counted when it carries the shared bound.
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
    pytest.param(
        "attribute-target-alias",
        "import asyncio\n"
        "\n"
        "\n"
        "class A:\n"
        "    pass\n"
        "\n"
        "\n"
        "A.wf = asyncio.wait_for\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await A.wf(waiter, {bound})\n",
        [(12, "class-attribute alias: asyncio.wait_for")],
        id="e-attribute-target-alias",
    ),
    pytest.param(
        "annotated-alias",
        "import asyncio\n"
        "\n"
        "\n"
        "wf: object = asyncio.wait_for\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await wf(waiter, {bound})\n",
        [(8, "annotated alias: asyncio.wait_for")],
        id="f-annotated-alias",
    ),
    pytest.param(
        "partial-and-getattr-aliases",
        "import asyncio\n"
        "import functools\n"
        "\n"
        "\n"
        "p = functools.partial(asyncio.wait_for)\n"
        "g = getattr(asyncio, \"wait_for\")\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    first = await p(waiter, {bound})\n"
        "    second = await g(waiter, {bound})\n"
        "    return first, second\n",
        [(10, "functools.partial of asyncio.wait_for"),
         (11, 'getattr(asyncio, "wait_for") alias')],
        id="g-partial-and-getattr-aliases",
    ),
    pytest.param(
        "mapping-subscript-aliases",
        "import asyncio\n"
        "\n"
        "\n"
        "d = asyncio.__dict__[\"wait_for\"]\n"
        "v = vars(asyncio)[\"wait_for\"]\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    first = await d(waiter, {bound})\n"
        "    second = await v(waiter, {bound})\n"
        "    return first, second\n",
        [(9, 'asyncio.__dict__["wait_for"]'),
         (10, 'vars(asyncio)["wait_for"]')],
        id="h-mapping-subscript-aliases",
    ),
    pytest.param(
        "chained-direct-import-alias",
        "from asyncio import wait_for\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    wf = wait_for\n"
        "    return await wf(waiter, {bound})\n",
        [(6, "chained alias via wait_for: asyncio.wait_for")],
        id="i-chained-direct-import-alias",
    ),
    pytest.param(
        "alias-of-an-alias",
        "import asyncio\n"
        "\n"
        "wf = asyncio.wait_for\n"
        "wf2 = wf\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await wf2(waiter, {bound})\n",
        [(8, "chained alias via wf: asyncio.wait_for")],
        id="j-alias-of-an-alias",
    ),
    pytest.param(
        "alias-of-the-asyncio-module",
        "import asyncio\n"
        "\n"
        "A = asyncio\n"
        "A.wf = asyncio.wait_for\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await A.wf(waiter, {bound})\n",
        [(8, "class-attribute alias: asyncio.wait_for")],
        id="k-alias-of-the-asyncio-module",
    ),
    pytest.param(
        "partial-in-callee-position",
        "import asyncio\n"
        "import functools\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await functools.partial(asyncio.wait_for)(waiter, {bound})\n",
        [(6, "functools.partial of asyncio.wait_for")],
        id="l-partial-in-callee-position",
    ),
    pytest.param(
        "dunder-call",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await asyncio.wait_for.__call__(waiter, {bound})\n",
        [(5, "asyncio.wait_for.__call__")],
        id="m-dunder-call",
    ),
    pytest.param(
        "default-parameter-binding",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter, wf=asyncio.wait_for):\n"
        "    return await wf(waiter, {bound})\n",
        [(5, "default-parameter binding: asyncio.wait_for")],
        id="n-default-parameter-binding",
    ),
    pytest.param(
        "chained-default-parameter-binding",
        "import asyncio\n"
        "\n"
        "P = asyncio.wait_for\n"
        "\n"
        "\n"
        "async def spelled(waiter, *, wf=P):\n"
        "    return await wf(waiter, {bound})\n",
        [(7, "default-parameter binding via P: asyncio.wait_for")],
        id="o-chained-default-parameter-binding",
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


# The shapes this audit still resolves to NO record, each with a tiny synthetic
# module carrying it.  A silence claim in the module docstring's BOUNDARY section
# is backed by one of these: the case runs the audit on exactly that snippet and
# requires zero records, and it requires the shape's phrase to still be in the
# docstring, so the prose and its probe cannot drift apart.
SILENT_SHAPES = [
    pytest.param(
        "threading-event-wait",
        "import asyncio\n"
        "import threading\n"
        "\n"
        "\n"
        "def spelled(event):\n"
        "    return event.wait(HANDSHAKE_SECONDS)\n"
        "\n"
        "\n"
        "async def spelled_without_a_bound(event):\n"
        "    return await event.wait()\n",
        "event.wait(HANDSHAKE_SECONDS)",
        id="threading-event-wait",
    ),
    pytest.param(
        "threading-event-wait-sub-second",
        "import threading\n"
        "\n"
        "\n"
        "def spelled(event):\n"
        "    return event.wait(0.5)\n",
        "event.wait(0.5)",
        id="threading-event-wait-sub-second",
    ),
    pytest.param(
        "exec-built-alias",
        "import asyncio\n"
        "\n"
        "\n"
        "def build():\n"
        "    exec(\"wf = asyncio.wait_for\", globals())\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await wf(waiter, .25)\n",
        'exec("wf = asyncio.wait_for")',
        id="exec-built-alias",
    ),
    pytest.param(
        "callee-from-another-module",
        "import helpers\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    wf = helpers.pick()\n"
        "    return await wf(waiter, .25)\n",
        "another module's function",
        id="callee-from-another-module",
    ),
    pytest.param(
        "getattr-dunder-call",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await getattr(asyncio.wait_for, \"__call__\")(waiter, .25)\n",
        'getattr(asyncio.wait_for, "__call__")',
        id="getattr-dunder-call",
    ),
    pytest.param(
        "tuple-unpacked-alias",
        "import asyncio\n"
        "from asyncio import wait_for\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    wf, = (wait_for,)\n"
        "    return await wf(waiter, .25)\n",
        "a tuple-unpacked alias",
        id="tuple-unpacked-alias",
    ),
    pytest.param(
        "list-subscript-alias",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    wf = [asyncio.wait_for][0]\n"
        "    return await wf(waiter, .25)\n",
        "a list subscript",
        id="list-subscript-alias",
    ),
    pytest.param(
        "conditional-alias",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter, flag):\n"
        "    wf = asyncio.wait_for if flag else asyncio.wait_for\n"
        "    return await wf(waiter, .25)\n",
        "a conditional expression picking the primitive",
        id="conditional-alias",
    ),
    pytest.param(
        "dynamic-getattr-name",
        "import helpers\n"
        "\n"
        "NAME = \"wait_for\"\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await getattr(helpers, NAME)(waiter, .25)\n",
        "a ``getattr`` whose name is not a literal",
        id="dynamic-getattr-name",
    ),
    pytest.param(
        "named-bound",
        "import helpers\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    bound = 20.0\n"
        "    wf = helpers.pick()\n"
        "    return await wf(waiter, bound)\n",
        "a bound read from a name",
        id="named-bound",
    ),
    pytest.param(
        "computed-bound",
        "import helpers\n"
        "\n"
        "H = 40.0\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    wf = helpers.pick()\n"
        "    return await wf(waiter, H * 0.5)\n",
        "a bound computed from a name",
        id="computed-bound",
    ),
    pytest.param(
        "lambda-bound",
        "import helpers\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    wf = helpers.pick()\n"
        "    return await wf(waiter, (lambda: 20.0)())\n",
        "a bound from a lambda call",
        id="lambda-bound",
    ),
]


@pytest.mark.parametrize("shape, source, phrase", SILENT_SHAPES)
def test_a_shape_the_prose_calls_silent_is_measured_silent(tmp_path, shape, source, phrase):
    """Zero records for exactly this snippet: the prose may not claim it on its own."""
    sites = _audit_synthetic(tmp_path, source, f"silent_{shape}.py")
    assert not sites, (
        f"the {shape} shape is described as unrecorded, but the audit returned "
        f"{[(s.line, s.form, s.bound, s.classification) for s in sites]}")
    assert phrase in __doc__, (
        f"the {shape} probe backs the prose phrase {phrase!r}, which is no longer in "
        f"the module docstring")


# The exclusion in BOUNDARY (i) is for the API, not for large numbers: the same
# ``x.wait(...)`` shape carrying a numeric literal bound of 1.0 or more is refused
# as unclassified instead of skipped, so a blocking handshake cannot hide behind
# "it is a different API".
WAIT_OUTSIDE_ASYNCIO_WITH_A_LITERAL = [
    pytest.param(
        "threading-literal",
        "import threading\n"
        "\n"
        "\n"
        "def spelled(event):\n"
        "    return event.wait(20.0)\n",
        5,
        "event.wait",
        id="threading-literal",
    ),
    pytest.param(
        "threading-keyword",
        "import threading\n"
        "\n"
        "\n"
        "def spelled(event):\n"
        "    return event.wait(timeout=5)\n",
        5,
        "event.wait",
        id="threading-keyword",
    ),
    pytest.param(
        "getattr-literal",
        "import threading\n"
        "\n"
        "\n"
        "def spelled(event):\n"
        "    return getattr(event, \"wait\")(20.0)\n",
        5,
        'getattr(event, "wait")',
        id="getattr-literal",
    ),
    pytest.param(
        "awaited-event-literal",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(event):\n"
        "    return await event.wait(5.0)\n",
        5,
        "event.wait",
        id="awaited-event-literal",
    ),
]


@pytest.mark.parametrize("shape, source, line, callee",
                         WAIT_OUTSIDE_ASYNCIO_WITH_A_LITERAL)
def test_a_wait_shaped_call_outside_the_asyncio_vocabulary_refuses_a_literal_bound(
        tmp_path, shape, source, line, callee):
    """A different API is refused at a 1.0-or-more literal; below that it is silent."""
    sites = _audit_synthetic(tmp_path, source, f"outside_vocabulary_{shape}.py")
    refused = [site for site in sites if site.classification == REFUSED]
    assert len(refused) == 1, (
        f"the {shape} call must be refused once, audited "
        f"{[(s.line, s.form, s.bound, s.classification) for s in sites]}")
    site = refused[0]
    assert site.line == line, f"the audit refused line {site.line}, not line {line}"
    assert site.form == FORM_WAIT_OTHER_API, (
        f"the refusal must name the form it read, got {site.form!r}")
    # ``ast.unparse`` picks its own quote style, so compare the spelling modulo quotes.
    assert callee.replace('"', "'") in site.callee.replace('"', "'"), (
        f"the refusal must name the shape it read ({callee!r} in {site.callee!r})")
    assert "1.0" in site.message, (
        f"the refusal must say the literal reached the 1.0 threshold: {site.message}")


# The rule of last resort, exercised: a callee the resolver cannot place, on a call
# whose arguments carry a literal bound of 1.0 or more, is recorded and refused.
UNRESOLVED_LITERAL = [
    pytest.param(
        "exec-built-alias",
        "import asyncio\n"
        "\n"
        "\n"
        "def build():\n"
        "    exec(\"wf = asyncio.wait_for\", globals())\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await wf(waiter, 20.0)\n",
        9,
        "wf",
        "20.0",
        id="exec-built-alias-with-a-big-literal",
    ),
    pytest.param(
        "callee-from-another-module",
        "import helpers\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    wf = helpers.pick()\n"
        "    return await wf(waiter, 20.0)\n",
        6,
        "wf",
        "20.0",
        id="another-module-callee-with-a-big-literal",
    ),
    pytest.param(
        "getattr-dunder-call",
        "import asyncio\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    return await getattr(asyncio.wait_for, \"__call__\")(waiter, 20.0)\n",
        5,
        'getattr(asyncio.wait_for, "__call__")',
        "20.0",
        id="getattr-dunder-call-with-a-big-literal",
    ),
    pytest.param(
        "keyword-literal",
        "import helpers\n"
        "\n"
        "\n"
        "async def spelled(waiter):\n"
        "    wf = helpers.pick()\n"
        "    return await wf(waiter, timeout=20.0)\n",
        6,
        "wf",
        "20.0",
        id="keyword-literal-on-an-unresolved-callee",
    ),
]


@pytest.mark.parametrize("shape, source, line, callee, literal", UNRESOLVED_LITERAL)
def test_an_unresolved_await_with_a_big_literal_is_refused(
        tmp_path, shape, source, line, callee, literal):
    """The literal bound of an await the resolver cannot place is refused, not skipped."""
    sites = _audit_synthetic(tmp_path, source, f"unresolved_literal_{shape}.py")
    refused = [site for site in sites if site.classification == REFUSED]
    assert len(refused) == 1, (
        f"the {shape} shape must be refused once, audited "
        f"{[(s.line, s.form, s.bound, s.classification) for s in sites]}")
    site = refused[0]
    assert site.line == line, f"the audit refused line {site.line}, not line {line}"
    assert site.form == FORM_UNRESOLVED_LITERAL, (
        f"the refusal must name the rule it applied, got {site.form!r}")
    # ``ast.unparse`` picks its own quote style, so compare the spelling modulo quotes.
    assert (callee.replace('"', "'") == site.callee.replace('"', "'")), (
        f"the refusal must name the unresolved callee {callee!r}, got {site.callee!r}")
    assert site.callee in site.message, (
        f"the refusal message must name the unresolved callee {site.callee!r}: "
        f"{site.message}")
    assert literal in site.bound, (
        f"the refusal must carry the literal it read, got {site.bound!r}")
    assert literal in site.message and "1.0" in site.message, (
        f"the refusal must say the literal reached the 1.0 threshold: {site.message}")


# The known-site table is keyed by function name; this is what that means in practice.
RENAMED_GUARDED_TEST = "test_quiet_recovery_release_allows_fresh_disposition"


def test_renaming_a_guarded_test_loses_its_known_site_key(tmp_path):
    """A rename in the guarded module is a table edit here, not a silent census."""
    original = DEFAULT_MODULE.read_text(encoding="utf-8")
    assert original.count(RENAMED_GUARDED_TEST) == 1, (
        f"the guarded module must name {RENAMED_GUARDED_TEST} exactly once, found "
        f"{original.count(RENAMED_GUARDED_TEST)}")
    copy = tmp_path / "test_state_wait_contention.py"
    copy.write_text(original.replace(RENAMED_GUARDED_TEST, RENAMED_GUARDED_TEST + "_renamed"),
                    encoding="utf-8")
    lost = KNOWN_MUST_COMPLETE_SITES - _must_complete_site_keys(audit_wait_bounds(copy))
    assert lost == {(RENAMED_GUARDED_TEST, "wait_for")}, (
        f"renaming {RENAMED_GUARDED_TEST} must lose exactly its own known-site key, "
        f"lost {sorted(lost)} of {sorted(KNOWN_MUST_COMPLETE_SITES)}")
    assert DEFAULT_MODULE.read_text(encoding="utf-8") == original, (
        "the witness must not write to the guarded module itself")


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
