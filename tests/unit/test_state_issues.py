"""Issues sit beside the State DAG: reporting never moves a node, and every
resolution must name the State record that absorbed it."""
import time

import pytest

from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateConflict, StateGraphError, StateNotFound
from core.state_service import StateService

CRIT = [{"id": "c1", "kind": "test", "description": "works"}]


@pytest.fixture
def svc(tmp_path):
    s = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor="alice@example.test",
                     project_read_trusted=True)
    s.create_project("p", "P")
    s.create_project("q", "Q")
    s.store.add_nodes("p", [{"key": "a", "goal": "A", "acceptance": CRIT}])
    return s


def _report(svc, request_key="r1", **kw):
    args = {"project_id": "p", "request_key": request_key, "kind": "defect", "title": "input dropped",
            "body": "measured: key press during animation is discarded", "director_identity": "wuxia"}
    return execute(svc, "report_issue", args | kw, allow_write=True)


def _node_row(svc, k="a"):
    n = svc.store.get_node("p", k)
    return n["status"], n["revision"], n["readiness"]


def test_report_and_link_never_touch_the_node(svc):
    before = _node_row(svc)
    frontier = svc.store.frontier("p")
    issue = _report(svc, node_keys=["a"])
    assert issue["status"] == "open" and issue["created"] is True
    assert [n["node_key"] for n in issue["nodes"]] == ["a"]
    execute(svc, "link_issue", {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
                                "node_keys": [], "reason": "not this node", "replace_existing": True},
            allow_write=True)
    assert _node_row(svc) == before
    assert svc.store.frontier("p") == frontier
    types = [e["event_type"] for e in svc.store.events("p")]
    assert "issue_reported" in types and "issue_linked" in types and "node_revised" not in types


def test_report_is_idempotent_by_request_key_and_rejects_reuse(svc):
    first = _report(svc)
    again = _report(svc)
    assert again["issue_id"] == first["issue_id"] and again["created"] is False
    with pytest.raises(StateConflict):
        _report(svc, title="something else")


def test_unknown_node_and_unknown_kind_are_refused(svc):
    with pytest.raises(StateNotFound):
        _report(svc, node_keys=["missing"])
    with pytest.raises(StateGraphError):
        _report(svc, kind="bug")


def test_open_defect_on_verified_node_is_flagged(svc, monkeypatch):
    issue = _report(svc, node_keys=["a"])
    assert issue["contradicts_acceptance"] == []
    with svc.store.transaction(write=True) as conn:
        conn.execute("UPDATE state_nodes SET status='VERIFIED' WHERE project_id='p' AND node_key='a'")
    listed = execute(svc, "list_issues", {"project_id": "p", "statuses": ["open"]})
    assert listed["issues"][0]["contradicts_acceptance"] == ["a"]
    assert "body" not in listed["issues"][0]
    ctx = execute(svc, "get_node", {"project_id": "p", "node_key": "a"})
    assert [i["issue_id"] for i in ctx["open_issues"]] == [issue["issue_id"]]
    overview = svc.portfolio.overview("p")
    assert overview["issue_counts"] == {"open": 1}
    assert {n["node_key"]: n["open_issue_count"] for n in overview["nodes"]} == {"a": 1}


def test_absorbed_requires_a_revision_made_after_the_report(svc):
    issue = _report(svc, node_keys=["a"])
    resolve = {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
               "resolution": "absorbed", "reason": "added criterion", "director_identity": "wuxia",
               "node_key": "a", "node_revision": 1}
    # Revision 1 predates the report: naming it would assert a change that was never made.
    with pytest.raises(StateConflict, match="predates"):
        execute(svc, "resolve_issue", resolve, allow_write=True)
    with pytest.raises(StateGraphError):
        execute(svc, "resolve_issue", resolve | {"node_revision": None}, allow_write=True)
    time.sleep(0.001)
    svc.store.revise_node("p", "a", 1, "absorb defect",
                          acceptance=CRIT + [{"id": "c2", "kind": "test", "description": "no drop"}])
    done = execute(svc, "resolve_issue", resolve | {"node_revision": 2}, allow_write=True)
    assert done["status"] == "absorbed" and done["resolution"]["node_revision"] == 2
    with pytest.raises(StateConflict, match="already absorbed"):
        execute(svc, "resolve_issue", resolve | {"expected_version": 2, "node_revision": 2}, allow_write=True)


