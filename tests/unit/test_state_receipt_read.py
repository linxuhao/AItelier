"""A published acceptance is identified by its RECEIPT id, so that id must read.

The gap this pins: every existing read of an acceptance starts at the ATTEMPT
(`evidence`, `attempt_detail`, `get_attempt`), and the node that held it loses
the reference the moment it moves on — `_invalidate` (called by every revise /
split / supersede) sets `verified_receipt=NULL`, so `get_node` on the CURRENT
node cannot recover a real acceptance that was issued earlier. A transport
holding only the receipt it was given (the public proof a reader is handed) had
no supported entry back to the facts behind it. State already stored them:
`state_acceptances` is append-only and immutable, and `state_evidence` is what
the receipt's own `evidence_ids` point at.

So this adds ONE read action to the existing surface — `get_receipt`, a
`project_id` + `receipt_id` READ_REQUESTS entry dispatched like every neighbour
— and no new framework, table or token. It is PUBLIC in the one visibility
table, for exactly the reason `get_attempt`/`evidence` are: it reports the facts
of a project that has been opened. Project privacy is the same predicate every
other state read uses (`store.is_project_public`, applied for an anonymous
caller at `execute` and again in the reader), so a private project's acceptance
stays unreachable to a caller who merely knows a receipt id, and the refusal is
byte-identical to a project that does not exist.

Most tests here go through the PRODUCT HTTP surface (`api.main.app`, with the
write gate armed and the State database overridden) rather than calling the
reader: a helper that only exercises the function would not prove the action is
reachable, classified and guarded.

Two of them go BELOW that surface on purpose, one layer at a time, because the
project-privacy verdict is applied twice (once at the `execute` chokepoint in
`core.state_commands`, once inside `StateService.get_receipt`) and a request that
travels the whole route only ever proves the pair: either refusal alone leaves
it refused. So each half is probed on its own, on a path the other half does not
run on: one calls the reader directly with an anonymous service (the reader is
the only gate there), one calls `execute` with a spy on the reader (the
chokepoint is the only gate there). Either predicate can be deleted with the
whole shipping suite still green, so each needs its own test.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import api.main as host
from api import authz
from api.dependencies import get_db_manager, get_workspace_manager
from core.state_commands import ProjectPrivate, execute
from core.state_database import StateDatabase
from core.state_service import StateService
from core.workspace_manager import WorkspaceManager

ADMIN = {"X-AItelier-Admin-Token": "off-tunnel-admin"}
ARTIFACT = "a" * 64
CRITERIA = ("behaviour", "review")


def spec(key="goal"):
    return {"key": key, "goal": "Deliver " + key, "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Required behaviour passes"},
        {"id": "review", "kind": "review", "description": "A subagent independently reviews it"}]}


def report_file(path: Path, artifact: str, criterion: str, verdict: str = "pass"):
    """A complete structured report the existing evidence path accepts."""
    body = json.dumps({"status": "completed", "settled": True, "usable": True,
                       "criterion_id": criterion, "verdict": verdict,
                       "artifact": artifact}, sort_keys=True).encode()
    path.write_bytes(body)
    return str(path), hashlib.sha256(body).hexdigest()


def accept(service, reports: Path, project_id: str, node_key="goal", artifact=ARTIFACT,
           external_id="job", prior=()):
    """One REAL acceptance, through the existing writes: an external attempt
    observed as a candidate, evidence for every criterion, then acceptance.

    External execution on purpose — that is the shape a harness publishes a
    proof from, and it needs no SkillFlow run to exist here.

    ``prior`` — ``(suffix, criterion_id, verdict)`` rows recorded BEFORE the
    passing ones below, so the acceptance supersedes them and the receipt pins
    only the latest row per required criterion. The attempt then holds more
    evidence rows than its receipt attests, which is the shape
    `test_a_receipt_returns_only_the_evidence_its_own_ids_pin` needs. They land
    before ``verify_node``: recording evidence after an acceptance invalidates
    it, which is the product's rule, not a shortcut here."""
    reports.mkdir(parents=True, exist_ok=True)
    service.create_project(project_id, "Project " + project_id)
    service.store.add_nodes(project_id, [spec(node_key)])
    attempt = service.start_external_attempt(project_id, node_key, 1, "my-harness",
                                            external_id, "req-" + external_id)
    terminal = reports / (external_id + "-candidate.json")
    terminal.write_bytes(json.dumps({"attempt": attempt["attempt_id"], "status": "candidate",
                                     "settled": True, "usable": True},
                                    sort_keys=True).encode())
    service.report_external_attempt(
        attempt["attempt_id"], "completion-1", attempt["observation_version"],
        attempt["context_hash"], "candidate", str(terminal),
        hashlib.sha256(terminal.read_bytes()).hexdigest(),
        quiescent=True, artifact=artifact, artifact_kind="sha256")

    def record(suffix, criterion, verdict):
        path, sha = report_file(reports / (external_id + "-" + suffix + ".json"),
                                artifact, criterion, verdict)
        service.record_evidence(attempt["attempt_id"], external_id + "-" + suffix,
                               criterion, verdict, artifact, path, sha, "verifier")

    for suffix, criterion, verdict in prior:
        record(suffix, criterion, verdict)
    for criterion in CRITERIA:
        record(criterion, criterion, "pass")
    return service.verify_node(project_id, node_key, 1, attempt["attempt_id"])


