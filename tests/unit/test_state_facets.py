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
    lint = store.facet_lint("game")
    assert lint["violations"] == [] and lint["warnings"] == [] and (lint["faceted"], lint["legacy"]) == (6, 0)
    assert lint["facets"] == facets


def test_unfaceted_nodes_behave_exactly_as_before(store):
    store.add_nodes("game", [node("a"), node("b", ["a"]), node("c", ["b"])])
    assert all(n["facet"] is None for n in store.get_graph("game")["nodes"])
    lint = store.facet_lint("game")
    assert lint["violations"] == [] and (lint["faceted"], lint["legacy"]) == (0, 3) and lint["facets"] == {}


def test_set_node_facet_labels_without_a_revision(store):
    store.add_nodes("game", [node("d"), node("a", ["d"])])
    with pytest.raises(StateGraphError, match="set_node_facet on `d` first"):
        store.set_node_facet("game", "a", "content")
    assert store.set_node_facet("game", "d", "design") == {"key": "d", "facet": "design", "previous": None, "changed": True}
    assert store.set_node_facet("game", "a", "content")["changed"] is True
    assert store.set_node_facet("game", "a", "content")["changed"] is False
    assert store.get_node("game", "a")["revision"] == 1, "a label is not a contract change"
    assert [e["event_type"] for e in store.events("game")][-2:] == ["node_facet_set", "node_facet_set"]
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
    lint = store.facet_lint("game")
    assert lint["violations"] == [] and (lint["faceted"], lint["legacy"]) == (0, 1)


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


# ── R3: the shipping closure (director's review, 2026-09-09) ────────────────

from core.state_graph import shipping_gaps, facet_warnings


def test_an_integration_node_that_reaches_a_contract_must_also_depend_on_its_implementation():
    # Before facets, v → b → a reached a's implementation. After, b builds on
    # a.contract, so v silently stopped requiring a to be built.
    facets = {"a.contract": "contract", "a": "content", "b.contract": "contract", "b": "content", "v": "integration"}
    graph = {"a.contract": [], "a": ["a.contract"], "b.contract": ["a.contract"], "b": ["b.contract", "a.contract"], "v": ["b"]}
    assert shipping_gaps(facets, graph) == {"v": ["a"]}
    assert "builds on the contracts of `a`" in facet_warnings(facets, graph)[0]
    # Adding the edge closes the gap; it does not serialize a and b.
    assert shipping_gaps(facets, dict(graph, v=["a", "b"])) == {}
    assert facet_violations(facets, dict(graph, v=["a", "b"])) == []


def test_shipping_gap_ignores_a_contract_whose_goal_has_no_implementation_node_yet():
    facets = {"a.contract": "contract", "b": "content", "v": "integration"}
    graph = {"a.contract": [], "b": ["a.contract"], "v": ["b"]}
    assert shipping_gaps(facets, graph) == {}


def test_shipping_gap_is_a_warning_not_a_rejection(store):
    store.add_nodes("game", [node("a.contract", facet="contract"), node("a", ["a.contract"], facet="content"),
                             node("b", ["a.contract"], facet="content"), node("v", ["b"], facet="integration")])
    lint = store.facet_lint("game")
    assert lint["violations"] == [] and lint["shipping_gaps"] == {"v": ["a"]}
    assert len(lint["warnings"]) == 1
    store.revise_node("game", "v", 1, "close the shipping set", dependencies=["a", "b"])
    assert store.facet_lint("game")["shipping_gaps"] == {}


def test_relabel_is_allowed_until_something_was_accepted_under_the_label(store):
    store.add_nodes("game", [node("a.contract", facet="contract"), node("a", ["a.contract"], facet="content"),
                             node("p", ["a"], facet="integration")])
    # A gate migrated as content by mistake: content → integration widens what it may depend on.
    store.add_nodes("game", [node("q", ["a.contract"], facet="content")])
    assert store.set_node_facet("game", "q", "integration") == {"key": "q", "facet": "integration", "previous": "content", "changed": True}
    assert store.get_node("game", "q")["revision"] == 1
    # The other direction is refused when it would make an existing edge illegal.
    with pytest.raises(StateGraphError, match="cannot depend on `a` \\(content\\)"):
        store.set_node_facet("game", "p", "content")
    # And any re-label is refused once the node was accepted as what it was.
    with store.db.get_connection() as conn:
        conn.execute("UPDATE state_nodes SET status='VERIFIED',verified_receipt='r' WHERE node_key='q'")
        conn.commit()
    with pytest.raises(StateConflict, match="was accepted as integration"):
        store.set_node_facet("game", "q", "content")