def test_promoted_requires_a_new_node_and_duplicate_rejected_shapes(svc):
    issue = _report(svc)
    base = {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
            "reason": "r", "director_identity": "d"}
    with pytest.raises(StateConflict):
        execute(svc, "resolve_issue", base | {"resolution": "promoted", "node_key": "a"}, allow_write=True)
    time.sleep(0.001)
    svc.store.add_nodes("p", [{"key": "b", "goal": "B", "acceptance": CRIT}])
    done = execute(svc, "resolve_issue", base | {"resolution": "promoted", "node_key": "b"}, allow_write=True)
    assert done["status"] == "promoted" and [n["node_key"] for n in done["nodes"]] == ["b"]

    other = _report(svc, request_key="r2")
    base2 = base | {"issue_id": other["issue_id"]}
    with pytest.raises(StateGraphError):
        execute(svc, "resolve_issue", base2 | {"resolution": "rejected", "node_key": "a"}, allow_write=True)
    with pytest.raises(StateGraphError):
        execute(svc, "resolve_issue", base2 | {"resolution": "duplicate", "duplicate_of": issue["issue_id"],
                                               "node_key": "a"}, allow_write=True)
    # An existing node that already covered it is a duplicate, with no timing rule.
    dup = execute(svc, "resolve_issue", base2 | {"resolution": "duplicate", "node_key": "a"}, allow_write=True)
    assert dup["status"] == "duplicate"


def test_version_cas_and_project_isolation(svc):
    issue = _report(svc)
    with pytest.raises(StateConflict, match="version"):
        execute(svc, "resolve_issue", {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 9,
                                       "resolution": "rejected", "reason": "r", "director_identity": "d"},
                allow_write=True)
    with pytest.raises(StateNotFound):
        execute(svc, "get_issue", {"project_id": "q", "issue_id": issue["issue_id"]})


def test_list_paginates_in_report_order_and_writes_are_not_on_read_surface(svc):
    ids = [_report(svc, request_key=f"r{i}", title=f"t{i}")["issue_id"] for i in range(5)]
    seen, after = [], 0
    while True:
        page = execute(svc, "list_issues", {"project_id": "p", "after": after, "limit": 2})
        seen += [i["issue_id"] for i in page["issues"]]
        after = page["next_after"]
        if not page["has_more"]:
            break
    assert seen == ids
    with pytest.raises(StateGraphError, match="read surface"):
        execute(svc, "report_issue", {}, allow_write=False)


def test_rest_issue_reads_serve_the_ui(tmp_path):
    from fastapi.testclient import TestClient
    from api.state_only import create_app
    app = create_app(str(tmp_path / "state.sqlite"), "x" * 40)
    auth = {"Authorization": "Bearer " + "x" * 40}
    with TestClient(app) as client:
        assert client.post("/api/state/commands/create_project", json={"project_id": "p", "title": "P"},
                           headers=auth).status_code == 200
        made = client.post("/api/state/commands/report_issue", headers=auth, json={
            "project_id": "p", "request_key": "k", "kind": "gap", "title": "t", "body": "b",
            "director_identity": "d"}).json()
        listed = client.get("/api/state/projects/p/issues?status=open&limit=5", headers=auth)
        assert listed.status_code == 200 and [i["issue_id"] for i in listed.json()["issues"]] == [made["issue_id"]]
        assert client.get("/api/state/projects/p/issues?status=absorbed", headers=auth).json()["issues"] == []
        assert client.get(f"/api/state/projects/p/issues/{made['issue_id']}", headers=auth).json()["body"] == "b"
        assert client.get("/api/state/projects/p/issues/iss-missing", headers=auth).status_code == 404
        assert client.get("/api/state/projects/p/issues", headers={}).status_code == 401


def _acceptance(svc, receipt_id, project_id="p", node_key="a", artifact="a" * 40,
                evidence_ids='["ev-1"]'):
    """Disposable fixture: one attempt row plus one verification receipt.

    Only the resolve path is under test, so the receipt is inserted directly
    (the real verify path needs a launched workflow); the receipt is an
    append-only fact the resolve command must treat as read-only."""
    attempt_id = "att-" + receipt_id
    with svc.store.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
            "dependency_snapshot,context_json,request_key,request_hash,workflow,execution_project_id,"
            "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (attempt_id, "p", node_key, 1, "c" * 64, "{}", "{}", "rk-" + attempt_id, "rh", "wf",
             "exec-" + attempt_id, "candidate", "now", "now"))
        conn.execute(
            "INSERT INTO state_acceptances(receipt_id,project_id,node_key,node_revision,attempt_id,"
            "artifact_ref,contract_hash,dependency_snapshot,evidence_ids,reviewer,created_at,provenance_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, project_id, node_key, 1, attempt_id, artifact, "c" * 64, "{}",
             evidence_ids, "rev", "now", "{}"))


