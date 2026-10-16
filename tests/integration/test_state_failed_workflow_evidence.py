"""Failed workflow diagnostics through real initialized host/SkillFlow bindings."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from api.state_http import create_state_router
from core.state_graph import StateConflict, StateGraphError
from core.state_service import StateService
from tests.integration.test_state_graph_entrypoints import live, start, finish

ARTIFACT = hashlib.sha256(b"retained failed workflow diagnostics").hexdigest()


def failed(live):
    attempt = start(live)
    result = live.sf.fail_run(attempt["run_id"], "owned deterministic workflow failure")
    assert live.sf.get_run(attempt["run_id"])["status"] == "failed", result
    attempt = live.service.reconcile_attempt(attempt["attempt_id"])
    assert attempt["status"] == "failed" and attempt["artifact_ref"] is None
    assert live.sf.audit_operation_owners(attempt["run_id"])["alive"] == 0
    return attempt


def report(live, attempt, criterion="behaviour", verdict="pass", suffix="", **changes):
    body = {"status": "completed", "settled": True, "usable": True,
            "criterion_id": criterion, "verdict": verdict, "artifact": ARTIFACT}
    body.update(changes)
    data = json.dumps(body, sort_keys=True).encode()
    path = live.tmp / (criterion + suffix + ".json")
    path.write_bytes(data)
    return {"attempt_id": attempt["attempt_id"], "evidence_id": "each-" + criterion + suffix,
            "criterion_id": criterion, "verdict": verdict, "artifact": ARTIFACT,
            "report_ref": str(path), "report_sha256": hashlib.sha256(data).hexdigest()}


def client_for(service):
    def access(request: Request):
        if request.headers.get("Authorization") != "Bearer fixture-owned-token":
            raise HTTPException(401, "fixture writer token required")
    app = FastAPI()
    app.include_router(create_state_router(lambda: service, access))
    return TestClient(app)


HEADERS = {"Authorization": "Bearer fixture-owned-token"}


def test_real_failed_workflow_retains_partial_each_without_promotion(live):
    attempt = failed(live)
    before = live.sf.get_run(attempt["run_id"])
    for criterion, verdict in [("behaviour", "pass"), ("review", "fail")]:
        args = report(live, attempt, criterion, verdict)
        row = live.service.record_evidence(**args)
        assert row["verdict"] == verdict
        assert live.service.record_evidence(**args) == row
    assert live.sf.get_run(attempt["run_id"]) == before
    current = live.service.attempts.get(attempt["attempt_id"])
    assert current["status"] == "failed" and current["artifact_ref"] is None
    assert current["execution_kind"] == "skillflow"
    assert live.service.store.get_node("game", "a")["status"] == "OPEN"
    assert len(live.service.attempts.evidence(attempt["attempt_id"])) == 2
    with pytest.raises(StateConflict, match="completed candidate"):
        live.service.verify_node("game", "a", 1, attempt["attempt_id"])
    with pytest.raises(StateConflict, match="exactly one"):
        live.service.record_evidence(**report(live, attempt, suffix="-conflict"))
    with pytest.raises(StateConflict, match="same artifact"):
        args = report(live, attempt, "review", suffix="-other", artifact="f" * 64)
        args["artifact"] = "f" * 64
        live.service.record_evidence(**args)


def test_failed_all_pass_remains_unverifiable_and_lazy_bound_rest_is_authenticated(live):
    attempt = failed(live)
    runtime_calls = []
    def runtime():
        runtime_calls.append(True)
        return live.sf, live.registry
    service = StateService(live.db, live.ws, runtime_factory=runtime,
                           actor="fixture-rest", project_read_trusted=True)
    with client_for(service) as client:
        args = report(live, attempt)
        assert client.post("/api/state/commands/record_evidence", json=args).status_code == 401
        assert service.attempts.evidence(attempt["attempt_id"]) == []
        for criterion in ["behaviour", "review"]:
            result = client.post("/api/state/commands/record_evidence",
                                 json=report(live, attempt, criterion), headers=HEADERS)
            assert result.status_code == 200, result.text
        verify = client.post("/api/state/commands/verify_node", json={
            "project_id": "game", "node_key": "a", "expected_revision": 1,
            "attempt_id": attempt["attempt_id"]}, headers=HEADERS)
        assert verify.status_code == 409
    assert runtime_calls == [True]
    assert service.attempts.get(attempt["attempt_id"])["artifact_ref"] is None


@pytest.mark.parametrize("case", ["alive", "lost", "unknown"])
def test_actual_unretired_operations_never_allow_diagnostic_rows(live, monkeypatch, case):
    attempt = start(live)
    rid = attempt["run_id"]
    op = None
    if case == "lost":
        code = ("from skillflow.core import SkillFlow; import sys; "
                "sf=SkillFlow(sys.argv[1]); print(sf._admit_op('fixture',sys.argv[2],detail='dead-child'),flush=True)")
        child = subprocess.run([sys.executable, "-c", code, str(live.tmp / "skillflow.sqlite"), rid],
                               capture_output=True, text=True, check=True)
        op = int(child.stdout.strip())
    else:
        op = live.sf._admit_op("fixture", rid, detail="owned-blocked-operation")
        if case == "unknown":
            # Fault at the OS liveness observation only; actual SDK audit and
            # recorded admitted operation still supply the eligibility result.
            monkeypatch.setattr("skillflow.core.owner_is_dead", lambda owner: None)
    try:
        result = live.sf.fail_run(rid, "owned operation has not retired")
        audit = live.sf.audit_operation_owners(rid)
        assert audit[case] if case != "alive" else audit["alive"] == 1
        assert live.sf.get_run(rid)["status"] != "failed", result
        with pytest.raises(StateConflict):
            live.service.record_evidence(**report(live, attempt))
        assert live.service.attempts.evidence(attempt["attempt_id"]) == []
    finally:
        live.sf._retire_op(op)


def test_revived_failed_run_and_unknown_execution_refuse(live, monkeypatch):
    attempt = failed(live)
    live.sf.resume_run(attempt["run_id"])
    assert live.sf.get_run(attempt["run_id"])["status"] != "failed"
    with pytest.raises(StateConflict, match="revived"):
        live.service.record_evidence(**report(live, attempt))
    assert live.service.attempts.evidence(attempt["attempt_id"]) == []
    original = live.sf.get_run
    monkeypatch.setattr(live.sf, "get_run", lambda run: None if run == attempt["run_id"] else original(run))
    with pytest.raises(StateConflict, match="unknown"):
        live.service.record_evidence(**report(live, attempt))


@pytest.mark.parametrize("change", [{"settled": False}, {"status": "candidate"},
                                    {"usable": False}, {"verdict": "fail"},
                                    {"artifact": "f" * 64}, {"criterion_id": "other"}])
def test_failed_workflow_rejects_invalid_scoped_reports(live, change):
    attempt = failed(live)
    args = report(live, attempt)
    path = Path(args["report_ref"])
    body = json.loads(path.read_text())
    body.update(change)
    data = json.dumps(body, sort_keys=True).encode()
    path.write_bytes(data)
    args["report_sha256"] = hashlib.sha256(data).hexdigest()
    with pytest.raises(StateConflict):
        live.service.record_evidence(**args)
    assert live.service.attempts.evidence(attempt["attempt_id"]) == []


def test_failed_workflow_stale_and_newer_attempts_refuse(live):
    attempt = failed(live)
    newer = live.service.attempts.reserve("game", "a", 1, "state_fixture", "newer")
    assert newer["attempt_id"] != attempt["attempt_id"]
    with pytest.raises(StateConflict, match="newer attempt"):
        live.service.record_evidence(**report(live, attempt))
    assert live.service.attempts.evidence(attempt["attempt_id"]) == []


def test_failed_workflow_requires_runtime_and_rejects_incomplete_audit(live, monkeypatch):
    attempt = failed(live)
    args = report(live, attempt)
    with pytest.raises(StateConflict, match="bound execution runtime"):
        live.service.attempts.record_evidence(**args, reviewer="fixture")
    def unavailable(run_id):
        raise OSError("owned audit-observation fault")
    monkeypatch.setattr(live.sf, "audit_operation_owners", unavailable)
    with pytest.raises(StateConflict, match="quiescence"):
        live.service.attempts.record_evidence(**args, reviewer="fixture", sf=live.sf)
    assert live.service.attempts.evidence(attempt["attempt_id"]) == []


def test_failed_workflow_revised_context_and_invalid_row_fields_refuse(live):
    attempt = failed(live)
    for field, value in [("criterion_id", "missing"), ("artifact", "not-a-digest"),
                         ("verdict", "skip"), ("report_sha256", "0" * 64)]:
        args = report(live, attempt)
        args[field] = value
        with pytest.raises((StateConflict, StateGraphError)):
            live.service.record_evidence(**args)
    live.service.store.revise_node("game", "a", 1, "owned changed acceptance context", goal="Revised goal")
    with pytest.raises(StateConflict):
        live.service.record_evidence(**report(live, attempt))
    assert live.service.attempts.evidence(attempt["attempt_id"]) == []
