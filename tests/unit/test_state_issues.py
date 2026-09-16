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
    s = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor="alice@example.test")
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
                                "node_keys": [], "reason": "not this node"}, allow_write=True)
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