def test_fixed_closes_with_a_receipt_and_its_exact_artifact_without_touching_the_node(svc):
    _acceptance(svc, "receipt-fixed", artifact="a" * 40)
    issue = _report(svc)
    before = _node_row(svc)
    done = execute(svc, "resolve_issue", {
        "project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
        "resolution": "fixed", "reason": "already fixed under an accepted contract",
        "director_identity": "wuxia", "receipt_id": "receipt-fixed", "artifact": "a" * 40},
        allow_write=True)
    assert done["status"] == "fixed"
    assert done["resolution"]["receipt_id"] == "receipt-fixed"
    assert done["resolution"]["artifact_ref"] == "a" * 40
    assert [n["node_key"] for n in done["nodes"]] == ["a"]
    # A fixed closure creates, revises and verifies nothing.
    assert _node_row(svc) == before
    types = [e["event_type"] for e in svc.store.events("p")]
    assert "issue_resolved" in types and "node_verified" not in types and "node_revised" not in types


def test_fixed_refusals_leave_the_issue_untouched(svc):
    _acceptance(svc, "receipt-other", project_id="q", artifact="b" * 40)
    _acceptance(svc, "receipt-same", artifact="e" * 40)
    _acceptance(svc, "receipt-empty", artifact="c" * 40, evidence_ids="[]")
    issue = _report(svc, node_keys=["a"])
    base = {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
            "resolution": "fixed", "reason": "r", "director_identity": "wuxia"}
    with pytest.raises(StateNotFound):
        execute(svc, "resolve_issue", base | {"receipt_id": "receipt-missing", "artifact": "a" * 40},
                allow_write=True)
    with pytest.raises(StateConflict, match="different project"):
        execute(svc, "resolve_issue", base | {"receipt_id": "receipt-other", "artifact": "b" * 40},
                allow_write=True)
    # A same-project receipt whose declared artifact differs is refused on the artifact.
    with pytest.raises(StateConflict, match="exact artifact"):
        execute(svc, "resolve_issue", base | {"receipt_id": "receipt-same", "artifact": "d" * 40},
                allow_write=True)
    with pytest.raises(StateConflict, match="no resolved evidence"):
        execute(svc, "resolve_issue", base | {"receipt_id": "receipt-empty", "artifact": "c" * 40},
                allow_write=True)
    with pytest.raises(StateGraphError):
        execute(svc, "resolve_issue", base | {"receipt_id": "receipt-other"}, allow_write=True)
    # Every refusal rolled back: still open at version 1 with no resolution event.
    fresh = execute(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})
    assert fresh["status"] == "open" and fresh["version"] == 1 and fresh["resolution"] is None
    assert "issue_resolved" not in [e["event_type"] for e in svc.store.events("p")]


def test_legacy_resolutions_reject_receipt_fields_and_refuse_without_mutation(svc):
    """Regression: duplicate once accepted a sole receipt_id or artifact and
    mutated the issue; a legacy resolution naming fixed-only provenance must be
    refused before anything is read, in both actual mutating shapes."""
    _acceptance(svc, "receipt-legacy", artifact="f" * 40)
    issue = _report(svc)
    base = {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
            "resolution": "duplicate", "reason": "r", "director_identity": "d"}
    with pytest.raises(StateGraphError, match="only accepted for the fixed resolution"):
        execute(svc, "resolve_issue", base | {"receipt_id": "receipt-legacy"}, allow_write=True)
    with pytest.raises(StateGraphError, match="only accepted for the fixed resolution"):
        execute(svc, "resolve_issue", base | {"artifact": "f" * 40}, allow_write=True)
    with pytest.raises(StateGraphError, match="only accepted for the fixed resolution"):
        execute(svc, "resolve_issue", base | {"duplicate_of": issue["issue_id"],
                                              "receipt_id": "receipt-legacy",
                                              "artifact": "f" * 40}, allow_write=True)
    # Other legacy resolutions are equally strict.
    with pytest.raises(StateGraphError, match="only accepted for the fixed resolution"):
        execute(svc, "resolve_issue", base | {"resolution": "rejected",
                                              "receipt_id": "receipt-legacy"}, allow_write=True)
    # Every refusal rolled back: still open at version 1, no resolution stored.
    fresh = execute(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})
    assert fresh["status"] == "open" and fresh["version"] == 1 and fresh["resolution"] is None
    assert "issue_resolved" not in [e["event_type"] for e in svc.store.events("p")]
    counts = execute(svc, "list_issues", {"project_id": "p"})["status_counts"]
    assert counts == {"open": 1}


