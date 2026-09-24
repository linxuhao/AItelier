"""An undeclared reader of a private table is UNTRUSTED.

Criterion ``an-undeclared-reader-is-untrusted``. The trust level is a property
of the object that OWNS the data, declared once at a real construction point.
Any object that can read a private table and was built without declaring a
level must refuse the read - exactly like ``StateService``, whose constructor
already rejects an undeclared level. A leaf rebuilt from an untrusted service's
store or db cannot become trusted by staying silent.

The declaration list is DERIVED with ``ast`` from the shipped source (never
hand-written): every function parameter and every class attribute named
``project_read_trusted`` is collected with its default, and none may default to
``True``. A planted ``True`` default is caught by the derivation's own test.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from core.director_messaging import SQLiteDirectorMessaging
from core.state_commands import ProjectPrivate
from core.state_database import StateDatabase
from core.state_driver_notes import StateDriverNotes
from core.state_graph import StateGraphStore
from core.state_service import StateService
from tests.support.state_author_surface import seed_private_mail

REPO = Path(__file__).resolve().parents[2]
PROJECT = "undeclared-reader"
UNOPENED = "undeclared-reader-unopened"
SECRET = "UNDECLARED-READER-SECRET-9182"


def _literal(node):
    if node is None:
        return "<none>"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return "<expr>"


def _default_for(args, name):
    positional = args.posonlyargs + args.args
    names = [a.arg for a in positional]
    if name in names and args.defaults:
        index = names.index(name) - (len(names) - len(args.defaults))
        if index >= 0:
            return _literal(args.defaults[index])
    keyword = [a.arg for a in args.kwonlyargs]
    if name in keyword:
        return _literal(args.kw_defaults[keyword.index(name)])
    return "<none>"


def _scan_source(source, label):
    found = []
    for node in ast.walk(ast.parse(source, filename=label)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(a.arg == "project_read_trusted"
                   for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs):
                found.append((f"{label}:{node.lineno}", "parameter",
                              _default_for(node.args, "project_read_trusted")))
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, ast.Assign):
                    targets, value = sub.targets, sub.value
                elif isinstance(sub, ast.AnnAssign):
                    targets, value = [sub.target], sub.value
                else:
                    continue
                if any(getattr(t, "id", None) == "project_read_trusted"
                       or getattr(t, "attr", None) == "project_read_trusted"
                       for t in targets):
                    found.append((f"{label}:{sub.lineno}", "attribute", _literal(value)))
    return found


def _declarations():
    found = []
    for root in (REPO / "core", REPO / "api"):
        for path in sorted(root.rglob("*.py")):
            found.extend(_scan_source(path.read_text(encoding="utf-8"),
                                      str(path.relative_to(REPO))))
    return found


def test_the_derivation_sees_a_planted_trusted_default():
    planted = ("class Rogue:\n"
               "    project_read_trusted = True\n"
               "    def __init__(self, x, project_read_trusted=True):\n"
               "        pass\n")
    defaults = [default for _, _, default in _scan_source(planted, "<planted>")]
    assert defaults.count("True") == 2, defaults


def test_no_declaration_defaults_to_trusted():
    declarations = _declarations()
    assert declarations, "derivation found no project_read_trusted declarations - parser broken"
    print("DECLARATIONS =", declarations)
    offenders = [f"{where} ({kind} = {default})"
                 for where, kind, default in declarations if default == "True"]
    assert not offenders, f"these declarations default to trusted: {offenders}"


def _trusted(tmp_path, name="undeclared.sqlite"):
    service = StateService(StateDatabase(str(tmp_path / name)), actor="seeder",
                           project_read_trusted=True)
    service.create_project(PROJECT, PROJECT)
    service.driver_notes.update(PROJECT, "permanent", SECRET, 0, "director")
    seed_private_mail(service, PROJECT, SECRET)
    # PROJECT is OPEN: its notebook is public (owner ruling 2026-09-22) and its
    # mailbox stays writer-only, so a delivered mailbox would be a real leak,
    # not the project gate doing its job. UNOPENED's notebook stays private.
    service.create_project(UNOPENED, UNOPENED)
    service.driver_notes.update(UNOPENED, "permanent", SECRET, 0, "director")
    service.open_project(PROJECT)
    return service


def _anonymous(service):
    """The service an anonymous request gets over the same database."""
    return StateService(service.db, actor="anonymous", project_read_trusted=False)


def test_a_notebook_rebuilt_without_a_declaration_delivers_no_unopened_note(tmp_path):
    """Rebuilt from the anonymous service's store, the notebook reads the
    OPENED project's notebook (public) and never the unopened one's."""
    anonymous = _anonymous(_trusted(tmp_path))
    notebook = StateDriverNotes(anonymous.store, "anonymous-rebuilder")
    assert notebook.project_read_trusted is False
    assert SECRET in notebook.get(PROJECT)["permanent"]
    unopened = notebook.get(UNOPENED)
    print("REBUILT_NOTEBOOK_UNOPENED =", unopened)
    assert SECRET not in repr(unopened)


def test_a_messaging_provider_rebuilt_without_a_declaration_refuses(tmp_path):
    service = _trusted(tmp_path)
    provider = SQLiteDirectorMessaging(service.store, "anonymous-rebuilder")
    with pytest.raises(ProjectPrivate):
        provider.list_director_messages(PROJECT)


def test_a_graph_store_rebuilt_from_the_db_refuses(tmp_path):
    service = _trusted(tmp_path)
    store = StateGraphStore(service.db)
    with pytest.raises(ProjectPrivate):
        store.events(PROJECT)


def test_rebuilding_from_an_untrusted_store_stays_untrusted(tmp_path):
    """A leaf rebuilt from the anonymous store that DECLARES itself trusted
    passes its own action verdict, and the handle still refuses the rows."""
    anonymous = _anonymous(_trusted(tmp_path))
    forged = SQLiteDirectorMessaging(anonymous.store, "anonymous-rebuilder",
                                     project_read_trusted=True)
    with pytest.raises(ProjectPrivate):
        forged.list_director_messages(PROJECT)
    rebuilt = StateGraphStore(anonymous.db, project_read_trusted=True)
    assert rebuilt.project_read_trusted is False
    with pytest.raises(ProjectPrivate):
        rebuilt.events(PROJECT)


def test_explicitly_declared_readers_still_read(tmp_path):
    service = _trusted(tmp_path)
    assert SECRET in service.driver_notes.get(PROJECT)["permanent"]
    assert service.store.events(PROJECT)
    assert isinstance(service.director_messages.list_director_messages(PROJECT), dict)


def test_the_wrapper_exposes_no_undecorated_body(tmp_path):
    service = _trusted(tmp_path)
    anonymous = StateDriverNotes(service.store, "anonymous-rebuilder")
    assert not hasattr(anonymous.get, "__wrapped__")
    assert not hasattr(service.driver_notes.get, "__wrapped__")


def _schema_state_tables():
    """Every ``state_*`` table named by a CREATE TABLE in shipped core code."""
    pattern = re.compile(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)")
    names = set()
    for path in (REPO / "core").rglob("*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            if match.group(1).startswith("state_"):
                names.add(match.group(1))
    return names


def test_every_state_table_is_classified_in_one_place():
    """The private set is derived from the schema, not from a dispatch table.

    A ``state_*`` table the schema creates and the one classification does not
    name fails here, so adding a table that nobody classified cannot silently
    become public.
    """
    from core.state_privacy import PRIVATE_STATE_TABLES, PUBLIC_STATE_TABLES

    from_schema = _schema_state_tables()
    assert from_schema, "derivation found no state_* tables - parser broken"
    assert not (set(PRIVATE_STATE_TABLES) & set(PUBLIC_STATE_TABLES))
    unclassified = sorted(from_schema - set(PRIVATE_STATE_TABLES) - set(PUBLIC_STATE_TABLES))
    print("PRIVATE_STATE_TABLES =", sorted(PRIVATE_STATE_TABLES))
    print("PUBLIC_STATE_TABLES =", sorted(PUBLIC_STATE_TABLES))
    print("UNCLASSIFIED =", unclassified)
    assert not unclassified, f"state tables nobody classified: {unclassified}"


# ---------------------------------------------------------------- construction points
# Shipped code: every package and top-level script outside tests/ that can build
# a State object.
_SHIPPED = ("core", "api", "aitelier", "scripts", "examples", "cli", "mutation_gate.py")


def _shipped_paths():
    for entry in _SHIPPED:
        path = REPO / entry
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*.py"))


def _trust_call_sites():
    """Every CALL in shipped code that passes ``project_read_trusted=``, with
    the expression it passes - the construction points a trust level enters by.
    Derived with ``ast``; a site is ``(file:line, callee, expression)``."""
    sites = []
    for path in _shipped_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "project_read_trusted":
                        sites.append((f"{path.relative_to(REPO)}:{node.lineno}",
                                      ast.unparse(node.func), ast.unparse(keyword.value)))
    return sites


def _functions_named(names):
    """The shipped definitions of ``names`` (the trust functions a site calls)."""
    found = {}
    for path in _shipped_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                found[node.name] = (f"{path.relative_to(REPO)}:{node.lineno}", node)
    return found


def _true_constant(node):
    return isinstance(node, ast.Constant) and node.value is True


def _conditional_true(expression: str) -> bool:
    """A conditional expression with a literal ``True`` branch."""
    return any(isinstance(node, ast.IfExp)
               and (_true_constant(node.body) or _true_constant(node.orelse))
               for node in ast.walk(ast.parse(expression, mode="eval")))


def test_the_trusted_construction_points_are_derived_from_code():
    """The list the delivery note carries: every call site that declares a trust
    level, the expression it declares, and - for an expression that is a call -
    the function behind it with its ``return`` values and ``except`` branches.

    Asserted: no site's expression is a conditional with a literal ``True``
    branch (the ``... if request is not None else True`` shape), and no trust
    function returns ``True`` from an ``except`` branch or from a ``None``
    check of its request."""
    sites = _trust_call_sites()
    assert sites, "derivation found no construction point - parser broken"
    called = set()
    offenders = []
    for where, callee, expression in sites:
        print(f"SITE {where} {callee}(project_read_trusted={expression})")
        if _conditional_true(expression):
            offenders.append(f"{where}: conditional with a literal True branch")
        for node in ast.walk(ast.parse(expression, mode="eval")):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
    # One level down: the functions the sites call, and the functions THEY
    # delegate the verdict to.
    frontier, seen = set(called), set()
    while frontier:
        functions = _functions_named(frontier)
        seen |= frontier
        frontier = set()
        for name, (where, function) in sorted(functions.items()):
            returns = [ast.unparse(n.value) for n in ast.walk(function)
                       if isinstance(n, ast.Return) and n.value is not None]
            handlers = [ast.unparse(h).splitlines()[0] for h in ast.walk(function)
                        if isinstance(h, ast.ExceptHandler)]
            print(f"TRUST_FUNCTION {name} at {where} returns={returns} except={handlers}")
            for node in ast.walk(function):
                if isinstance(node, ast.ExceptHandler):
                    for inner in ast.walk(node):
                        if isinstance(inner, ast.Return) and _true_constant(inner.value):
                            offenders.append(f"{where}: {name} returns True from an except branch")
                if isinstance(node, ast.If) and "None" in ast.unparse(node.test):
                    for inner in node.body:
                        if isinstance(inner, ast.Return) and _true_constant(inner.value):
                            offenders.append(f"{where}: {name} returns True when its input is None")
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Call) \
                        and isinstance(node.value.func, ast.Name) \
                        and node.value.func.id not in seen:
                    frontier.add(node.value.func.id)
    literal_true = [where for where, _, expression in sites if expression == "True"]
    print("LITERAL_TRUE_SITES =", literal_true)
    print("TRUST_FUNCTIONS =", sorted(seen))
    assert not offenders, offenders


def test_the_derivation_sees_a_planted_else_true():
    """The derivation's own teeth: the r10 call-site shape, planted."""
    assert _conditional_true("mcp_read_trust(request) if request is not None else True")
    assert not _conditional_true("mcp_read_trust(request)")