@pytest.fixture
def http(tmp_path, monkeypatch):
    """The product app's HTTP surface over an isolated State database.

    The gate is ARMED, so a client with no credential is a genuinely anonymous
    visitor and the admin token is the writer — the same two poles the product
    deployment has. The lifespan is deliberately not entered (no scheduler, no
    claim recovery): the HTTP entry is the thing under test, not the backend.
    """
    state_db = StateDatabase(str(tmp_path / "state.sqlite"))
    ws = WorkspaceManager(str(tmp_path / "ws"))
    monkeypatch.setattr(authz, "gate_enabled", lambda: True)
    monkeypatch.setattr(authz, "WRITERS", set())
    monkeypatch.setattr(authz, "ADMIN_TOKEN", "off-tunnel-admin")
    monkeypatch.setattr(authz.cf_access, "email_from_request_headers", lambda *a, **k: None)
    # TestClient's client host is not 127.0.0.1; this is the deployment's own
    # "fronted by a tunnel" switch, which is how the product app runs.
    monkeypatch.setattr(host, "_ALLOW_EXTERNAL", True)
    host.app.dependency_overrides[get_db_manager] = lambda: state_db
    host.app.dependency_overrides[get_workspace_manager] = lambda: ws
    service = StateService(state_db, ws, actor="owner@example", project_read_trusted=True)
    client = TestClient(host.app)
    try:
        yield SimpleNamespace(client=client, service=service, reports=tmp_path / "reports",
                              state_db=state_db, ws=ws)
    finally:
        client.close()
        host.app.dependency_overrides.pop(get_db_manager, None)
        host.app.dependency_overrides.pop(get_workspace_manager, None)


def anonymous_reader(http):
    """A service built the way `api.authz` builds one for a visitor with no
    credential: the SAME State database, an explicit untrusted level. That level
    is not a flag a caller can flip on the object afterwards — `StateService`
    takes it once, and an untrusted service holds an `UntrustedDatabase` whose
    every connection is armed (so the rows it may read are the public ones)."""
    return StateService(http.state_db, http.ws, actor="visitor",
                        project_read_trusted=False)


def read_receipt(client, project_id, receipt_id, headers=None):
    return client.post("/api/state/query/get_receipt",
                       json={"project_id": project_id, "receipt_id": receipt_id},
                       headers=headers or {})