def test_fixed_is_a_distinct_status_and_the_four_existing_semantics_still_resolve(svc):
    # The read filter and typed command expose fixed beside the four originals.
    execute(svc, "list_issues", {"project_id": "p", "statuses": ["fixed"]})
    with pytest.raises(StateGraphError):
        execute(svc, "list_issues", {"project_id": "p", "statuses": ["closed"]})
    # rejected is still reason-only and still terminal.
    issue = _report(svc)
    done = execute(svc, "resolve_issue", {"project_id": "p", "issue_id": issue["issue_id"],
                                           "expected_version": 1, "resolution": "rejected",
                                           "reason": "not a defect", "director_identity": "wuxia"},
                    allow_write=True)
    assert done["status"] == "rejected"


def _nodes(svc, *keys, project_id="p"):
    svc.store.add_nodes(project_id, [{"key": k, "goal": k.upper(), "acceptance": CRIT} for k in keys])


def _link(svc, issue_id, version, node_keys, **kw):
    args = {"project_id": "p", "issue_id": issue_id, "expected_version": version,
            "node_keys": node_keys, "reason": "provenance"}
    return execute(svc, "link_issue", args | kw, allow_write=True)


def _keys(view):
    return [n["node_key"] for n in view["nodes"]]


def _fresh(svc, issue_id):
    return execute(svc, "get_issue", {"project_id": "p", "issue_id": issue_id})


def test_link_defaults_to_adding_and_preserves_existing_links(svc):
    _nodes(svc, "b", "c", "d", "e", "f", "g")
    issue = _report(svc, node_keys=["a", "b", "c", "d", "e"])
    before = {k: _node_row(svc, k) for k in "abcdefg"}
    frontier = svc.store.frontier("p")
    added = _link(svc, issue["issue_id"], 1, ["f"])
    assert _keys(added) == ["a", "b", "c", "d", "e", "f"]
    assert added["effective_node_keys"] == ["a", "b", "c", "d", "e", "f"]
    assert added["added_node_keys"] == ["f"] and added["removed_node_keys"] == []
    assert added["replace_existing"] is False and added["version"] == 2
    # Repeating the same incremental request (current version) is idempotent on the set.
    again = _link(svc, issue["issue_id"], 2, ["f"])
    assert _keys(again) == ["a", "b", "c", "d", "e", "f"] and again["added_node_keys"] == []
    assert again["version"] == 3
    # Readback agrees with the response.
    assert _keys(_fresh(svc, issue["issue_id"])) == ["a", "b", "c", "d", "e", "f"]
    # Nodes and frontier are untouched.
    assert {k: _node_row(svc, k) for k in "abcdefg"} == before
    assert svc.store.frontier("p") == frontier
    # The audit event records the effective set and the (default) intent.
    event = [e for e in svc.store.events("p") if e["event_type"] == "issue_linked"][0]
    assert event["payload"]["node_keys"] == ["a", "b", "c", "d", "e", "f"]
    assert event["payload"]["replace_existing"] is False
    assert event["payload"]["added_node_keys"] == ["f"] and event["payload"]["removed_node_keys"] == []


def test_empty_default_link_preserves_all_links_but_still_bumps_version(svc):
    _nodes(svc, "b")
    issue = _report(svc, node_keys=["a", "b"])
    kept = _link(svc, issue["issue_id"], 1, [])
    assert _keys(kept) == ["a", "b"] and kept["version"] == 2
    assert _keys(_fresh(svc, issue["issue_id"])) == ["a", "b"]
    explicit_false = _link(svc, issue["issue_id"], 2, [], replace_existing=False)
    assert _keys(explicit_false) == ["a", "b"]


