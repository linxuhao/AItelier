"""Attempts and acceptance receipts connecting state goals to SkillFlow runs.

The workflow engine is observed through its public API, never by writing its
SQLite tables. Completion produces a candidate, not a verified fact. Evidence
is an explicit trusted-verifier attestation; this module checks its scope and
completeness, not the honesty of the person or test runner that produced it.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid

from core.state_graph import (StateConflict, StateGraphError, StateGraphStore,
                              StateNotFound, canonical, digest, integer, key, now, text)

ACTIVE = ("reserved", "launching", "running", "paused", "unknown")
SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS state_attempts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL, node_key TEXT NOT NULL, node_revision INTEGER NOT NULL,
    contract_hash TEXT NOT NULL, dependency_snapshot TEXT NOT NULL, context_json TEXT NOT NULL,
    request_key TEXT NOT NULL, request_hash TEXT NOT NULL, workflow TEXT NOT NULL,
    execution_project_id TEXT NOT NULL UNIQUE, run_id TEXT UNIQUE,
    graph_version INTEGER, graph_digest TEXT,
    status TEXT NOT NULL CHECK(status IN ('reserved','launching','running','paused','unknown','candidate','failed','superseded')),
    artifact_ref TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id,node_key,request_key),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS state_attempts_one_active ON state_attempts(project_id,node_key)
WHERE status IN ('reserved','launching','running','paused','unknown');
CREATE TABLE IF NOT EXISTS state_evidence (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, evidence_id TEXT NOT NULL UNIQUE,
    attempt_id TEXT NOT NULL, criterion_id TEXT NOT NULL, kind TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail','skip')),
    artifact_ref TEXT NOT NULL, report_ref TEXT NOT NULL, report_sha256 TEXT NOT NULL,
    reviewer TEXT NOT NULL, detail TEXT NOT NULL, payload_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
CREATE TABLE IF NOT EXISTS state_acceptances (
    receipt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    node_revision INTEGER NOT NULL, attempt_id TEXT NOT NULL,
    artifact_ref TEXT NOT NULL, contract_hash TEXT NOT NULL, dependency_snapshot TEXT NOT NULL,
    evidence_ids TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
CREATE INDEX IF NOT EXISTS state_attempts_node ON state_attempts(project_id,node_key,seq);
CREATE INDEX IF NOT EXISTS state_evidence_attempt ON state_evidence(attempt_id,seq);
CREATE TRIGGER IF NOT EXISTS state_evidence_no_update BEFORE UPDATE ON state_evidence
BEGIN SELECT RAISE(ABORT,'state evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_evidence_no_delete BEFORE DELETE ON state_evidence
BEGIN SELECT RAISE(ABORT,'state evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_acceptance_no_update BEFORE UPDATE ON state_acceptances
BEGIN SELECT RAISE(ABORT,'acceptance receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_acceptance_no_delete BEFORE DELETE ON state_acceptances
BEGIN SELECT RAISE(ABORT,'acceptance receipts are append-only'); END;
"""