# ---------------------------------------------------------------- list_projects
def test_an_untrusted_store_lists_no_unopened_project_by_default(tmp_path):
    service = _trusted(tmp_path)
    anonymous = StateGraphStore(service.db)          # declares nothing
    listed = [p["project_id"] for p in anonymous.list_projects()]
    print("UNTRUSTED_DEFAULT_LIST =", listed)
    assert UNOPENED not in listed and PROJECT in listed
    asked = [p["project_id"] for p in anonymous.list_projects(public_only=False)]
    print("UNTRUSTED_ASKED_FOR_ALL =", asked)
    assert UNOPENED not in asked
    trusted = [p["project_id"] for p in service.store.list_projects()]
    assert UNOPENED in trusted, "control: the trusted store lists the unopened project"


# ---------------------------------------------------------------- MCP without a request
class _NoRequestContext:
    """An MCP context whose transport produced no request object."""

    @property
    def request_context(self):
        raise LookupError("this transport carries no request")


def _mcp_tools(monkeypatch, db):
    import api.dependencies as dependencies
    from api.state_graph_tools import register_state_tools

    tools = {}

    def tool(name, kind, description):
        def register(function):
            tools[name] = function
            return function
        return register

    class FakeMCP:
        def get_context(self):
            return _NoRequestContext()

        def prompt(self, **kwargs):
            return lambda function: function

        def resource(self, *args, **kwargs):
            return lambda function: function

    monkeypatch.setattr(dependencies, "get_db_manager", lambda: db)
    monkeypatch.setattr(dependencies, "get_workspace_manager", lambda: None)
    register_state_tools(tool, FakeMCP())
    return tools