def test_explicit_replacement_and_empty_replacement_remove_links(svc):
    _nodes(svc, "b", "c")
    issue = _report(svc, node_keys=["a", "b"])
    replaced = _link(svc, issue["issue_id"], 1, ["c"], replace_existing=True)
    assert _keys(replaced) == ["c"] and replaced["effective_node_keys"] == ["c"]
    assert replaced["removed_node_keys"] == ["a", "b"] and replaced["added_node_keys"] == ["c"]
    assert replaced["replace_existing"] is True and replaced["version"] == 2
    assert _keys(_fresh(svc, issue["issue_id"])) == ["c"]
    cleared = _link(svc, issue["issue_id"], 2, [], replace_existing=True)
    assert _keys(cleared) == [] and cleared["effective_node_keys"] == []
    assert cleared["removed_node_keys"] == ["c"] and cleared["version"] == 3
    assert _keys(_fresh(svc, issue["issue_id"])) == []
    event = [e for e in svc.store.events("p") if e["event_type"] == "issue_linked"][-1]
    assert event["payload"]["replace_existing"] is True and event["payload"]["node_keys"] == []
    assert event["payload"]["removed_node_keys"] == ["c"]


def test_stale_concurrent_writer_conflicts_and_retry_keeps_both_additions(svc):
    _nodes(svc, "b", "c")
    issue = _report(svc, node_keys=["a"])
    # Two writers read version 1; the first lands, the second must not overwrite blindly.
    _link(svc, issue["issue_id"], 1, ["b"])
    with pytest.raises(StateConflict, match="version conflict"):
        _link(svc, issue["issue_id"], 1, ["c"])
    with pytest.raises(StateConflict, match="version conflict"):
        _link(svc, issue["issue_id"], 1, [], replace_existing=True)
    stale = _fresh(svc, issue["issue_id"])
    assert _keys(stale) == ["a", "b"] and stale["version"] == 2
    retried = _link(svc, issue["issue_id"], stale["version"], ["c"])
    assert _keys(retried) == ["a", "b", "c"] and retried["version"] == 3


def test_invalid_link_input_rolls_back_without_partial_changes(svc):
    _nodes(svc, "b")
    _nodes(svc, "foreign", project_id="q")
    issue = _report(svc, node_keys=["a", "b"])
    events = len(svc.store.events("p"))
    for mode in (False, True):
        with pytest.raises(StateNotFound):
            _link(svc, issue["issue_id"], 1, ["missing"], replace_existing=mode)
        with pytest.raises(StateNotFound):  # cross-project node
            _link(svc, issue["issue_id"], 1, ["foreign"], replace_existing=mode)
        with pytest.raises(StateNotFound):  # one valid + one unknown is refused whole
            _link(svc, issue["issue_id"], 1, ["b", "missing"], replace_existing=mode)
    for bad in ("true", "false", 1, 0, None, "yes"):
        with pytest.raises(StateGraphError):
            _link(svc, issue["issue_id"], 1, [], replace_existing=bad)
    with pytest.raises(StateGraphError):
        svc.issues.link("p", issue["issue_id"], 1, [], "provenance", replace_existing="true")
    fresh = _fresh(svc, issue["issue_id"])
    assert _keys(fresh) == ["a", "b"] and fresh["version"] == 1
    assert len(svc.store.events("p")) == events


def test_link_max_effective_links_is_enforced_in_both_modes(svc):
    names = [f"n{i:02d}" for i in range(21)]
    _nodes(svc, *names)
    issue = _report(svc, node_keys=names[:20])
    with pytest.raises(StateGraphError, match="at most 20"):
        _link(svc, issue["issue_id"], 1, [names[20]])
    fresh = _fresh(svc, issue["issue_id"])
    assert _keys(fresh) == names[:20] and fresh["version"] == 1
    with pytest.raises(StateGraphError):  # the request itself may not exceed 20 either
        _link(svc, issue["issue_id"], 1, names, replace_existing=True)
    # Re-adding an already linked node keeps the effective set at 20 and is allowed.
    assert len(_keys(_link(svc, issue["issue_id"], 1, [names[0]]))) == 20
    # A deliberate replacement may swap a node in.
    swapped = _link(svc, issue["issue_id"], 2, [names[20]], replace_existing=True)
    assert _keys(swapped) == [names[20]]


def test_link_on_a_closed_issue_is_refused_in_both_modes_without_change(svc):
    issue = _report(svc, node_keys=["a"])
    execute(svc, "resolve_issue", {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
                                   "resolution": "rejected", "reason": "no", "director_identity": "wuxia"},
            allow_write=True)
    for mode in (False, True):
        with pytest.raises(StateConflict, match="already rejected"):
            _link(svc, issue["issue_id"], 2, [], replace_existing=mode)
    fresh = _fresh(svc, issue["issue_id"])
    assert _keys(fresh) == ["a"] and fresh["version"] == 2
