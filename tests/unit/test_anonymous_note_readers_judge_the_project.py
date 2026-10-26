"""The driver-note readers judge the PROJECT at the read, not only at execute().

Residual R1 item 3 (iss-e8361e3eb60b436f): on the anonymous service,
``service.driver_notes.get(<unopened>)`` used to RETURN an empty note while
``service.driver_notes.get(<missing>)`` raised ``StateNotFound`` - the method
itself was an existence oracle; only ``execute`` (``_refuse_if_project_not_public``)
and the product route equalised them.

For an UNTRUSTED owner every public-readable note reader now raises the same
``ProjectPrivate`` (type, status_code, detail and str) for an unopened project
and for a missing one, whether reached through the decorated method or through
its unwrapped body, using the same ``store.is_project_public`` predicate as
``execute``. Opened projects, trusted callers and driver notebooks are unchanged.

Each reader is its own parametrized case, so dropping the verdict for ONE
reader turns exactly that reader's cases red.
"""
from __future__ import annotations

import pytest

from core.state_commands import ProjectPrivate, execute
import core.state_commands as state_commands
from core.state_graph import StateGraphError, StateNotFound
from core.state_service import StateService
from tests.support import state_canaries as C

MISSING = "canary-no-such-project"
UNKNOWN_ENTRY = "c0ffee0c0ffe"   # the SHUT project's planted canary entry id

# reader method -> (execute action, extra keyword arguments)
READERS = {
    "get": ("get_driver_note", {}),
    "history": ("driver_note_history", {}),
    "search": ("search_driver_note_history", {"query": ""}),
    "get_entry": ("get_driver_note_entry", {"entry_id": UNKNOWN_ENTRY}),
    "entry_index": ("driver_note_index", {}),
    "search_entries": ("search_driver_note_entries", {"query": ""}),
    "check_index": ("check_driver_note_index", {}),
}


def _closure_body(method):
    """The undecorated body below ``@writer_only_read`` (same reach as the
    helper in test_every_private_table_reader_is_judged_at_the_read.py)."""
    function = getattr(method, "__func__", method)
    for cell in function.__closure__ or ():
        if callable(cell.cell_contents):
            return cell.cell_contents
    raise AssertionError(f"{function.__qualname__} has no callable in its closure")


def _call(notes, name, project_id, bare, **overrides):
    _, extra = READERS[name]
    kwargs = {**extra, **overrides}
    method = getattr(notes, name)
    if bare:
        return _closure_body(method)(notes, project_id, **kwargs)
    return method(project_id, **kwargs)


def _refusal(call):
    with pytest.raises(ProjectPrivate) as refused:
        call()
    exc = refused.value
    return type(exc), exc.status_code, exc.detail, str(exc)


@pytest.fixture
def database(tmp_path):
    return C.new_database(tmp_path, "note-readers.sqlite")


@pytest.fixture
def anonymous(database):
    return StateService(database, actor="anonymous", project_read_trusted=False)


@pytest.fixture
def trusted(database):
    return StateService(database, actor="seeder", project_read_trusted=True)


@pytest.mark.parametrize("bare", [False, True], ids=["wrapped", "bare-body"])
@pytest.mark.parametrize("name", sorted(READERS))
def test_unopened_and_missing_raise_the_identical_refusal(anonymous, name, bare):
    action, _ = READERS[name]
    notes = anonymous.driver_notes
    unopened = _refusal(lambda: _call(notes, name, C.SHUT, bare))
    missing = _refusal(lambda: _call(notes, name, MISSING, bare))
    print(f"READER {name} bare={bare} unopened={unopened} missing={missing}")
    assert unopened == missing
    assert unopened[1] == 403
    # Byte-identical to the refusal execute() already gives for the same action.
    args = {"project_id": C.SHUT, **({"entry_id": UNKNOWN_ENTRY}
                                     if action == "get_driver_note_entry" else {})}
    assert unopened == _refusal(lambda: execute(anonymous, action, args))


@pytest.mark.parametrize("bare", [False, True], ids=["wrapped", "bare-body"])
@pytest.mark.parametrize("name", sorted(READERS))
def test_an_opened_project_still_answers_the_anonymous_caller(anonymous, name, bare):
    notes = anonymous.driver_notes
    overrides = {}
    if name == "get_entry":
        overrides["entry_id"] = notes.entry_index(C.OPEN)["entries"][0]["entry_id"]
    answer = _call(notes, name, C.OPEN, bare, **overrides)
    assert isinstance(answer, dict)
    assert C.leaked_tables(repr(answer)) == []
    if name in {"get", "entry_index", "get_entry"}:
        assert C.OPEN_NOTE in repr(answer), (name, answer)
    action, _ = READERS[name]
    args = {"project_id": C.OPEN, **overrides}
    if name in {"search", "search_entries"}:
        args["query"] = ""
    assert execute(anonymous, action, args) == answer


@pytest.mark.parametrize("name", sorted(READERS))
def test_the_trusted_caller_keeps_todays_answers(trusted, name):
    """Control: an unopened project the trusted caller owns still reads, and a
    missing project is still StateNotFound - never ProjectPrivate."""
    trusted.create_project("canary-fresh-unopened", "fresh")
    notes = trusted.driver_notes
    with pytest.raises(StateNotFound):
        _call(notes, name, MISSING, False)
    if name == "get_entry":
        with pytest.raises(StateNotFound):
            _call(notes, name, "canary-fresh-unopened", False)
        return
    answer = _call(notes, name, "canary-fresh-unopened", False)
    assert isinstance(answer, dict)
    if name == "get":
        assert answer["revision"] == 0 and answer["entry_count"] == 0
        assert answer["index"] == []
    # The trusted caller still reads the unopened project's own notebook.
    assert C.canary("state_driver_note_entries") in repr(
        notes.entry_index(C.SHUT, include_delisted=True))


