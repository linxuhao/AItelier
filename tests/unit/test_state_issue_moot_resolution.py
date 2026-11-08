"""`moot` closes an observation whose SUBJECT OBJECT no longer exists.

Four open issues sit in exactly that state: the directory, commit or report dir
the report was about is measurably gone, so no node can absorb it, no shipped
artifact carries a fix, and `rejected` would throw the observation away. Every
existing resolution would have to lie. `moot` names the artifact at which the
absence was measured, keeps the reported body verbatim, and closes.
"""
import pytest

from core import state_issues
from core.state_commands import describe, execute
from core.state_database import StateDatabase
from core.state_driver_guide import STATE_DRIVER_GUIDE
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService

CRIT = [{"id": "c1", "kind": "test", "description": "works"}]
SUBJECT = ("measured 2026-11-08: the report dir ~/.AItelier/reports/att-9f2 is gone; "
           "`ls -d` exits 2, so the report cannot be re-read or absorbed")
ABSENT_AT = "a" * 40
DEFINITION = ("the subject object was measurably absent at the named artifact; the observation "
              "is retained verbatim and cannot be re-measured")


@pytest.fixture
def svc(tmp_path):
    s = StateService(StateDatabase(str(tmp_path / "state.sqlite")), actor="alice@example.test",
                     project_read_trusted=True)
    s.create_project("p", "P")
    s.create_project("q", "Q")
    s.store.add_nodes("p", [{"key": "a", "goal": "A", "acceptance": CRIT}])
    return s


def _report(svc, request_key="r1", **kw):
    args = {"project_id": "p", "request_key": request_key, "kind": "defect",
            "title": "last attempt report dir missing", "body": SUBJECT,
            "director_identity": "wuxia"}
    return execute(svc, "report_issue", args | kw, allow_write=True)


def _acceptance(svc, receipt_id, project_id="p", node_key="a", artifact="a" * 40,
                evidence_ids='["ev-1"]'):
    """One attempt row plus one verification receipt, inserted directly: only the
    resolve path is under test and the receipt is a read-only fact to it."""
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


# (a) the closure itself -----------------------------------------------------
def test_moot_closes_an_observation_whose_subject_is_measurably_gone(svc):
    issue = _report(svc)
    before = execute(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})
    assert before["status"] == "open" and before["resolution"] is None
    body_bytes = before["body"].encode()
    assert body_bytes == SUBJECT.encode()

    resolve = {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
               "resolution": "moot", "director_identity": "wuxia", "artifact": ABSENT_AT,
               "reason": "subject gone before it could be absorbed or shipped; measured absent 2026-11-08"}
    resolved = execute(svc, "resolve_issue", resolve, allow_write=True)
    assert resolved["status"] == "moot"
    assert resolved["resolution"]["resolution"] == "moot"
    assert resolved["resolution"]["artifact_ref"] == ABSENT_AT

    after = execute(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})
    assert after["status"] == "moot"                       # closed
    assert after["version"] == 2
    assert after["resolution"]["artifact_ref"] == ABSENT_AT
    assert after["resolution"]["reason"].startswith("subject gone")
    # The body is the only surviving record of what was observed: byte-identical.
    assert after["body"].encode() == body_bytes
    with svc.store.transaction() as conn:
        row = conn.execute("SELECT body,status,title,kind FROM state_issues WHERE issue_id=?",
                           (issue["issue_id"],)).fetchone()
    assert row["body"].encode() == body_bytes and row["status"] == "moot"
    assert row["title"] == "last attempt report dir missing" and row["kind"] == "defect"

    # Terminal, like every resolution: the fix is a new report, not a reopening.
    with pytest.raises(StateConflict, match="already moot"):
        execute(svc, "resolve_issue", resolve | {"expected_version": 2}, allow_write=True)

    # The status is readable: the filter and the counts see it.
    listed = execute(svc, "list_issues", {"project_id": "p", "statuses": ["moot"]})
    assert [i["issue_id"] for i in listed["issues"]] == [issue["issue_id"]]
    assert listed["status_counts"] == {"moot": 1}


# (b) refusals, quoted -------------------------------------------------------
def test_moot_refusals_are_quoted_exactly(svc):
    issue = _report(svc)
    base = {"project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
            "resolution": "moot", "reason": "subject gone", "director_identity": "wuxia"}
    cases = [
        # No artifact: a moot closure nobody can re-check. The message says WHAT
        # the artifact is, not only which field the shape expects.
        base,
        base | {"artifact": ABSENT_AT, "node_key": "a"},
        base | {"artifact": ABSENT_AT, "duplicate_of": issue["issue_id"]},
        base | {"artifact": ABSENT_AT, "receipt_id": "receipt-x"},
    ]
    messages = []
    for args in cases:
        with pytest.raises(StateGraphError) as excinfo:
            execute(svc, "resolve_issue", args, allow_write=True)
        messages.append(str(excinfo.value))
    assert messages == [
        "moot requires artifact: the exact artifact or HEAD at which the subject object was "
        "measured absent; moot takes exactly: artifact",
        "moot takes exactly: artifact",
        "moot takes exactly: artifact",
        "receipt_id is only accepted for the fixed resolution",
    ]
    # Every refusal rolled back: still open at version 1, nothing linked, no event.
    fresh = execute(svc, "get_issue", {"project_id": "p", "issue_id": issue["issue_id"]})
    assert fresh["status"] == "open" and fresh["version"] == 1 and fresh["resolution"] is None
    assert fresh["nodes"] == []
    assert "issue_resolved" not in [e["event_type"] for e in svc.store.events("p")]


