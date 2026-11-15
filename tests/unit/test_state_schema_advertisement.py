"""What `describe()` publishes about a notebook scope must be what the model takes.

A notebook action accepts exactly one of `project_id` or `driver_id`, and
`execute` pops `driver_id` and routes a truthy one to that driver's PRIVATE
notebook (`core/state_commands.py`, the `NotebookScopeRequest` branch of
`execute`). Nothing about that routing asks the caller to use
`driver_arguments`: an ordinary `arguments` object carrying a string
`driver_id` is accepted and lands in the private notebook.

An advertisement narrower than that is not a cosmetic mismatch. A client that
validates its call against `arguments` may only drop the key it is told is
invalid, and the write then lands in the nameable project's PUBLIC notebook —
the caller loses its target without any refusal to notice.

So the expectation here is derived from the request model itself (the
`driver_id` annotation on the request class), never from a copy of the
advertised JSON: the advertisement is the thing under test, and a hardcoded
copy of it would only restate the bug.
"""
import types
from typing import Union, get_args, get_origin

import pytest

from core.drivers import DriverRegistry
from core.state_commands import REQUESTS, describe, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

# One READ and one WRITE action, so the advertisement is checked on both
# surfaces a schema-driven client builds calls from.
READ_ACTION = "driver_note_index"
WRITE_ACTION = "write_driver_note_entry"

_JSON_TYPE = {str: "string", int: "integer", float: "number", bool: "boolean",
              type(None): "null"}


def _declared_json_types(annotation) -> set:
    """The JSON types a Pydantic field annotation admits: `str | None` -> the string and null types."""
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        out = set()
        for part in get_args(annotation):
            out |= _declared_json_types(part)
        return out
    return {_JSON_TYPE[annotation]}


def _advertised_json_types(schema: dict) -> set:
    """The JSON types an advertised schema fragment admits, whatever shape the union is written in."""
    declared = schema.get("type")
    out = set(declared if isinstance(declared, list) else [declared]) - {None}
    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key, []):
            out |= _advertised_json_types(branch)
    return out


@pytest.fixture
def world(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    owner = StateService(db, actor="fixture", project_read_trusted=True)
    owner.create_project("alpha", "alpha")
    registry = DriverRegistry(db, "test-only-pepper-not-a-production-secret")
    registry.register("ada", "ada", actor="test")
    registry.register("bob", "bob", actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    drv = {d: StateService(db, actor="driver:" + d, driver_id=d, project_read_trusted=True)
           for d in ("ada", "bob")}
    return owner, drv


@pytest.mark.parametrize("action", [READ_ACTION, WRITE_ACTION])
def test_project_variant_advertises_every_driver_id_shape_the_model_takes(action):
    """RED at the base commit, GREEN once the advertisement stops narrowing.

    The base `_operation_schema` overwrote the model's own union with
    `{"type": "null", "default": None}` (core/state_commands.py line 1269 at that
    commit), so `arguments.properties.driver_id` admitted only null while
    `NotebookScopeRequest.driver_id` admits null AND a string. That one assertion
    is the whole difference measured here: the advertised type set must equal the
    type set the request model declares, not a subset of it.
    """
    model = REQUESTS[action]
    field = model.model_fields["driver_id"]
    advertised = describe()["operations"][action]["arguments"]["properties"]["driver_id"]
    declared = _declared_json_types(field.annotation)
    measured = _advertised_json_types(advertised)
    assert measured == declared, (
        f"{action} advertises driver_id as {advertised!r}, admitting {sorted(measured)}, "
        f"while the request model takes {sorted(declared)}")
    assert "string" in measured


def test_the_model_accepts_the_project_shaped_call_the_base_schema_forbade():
    """The value the old advertisement ruled out is one the model validates."""
    call = REQUESTS[WRITE_ACTION].model_validate({
        "project_id": None, "driver_id": "ada",
        "assertion": "a", "body": "b", "director_identity": "ada"})
    assert call.driver_id == "ada" and call.project_id is None


def test_a_project_shaped_write_with_a_string_driver_id_lands_in_the_private_notebook(world):
    """The shape the advertisement now admits is the shape `execute` already honours."""
    owner, drv = world
    private_rule = "private rule only ada wrote"
    written = execute(drv["ada"], WRITE_ACTION,
                      {"project_id": None, "driver_id": "ada",
                       "assertion": private_rule, "body": "why"}, allow_write=True)
    assert written["address"].startswith("dnote://ada/")
    private = execute(drv["ada"], READ_ACTION, {"driver_id": "ada"})
    project = execute(owner, READ_ACTION, {"project_id": "alpha"})
    assert private_rule in str(private["entries"])
    assert private_rule not in str(project["entries"])


def test_a_project_shaped_write_naming_no_driver_still_lands_in_the_project_notebook(world):
    """The project scope is untouched: no driver_id means the project's own notebook."""
    owner, drv = world
    project_rule = "project rule for alpha"
    written = execute(drv["ada"], WRITE_ACTION,
                      {"project_id": "alpha", "assertion": project_rule, "body": "why"},
                      allow_write=True)
    assert written["address"].startswith("note://alpha/")
    project = execute(owner, READ_ACTION, {"project_id": "alpha"})
    private = execute(drv["ada"], READ_ACTION, {"driver_id": "ada"})
    assert project_rule in str(project["entries"])
    assert project_rule not in str(private["entries"])


def test_two_scopes_at_once_stay_refused(world):
    """Widening the advertisement admits no new input: the model's one-scope rule decides."""
    _, drv = world
    with pytest.raises(StateGraphError) as refusal:
        execute(drv["ada"], WRITE_ACTION,
                {"project_id": "alpha", "driver_id": "ada",
                 "assertion": "a", "body": "b"}, allow_write=True)
    # The verdict is the refusal, not its wording: name the two fields it is about.
    message = str(refusal.value)
    assert "project_id" in message and "driver_id" in message


def test_the_private_variant_keeps_naming_the_driver_scope():
    """The private path stays discoverable beside the widened project variant."""
    private = describe()["operations"][WRITE_ACTION]["driver_arguments"]
    assert _advertised_json_types(private["properties"]["driver_id"]) == {"string"}
    assert "driver_id" in private["required"]
    assert "project_id" not in private["required"]
