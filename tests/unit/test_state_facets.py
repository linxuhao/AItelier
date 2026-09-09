"""Facet rules: build on contracts, never on implementations (design/state_facets.md).

Every rule is asserted twice — once as a rejection whose message names the fix,
once as the corresponding legal shape — so deleting a clause fails a test rather
than silently widening what the engine accepts.
"""
import pytest

from core.db_manager import DBManager
from core.state_commands import execute
from core.state_graph import StateConflict, StateGraphError, StateGraphStore, facet_violations
from core.state_service import StateService


def node(k, deps=(), facet=None):
    spec = {"key": k, "goal": f"Deliver {k}", "dependencies": list(deps),
            "acceptance": [{"id": "c", "kind": "test", "description": "Real check passes"}]}
    if facet:
        spec["facet"] = facet
    return spec


@pytest.fixture
def store(tmp_path):
    result = StateGraphStore(DBManager(str(tmp_path / "state.sqlite")))
    result.create_project("game", "Game")
    return result


# ── the pure rule ────────────────────────────────────────────────────────────

def test_legacy_nodes_are_exempt_in_both_directions():
    facets = {"a": None, "b": None, "c": None}
    graph = {"a": [], "b": ["a"], "c": ["b"]}
    assert facet_violations(facets, graph) == []


def test_content_may_not_build_on_content_and_the_message_names_the_contract_to_create():
    problems = facet_violations({"a": "content", "b": "content"}, {"a": [], "b": ["a"]})
    assert len(problems) == 1
    assert "Create `a.contract`" in problems[0] and "facet integration" in problems[0]


def test_contract_and_design_are_the_only_buildable_targets():
    facets = {"d": "design", "a.contract": "contract", "a.test": "test", "a": "content",
              "b.contract": "contract", "b": "content"}
    graph = {"d": [], "a.contract": ["d"], "a.test": ["a.contract"], "a": ["a.contract", "a.test"],
             "b.contract": ["a.contract"], "b": ["b.contract", "a.contract"]}
    assert facet_violations(facets, graph) == []
    # A test node is not buildable by anyone but its own implementation.
    bad = dict(graph, **{"b": ["b.contract", "a.test"]})
    assert any("cannot depend on `a.test` (test)" in p for p in facet_violations(facets, bad))


def test_integration_may_depend_on_implementations():
    facets = {"a": "content", "b": "content", "v": "integration"}
    graph = {"a": [], "b": [], "v": ["a", "b"]}
    assert facet_violations(facets, graph) == []


def test_a_faceted_node_may_not_depend_on_a_legacy_node():
    problems = facet_violations({"old": None, "new": "content"}, {"old": [], "new": ["old"]})
    assert problems == ["`new` (content) depends on `old`, which has no facet yet; set_node_facet on `old` first "
                        "— dependencies are faceted bottom-up"]


def test_if_you_made_them_they_chain():
    facets = {"k.contract": "contract", "k.test": "test", "k": "content"}
    # test must depend on its contract; implementation must depend on both.
    problems = facet_violations(facets, {"k.contract": [], "k.test": [], "k": []})
    assert {p.split(" but")[0] for p in problems} == {"`k.test` has `k.contract`", "`k` has `k.contract`", "`k` has `k.test`"}
    assert facet_violations(facets, {"k.contract": [], "k.test": ["k.contract"], "k": ["k.contract", "k.test"]}) == []
    # Without a test node nothing requires one: R1 alone guarantees contracts.
    assert facet_violations({"k.contract": "contract", "k": "content"}, {"k.contract": [], "k": ["k.contract"]}) == []


def test_key_suffix_must_agree_with_the_facet():
    assert facet_violations({"k.contract": "content"}, {"k.contract": []}) == \
        ["`k.contract` ends with .contract so its facet must be contract, not content"]
    assert facet_violations({"k.test": "test", "k.contract": "contract"}, {"k.test": ["k.contract"], "k.contract": []}) == []


# ── the store ────────────────────────────────────────────────────────────────

def test_add_nodes_rejects_a_violation_and_writes_nothing(store):
    with pytest.raises(StateGraphError, match="Create `a.contract`"):
        store.add_nodes("game", [node("a", facet="content"), node("b", ["a"], facet="content")])
    assert store.get_graph("game")["nodes"] == []


