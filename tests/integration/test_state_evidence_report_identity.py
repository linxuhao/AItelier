"""Criterion identity is enforced by the normal State service, using real bytes."""
import json

import pytest

from core.state_database import StateDatabase
from core.state_graph import StateConflict
from core.state_service import StateService
from tests.integration.test_composed_product_delivery_cycle import (
    behavior, git, independent_report, seed_repo, sha)


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    artifact = seed_repo(repo)
    tested = behavior(repo, "old")
    assert tested.returncode == 0, tested.stderr
    test_log = tmp_path / "test.json"
    test_log.write_text(json.dumps({
        "returncode": tested.returncode, "stdout": tested.stdout,
        "tree": git(repo, "rev-parse", "HEAD^{tree}")}))
    service = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                           actor="independent-checker", project_read_trusted=True)
    service.create_project("delivery", "criterion report integrity")
    service.store.add_nodes("delivery", [{
        "key": "a", "goal": "deliver actual candidate",
        "acceptance": [{"id": "test", "kind": "test", "description": "real behavior"}]}])
    attempt = service.start_external_attempt(
        "delivery", "a", 1, "subprocess-check", "worker-a", "once")
    bundle = tmp_path / "candidate.bundle"
    git(repo, "bundle", "create", str(bundle), "HEAD")
    terminal = tmp_path / "candidate.json"
    terminal.write_text(json.dumps({
        "status": "candidate", "settled": True, "usable": True,
        "artifact": artifact, "git_bundle": {"path": str(bundle), "sha256": sha(bundle)}}))
    service.report_external_attempt(
        attempt["attempt_id"], "end", 0, attempt["context_hash"], "candidate",
        str(terminal), sha(terminal), quiescent=True, artifact=artifact, artifact_kind="git-sha1")
    report, _ = independent_report(tmp_path, "independent", repo, test_log, "test")
    return service, attempt, artifact, report


def record(candidate, verdict="pass"):
    service, attempt, artifact, report = candidate
    return service.record_evidence(
        attempt["attempt_id"], "check", "test", verdict, artifact, str(report), sha(report))


INVALID_IDENTITIES = [
    ("original-envelope", None, None),
    ("terminal-only-candidate", None, None),
    *[(field + "-" + label, field, value)
      for field in ("artifact", "criterion_id", "verdict")
      for label, value in (("missing", ...), ("null", None), ("empty", ""),
                           ("wrong", "another-value"), ("nontext", True))],
    ("conflicting-artifact-alias", "artifact_ref", "another-artifact"),
    ("null-artifact-alias", "artifact_ref", None),
    ("empty-artifact-alias", "artifact_ref", ""),
    ("conflicting-criterion-alias", "criterion", "another-check"),
    ("null-criterion-alias", "criterion", None),
    ("empty-criterion-alias", "criterion", ""),
    ("conflicting-canonical-artifact", "artifact", "another-artifact"),
    ("conflicting-canonical-criterion", "criterion_id", "another-check"),
    ("noncompleted", "status", "candidate"),
]


@pytest.mark.parametrize("case,field,value", INVALID_IDENTITIES,
                         ids=[case[0] for case in INVALID_IDENTITIES])
def test_invalid_identity_refuses_before_evidence_or_acceptance(candidate, case, field, value):
    service, attempt, artifact, report = candidate
    payload = json.loads(report.read_text())
    if case == "original-envelope":
        payload = {k: v for k, v in payload.items()
                   if k not in {"status", "settled", "usable", "artifact", "criterion_id"}}
        payload["candidate"] = artifact
    elif case == "terminal-only-candidate":
        payload.pop("artifact")
        payload.pop("criterion_id")
        payload["candidate"] = artifact
    elif value is ...:
        payload.pop(field)
    else:
        # Matching aliases must not hide a bad canonical identity, either.
        if case == "conflicting-canonical-artifact":
            payload["artifact_ref"] = artifact
        if case == "conflicting-canonical-criterion":
            payload["criterion"] = "test"
        payload[field] = value
    report.write_text(json.dumps(payload))
    with pytest.raises(StateConflict) as refusal:
        record(candidate)
    print(case + ": " + str(refusal.value))
    assert service.attempts.evidence(attempt["attempt_id"]) == []
    with pytest.raises(StateConflict, match="missing/failed/skipped"):
        service.verify_node("delivery", "a", 1, attempt["attempt_id"])
    with service.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_acceptances").fetchone()[0] == 0


@pytest.mark.parametrize("shape", ["canonical", "aliases", "matching-aliases", "extra-candidate"])
@pytest.mark.parametrize("verdict", ["pass", "fail", "skip"])
def test_explicit_identity_preserves_supported_shapes_and_verdicts(candidate, shape, verdict):
    service, attempt, artifact, report = candidate
    payload = json.loads(report.read_text())
    payload["verdict"] = verdict
    if shape in {"aliases", "matching-aliases"}:
        payload.update(artifact_ref=artifact, criterion="test")
        if shape == "aliases":
            payload.pop("artifact")
            payload.pop("criterion_id")
    if shape == "extra-candidate":
        payload["candidate"] = "harmless-extra-field"
    report.write_text(json.dumps(payload))
    row = record(candidate, verdict)
    assert row["verdict"] == verdict and row["artifact_ref"] == artifact
    # Source bytes are retained independently of the producer's original path.
    report.unlink()
    if verdict == "pass":
        receipt = service.verify_node("delivery", "a", 1, attempt["attempt_id"])
        assert receipt["artifact_ref"] == artifact
    else:
        with pytest.raises(StateConflict, match="missing/failed/skipped"):
            service.verify_node("delivery", "a", 1, attempt["attempt_id"])


def test_independent_subprocess_checks_actual_receipt(candidate, tmp_path):
    _, _, _, report = candidate
    payload = json.loads(report.read_text())
    test_log = tmp_path / "test.json"
    repo = tmp_path / "repo"
    receipt = json.loads(test_log.read_text())
    receipt["tree"] = "0" * 40
    test_log.write_text(json.dumps(receipt))
    import subprocess
    with pytest.raises(subprocess.CalledProcessError):
        independent_report(tmp_path, "wrong-tree", repo, test_log, payload["criterion_id"])
    receipt["tree"] = git(repo, "rev-parse", "HEAD^{tree}")
    receipt["returncode"] = 1
    test_log.write_text(json.dumps(receipt))
    with pytest.raises(subprocess.CalledProcessError):
        independent_report(tmp_path, "failed-receipt", repo, test_log, payload["criterion_id"])