def test_an_opened_project_receipt_is_readable_with_the_evidence_it_rests_on(http):
    receipt = accept(http.service, http.reports, "published")
    opened = http.client.post("/api/state/projects/published/open", headers=ADMIN)
    assert opened.status_code == 200 and opened.json()["visibility"] == "public"

    # The scenario this read exists for: the node moves on, its verified_receipt
    # is CLEARED, and the current node can no longer name the real acceptance.
    http.service.revise_node("published", "goal", 1, "next round of work")
    node = http.service.store.get_node("published", "goal")
    assert node["revision"] == 2 and node["verified_receipt"] is None

    response = read_receipt(http.client, "published", receipt["receipt_id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["receipt_id"] == receipt["receipt_id"]
    assert body["project_id"] == "published"
    assert body["node_key"] == "goal"
    assert body["node_revision"] == 1
    assert body["artifact_ref"] == ARTIFACT
    assert body["reviewer"] == "owner@example"
    assert body["created_at"] == receipt["created_at"]
    assert [row["criterion_id"] for row in body["evidence"]] == list(CRITERIA)
    for row in body["evidence"]:
        assert row["verdict"] == "pass"
        assert row["artifact_ref"] == ARTIFACT
        assert len(row["report_sha256"]) == 64
    assert [row["evidence_id"] for row in body["evidence"]] == \
        json.loads(receipt["evidence_ids"])
    # Identifiers and verdicts only — never the report body.
    assert "detail" not in json.dumps(body)


def test_the_same_call_is_refused_for_an_anonymous_reader_of_a_project_nobody_opened(http):
    receipt = accept(http.service, http.reports, "closed")

    response = read_receipt(http.client, "closed", receipt["receipt_id"])
    assert response.status_code == 403, response.text
    # The ONE refusal every unopened project (and every nonexistent one) gets:
    # no receipt id, artifact, node key or reviewer leaks into it.
    assert "not available" in response.json()["detail"]
    for secret in (receipt["receipt_id"], ARTIFACT, "goal", "verifier"):
        assert secret not in response.text

    # The SAME call by a WRITER succeeds — so the refusal is the privacy rule,
    # not a broken action.
    allowed = read_receipt(http.client, "closed", receipt["receipt_id"], headers=ADMIN)
    assert allowed.status_code == 200 and allowed.json()["artifact_ref"] == ARTIFACT


def test_a_nonexistent_receipt_id_is_refused_by_name(http):
    accept(http.service, http.reports, "published")
    assert http.client.post("/api/state/projects/published/open", headers=ADMIN).status_code == 200

    response = read_receipt(http.client, "published", "receipt-nobody-issued")
    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert "receipt-nobody-issued" in detail and "not found" in detail
    assert response.json() != {}


def test_a_receipt_id_from_another_project_is_refused_by_name(http):
    accept(http.service, http.reports, "first", external_id="one")
    foreign = accept(http.service, http.reports, "second", external_id="two")
    for project_id in ("first", "second"):
        assert http.client.post(f"/api/state/projects/{project_id}/open",
                                headers=ADMIN).status_code == 200

    response = read_receipt(http.client, "first", foreign["receipt_id"])
    # Both projects are OPEN, so the only thing that can refuse this is the
    # receipt's own project scope — never the project-privacy verdict.
    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert foreign["receipt_id"] in detail and "not found" in detail
    # None of the other project's facts come back with the refusal.
    for leaked in (foreign["artifact_ref"], foreign["receipt_id"], "goal"):
        assert leaked not in json.dumps(response.json().get("receipt", {}))
    assert "receipt" not in response.json()

    # Indistinguishable from a receipt that never existed: the scoped refusal is
    # not an oracle for another project's receipts.
    absent = read_receipt(http.client, "first", "receipt-nobody-issued")
    assert absent.status_code == response.status_code == 404
    assert absent.json()["detail"].replace("receipt-nobody-issued", "") == \
        response.json()["detail"].replace(foreign["receipt_id"], "")


def test_a_receipt_returns_only_the_evidence_its_own_ids_pin(http):
    """The receipt attests a SUBSET of the attempt's evidence, and the read must
    return that subset — not "everything recorded against the attempt".

    `verify` pins the latest row per REQUIRED criterion, so an attempt can hold
    more: verdicts the acceptance superseded. Returning the attempt's rows
    instead of the receipt's own `evidence_ids` would hand a reader a verdict
    that was replaced (here a FAIL, and a SKIP) while every criterion the
    reader was promised still looked right."""
    receipt = accept(http.service, http.reports, "pinned", external_id="pinned",
                     prior=(("behaviour-failed", "behaviour", "fail"),
                            ("behaviour-skipped", "behaviour", "skip")))
    assert http.client.post("/api/state/projects/pinned/open", headers=ADMIN).status_code == 200

    pinned = json.loads(receipt["evidence_ids"])
    recorded = {row["evidence_id"] for row in http.service.attempts.evidence(receipt["attempt_id"])}
    # A STRICT subset, and the scenario is worth nothing unless it is strict:
    # four rows recorded, two attested.
    assert len(recorded) == 4 and len(pinned) == 2
    assert set(pinned) < recorded

    response = read_receipt(http.client, "pinned", receipt["receipt_id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["evidence_id"] for row in body["evidence"]] == pinned
    assert [row["criterion_id"] for row in body["evidence"]] == list(CRITERIA)
    # Every verdict in the answer is the one the acceptance rests on: the
    # superseded FAIL and SKIP are nowhere in it, by id or by body.
    assert {row["verdict"] for row in body["evidence"]} == {"pass"}
    for discarded in recorded - set(pinned):
        assert discarded not in response.text


def test_the_reader_refuses_an_unopened_projects_receipt_without_the_execute_chokepoint(http):
    """The SECOND half of the project-privacy verdict, on its own.

    `StateService.get_receipt` applies `refuse_unopened_project` itself, so a
    route that reaches the reader without going through
    `core.state_commands.execute` (a forged route, an embedder, a future
    transport) cannot hand an anonymous caller a project nobody opened. This
    path never runs `execute`, so the chokepoint cannot be what refuses it:
    delete the reader's own predicate and THIS test returns a receipt.

    The row authorizer is not a backstop here — `state_acceptances` and
    `state_evidence` are PUBLIC tables — which is asserted directly below: the
    anonymous handle can read the very row this read would return. The
    predicate is the whole barrier."""
    receipt = accept(http.service, http.reports, "closed", external_id="bypass")
    visitor = anonymous_reader(http)

    # The facts are there and the untrusted handle may read them (public tables,
    # no project filter), so nothing beneath this predicate would stop the read.
    with visitor.store.transaction() as conn:
        assert conn.execute("SELECT receipt_id FROM state_acceptances WHERE receipt_id=?",
                            (receipt["receipt_id"],)).fetchone() is not None
        assert conn.execute("SELECT COUNT(*) FROM state_evidence WHERE attempt_id=?",
                            (receipt["attempt_id"],)).fetchone()[0] == len(CRITERIA)

    with pytest.raises(ProjectPrivate) as refused:
        visitor.get_receipt("closed", receipt["receipt_id"])
    assert "not available" in str(refused.value)
    # The one refusal, indistinguishable from a project that does not exist.
    with pytest.raises(ProjectPrivate) as absent:
        visitor.get_receipt("never-created", receipt["receipt_id"])
    assert str(absent.value) == str(refused.value)

    # The SAME reader on a trusted service returns it — the read is not broken.
    assert http.service.get_receipt("closed", receipt["receipt_id"])["artifact_ref"] == ARTIFACT


def test_the_execute_chokepoint_refuses_a_private_receipt_before_the_reader_is_reached(
        http, monkeypatch):
    """The FIRST half of the project-privacy verdict, on its own.

    Every transport funnels into `core.state_commands.execute`, which refuses an
    anonymous read of a project nobody opened BEFORE it validates arguments and
    BEFORE it dispatches. That ordering is the property: a refusal decided at
    the chokepoint cannot depend on the reader remembering its own guard, and a
    structural error is never returned to an anonymous caller (a validation
    message would leak the request shape of a project they may not read).

    Two probes, both red if the chokepoint's project verdict is deleted:
      * a spy on the reader proves `execute` refused WITHOUT dispatching — the
        user-visible outcome alone cannot, because the reader refuses too;
      * a request that would fail argument validation answers 403 "not
        available", never 422 with the field names."""
    receipt = accept(http.service, http.reports, "closed", external_id="chokepoint")
    visitor = anonymous_reader(http)

    dispatched = []
    original = visitor.get_receipt

    def spy(project_id, receipt_id):
        dispatched.append((project_id, receipt_id))
        return original(project_id, receipt_id)

    # Installed on the instance, which is what `_handlers(service)` looks up —
    # a spy that missed would make the negative assertion below pass forever,
    # so the trusted control at the end proves it sits on the dispatch path.
    monkeypatch.setattr(visitor, "get_receipt", spy)
    with pytest.raises(ProjectPrivate):
        execute(visitor, "get_receipt",
                {"project_id": "closed", "receipt_id": receipt["receipt_id"]})
    assert dispatched == []

    # Same refusal with an argument set that cannot validate: privacy is decided
    # before the request shape is judged, so no field name comes back.
    malformed = read_receipt(http.client, "closed", None)
    assert malformed.status_code == 403, malformed.text
    assert malformed.json()["detail"] == str(ProjectPrivate())
    for field in ("receipt_id", "artifact_ref", "node_key", "String should"):
        assert field not in malformed.text

    # Control: the same spy on a TRUSTED service does record a real dispatch, so
    # the empty list above is the chokepoint's refusal and not a spy that missed
    # the handler lookup.
    recorded = []
    real = http.service.get_receipt

    def trusted_spy(project_id, receipt_id):
        recorded.append((project_id, receipt_id))
        return real(project_id, receipt_id)

    monkeypatch.setattr(http.service, "get_receipt", trusted_spy)
    assert execute(http.service, "get_receipt",
                   {"project_id": "closed", "receipt_id": receipt["receipt_id"]})["receipt_id"] \
        == receipt["receipt_id"]
    assert recorded == [("closed", receipt["receipt_id"])]