def test_add_nodes_accepts_the_legal_shape_and_records_the_facet(store):
    store.add_nodes("game", [node("d", facet="design"), node("a.contract", ["d"], facet="contract"),
                             node("a.test", ["a.contract"], facet="test"),
                             node("a", ["a.contract", "a.test"], facet="content"),
                             node("b", ["a.contract"], facet="content"), node("v", ["a", "b"], facet="integration")])
    facets = {n["node_key"]: n["facet"] for n in store.get_graph("game")["nodes"]}
    assert facets == {"d": "design", "a.contract": "contract", "a.test": "test", "a": "content", "b": "content", "v": "integration"}
    created = [e for e in store.events("game") if e["event_type"] == "node_created" and e["node_key"] == "a"]
    assert created[0]["payload"]["facet"] == "content"
    assert store.facet_lint("game") == {"violations": [], "faceted": 6, "legacy": 0, "facets": facets}


def test_unfaceted_nodes_behave_exactly_as_before(store):
    store.add_nodes("game", [node("a"), node("b", ["a"]), node("c", ["b"])])
    assert all(n["facet"] is None for n in store.get_graph("game")["nodes"])
    assert store.facet_lint("game") == {"violations": [], "faceted": 0, "legacy": 3, "facets": {}}


def test_set_node_facet_labels_once_without_a_revision(store):
    store.add_nodes("game", [node("d"), node("a", ["d"])])
    with pytest.raises(StateGraphError, match="set_node_facet on `d` first"):
        store.set_node_facet("game", "a", "content")
    assert store.set_node_facet("game", "d", "design") == {"key": "d", "facet": "design", "changed": True}
    assert store.set_node_facet("game", "a", "content")["changed"] is True
    assert store.set_node_facet("game", "a", "content")["changed"] is False
    assert store.get_node("game", "a")["revision"] == 1, "a label is not a contract change"
    assert [e["event_type"] for e in store.events("game")][-2:] == ["node_facet_set", "node_facet_set"]
    with pytest.raises(StateConflict, match="set once"):
        store.set_node_facet("game", "a", "contract")
    with pytest.raises(StateGraphError):
        store.set_node_facet("game", "a", "magic")


def test_labelling_keeps_verified_design_nodes_verified(store):
    store.add_nodes("game", [node("d")])
    with store.db.get_connection() as conn:
        conn.execute("UPDATE state_nodes SET status='VERIFIED',verified_receipt='r' WHERE node_key='d'")
        conn.commit()
    store.set_node_facet("game", "d", "design")
    assert store.get_node("game", "d")["status"] == "VERIFIED"


def test_revise_node_re_checks_edges(store):
    store.add_nodes("game", [node("a.contract", facet="contract"), node("a", ["a.contract"], facet="content"),
                             node("b", ["a.contract"], facet="content")])
    with pytest.raises(StateGraphError, match="cannot depend on `a` \\(content\\)"):
        store.revise_node("game", "b", 1, "point at the implementation", dependencies=["a"])
    assert store.get_node("game", "b")["revision"] == 1


def test_a_faceted_node_cannot_be_added_on_top_of_a_legacy_node(store):
    store.add_nodes("game", [node("a")])
    with pytest.raises(StateGraphError, match="set_node_facet on `a` first"):
        store.add_nodes("game", [node("b", ["a"], facet="content")])
    # The lint over a stored graph is an invariant probe: the engine never
    # holds a violating state, so its job is the faceted/legacy census.
    assert store.facet_lint("game") == {"violations": [], "faceted": 0, "legacy": 1, "facets": {}}


# ── the command surface and the projection ──────────────────────────────────

def test_commands_expose_set_node_facet_and_facet_lint(tmp_path):
    service = StateService(DBManager(str(tmp_path / "host.sqlite")))
    service.create_project("game", "Game")
    service.store.add_nodes("game", [node("d")])
    with pytest.raises(StateGraphError):
        execute(service, "set_node_facet", {"project_id": "game", "node_key": "d", "facet": "design"})
    execute(service, "set_node_facet", {"project_id": "game", "node_key": "d", "facet": "design"}, allow_write=True)
    assert execute(service, "facet_lint", {"project_id": "game"})["facets"] == {"d": "design"}
    assert service.portfolio.overview("game")["nodes"][0]["facet"] == "design"
