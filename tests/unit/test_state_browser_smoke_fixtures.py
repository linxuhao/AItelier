"""Focused controls for the fixtures of the standalone State browser smoke.

``tests/browser/state_project_smoke.py`` is a standalone script (it needs
Playwright, Chromium and a built SPA) and is not collected by pytest. These
tests load that script *without* Playwright and call the very helpers its
``main`` uses, on temporary State/SQLite/SDK databases. They do not run a
browser, and they do not weaken any State validator: the old fixture envelope
is shown to be refused by the real API, the current one to be accepted.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from core.state_graph import StateConflict

SMOKE = Path(__file__).resolve().parents[1] / "browser" / "state_project_smoke.py"


@pytest.fixture
def smoke(tmp_path, monkeypatch):
    for name, part in [("AITELIER_HOME", "home"), ("DPE_DB_PATH", "host.db"),
                       ("SKILLFLOW_DB_PATH", "engine.db"), ("DPE_WS_PATH", "workspaces"),
                       ("DPE_PROJECTS_PATH", "projects")]:
        monkeypatch.setenv(name, str(tmp_path / "env" / part))
    spec = importlib.util.spec_from_file_location("state_project_smoke_under_test", SMOKE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _node_status(service, project_id, node_key):
    with service.store.transaction() as conn:
        return conn.execute("SELECT status FROM state_nodes WHERE project_id=? AND node_key=?",
                            (project_id, node_key)).fetchone()[0]


def _acceptances(service, project_id):
    with service.store.transaction() as conn:
        return conn.execute("SELECT COUNT(*) FROM state_acceptances WHERE project_id=?",
                            (project_id,)).fetchone()[0]


def test_script_imports_no_playwright_at_module_load(smoke):
    tree = ast.parse(SMOKE.read_text(encoding="utf-8"))
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    assert not [name for name in top_level if name.split(".")[0] == "playwright"]
    assert not hasattr(smoke, "sync_playwright")


def test_workflow_fixture_first_fail_is_a_real_file_and_current_envelope(smoke, tmp_path):
    service, sf, run_id, external = smoke.fixture(tmp_path)
    try:
        attempts = service.attempts.list("shrimp-preview", "ui.responsive", limit=10)
        assert len(attempts) == 1
        attempt = attempts[0]
        assert attempt["run_id"] == run_id and attempt["status"] == "candidate"
        # A real artifact digest of real synthetic bytes, not an invented Git commit.
        assert attempt["artifact_ref"] == hashlib.sha256(smoke.SYNTHETIC_CANDIDATE).hexdigest()

        evidence = service.attempts.evidence(attempt["attempt_id"])
        assert len(evidence) == 1
        row = evidence[0]
        assert (row["evidence_id"], row["criterion_id"], row["verdict"]) == (
            "ui-first-failure", "behaviour", "fail")
        assert row["artifact_ref"] == attempt["artifact_ref"]

        report = Path(tmp_path / "fixture-reports" / "geometry-failure.json")
        assert report.is_absolute() and report.is_file()
        body = report.read_bytes()
        assert hashlib.sha256(body).hexdigest() == row["report_sha256"]
        envelope = json.loads(body)
        assert envelope["status"] == "completed"
        assert envelope["settled"] is True and envelope["usable"] is True
        assert envelope["criterion_id"] == "behaviour"
        assert envelope["verdict"] == "fail"
        assert envelope["artifact"] == attempt["artifact_ref"]
        assert envelope["synthetic"] is True and "not game acceptance" in envelope["label"]
        # The retained copy State keeps is an absolute file with the same bytes.
        retained = Path(row["report_ref"])
        assert retained.is_absolute() and retained.read_bytes() == body

        # The visible first FAIL is not an acceptance and does not verify the node.
        assert _node_status(service, "shrimp-preview", "ui.responsive") == "CANDIDATE"
        assert _acceptances(service, "shrimp-preview") == 0
        # The paused external run remains an unmodified legacy run.
        assert sf.get_run(external)["status"] == "paused"
        assert len(sf.list_runs()) == 2
    finally:
        sf._conn.close()


def test_old_workflow_fixture_envelope_is_refused_by_the_real_api(smoke, tmp_path):
    service, sf, _run_id, attempt_id, artifact = smoke.seed_workflow(tmp_path)
    try:
        # The old report shape: relative, nonexistent path and made-up digest.
        # The artifact here is the real synthetic SHA-256 seeded above. This
        # probes service report binding, not the old fixture's lower-level call.
        with pytest.raises(StateConflict):
            service.record_evidence(attempt_id, "ui-first-failure", "behaviour", "fail", artifact,
                                    "fixture/geometry-failure.json", "f" * 64,
                                    "Synthetic failed geometry observation")
        assert service.attempts.evidence(attempt_id) == []
        # An absolute, existing report whose JSON lacks the current binding
        # fields is refused too, and a report binding another verdict.
        bare_path, bare_sha = smoke.write_report(tmp_path / "old" / "bare.json",
                                                 {"returncode": 0, "stdout": "", "stderr": ""})
        with pytest.raises(StateConflict):
            service.record_evidence(attempt_id, "ui-first-failure", "behaviour", "fail", artifact,
                                    bare_path, bare_sha)
        wrong_path, wrong_sha = smoke.write_report(
            tmp_path / "old" / "wrong-verdict.json",
            smoke.evidence_envelope("behaviour", "pass", artifact))
        with pytest.raises(StateConflict):
            service.record_evidence(attempt_id, "ui-first-failure", "behaviour", "fail", artifact,
                                    wrong_path, wrong_sha)
        assert service.attempts.evidence(attempt_id) == []
        # The exact current helper then succeeds on the same attempt.
        smoke.record_first_failure(service, tmp_path, attempt_id, artifact)
        assert [e["verdict"] for e in service.attempts.evidence(attempt_id)] == ["fail"]
    finally:
        sf._conn.close()


def test_external_fixture_runs_checker_and_verifies_through_real_state_api(smoke, tmp_path):
    own, attempt_id = smoke.external_fixture(tmp_path)
    attempt = own.attempts.get(attempt_id)
    artifact = hashlib.sha256(smoke.CHECK_SOURCE.encode()).hexdigest()
    assert attempt["execution_kind"] == "external" and attempt["status"] == "candidate"
    assert attempt["artifact_ref"] == artifact and not attempt["run_id"]
    assert attempt["harness"] == "director-subagents"
    assert attempt["external_id"] == "session/checker-1"

    evidence = own.attempts.evidence(attempt_id)
    assert len(evidence) == 1 and evidence[0]["verdict"] == "pass"
    assert evidence[0]["criterion_id"] == "actual-test"
    for name in ("external-candidate.json", "external-evidence.json"):
        report = tmp_path / "fixture-reports" / name
        assert report.is_absolute() and report.is_file()
        envelope = json.loads(report.read_bytes())
        assert envelope["settled"] is True and envelope["usable"] is True
        assert envelope["artifact"] == artifact
        assert envelope["synthetic"] is True
    assert json.loads((tmp_path / "fixture-reports" / "external-candidate.json").read_bytes()
                      )["status"] == "candidate"
    assert json.loads((tmp_path / "fixture-reports" / "external-evidence.json").read_bytes()
                      )["status"] == "completed"
    assert _node_status(own, "external-only", "external.goal") == "VERIFIED"
    assert _acceptances(own, "external-only") == 1
    # A State-only database: no workflow tables exist.
    with own.db.get_connection() as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='runs'").fetchone()


def test_old_external_fixture_envelope_is_refused_by_the_real_api(smoke, tmp_path):
    own, attempt = smoke.start_external(tmp_path)
    check = smoke.run_external_checker()
    artifact = hashlib.sha256(smoke.CHECK_SOURCE.encode()).hexdigest()
    # The pre-repair terminal report: only returncode/stdout/stderr, no
    # status/settled/usable/artifact.
    old_path, old_sha = smoke.write_report(
        tmp_path / "old" / "external-fixture-test.json",
        {"returncode": check.returncode, "stdout": check.stdout, "stderr": check.stderr})
    with pytest.raises(StateConflict):
        own.report_external_attempt(attempt["attempt_id"], "complete", 0, attempt["context_hash"],
                                    "candidate", old_path, old_sha, True, artifact, "sha256")
    # A report that names a different artifact is refused as well.
    other_path, other_sha = smoke.write_report(
        tmp_path / "old" / "other-artifact.json", smoke.candidate_envelope("b" * 64))
    with pytest.raises(StateConflict):
        own.report_external_attempt(attempt["attempt_id"], "complete", 0, attempt["context_hash"],
                                    "candidate", other_path, other_sha, True, artifact, "sha256")
    assert own.attempts.get(attempt["attempt_id"])["status"] != "candidate"
    # The exact current helper succeeds on the same attempt.
    smoke.report_external_candidate(own, tmp_path, attempt, check)
    assert own.attempts.get(attempt["attempt_id"])["status"] == "candidate"
    smoke.record_external_evidence(own, tmp_path, attempt["attempt_id"], check)
    assert _node_status(own, "external-only", "external.goal") == "VERIFIED"


def test_external_checker_starts_after_durable_state_registration(smoke, tmp_path, monkeypatch):
    real_checker = smoke.run_external_checker
    witnessed = []

    def observe_then_run_real_checker():
        path = tmp_path / "external-only.sqlite"
        assert path.is_file(), "external checker started before State registration"
        # Read-only: observing the start must not create a database or owner.
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT attempt_id,status FROM state_attempts WHERE project_id=? AND node_key=?",
                ("external-only", "external.goal")).fetchall()
        assert len(rows) == 1 and rows[0][1] == "running"
        witnessed.append(rows[0][0])
        result = real_checker()
        assert result.returncode == 0
        return result

    monkeypatch.setattr(smoke, "run_external_checker", observe_then_run_real_checker)
    own, attempt_id = smoke.external_fixture(tmp_path)
    assert witnessed == [attempt_id]
    assert own.attempts.get(attempt_id)["status"] == "candidate"
    assert _node_status(own, "external-only", "external.goal") == "VERIFIED"