def test_the_driver_notebook_scope_is_unchanged(anonymous):
    """Driver notebooks keep their own refusal for an anonymous caller."""
    scoped = anonymous.driver_notes.for_driver("canary-driver")
    with pytest.raises(StateGraphError) as refused:
        scoped.get("canary-driver")
    assert not isinstance(refused.value, ProjectPrivate)
    assert str(refused.value) == "private_notebook"


@pytest.mark.parametrize("name", ["write_entry", "supersede_entry", "delist_entry"])
def test_the_entry_writers_on_the_anonymous_graph_refuse_alike(anonymous, name):
    """Control (already true on the base): the three entry writers that share the
    get_driver_note_entry action are refused identically for both projects."""
    notes = anonymous.driver_notes
    args = {
        "write_entry": ("assertion", "body", "anonymous"),
        "supersede_entry": (UNKNOWN_ENTRY, "assertion", "body", "reason", "anonymous"),
        "delist_entry": (UNKNOWN_ENTRY, "reason", "anonymous"),
    }[name]
    method = getattr(notes, name)
    assert (_refusal(lambda: method(C.SHUT, *args))
            == _refusal(lambda: method(MISSING, *args)))


# ---------------------------------------------------------------- the WRITERS
# writer method -> (its own WRITE action, positional arguments after project_id)
WRITERS = {
    "write_entry": ("write_driver_note_entry", ("assertion", "body", "anonymous")),
    "supersede_entry": ("supersede_driver_note_entry",
                        (UNKNOWN_ENTRY, "assertion", "body", "reason", "anonymous")),
    "delist_entry": ("delist_driver_note_entry", (UNKNOWN_ENTRY, "reason", "anonymous")),
}


class _DatabaseTripwire:
    """A database/store stand-in that fails the test on ANY use and counts it."""

    def __init__(self):
        self.touched = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise AssertionError(f"the untrusted writer touched the database stand-in: {name}")


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_no_entry_writer_carries_a_public_read_label(name):
    """Structural: a writer is judged by its OWN write action, never by a read
    action, and certainly not by a public one (iss-9e7abf05429d497a)."""
    from core.state_driver_notes import StateDriverNotes
    method = getattr(StateDriverNotes, name)

    action, _ = WRITERS[name]
    read_label = getattr(method, "__state_read_action__", None)
    assert read_label is None, (
        f"StateDriverNotes.{name} carries the READ label {read_label!r} "
        f"(public: {read_label in state_commands.PUBLIC_READS})")
    assert getattr(method, "__state_write_action__", None) == action, name
    assert action in state_commands.WRITE_REQUESTS
    assert action not in state_commands.PUBLIC_READS
    assert action not in state_commands.WRITER_ONLY_READS
    assert state_commands.read_visibility(action) == "private"
    # The reader that shares the old label stays a public read, unchanged.
    assert "get_driver_note_entry" in state_commands.PUBLIC_READS
    assert "get_driver_note_entry" not in state_commands.WRITER_ONLY_READS


@pytest.mark.parametrize("project", [C.SHUT, MISSING, C.OPEN], ids=["unopened", "missing", "opened"])
@pytest.mark.parametrize("name", sorted(WRITERS))
def test_an_untrusted_owner_is_refused_before_any_database_access(anonymous, name, project):
    """The refusal happens AT THE METHOD: the store behind it is a tripwire that
    fails on any attribute access, so no connection or transaction can open."""
    _, args = WRITERS[name]
    notes = anonymous.driver_notes
    tripwire = _DatabaseTripwire()
    notes.store = tripwire
    with pytest.raises(ProjectPrivate):
        getattr(notes, name)(project, *args)
    assert tripwire.touched == [], (name, tripwire.touched)


@pytest.mark.parametrize("name", sorted(WRITERS))
def test_an_untrusted_writer_opens_no_connection_and_writes_nothing(anonymous, name, monkeypatch):
    """Same property on the REAL anonymous store: the refusal is identical for
    an unopened and a missing project, and sqlite3.connect is never called."""
    import sqlite3
    _, args = WRITERS[name]
    notes = anonymous.driver_notes
    real_connect = sqlite3.connect
    opened = []

    def counting_connect(*a, **k):
        opened.append(a)
        return real_connect(*a, **k)

    monkeypatch.setattr(sqlite3, "connect", counting_connect)
    method = getattr(notes, name)
    unopened = _refusal(lambda: method(C.SHUT, *args))
    missing = _refusal(lambda: method(MISSING, *args))
    assert unopened == missing
    assert unopened[1] == 403
    assert opened == [], f"{name} opened {len(opened)} connection(s) for an untrusted owner"


def test_a_trusted_owner_still_writes_supersedes_and_delists(trusted):
    """Positive control, one per writer: the trusted owner is unchanged."""
    notes = trusted.driver_notes
    trusted.create_project("canary-writer-trusted", "writer")
    pid = "canary-writer-trusted"
    written = notes.write_entry(pid, "first rule", "first body", "trusted-director")
    assert written["entry_count"] == 1
    first = written["entry_id"]
    moved = notes.supersede_entry(pid, first, "second rule", "second body", "changed",
                                  "trusted-director")
    assert moved["superseded"]["entry_id"] == first
    successor = moved["successor"]["entry_id"]
    delisted = notes.delist_entry(pid, successor, "obsolete", "trusted-director")
    assert delisted["listing"] == "delisted"