def test_no_request_object_is_no_credential():
    from api.state_graph_tools import mcp_read_trust
    assert mcp_read_trust(None) is False


def test_the_request_decides_when_there_is_one(monkeypatch):
    """Control: with a request the verdict is `_mcp_may_read_private`'s, so the
    None branch above is what made the difference."""
    import types
    from api.state_graph_tools import mcp_read_trust
    request = types.SimpleNamespace(app=types.SimpleNamespace(
        state=types.SimpleNamespace(_test_mode=True)), headers={}, cookies={})
    assert mcp_read_trust(request) is True


def test_an_mcp_call_without_a_request_reads_as_anonymous(tmp_path, monkeypatch):
    """Through the registered MCP tool: `_request_from` fails on this context,
    so the service is built with NO credential and must be untrusted - the
    mailbox is refused and the project list names no unopened project."""
    import asyncio
    service = _trusted(tmp_path)
    tools = _mcp_tools(monkeypatch, service.db)
    with pytest.raises(ProjectPrivate):
        asyncio.run(tools["state_graph_read"]("list_director_messages", {"project_id": PROJECT}))
    listed = asyncio.run(tools["state_graph_read"]("list_projects", {}))["result"]
    names = [p["project_id"] for p in listed]
    print("MCP_NO_REQUEST_LIST =", names)
    assert UNOPENED not in names and PROJECT in names