def artifact_ref(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise StateGraphError("artifact_ref must be an exact lowercase Git SHA or SHA-256 digest")
    return value


def _public(row: dict) -> dict:
    result = dict(row)
    result["context"] = json.loads(result.pop("context_json"))
    result["dependencies"] = json.loads(result.pop("dependency_snapshot"))
    return result


class StateAttempts:
    def __init__(self, store: StateGraphStore):
        self.store = store
        with store.db.get_connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    @staticmethod
    def _attempt(conn, attempt_id):
        row = conn.execute("SELECT * FROM state_attempts WHERE attempt_id=?", (key(attempt_id, "attempt id"),)).fetchone()
        if row is None:
            raise StateNotFound(f"attempt {attempt_id!r} not found")
        return dict(row)

    def get(self, attempt_id: str) -> dict:
        with self.store.transaction() as conn:
            return _public(self._attempt(conn, attempt_id))

    def list(self, project_id: str, node_key: str, limit: int = 100) -> list[dict]:
        integer(limit, "limit", 1, 500)
        with self.store.transaction() as conn:
            self.store._node(conn, project_id, node_key)
            return [_public(dict(r)) for r in conn.execute("SELECT * FROM state_attempts "
                    "WHERE project_id=? AND node_key=? ORDER BY seq DESC LIMIT ?", (project_id, node_key, limit))]

    def _pins_current(self, conn, attempt):
        node = self.store._node(conn, attempt["project_id"], attempt["node_key"])
        if (node["revision"] != attempt["node_revision"] or node["contract_hash"] != attempt["contract_hash"]
                or node["status"] == "SUPERSEDED"):
            return False
        snapshot = self.store.dependency_snapshot(conn, attempt["project_id"], attempt["node_key"])
        return (canonical(snapshot) == attempt["dependency_snapshot"]
                and all(d["status"] == "VERIFIED" and d["verified_receipt"] for d in snapshot.values()))

    def reserve(self, project_id: str, node_key: str, expected_revision: int,
                workflow: str, request_key: str, instruction: str = "") -> dict:
        """Idempotent intent, persisted before a workflow can be launched."""
        key(workflow, "workflow")
        key(request_key, "request key")
        integer(expected_revision, "expected_revision", 1)
        if not isinstance(instruction, str) or len(instruction) > 20000:
            raise StateGraphError("instruction must be text of at most 20000 characters")
        request_hash = digest({"revision": expected_revision, "workflow": workflow, "instruction": instruction})
        with self.store.transaction(write=True) as conn:
            node = self.store._node(conn, project_id, node_key)
            old = conn.execute("SELECT * FROM state_attempts WHERE project_id=? AND node_key=? AND request_key=?",
                               (project_id, node_key, request_key)).fetchone()
            if old:
                if old["request_hash"] != request_hash:
                    raise StateConflict("request key already used with a different launch request")
                return _public(dict(old))
            from core.state_metadata import require_dispatch
            require_dispatch(conn, project_id, node_key)
            if node["revision"] != expected_revision or node["status"] in {"VERIFIED", "SUPERSEDED"}:
                raise StateConflict("node is closed or revision changed; revise/reload before attempting it")
            deps = self.store.dependency_snapshot(conn, project_id, node_key)
            if not all(d["status"] == "VERIFIED" and d["verified_receipt"] for d in deps.values()):
                raise StateConflict("dependencies are not verified")
            ctx = {"state_project_id": project_id, "node_key": node_key, "revision": expected_revision,
                   "goal": node["goal"], "acceptance": json.loads(node["contract_json"]),
                   "contract_hash": node["contract_hash"], "dependencies": deps, "instruction": instruction}
            uid = uuid.uuid4().hex
            aid, execution = "attempt-" + uid, "sg-" + uid
            try:
                conn.execute("INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
                             "dependency_snapshot,context_json,request_key,request_hash,workflow,execution_project_id,"
                             "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)",
                             (aid, project_id, node_key, expected_revision, node["contract_hash"], canonical(deps),
                              canonical(ctx), request_key, request_hash, workflow, execution, now(), now()))
            except sqlite3.IntegrityError as exc:
                raise StateConflict("an active attempt already owns this node; recover it rather than duplicating") from exc
            conn.execute("UPDATE state_nodes SET status='OPEN',updated_at=? WHERE project_id=? AND node_key=?",
                         (now(), project_id, node_key))
            self.store._event(conn, project_id, node_key, "attempt_reserved", {"attempt_id": aid,
                              "execution_project_id": execution, "workflow": workflow, "revision": expected_revision})
            return _public(self._attempt(conn, aid))

    def pin_host_contract(self, attempt_id: str, descriptor: dict) -> dict:
        """Freeze host output/source metadata before dispatch; not workflow state."""
        fields = {"source_repo", "seed_file", "output_step", "scheduler_owned", "repo_mode"}
        if (not isinstance(descriptor, dict) or set(descriptor) != fields
                or type(descriptor["scheduler_owned"]) is not bool
                or descriptor["repo_mode"] not in {"code", "none"}):
            raise StateGraphError("invalid host launch descriptor")
        for field in fields - {"scheduler_owned"}:
            value = descriptor[field]
            if value is not None:
                text(value, field, 4000)
        with self.store.transaction(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            binding = conn.execute("SELECT repo_path FROM state_source_bindings WHERE project_id=?",
                                   (attempt["project_id"],)).fetchone()
            if binding and binding["repo_path"] != descriptor["source_repo"]:
                raise StateConflict("source binding changed between planning and reservation")
            context = json.loads(attempt["context_json"])
            old = context.get("host_contract")
            if old is not None:
                if old != descriptor:
                    raise StateConflict("host launch contract changed; retire the unlaunched intent and choose a new request")
                return _public(attempt)
            if attempt["status"] != "reserved":
                raise StateConflict("host contract must be pinned before dispatch")
            context["host_contract"] = descriptor
            conn.execute("UPDATE state_attempts SET context_json=?,updated_at=? WHERE attempt_id=?",
                         (canonical(context), now(), attempt_id))
            self.store._event(conn, attempt["project_id"], attempt["node_key"], "host_contract_pinned",
                              {"attempt_id": attempt_id, "descriptor": descriptor})
            return _public(self._attempt(conn, attempt_id))

    def claim_launch(self, attempt_id: str) -> bool:
        """Only the first caller dispatches. Uncertain launches are not retried."""
        with self.store.transaction(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            if attempt["status"] != "reserved":
                return False
            from core.state_metadata import require_dispatch
            require_dispatch(conn, attempt["project_id"], attempt["node_key"])
            if not self._pins_current(conn, attempt):
                # No dispatch occurred: retiring this intent cannot abandon a
                # running worker. Otherwise a spec edit strands a node forever.
                conn.execute("UPDATE state_attempts SET status='superseded',updated_at=? WHERE attempt_id=?",
                             (now(), attempt_id))
                self.store._event(conn, attempt["project_id"], attempt["node_key"], "reservation_superseded",
                                  {"attempt_id": attempt_id})
                return False
            conn.execute("UPDATE state_attempts SET status='launching',updated_at=? WHERE attempt_id=?", (now(), attempt_id))
            self.store._event(conn, attempt["project_id"], attempt["node_key"], "attempt_launching", {"attempt_id": attempt_id})
            return True

    def retire_reservation(self, attempt_id: str, reason: str) -> dict:
        """Cancel only an undispatched intent; never infer a worker has stopped."""
        reason = text(reason, "reservation retirement reason", 4000)
        with self.store.transaction(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            if attempt["status"] == "superseded" and not attempt["run_id"]:
                return _public(attempt)
            if attempt["status"] != "reserved" or attempt["run_id"]:
                raise StateConflict("only an unlaunched reservation can be retired; recover/stop the actual run first")
            conn.execute("UPDATE state_attempts SET status='superseded',error=?,updated_at=? WHERE attempt_id=?",
                         (reason, now(), attempt_id))
            self.store._event(conn, attempt["project_id"], attempt["node_key"], "reservation_retired",
                              {"attempt_id": attempt_id, "reason": reason})
            return _public(self._attempt(conn, attempt_id))

    def launch_uncertain(self, attempt_id: str, reason: str) -> dict:
        reason = text(reason, "launch uncertainty", 4000)
        with self.store.transaction(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            if attempt["run_id"] or attempt["status"] not in {"launching", "unknown"}:
                return _public(attempt)
            conn.execute("UPDATE state_attempts SET status='unknown',error=?,updated_at=? WHERE attempt_id=?",
                         (reason, now(), attempt_id))
            self.store._event(conn, attempt["project_id"], attempt["node_key"], "launch_uncertain",
                              {"attempt_id": attempt_id, "reason": reason})
            return _public(self._attempt(conn, attempt_id))

    @staticmethod
    def _engine_row(sf, run_id, attempt):
        try:
            row = sf.get_run(run_id)
        except Exception as exc:
            raise StateConflict(f"cannot observe workflow: {type(exc).__name__}") from exc
        if not isinstance(row, dict) or row.get("id") != run_id:
            raise StateConflict("workflow run is unknown; a missing run is not successful")
        if row.get("project_id") != attempt["execution_project_id"] or row.get("graph_name") != attempt["workflow"]:
            raise StateConflict("run belongs to a different execution project or workflow")
        if row.get("status") not in {"pending", "running", "paused", "completed", "failed"}:
            raise StateConflict("unsupported workflow status; retain the attempt")
        if (type(row.get("graph_version")) is not int or row["graph_version"] < 1
                or not isinstance(row.get("graph_digest"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", row["graph_digest"])):
            raise StateConflict("workflow has no valid version/digest pin")
        # SkillFlow can warn and fall back to a current graph if its historical
        # version vanished. A state acceptance must not quietly inherit that
        # fallback: validate the actual pinned version through the public API.
        try:
            from skillflow.core import graph_digest
            version = sf.get_graph_version(row["graph_name"], row["graph_version"])
            valid = (isinstance(version, dict) and version.get("digest") == row["graph_digest"]
                     and graph_digest(version["graph"]) == row["graph_digest"])
        except Exception as exc:
            raise StateConflict("cannot verify workflow graph version history") from exc
        if not valid:
            raise StateConflict("pinned workflow graph version is missing or inconsistent")
        return row

    def bind_run(self, attempt_id: str, run_id: str, sf) -> dict:
        key(run_id, "run id")
        attempt = self.get(attempt_id)
        row = self._engine_row(sf, run_id, attempt)
        with self.store.transaction(write=True) as conn:
            current = self._attempt(conn, attempt_id)
            if current["run_id"]:
                if current["run_id"] != run_id:
                    raise StateConflict("attempt is already bound to another run")
                return _public(current)
            if current["status"] not in ACTIVE:
                raise StateConflict("terminal attempt cannot be rebound")
            try:
                conn.execute("UPDATE state_attempts SET run_id=?,graph_version=?,graph_digest=?,status=?,error=NULL,updated_at=? "
                             "WHERE attempt_id=?", (run_id, row.get("graph_version"), row.get("graph_digest"),
                             "paused" if row["status"] == "paused" else "running", now(), attempt_id))
            except sqlite3.IntegrityError as exc:
                raise StateConflict("run is already assigned to an attempt") from exc
            self.store._event(conn, current["project_id"], current["node_key"], "attempt_bound",
                              {"attempt_id": attempt_id, "run_id": run_id, "graph_version": row.get("graph_version"),
                               "graph_digest": row.get("graph_digest")})
            return _public(self._attempt(conn, attempt_id))

    def reconcile(self, attempt_id: str, sf, candidate_artifact: str | None = None) -> dict:
        """Observe a real run; never modify its graph, checkpoints or status."""
        attempt = self.get(attempt_id)
        if not attempt["run_id"]:
            raise StateConflict("attempt has no run; recover its launch first")
        row = self._engine_row(sf, attempt["run_id"], attempt)
        if (row.get("graph_version") != attempt["graph_version"]
                or row.get("graph_digest") != attempt["graph_digest"]):
            raise StateConflict("workflow definition pin changed")
        if candidate_artifact is not None:
            artifact_ref(candidate_artifact)
        outcome = row["status"]
        if outcome in {"completed", "failed"}:
            try:
                audit = sf.audit_operation_owners(attempt["run_id"])
            except Exception as exc:
                raise StateConflict(f"cannot audit operation quiescence: {type(exc).__name__}") from exc
            if (not isinstance(audit, dict) or not isinstance(audit.get("lost"), list)
                    or not isinstance(audit.get("unknown"), list) or type(audit.get("alive")) is not int
                    or audit["alive"] < 0):
                raise StateConflict("operation audit is incomplete; quiescence is unknown")
            if audit["lost"] or audit["unknown"] or audit["alive"]:
                outcome = "unknown"
        status = {"pending": "running", "running": "running", "paused": "paused",
                  "completed": "candidate", "failed": "failed", "unknown": "unknown"}[outcome]
        with self.store.transaction(write=True) as conn:
            current = self._attempt(conn, attempt_id)
            if current["run_id"] != row["id"]:
                raise StateConflict("attempt binding changed")
            fresh = self._pins_current(conn, current)
            if status in {"candidate", "failed"} and not fresh:
                status = "superseded"
            if current["status"] in {"candidate", "failed", "superseded"} and status in ACTIVE:
                raise StateConflict("terminal attempt's run was revived; use a new attempt")
            artifact = current["artifact_ref"]
            if candidate_artifact is not None and status == "candidate":
                if artifact and artifact != candidate_artifact:
                    raise StateConflict("candidate artifact is immutable; create another attempt")
                artifact = candidate_artifact
            changed = current["status"] != status or artifact != current["artifact_ref"]
            if changed:
                if status != "candidate":
                    node = self.store._node(conn, current["project_id"], current["node_key"])
                    receipt = conn.execute("SELECT attempt_id FROM state_acceptances WHERE receipt_id=?",
                                           (node["verified_receipt"],)).fetchone()
                    if receipt and receipt[0] == attempt_id:
                        affected = self.store._invalidate(conn, current["project_id"], current["node_key"])
                        self.store._event(conn, current["project_id"], current["node_key"], "acceptance_invalidated",
                                          {"reason": "accepted workflow no longer has a valid candidate outcome",
                                           "invalidated": affected, "attempt_id": attempt_id})
                conn.execute("UPDATE state_attempts SET status=?,artifact_ref=?,error=?,updated_at=? WHERE attempt_id=?",
                             (status, artifact, row.get("error_reason"), now(), attempt_id))
                if status == "candidate":
                    conn.execute("UPDATE state_nodes SET status='CANDIDATE',updated_at=? "
                                 "WHERE project_id=? AND node_key=? AND status!='VERIFIED'",
                                 (now(), current["project_id"], current["node_key"]))
                self.store._event(conn, current["project_id"], current["node_key"], "attempt_observed",
                                  {"attempt_id": attempt_id, "status": status, "artifact_ref": artifact,
                                   "stale_inputs": not fresh})
            return {**_public(self._attempt(conn, attempt_id)), "stale_inputs": not fresh}

    def _eligible_candidate(self, conn, attempt):
        if attempt["status"] != "candidate" or not attempt["artifact_ref"]:
            raise StateConflict("a completed candidate with a pinned artifact is required")
        if not self._pins_current(conn, attempt):
            raise StateConflict("candidate inputs are stale; re-run/re-validate against current dependencies")
        latest = conn.execute("SELECT attempt_id FROM state_attempts WHERE project_id=? AND node_key=? ORDER BY seq DESC LIMIT 1",
                              (attempt["project_id"], attempt["node_key"])).fetchone()
        if not latest or latest[0] != attempt["attempt_id"]:
            raise StateConflict("a newer attempt supersedes this candidate")

    def record_evidence(self, attempt_id: str, evidence_id: str, criterion_id: str, verdict: str,
                        artifact: str, report_ref: str, report_sha256: str, reviewer: str, detail: str = "") -> dict:
        """Append a scoped verifier attestation, never infer it from agent prose."""
        key(evidence_id, "evidence id")
        key(criterion_id, "criterion id")
        artifact_ref(artifact)
        if not isinstance(verdict, str) or verdict not in {"pass", "fail", "skip"}:
            raise StateGraphError("verdict must be pass, fail or skip")
        if not isinstance(report_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", report_sha256):
            raise StateGraphError("report_sha256 must pin the complete report bytes")
        report_ref = text(report_ref, "report reference", 2000)
        reviewer = text(reviewer, "verifier identity", 300)
        if not isinstance(detail, str) or len(detail) > 8000:
            raise StateGraphError("evidence detail must be bounded text")
        payload = {"attempt_id": attempt_id, "criterion_id": criterion_id, "verdict": verdict, "artifact_ref": artifact,
                   "report_ref": report_ref, "report_sha256": report_sha256, "reviewer": reviewer, "detail": detail}
        payload_hash = digest(payload)
        with self.store.transaction(write=True) as conn:
            attempt = self._attempt(conn, attempt_id)
            prior = conn.execute("SELECT * FROM state_evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            if prior:
                if prior["payload_hash"] != payload_hash:
                    raise StateConflict("evidence id already used with different content")
                return dict(prior)
            self._eligible_candidate(conn, attempt)
            if attempt["artifact_ref"] != artifact:
                raise StateConflict("evidence describes a different artifact")
            ctx = json.loads(attempt["context_json"])
            checks = {c["id"]: c for c in ctx["acceptance"]}
            if criterion_id not in checks:
                raise StateGraphError("evidence criterion is not in the pinned acceptance contract")
            conn.execute("INSERT INTO state_evidence(evidence_id,attempt_id,criterion_id,kind,verdict,artifact_ref,"
                         "report_ref,report_sha256,reviewer,detail,payload_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (evidence_id, attempt_id, criterion_id, checks[criterion_id]["kind"], verdict, artifact,
                          report_ref, report_sha256, reviewer, detail, payload_hash, now()))
            node = self.store._node(conn, attempt["project_id"], attempt["node_key"])
            if node["verified_receipt"]:
                affected = self.store._invalidate(conn, attempt["project_id"], attempt["node_key"])
                self.store._event(conn, attempt["project_id"], attempt["node_key"], "acceptance_invalidated",
                                  {"reason": "new evidence supersedes an accepted observation", "invalidated": affected})
            self.store._event(conn, attempt["project_id"], attempt["node_key"], "evidence_recorded", payload | {"evidence_id": evidence_id})
            return dict(conn.execute("SELECT * FROM state_evidence WHERE evidence_id=?", (evidence_id,)).fetchone())

    def verify(self, project_id: str, node_key: str, expected_revision: int,
               attempt_id: str, reviewer: str) -> dict:
        integer(expected_revision, "expected_revision", 1)
        reviewer = text(reviewer, "acceptance reviewer", 300)
        with self.store.transaction(write=True) as conn:
            node = self.store._node(conn, project_id, node_key)
            attempt = self._attempt(conn, attempt_id)
            if (attempt["project_id"] != project_id or attempt["node_key"] != node_key
                    or node["revision"] != expected_revision):
                raise StateConflict("acceptance target or revision mismatch")
            self._eligible_candidate(conn, attempt)
            latest = {r["criterion_id"]: dict(r) for r in conn.execute(
                "SELECT * FROM state_evidence WHERE attempt_id=? ORDER BY seq", (attempt_id,))}
            required = json.loads(node["contract_json"])
            failed = [c["id"] for c in required if c["id"] not in latest or latest[c["id"]]["verdict"] != "pass"]
            if failed:
                raise StateConflict("missing/failed/skipped acceptance evidence: " + ", ".join(failed))
            evidence_ids = canonical([latest[c["id"]]["evidence_id"] for c in required])
            if node["verified_receipt"]:
                receipt = conn.execute("SELECT * FROM state_acceptances WHERE receipt_id=?", (node["verified_receipt"],)).fetchone()
                if receipt and receipt["attempt_id"] == attempt_id and receipt["evidence_ids"] == evidence_ids:
                    return dict(receipt)
                raise StateConflict("different acceptance already exists; revise or record correcting evidence first")
            rid = "receipt-" + uuid.uuid4().hex
            conn.execute("INSERT INTO state_acceptances VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (rid, project_id, node_key, expected_revision, attempt_id, attempt["artifact_ref"],
                          node["contract_hash"], attempt["dependency_snapshot"], evidence_ids, reviewer, now()))
            conn.execute("UPDATE state_nodes SET status='VERIFIED',verified_receipt=?,updated_at=? "
                         "WHERE project_id=? AND node_key=?", (rid, now(), project_id, node_key))
            self.store._event(conn, project_id, node_key, "node_verified",
                              {"receipt_id": rid, "attempt_id": attempt_id, "revision": expected_revision,
                               "artifact_ref": attempt["artifact_ref"], "reviewer": reviewer})
            return dict(conn.execute("SELECT * FROM state_acceptances WHERE receipt_id=?", (rid,)).fetchone())

    def evidence(self, attempt_id: str) -> list[dict]:
        with self.store.transaction() as conn:
            self._attempt(conn, attempt_id)
            return [dict(r) for r in conn.execute("SELECT * FROM state_evidence WHERE attempt_id=? ORDER BY seq", (attempt_id,))]