# (c) the six existing resolutions are untouched -----------------------------
def test_the_six_existing_resolutions_keep_their_shapes(svc):
    _acceptance(svc, "receipt-c", artifact="c" * 40)
    base = {"project_id": "p", "reason": "r", "director_identity": "wuxia"}

    rejected_issue = _report(svc, request_key="r-rejected")
    done = execute(svc, "resolve_issue", base | {"issue_id": rejected_issue["issue_id"],
                                                 "expected_version": 1, "resolution": "rejected"},
                   allow_write=True)
    assert done["status"] == "rejected" and "artifact_ref" not in done["resolution"]
    # A legacy resolution given an artifact is still refused, and the message now
    # names every resolution that takes one.
    with pytest.raises(StateGraphError) as excinfo:
        execute(svc, "resolve_issue", base | {"issue_id": rejected_issue["issue_id"],
                                              "expected_version": 1, "resolution": "rejected",
                                              "artifact": ABSENT_AT}, allow_write=True)
    assert str(excinfo.value) == "artifact is only accepted for the fixed, observed and moot resolutions"

    fixed_issue = _report(svc, request_key="r-fixed")
    # fixed still requires BOTH: an artifact alone is not a receipt.
    with pytest.raises(StateGraphError) as excinfo:
        execute(svc, "resolve_issue", base | {"issue_id": fixed_issue["issue_id"],
                                              "expected_version": 1, "resolution": "fixed",
                                              "artifact": "c" * 40}, allow_write=True)
    assert str(excinfo.value) == "fixed takes exactly: artifact, receipt_id"
    fixed = execute(svc, "resolve_issue", base | {"issue_id": fixed_issue["issue_id"],
                                                  "expected_version": 1, "resolution": "fixed",
                                                  "receipt_id": "receipt-c", "artifact": "c" * 40},
                    allow_write=True)
    assert fixed["status"] == "fixed"
    assert fixed["resolution"]["receipt_id"] == "receipt-c"
    assert fixed["resolution"]["artifact_ref"] == "c" * 40

    # observed still takes the artifact alone, and still refuses a receipt.
    observed_issue = _report(svc, request_key="r-observed")
    observed = execute(svc, "resolve_issue", base | {"issue_id": observed_issue["issue_id"],
                                                     "expected_version": 1, "resolution": "observed",
                                                     "artifact": ABSENT_AT}, allow_write=True)
    assert observed["status"] == "observed" and observed["resolution"]["artifact_ref"] == ABSENT_AT
    with pytest.raises(StateGraphError, match="receipt_id is only accepted for the fixed resolution"):
        execute(svc, "resolve_issue", base | {"issue_id": observed_issue["issue_id"],
                                              "expected_version": 1, "resolution": "observed",
                                              "artifact": ABSENT_AT, "receipt_id": "receipt-c"},
                allow_write=True)


# (d) served help ------------------------------------------------------------
def test_the_served_help_names_moot_and_keeps_the_six(svc):
    """Read the way a caller does: state_graph_help publishes ``describe()``."""
    schema = describe()["operations"]["resolve_issue"]["arguments"]
    resolution = schema["properties"]["resolution"]
    assert resolution["enum"] == ["absorbed", "promoted", "duplicate", "fixed", "observed",
                                 "rejected", "moot"]
    assert f"moot: artifact only - {DEFINITION}" in resolution["description"]
    # The six existing descriptions are intact beside it.
    for value, opening in (("absorbed", "absorbed: node_key+node_revision"),
                          ("promoted", "promoted: node_key of a node"),
                          ("duplicate", "duplicate: exactly one of"),
                          ("fixed", "fixed: receipt_id+artifact"),
                          ("observed", "observed: artifact only"),
                          ("rejected", "rejected: reason only")):
        assert f"{value}: " in resolution["description"]
        assert opening in resolution["description"]
    # The artifact field documents all three resolutions that take one.
    artifact_help = schema["properties"]["artifact"]["description"]
    assert "fixed:" in artifact_help and "observed:" in artifact_help and "moot:" in artifact_help
    assert "measured absent" in artifact_help
    # The guide a driver actually reads defines it too.
    assert f"- moot: artifact only - {DEFINITION}" in STATE_DRIVER_GUIDE
    for value in ("absorbed", "promoted", "duplicate", "fixed", "observed", "rejected"):
        assert f"- {value}: " in STATE_DRIVER_GUIDE


# the status CHECK is widened, not only defined for new databases ------------
def test_an_existing_database_is_widened_to_accept_the_moot_status(tmp_path):
    """A live deployment already carries `observed` and predates `moot`; its
    status CHECK has to be rebuilt, or closing a moot issue fails on the CHECK
    while every other resolution keeps working."""
    db = StateDatabase(str(tmp_path / "old.sqlite"))
    with db.get_connection() as conn:
        conn.executescript(state_issues.SCHEMA.replace(",'moot'", ""))
        conn.commit()
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='state_issues'").fetchone()["sql"]
    assert "'moot'" not in sql and "'observed'" in sql      # really the old shape

    svc = StateService(db, actor="alice@example.test", project_read_trusted=True)
    svc.create_project("p", "P")
    with db.get_connection() as conn:
        widened = conn.execute("SELECT sql FROM sqlite_master WHERE name='state_issues'").fetchone()["sql"]
    assert "'moot'" in widened

    issue = _report(svc)
    done = execute(svc, "resolve_issue", {
        "project_id": "p", "issue_id": issue["issue_id"], "expected_version": 1,
        "resolution": "moot", "reason": "subject gone", "director_identity": "wuxia",
        "artifact": ABSENT_AT}, allow_write=True)
    assert done["status"] == "moot" and done["body"] == SUBJECT
