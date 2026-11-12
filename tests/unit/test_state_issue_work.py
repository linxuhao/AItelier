"""An issue is fixed through a work item, not a new DAG node.

Before this, dispatching a fix needed a node (todo_plan and the attempt are
keyed by node) and closing an issue as `fixed` needed a node's receipt, so
every fixed issue added a DAG node. `start_issue_work` gives an OPEN issue a
work item: a node row marked with the issue, outside the DAG, run through the
ordinary claim/attempt/evidence/verify path; verifying it resolves the issue.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateConflict
from core.state_service import StateService

CRIT = [{"id": "fixed", "kind": "test", "description": "the reported input is no longer dropped"}]
ARTIFACT = "a" * 64


@pytest.fixture
def svc(tmp_path):
    s = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor="alice@example.test",
                     project_read_trusted=True)
    s.create_project("p", "P")
    s.store.add_nodes("p", [{"key": "a", "goal": "A", "acceptance": CRIT}])
    return s


def _w(svc, action, args):
    return execute(svc, action, args, allow_write=True)


def _report(svc, request_key="r1"):
    return _w(svc, "report_issue", {"project_id": "p", "request_key": request_key, "kind": "defect",
                                    "title": "input dropped", "body": "measured: key press is discarded",
                                    "director_identity": "wuxia"})


def _start(svc, issue):
    return _w(svc, "start_issue_work", {"project_id": "p", "issue_id": issue["issue_id"],
                                        "expected_version": issue["version"], "acceptance": CRIT,
                                        "director_identity": "wuxia"})


def _verify(svc, tmp_path: Path, node_key, external_id="job"):
    """The real path: external attempt -> candidate -> evidence -> verify_node."""
    attempt = svc.start_external_attempt("p", node_key, 1, "harness", external_id, "req-" + external_id)
    terminal = tmp_path / (external_id + "-candidate.json")
    terminal.write_bytes(json.dumps({"attempt": attempt["attempt_id"], "status": "candidate",
                                     "settled": True, "usable": True}, sort_keys=True).encode())
    svc.report_external_attempt(attempt["attempt_id"], "completion-1", attempt["observation_version"],
                                attempt["context_hash"], "candidate", str(terminal),
                                hashlib.sha256(terminal.read_bytes()).hexdigest(),
                                quiescent=True, artifact=ARTIFACT, artifact_kind="sha256")
    body = json.dumps({"status": "completed", "settled": True, "usable": True, "criterion_id": "fixed",
                       "verdict": "pass", "artifact": ARTIFACT}, sort_keys=True).encode()
    report = tmp_path / (external_id + "-fixed.json")
    report.write_bytes(body)
    svc.record_evidence(attempt["attempt_id"], external_id + "-fixed", "fixed", "pass", ARTIFACT,
                        str(report), hashlib.sha256(body).hexdigest(), "verifier")
    return attempt, svc.verify_node("p", node_key, 1, attempt["attempt_id"])


def test_a_work_item_is_outside_the_dag(svc):
    issue = _report(svc)
    started = _start(svc, issue)
    key = started["work_item"]
    assert key == f"issue.{issue['issue_id']}" and started["revision"] == 1
    assert started["status"] == "open" and started["version"] == issue["version"] + 1
    graph = svc.store.get_graph("p")
    assert [n["node_key"] for n in graph["nodes"]] == ["a"]
    assert [w["node_key"] for w in graph["issue_work"]] == [key]
    assert [n["node_key"] for n in svc.store.frontier("p")["nodes"]] == ["a"]
    overview = execute(svc, "project_overview", {"project_id": "p"})
    assert [n["node_key"] for n in overview["nodes"]] == ["a"] and overview["counts"] == {"OPEN": 1}
    assert [w["node_key"] for w in overview["issue_work"]] == [key]
    assert overview["issue_work"][0]["issue_id"] == issue["issue_id"]
    catalog = execute(svc, "project_catalog", {})
    assert {p["project_id"]: p["node_count"] for p in catalog["projects"]}["p"] == 1
    # It is still a readable node: the driver reads its contract to dispatch it.
    node = svc.store.get_node("p", key)
    assert node["issue_id"] == issue["issue_id"] and node["readiness"] == "ready"
    assert node["acceptance"] == CRIT and node["dependencies"] == []
    assert _w(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})["work_item"] == key


def test_verifying_the_work_item_resolves_the_issue_as_fixed(svc, tmp_path):
    issue = _report(svc)
    key = _start(svc, issue)["work_item"]
    attempt, receipt = _verify(svc, tmp_path, key)
    done = _w(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})
    assert done["status"] == "fixed"
    res = done["resolution"]
    assert res["receipt_id"] == receipt["receipt_id"] and res["artifact_ref"] == ARTIFACT
    assert res["node_key"] == key and res["node_revision"] == 1 and res["via"] == "issue_work_item"
    assert svc.store.get_node("p", key)["status"] == "VERIFIED"
    overview = execute(svc, "project_overview", {"project_id": "p"})
    assert overview["issue_work"] == [] and overview["issue_work_counts"] == {"VERIFIED": 1}
    assert overview["counts"] == {"OPEN": 1}
    events = [e["event_type"] for e in svc.store.events("p")]
    assert events[-2:] == ["node_verified", "issue_resolved"]


def test_the_work_item_under_review_does_not_contradict_its_own_issue(svc, tmp_path):
    issue = _report(svc)
    key = _start(svc, issue)["work_item"]
    with svc.store.transaction(write=True) as conn:
        conn.execute("UPDATE state_nodes SET status='CANDIDATE' WHERE project_id='p' AND node_key=?", (key,))
    view = _w(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})
    assert view["contradicts_acceptance"] == []


def test_nothing_can_be_built_on_a_work_item(svc):
    issue = _report(svc)
    key = _start(svc, issue)["work_item"]
    with pytest.raises(StateConflict, match="nothing can depend on it"):
        svc.store.add_nodes("p", [{"key": "b", "goal": "B", "acceptance": CRIT, "dependencies": [key]}])
    with pytest.raises(StateConflict, match="nothing can depend on it"):
        svc.store.revise_node("p", "a", 1, "point at it", dependencies=[key])
    with pytest.raises(StateConflict, match="has no dependencies"):
        svc.store.revise_node("p", key, 1, "give it a dependency", dependencies=["a"])
    with pytest.raises(StateConflict, match="cannot be split"):
        svc.store.split_node("p", key, 1, [{"key": "c", "goal": "C", "acceptance": CRIT}], "split")
    # Its own contract may still be revised (a better acceptance for the fix).
    assert svc.store.revise_node("p", key, 1, "sharper check", acceptance=CRIT + [
        {"id": "regression", "kind": "test", "description": "a test pins it"}])["revision"] == 2


def test_resolve_waits_for_the_live_work_item_and_supersede_restarts(svc):
    issue = _report(svc)
    started = _start(svc, issue)
    key = started["work_item"]
    with pytest.raises(StateConflict, match="already has work item"):
        _start(svc, started)
    with pytest.raises(StateConflict, match="supersede it first"):
        _w(svc, "resolve_issue", {"project_id": "p", "issue_id": issue["issue_id"],
                                  "expected_version": started["version"], "resolution": "rejected",
                                  "reason": "no", "director_identity": "wuxia"})
    svc.store.supersede_node("p", key, 1, "wrong approach")
    again = _start(svc, _w(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]}))
    assert again["work_item"] == key + ".r2"
    svc.store.supersede_node("p", key + ".r2", 1, "drop it")
    fresh = _w(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})
    assert fresh["work_item"] is None
    closed = _w(svc, "resolve_issue", {"project_id": "p", "issue_id": issue["issue_id"],
                                       "expected_version": fresh["version"], "resolution": "rejected",
                                       "reason": "not a defect", "director_identity": "wuxia"})
    assert closed["status"] == "rejected"


def test_only_an_open_issue_at_its_version_gets_a_work_item(svc):
    issue = _report(svc)
    with pytest.raises(StateConflict, match="version conflict"):
        _w(svc, "start_issue_work", {"project_id": "p", "issue_id": issue["issue_id"],
                                     "expected_version": issue["version"] + 1, "acceptance": CRIT,
                                     "director_identity": "wuxia"})
    closed = _w(svc, "resolve_issue", {"project_id": "p", "issue_id": issue["issue_id"],
                                       "expected_version": issue["version"], "resolution": "rejected",
                                       "reason": "no", "director_identity": "wuxia"})
    with pytest.raises(StateConflict, match="already rejected"):
        _start(svc, closed)
    assert svc.store.get_graph("p")["issue_work"] == []


def test_an_issue_fixed_another_way_while_verifying_is_left_alone(svc, tmp_path):
    """The resolve guard makes this unreachable through the API; the verify hook
    still must not overwrite a resolution that is already recorded."""
    issue = _report(svc)
    key = _start(svc, issue)["work_item"]
    with svc.store.transaction(write=True) as conn:
        conn.execute("UPDATE state_issues SET status='rejected' WHERE issue_id=?", (issue["issue_id"],))
    _verify(svc, tmp_path, key)
    assert _w(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})["status"] == "rejected"

