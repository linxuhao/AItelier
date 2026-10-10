"""Multi-driver P4: advisory review-independence marking (design/multi-driver-coop.md §7.2, §10 P4).

Project setting review_independence off|advisory (default off, no required mode).
off is byte-identical to a pre-P4 receipt; advisory marks self_reviewed in the
receipt provenance and project_overview counts the marked receipts.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from core import state_claims
from core.state_claims import ClaimError
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_graph import StateConflict, StateGraphError, canonical, digest
from core.state_service import StateService
from tests.unit.test_state_p3_enforced_claims import Clock, LEASE, GRACE, registered

ARTIFACT = hashlib.sha256(b"p4 artifact").hexdigest()
NODE = {"goal": "G", "acceptance": [{"id": "test", "kind": "test", "description": "runs"},
                                    {"id": "review", "kind": "review", "description": "independent review"}]}


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(state_claims, "_clock", fake)
    return fake


@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.sqlite"


def svc(db, driver=None, *, admin=False):
    actor = f"driver:{driver}" if driver else "authorized-state-operator"
    return StateService(StateDatabase(str(db)), actor=actor, project_read_trusted=True,
                        driver_id=driver, is_admin=admin)


def write(service, action, **arguments):
    return execute(service, action, arguments, allow_write=True)


def read(service, action, **arguments):
    return execute(service, action, arguments)


def code_of(call):
    with pytest.raises(ClaimError) as caught:
        call()
    return caught.value.code


def project(db, advisory=False):
    owner = svc(db, "owner-cli", admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": k, **NODE} for k in ("a", "b", "c")])
    if advisory:
        write(owner, "set_review_independence", project_id="p", review_independence="advisory",
              expected_revision=0, reason="P4 test")
    return owner


def a_file(tmp_path, name, payload):
    path = tmp_path / name
    data = json.dumps(payload, sort_keys=True).encode()
    path.write_bytes(data)
    return str(path), hashlib.sha256(data).hexdigest()


def candidate(service, tmp_path, node, key, fence=None):
    """start_external_attempt + a terminal quiescent candidate report with retained bytes."""
    attempt = write(service, "start_external_attempt", project_id="p", node_key=node, expected_revision=1,
                    harness="h", external_id="job-" + key, request_key=key)
    # A candidate report digest binds to ONE observation: make each report distinct.
    ref, sha = a_file(tmp_path, f"report-{key}.json", {"status": "candidate", "settled": True, "usable": True, "key": key})
    args = dict(attempt_id=attempt["attempt_id"], observation_id="final", expected_version=attempt["observation_version"],
                context_hash=attempt["context_hash"], status="candidate", report_ref=ref, report_sha256=sha,
                quiescent=True, artifact=ARTIFACT, artifact_kind="sha256")
    if fence is not None:
        args["fence"] = fence
    write(service, "report_external_attempt", **args)
    return attempt


def evidence(service, tmp_path, attempt, criterion, label, **extra):
    ref, sha = a_file(tmp_path, f"evidence-{label}.json",
                      {"status": "completed", "settled": True, "usable": True, "verdict": "pass",
                       "criterion_id": criterion})
    return write(service, "record_evidence", attempt_id=attempt["attempt_id"], evidence_id=label,
                 criterion_id=criterion, verdict="pass", artifact=ARTIFACT, report_ref=ref, report_sha256=sha, **extra)


def verify(service, attempt, node):
    return write(service, "verify_node", project_id="p", node_key=node, expected_revision=1,
                 attempt_id=attempt["attempt_id"])


def provenance(receipt):
    return json.loads(receipt["provenance_json"])


def pre_p4_provenance(db, attempt_id):
    """The receipt provenance exactly as verify() wrote it before P4 (external attempt)."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        attempt = dict(conn.execute("SELECT * FROM state_attempts WHERE attempt_id=?", (attempt_id,)).fetchone())
        observation = dict(conn.execute(
            "SELECT * FROM state_external_observations WHERE attempt_id=? AND observation_id=?",
            (attempt_id, attempt["terminal_observation_id"])).fetchone())
    finally:
        conn.close()
    expected = {"execution_kind": attempt["execution_kind"]}
    expected.update(harness=attempt["harness"], external_id=attempt["external_id"],
                    reporting_actor=attempt["reporting_actor"], artifact_kind=attempt["artifact_kind"],
                    context_hash=digest(json.loads(attempt["context_json"])),
                    observation_id=observation["observation_id"], report_ref=observation["report_ref"],
                    report_sha256=observation["report_sha256"], quiescent=True)
    return canonical(expected)


