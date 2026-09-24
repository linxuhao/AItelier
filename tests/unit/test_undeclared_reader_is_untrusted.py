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

REPO = Path(__file__).resolve().parents[2]
PROJECT = "undeclared-reader"
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
    # The project is OPEN: only the ACTION keeps the notebook writer-only, so a
    # non-403 would be a real leak, not the project gate doing its job.
    service.open_project(PROJECT)
    return service


def test_a_notebook_rebuilt_without_a_declaration_refuses(tmp_path):
    service = _trusted(tmp_path)
    notebook = StateDriverNotes(service.store, "anonymous-rebuilder")
    with pytest.raises(ProjectPrivate):
        notebook.get(PROJECT)


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
    service = _trusted(tmp_path)
    service.store.project_read_trusted = False
    notebook = StateDriverNotes(service.store, "anonymous-rebuilder")
    with pytest.raises(ProjectPrivate):
        notebook.get(PROJECT)


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
