"""add_nodes: the generated State help advertises the real nested node-creation
contract (creation key `key`, goal, acceptance, optional dependencies/priority/
facet, criterion id/kind/description) through a self-contained schema, and a
client that builds its request only from that schema can create a node through
the normal command. Unknown fields (node_key, status, extra criterion fields)
are refused before any mutation; the graph's own validation stays authoritative.
"""

import copy

import pytest

from core.state_commands import REQUESTS, describe, execute
from core.state_database import StateDatabase
from core.state_graph import StateGraphError
from core.state_service import StateService

CRIT = {"id": "c", "kind": "test", "description": "d"}
BASE = {"key": "n", "goal": "G", "acceptance": [CRIT]}


def refs_in(schema):
    if isinstance(schema, dict):
        found = [schema["$ref"]] if "$ref" in schema else []
        for value in schema.values():
            found += refs_in(value)
        return found
    if isinstance(schema, list):
        return [r for value in schema for r in refs_in(value)]
    return []


def resolve(schema, root):
    """Resolve local #/$defs refs the way a schema-driven client must."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref = schema["$ref"]
            assert ref.startswith("#/"), f"non-local ref {ref}"
            target = root
            for part in ref[2:].split("/"):
                target = target[part]
            return resolve(target, root)
        return {k: resolve(v, root) for k, v in schema.items() if k != "$defs"}
    if isinstance(schema, list):
        return [resolve(v, root) for v in schema]
    return schema


def node_item_schema():
    arguments = describe()["operations"]["add_nodes"]["arguments"]
    nodes = resolve(arguments["properties"]["nodes"], arguments)
    return arguments, nodes["items"]


def build_from_schema(item, values):
    """A minimal schema-guided client: fills every required property and emits
    only properties the schema declares."""
    properties = item["properties"]
    spec = {name: values[name] for name in item["required"]}
    for name, value in values.items():
        if name in properties:
            spec[name] = value
    return spec


@pytest.fixture
def svc(tmp_path):
    service = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor="fixture",
                           project_read_trusted=True)
    service.create_project("p", "Owned temporary project")
    return service


def add(service, nodes, project_id="p"):
    return execute(service, "add_nodes", {"project_id": project_id, "nodes": nodes}, allow_write=True)


def snapshot(service):
    with service.store.transaction() as conn:
        return {t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1,2")]
                for t in ("state_nodes", "state_dependencies", "state_events")}


def test_help_exposes_creation_key_and_exact_node_fields():
    arguments, item = node_item_schema()
    assert item["type"] == "object"
    assert item["additionalProperties"] is False
    assert set(item["properties"]) == {"key", "goal", "acceptance", "dependencies", "priority", "facet"}
    assert set(item["required"]) == {"key", "goal", "acceptance"}
    assert "node_key" not in item["properties"] and "status" not in item["properties"]
    assert set(arguments["required"]) == {"project_id", "nodes"}


def test_help_exposes_criterion_shape_and_is_self_contained():
    arguments, item = node_item_schema()
    criterion = item["properties"]["acceptance"]["items"]
    assert criterion["additionalProperties"] is False
    assert set(criterion["properties"]) == {"id", "kind", "description"}
    assert set(criterion["required"]) == {"id", "kind", "description"}
    assert set(criterion["properties"]["kind"]["enum"]) == {"test", "review", "human", "artifact", "integration"}
    assert item["properties"]["acceptance"]["minItems"] == 1
    refs = refs_in(arguments)
    assert refs, "nested contract must be declared through local definitions"
    for ref in refs:
        assert ref.startswith("#/$defs/") and ref[len("#/$defs/"):] in arguments["$defs"], ref


def test_schema_guided_client_creates_real_node_with_defaults(svc):
    _, item = node_item_schema()
    spec = build_from_schema(item, {
        "key": "schema.made", "goal": "  Made from the schema  ",
        "acceptance": [build_from_schema(
            item["properties"]["acceptance"]["items"],
            {"id": "c1", "kind": "test", "description": "It works"})]})
    assert add(svc, [spec]) == {"created": ["schema.made"]}
    node = svc.store.get_node("p", "schema.made")
    assert node["goal"] == "Made from the schema"
    assert node["acceptance"] == [{"id": "c1", "kind": "test", "description": "It works"}]
    assert node["dependencies"] == [] and node["priority"] == 0 and node["facet"] is None
    assert node["status"] == "OPEN" and node["revision"] == 1


def test_optional_fields_reach_the_graph_unchanged(svc):
    crit = [{"id": "b", "kind": "review", "description": "B"}, {"id": "a", "kind": "human", "description": "A"}]
    add(svc, [{"key": "base", "goal": "Base", "acceptance": crit, "facet": "contract", "priority": 7},
              {"key": "child", "goal": "Child", "acceptance": crit, "dependencies": ["base"], "facet": "content"}])
    base, child = svc.store.get_node("p", "base"), svc.store.get_node("p", "child")
    assert (base["priority"], base["facet"]) == (7, "contract")
    assert [c["id"] for c in base["acceptance"]] == ["a", "b"]
    assert child["dependencies"] == ["base"] and child["facet"] == "content"


def test_validated_request_serializes_only_the_sent_fields():
    model = REQUESTS["add_nodes"].model_validate({"project_id": "p", "nodes": [BASE]})
    assert model.model_dump()["nodes"] == [BASE]


@pytest.mark.parametrize("bad", [
    {"node_key": "n", "goal": "G", "acceptance": [CRIT]},   # read-record name; deliberately not an alias
    {**BASE, "node_key": "n"},
    {**BASE, "status": "VERIFIED"},
    {**BASE, "workflow": "wf"},
    {**BASE, "acceptance": [{**CRIT, "extra": "x"}]},
    {**BASE, "acceptance": [{**CRIT, "kind": "magic"}]},
    {**BASE, "acceptance": []},
    {**BASE, "facet": "nonsense"},
    {**BASE, "dependencies": "base"},
    {**BASE, "priority": "high"},
    {"goal": "G", "acceptance": [CRIT]},
])
def test_unknown_or_malformed_nodes_are_refused_without_mutation(svc, bad):
    add(svc, [{"key": "keep", "goal": "Keep", "acceptance": [CRIT]}])
    before = snapshot(svc)
    with pytest.raises(StateGraphError):
        add(svc, [copy.deepcopy(BASE), bad])
    assert snapshot(svc) == before


def test_graph_validation_remains_authoritative(svc):
    add(svc, [BASE])
    before = snapshot(svc)
    with pytest.raises(StateGraphError):   # duplicate key: decided by the graph, not the schema
        add(svc, [BASE])
    with pytest.raises(StateGraphError):   # unknown dependency
        add(svc, [{**BASE, "key": "m", "dependencies": ["ghost"]}])
    with pytest.raises(StateGraphError):   # duplicate criterion ids
        add(svc, [{**BASE, "key": "m", "acceptance": [CRIT, CRIT]}])
    assert snapshot(svc) == before


def test_adjacent_commands_and_write_refusal_are_unchanged(svc):
    add(svc, [BASE])
    assert execute(svc, "get_node", {"project_id": "p", "node_key": "n"})["node"]["node_key"] == "n"
    before = snapshot(svc)
    with pytest.raises(StateGraphError):   # read surface refuses the mutating action
        execute(svc, "add_nodes", {"project_id": "p", "nodes": [{**BASE, "key": "other"}]})
    assert snapshot(svc) == before
    # split_node children are untouched by this change
    split = describe()["operations"]["split_node"]["arguments"]
    assert split["properties"]["children"]["items"]["type"] == "object"