def events(db, event_type):
    conn = sqlite3.connect(str(db))
    try:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload_json FROM state_events WHERE event_type=? ORDER BY seq", (event_type,))]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. advisory-review-marking
# ---------------------------------------------------------------------------
class TestAdvisoryReviewMarking:
    def test_off_is_the_default_and_the_receipt_is_byte_identical_to_pre_p4(self, db, tmp_path):
        owner = project(db)
        grok = svc(db, "grok")
        assert read(grok, "project_overview", project_id="p")["policy"]["review_independence"] == "off"
        attempt = candidate(grok, tmp_path, "a", "k1")
        # The owner reviews its own work: under off that is nobody's business.
        evidence(grok, tmp_path, attempt, "test", "e-test")
        evidence(grok, tmp_path, attempt, "review", "e-review")
        receipt = verify(grok, attempt, "a")
        assert receipt["provenance_json"] == pre_p4_provenance(db, attempt["attempt_id"])
        assert "self_reviewed" not in receipt["provenance_json"]
        overview = read(owner, "project_overview", project_id="p")
        assert overview["self_reviewed_receipts"] == 0
        assert overview["nodes"][0]["status"] == "VERIFIED"
        # A project with no policy or enforcement row at all, written by a non-driver caller, is untouched too.
        project(db.parent / "legacy.sqlite")
        legacy = svc(db.parent / "legacy.sqlite")
        legacy_attempt = candidate(legacy, tmp_path, "a", "k-legacy")
        evidence(legacy, tmp_path, legacy_attempt, "test", "l-test")
        evidence(legacy, tmp_path, legacy_attempt, "review", "l-review")
        legacy_receipt = verify(legacy, legacy_attempt, "a")
        assert legacy_receipt["provenance_json"] == pre_p4_provenance(db.parent / "legacy.sqlite",
                                                                      legacy_attempt["attempt_id"])
        assert read(legacy, "project_overview", project_id="p")["policy"]["review_independence"] == "off"

    def test_advisory_marks_the_owner_reviewing_itself_and_counts_it(self, db, tmp_path):
        owner = project(db, advisory=True)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        assert read(grok, "project_overview", project_id="p")["policy"]["review_independence"] == "advisory"
        # a: the owner records the review evidence itself -> marked, still VERIFIED.
        a = candidate(grok, tmp_path, "a", "k-a")
        evidence(codex, tmp_path, a, "test", "a-test")          # a non-owner test row is irrelevant
        evidence(grok, tmp_path, a, "review", "a-review")
        receipt = verify(grok, a, "a")
        assert provenance(receipt) == {**provenance(receipt), "review_independence": "advisory",
                                       "self_reviewed": True, "self_reviewed_criteria": ["review"]}
        assert pre_p4_provenance(db, a["attempt_id"]) != receipt["provenance_json"]
        assert json.loads(pre_p4_provenance(db, a["attempt_id"])).items() <= provenance(receipt).items()
        # b: another member reviews -> not marked.
        b = candidate(grok, tmp_path, "b", "k-b")
        evidence(grok, tmp_path, b, "test", "b-test")           # the owner's TEST evidence is not a review
        evidence(codex, tmp_path, b, "review", "b-review")
        clean = provenance(verify(codex, b, "b"))
        assert clean["self_reviewed"] is False and clean["self_reviewed_criteria"] == []
        # c: the owner's registered-style subagent identity reviews -> still the owner.
        c = candidate(grok, tmp_path, "c", "k-c")
        evidence(codex, tmp_path, c, "test", "c-test")
        evidence(grok, tmp_path, c, "review", "c-review", director_identity="grok/reviewer")
        assert provenance(verify(grok, c, "c"))["self_reviewed"] is True
        overview = read(owner, "project_overview", project_id="p")
        assert overview["self_reviewed_receipts"] == 2
        assert overview["counts"] == {"VERIFIED": 3}
        # The counter is a public fact: an untrusted reader sees the same number.
        owner.open_project("p")
        public = StateService(StateDatabase(str(db)), actor="anyone", project_read_trusted=False)
        assert read(public, "project_overview", project_id="p")["self_reviewed_receipts"] == 2

    def test_latest_evidence_decides_and_a_later_independent_review_clears_the_mark(self, db, tmp_path):
        project(db, advisory=True)
        grok, codex = svc(db, "grok"), svc(db, "codex")
        a = candidate(grok, tmp_path, "a", "k-a")
        evidence(codex, tmp_path, a, "test", "a-test")
        evidence(grok, tmp_path, a, "review", "a-review-self")
        evidence(codex, tmp_path, a, "review", "a-review-independent")   # later row wins
        assert provenance(verify(grok, a, "a"))["self_reviewed"] is False
        b = candidate(grok, tmp_path, "b", "k-b")
        evidence(codex, tmp_path, b, "test", "b-test")
        evidence(codex, tmp_path, b, "review", "b-review-independent")
        evidence(grok, tmp_path, b, "review", "b-review-self")           # the owner re-reviews last
        assert provenance(verify(codex, b, "b"))["self_reviewed"] is True

    def test_a_former_owner_after_take_over_is_still_a_self_reviewer(self, db, tmp_path, clock):
        project(db, advisory=True)
        grok, codex, third = svc(db, "grok"), svc(db, "codex"), svc(db, "third")
        attempt = write(grok, "start_external_attempt", project_id="p", node_key="a", expected_revision=1,
                        harness="h", external_id="job", request_key="k")
        clock.advance(LEASE + GRACE)
        taken = write(codex, "take_over_attempt", attempt_id=attempt["attempt_id"], expected_owner_fence=1,
                      reason="grok vanished")
        assert taken["owner_driver_id"] == "codex"
        ref, sha = a_file(tmp_path, "report.json", {"status": "candidate", "settled": True, "usable": True, "key": "takeover"})
        write(codex, "report_external_attempt", attempt_id=attempt["attempt_id"], observation_id="final",
              expected_version=taken["observation_version"], context_hash=attempt["context_hash"],
              status="candidate", report_ref=ref, report_sha256=sha, quiescent=True, artifact=ARTIFACT,
              artifact_kind="sha256", fence=2)
        evidence(third, tmp_path, attempt, "test", "t")
        evidence(grok, tmp_path, attempt, "review", "r-former-owner")
        marked = provenance(verify(third, attempt, "a"))
        assert marked["self_reviewed"] is True and marked["self_reviewed_criteria"] == ["review"]
        # Same shape for the current owner reviewing (b) and an outsider (c).
        b = candidate(codex, tmp_path, "b", "k-b")
        evidence(third, tmp_path, b, "test", "b-t")
        evidence(codex, tmp_path, b, "review", "b-r")
        assert provenance(verify(codex, b, "b"))["self_reviewed"] is True
        c = candidate(codex, tmp_path, "c", "k-c")
        evidence(third, tmp_path, c, "test", "c-t")
        evidence(third, tmp_path, c, "review", "c-r")
        assert provenance(verify(codex, c, "c"))["self_reviewed"] is False
        assert read(third, "project_overview", project_id="p")["self_reviewed_receipts"] == 2

    def test_a_former_owner_after_handoff_is_still_a_self_reviewer(self, db, tmp_path):
        project(db, advisory=True)
        registered(db, "grok", "codex")
        grok, codex, third = svc(db, "grok"), svc(db, "codex"), svc(db, "third")
        attempt = write(grok, "start_external_attempt", project_id="p", node_key="a", expected_revision=1,
                        harness="h", external_id="job", request_key="k")
        offer = write(grok, "offer_handoff", project_id="p", request_key="h1", expected_owner_fence=1,
                      attempt_id=attempt["attempt_id"], to_driver_id="codex",
                      package={"next_step": "finish", "context_hash": attempt["context_hash"],
                               "observation_version": attempt["observation_version"],
                               "workers": {"quiescent": True}})
        write(codex, "accept_handoff", project_id="p", handoff_id=offer["handoff_id"], expected_owner_fence=1)
        moved = [e for e in events(db, "attempt_ownership_transferred") if e["attempt_id"] == attempt["attempt_id"]]
        assert [(e["mode"], e["from_driver_id"], e["to_driver_id"]) for e in moved] == [("handoff", "grok", "codex")]
        ref, sha = a_file(tmp_path, "report.json", {"status": "candidate", "settled": True, "usable": True, "key": "handoff"})
        write(codex, "report_external_attempt", attempt_id=attempt["attempt_id"], observation_id="final",
              expected_version=attempt["observation_version"], context_hash=attempt["context_hash"],
              status="candidate", report_ref=ref, report_sha256=sha, quiescent=True, artifact=ARTIFACT,
              artifact_kind="sha256", fence=2)
        evidence(third, tmp_path, attempt, "test", "t")
        evidence(grok, tmp_path, attempt, "review", "r-former-owner")
        marked = provenance(verify(third, attempt, "a"))
        assert marked["self_reviewed"] is True and marked["self_reviewed_criteria"] == ["review"]

    def test_switch_rules_admin_only_cas_and_nothing_resets_it(self, db):
        owner = project(db)
        codex = svc(db, "codex")
        assert code_of(lambda: write(codex, "set_review_independence", project_id="p",
                                     review_independence="advisory", expected_revision=0, reason="r")) == "admin_required"
        with pytest.raises(StateGraphError):
            write(owner, "set_review_independence", project_id="p", review_independence="required",
                  expected_revision=0, reason="r")
        with pytest.raises(StateConflict):
            write(owner, "set_review_independence", project_id="p", review_independence="advisory",
                  expected_revision=5, reason="stale")
        # No multi_driver precondition: advisory is accepted directly.
        policy = write(owner, "set_review_independence", project_id="p", review_independence="advisory",
                       expected_revision=0, reason="r")
        assert policy == {**policy, "multi_driver": "on", "claim_enforcement": "off",
                          "review_independence": "advisory", "revision": 1}
        assert events(db, "review_independence_policy_changed")[-1]["review_independence"] == "advisory"
        # Enforcement and advisory marking share the row without clobbering each other.
        write(owner, "set_claim_enforcement", project_id="p", claim_enforcement="on", expected_revision=1, reason="r")
        policy = read(owner, "project_overview", project_id="p")["policy"]
        assert policy["claim_enforcement"] == "on" and policy["review_independence"] == "advisory"
        # Nothing else resets them: set_dispatch keeps both and set_multi_driver no longer exists.
        policy = write(owner, "set_dispatch", project_id="p", dispatch="hold", expected_revision=2, reason="r")
        assert policy == {**policy, "claim_enforcement": "on", "review_independence": "advisory", "revision": 3}
        with pytest.raises(StateGraphError, match="unknown state graph action"):
            write(owner, "set_multi_driver", project_id="p", multi_driver="off", expected_revision=3, reason="r")
        policy = read(owner, "project_overview", project_id="p")["policy"]
        assert policy == {**policy, "claim_enforcement": "on", "review_independence": "advisory", "revision": 3}
        # off can always be written back (revision advances).
        policy = write(owner, "set_review_independence", project_id="p", review_independence="off",
                       expected_revision=3, reason="r")
        assert policy == {**policy, "claim_enforcement": "on", "review_independence": "off", "revision": 4}

    def test_pre_p4_enforcement_table_gains_the_column_and_reads_off(self, db):
        # A real database whose enforcement table is put back into its P3 shape.
        project(db)
        write(svc(db, "owner-cli", admin=True), "set_claim_enforcement", project_id="p", claim_enforcement="on",
              expected_revision=0, reason="r")
        conn = sqlite3.connect(str(db))
        conn.executescript("""
            CREATE TABLE p3_enforcement AS SELECT project_id,claim_enforcement,reason,actor,updated_at
                FROM state_project_enforcement;
            DROP TABLE state_project_enforcement;
            CREATE TABLE state_project_enforcement (
                project_id TEXT PRIMARY KEY,
                claim_enforcement TEXT NOT NULL CHECK(claim_enforcement IN ('off','on')),
                reason TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL,
                FOREIGN KEY(project_id) REFERENCES state_projects(project_id));
            INSERT INTO state_project_enforcement SELECT * FROM p3_enforcement;
            DROP TABLE p3_enforcement;
        """)
        conn.commit()
        conn.close()
        owner = svc(db, "owner-cli", admin=True)      # opening migrates
        conn = sqlite3.connect(str(db))
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(state_project_enforcement)")]
            assert cols[-1] == "review_independence"
            assert conn.execute("SELECT claim_enforcement,review_independence FROM state_project_enforcement").fetchone() \
                == ("on", "off")
            assert [r[1] for r in conn.execute("PRAGMA table_info(state_project_policy)")][-1] == "multi_driver"
        finally:
            conn.close()
        assert read(owner, "project_overview", project_id="p")["policy"]["review_independence"] == "off"
        # Opening again changes nothing: same schema, same policy and enforcement rows.
        before = snapshot(db)
        svc(db, "owner-cli", admin=True)
        assert snapshot(db) == before


def snapshot(db):
    conn = sqlite3.connect(str(db))
    try:
        return (sorted(conn.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()),
                conn.execute("SELECT * FROM state_project_policy ORDER BY project_id").fetchall(),
                conn.execute("SELECT * FROM state_project_enforcement ORDER BY project_id").fetchall())
    finally:
        conn.close()
