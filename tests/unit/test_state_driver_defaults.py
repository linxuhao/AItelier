"""A registered driver may omit the arguments its credential already answers.

director_identity defaults to the driver id; a required project_id defaults to
the driver's project when it is an active member of exactly one. A notebook READ
with no scope named keeps that default. A notebook WRITE never takes one: it
must name its notebook (project_id or driver_id). Everything else about the
request is validated exactly as before.
"""
import pytest

from core.drivers import DriverRegistry
from core.state_commands import _driver_defaults, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

ISSUE = {"request_key": "k1", "kind": "defect", "title": "t", "body": "b"}


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    owner = StateService(db, actor="fixture", project_read_trusted=True)
    owner.create_project("alpha", "alpha")
    owner.create_project("beta", "beta")
    registry = DriverRegistry(db, "test-only-pepper-not-a-production-secret")
    for did in ("ada", "bob"):
        registry.register(did, did, actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    registry.set_membership("alpha", "bob", "member", 0, "fixture", actor="test")
    registry.set_membership("beta", "bob", "member", 0, "fixture", actor="test")
    drv = {d: StateService(db, actor="driver:" + d, driver_id=d, project_read_trusted=True) for d in ("ada", "bob")}
    return owner, drv, registry, db


def test_single_project_driver_omits_project_and_identity(world):
    _, drv, _, _ = world
    issue = execute(drv["ada"], "report_issue", dict(ISSUE), allow_write=True)
    assert issue["project_id"] == "alpha"
    assert issue["reported_by"]["director_identity"] == "ada"


def test_explicit_values_are_kept_and_still_checked(world):
    _, drv, _, _ = world
    issue = execute(drv["ada"], "report_issue", {**ISSUE, "director_identity": "ada/sub"}, allow_write=True)
    assert issue["reported_by"]["director_identity"] == "ada/sub"
    with pytest.raises(StateGraphError, match="director_identity must be"):
        execute(drv["ada"], "report_issue", {**ISSUE, "request_key": "k2", "director_identity": "bob"},
                allow_write=True)


def _entry_rows(db):
    # Both notebooks: the project's public one and the driver's private one.
    with db.get_connection() as conn:
        project = conn.execute("SELECT COUNT(*) FROM state_driver_note_entries").fetchone()[0]
        private = conn.execute("SELECT COUNT(*) FROM driver_note_entries").fetchone()[0]
    return project, private


def test_notebook_write_naming_no_scope_is_refused_not_defaulted(world):
    """iss-4cbf348f-1115: a notebook WRITE must name its notebook.

    Measured defect: a write_driver_note_entry whose driver_id did not survive
    the transport was silently completed as a PUBLIC project-notebook write
    (note://aitelier/...). A write with no scope is now refused; no row is
    created in either notebook.
    """
    _, drv, _, db = world
    before = _entry_rows(db)
    with pytest.raises(StateGraphError) as refusal:
        execute(drv["ada"], "write_driver_note_entry", {"assertion": "a rule", "body": "why"}, allow_write=True)
    message = str(refusal.value)
    assert "project_id" in message and "driver_id" in message
    assert "default" in message
    assert _entry_rows(db) == before
    # The read keeps its convenience: the sole project is still the default scope.
    assert _driver_defaults(drv["ada"], "driver_note_index", {})["project_id"] == "alpha"
    assert _driver_defaults(drv["ada"], "get_driver_note", {})["project_id"] == "alpha"


def test_notebook_supersede_naming_no_scope_is_refused_not_defaulted(world):
    """iss-4cbf348f4e7f4742: the same refusal covers supersede_driver_note_entry.

    One scope rule guards both writers. A default that only the write action lost
    would leave the supersede path able to publish private text into the project
    notebook, so the second writer is witnessed here rather than assumed.
    """
    _, drv, _, db = world
    first = execute(drv["ada"], "write_driver_note_entry",
                    {"project_id": "alpha", "assertion": "a rule", "body": "why",
                     "director_identity": "ada"}, allow_write=True)
    before = _entry_rows(db)
    with pytest.raises(StateGraphError) as refusal:
        execute(drv["ada"], "supersede_driver_note_entry",
                {"entry_id": first["entry_id"], "assertion": "successor", "body": "b",
                 "reason": "replace"}, allow_write=True)
    message = str(refusal.value)
    assert "project_id" in message and "driver_id" in message
    assert _entry_rows(db) == before
    index = execute(drv["ada"], "driver_note_index", {"project_id": "alpha"})
    assert "a rule" in str(index) and "successor" not in str(index)


def test_driver_scoped_notebook_writes_answer_a_private_address_through_execute(world):
    """iss-4cbf348f-1115: drive the server entry point with the arguments the driver sent.

    The driver names its private notebook with driver_id and no project_id. Both
    successive writes must answer a dnote:// address (the private notebook); the
    control arm names project_id and must answer a note:// address (the project's
    public notebook). Nothing is defaulted in either direction.
    """
    _, drv, _, _ = world
    first = execute(drv["ada"], "write_driver_note_entry",
                    {"assertion": "private draft one", "body": "b", "driver_id": "ada"}, allow_write=True)
    second = execute(drv["ada"], "write_driver_note_entry",
                     {"assertion": "private draft two", "body": "b", "driver_id": "ada"}, allow_write=True)
    assert first["address"].startswith("dnote://ada/")
    assert second["address"].startswith("dnote://ada/")
    control = execute(drv["ada"], "write_driver_note_entry",
                      {"assertion": "public rule", "body": "b", "project_id": "alpha",
                       "director_identity": "ada"}, allow_write=True)
    assert control["address"].startswith("note://alpha/")


def test_notebook_entry_naming_a_driver_gets_no_project(world):
    _, drv, _, _ = world
    out = _driver_defaults(drv["ada"], "write_driver_note_entry", {"driver_id": "ada", "assertion": "a", "body": "b"})
    assert "project_id" not in out
    execute(drv["ada"], "write_driver_note_entry", {"driver_id": "ada", "assertion": "private", "body": "b"},
            allow_write=True)


def test_optional_project_id_is_left_alone(world):
    _, drv, _, _ = world
    assert "project_id" not in _driver_defaults(drv["ada"], "user_conversation_unread", {})
    assert "project_id" not in _driver_defaults(drv["ada"], "send_driver_message", {"request_key": "r"})


def test_two_project_driver_must_still_name_the_project(world):
    _, drv, _, _ = world
    with pytest.raises(StateGraphError, match="project_id"):
        execute(drv["bob"], "report_issue", dict(ISSUE), allow_write=True)


def test_removed_membership_does_not_count(world):
    _, drv, registry, _ = world
    registry.set_membership("beta", "bob", "removed", 1, "fixture", actor="test")
    issue = execute(drv["bob"], "report_issue", dict(ISSUE), allow_write=True)
    assert issue["project_id"] == "alpha"


def test_untrusted_or_owner_callers_get_no_defaults(world):
    owner, _, _, db = world
    with pytest.raises(StateGraphError, match="project_id"):
        execute(owner, "report_issue", {**ISSUE, "director_identity": "x"}, allow_write=True)
    untrusted = StateService(db, actor="driver:ada", driver_id="ada", project_read_trusted=False)
    assert _driver_defaults(untrusted, "report_issue", dict(ISSUE)) == ISSUE


@pytest.mark.parametrize("action", ["close_project", "refresh_project", "import_tasks"])
def test_project_level_actions_are_never_defaulted(world, action):
    _, drv, _, _ = world
    assert "project_id" not in _driver_defaults(drv["ada"], action, {})


def test_a_redundant_project_id_is_still_refused(world):
    _, drv, _, _ = world
    with pytest.raises(StateGraphError, match="Extra inputs"):
        execute(drv["ada"], "get_attempt", {"attempt_id": "missing", "project_id": "alpha"})
