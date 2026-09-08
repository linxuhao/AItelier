"""Private project-level queries and migration safeguards for the State DAG.

Reference links never adopt a run, claim its output, or certify a goal. Holds
prevent *new* dispatch; they do not pretend to cancel an already admitted run.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from core.state_graph import StateConflict, StateGraphError, digest, integer, key, now, text
from core.state_metadata import node_hold, project_policy

ATTEMPT_COLUMNS = ("seq", "attempt_id", "project_id", "node_key", "node_revision", "workflow",
                   "execution_project_id", "run_id", "status", "artifact_ref", "created_at", "updated_at")


class StatePortfolio:
    def __init__(self, store, actor="authorized-state-operator"):
        self.store, self.actor = store, actor

    def source_binding(self, project_id):
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            row = conn.execute("SELECT * FROM state_source_bindings WHERE project_id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _source(conn, project):
        row = conn.execute("SELECT * FROM state_source_bindings WHERE project_id=?", (project["project_id"],)).fetchone()
        if row:
            return {**dict(row), "kind": "stable_binding"}
        legacy_id = project["source_project_id"]
        legacy = conn.execute("SELECT repo_path FROM runs WHERE project_id=?", (legacy_id,)).fetchone() if legacy_id else None
        return {"kind": "legacy_binding" if legacy_id else "none", "source_project_id": legacy_id,
                "repo_path": legacy[0] if legacy else None, "common_dir": None, "revision": 0}

    def bind_source(self, project_id, repo_path, common_dir, expected_revision):
        """Inputs were checked by the service. Cannot retarget any past attempt."""
        integer(expected_revision, "expected_revision", 0)
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            prior = conn.execute("SELECT * FROM state_source_bindings WHERE project_id=?", (project_id,)).fetchone()
            actual = prior["revision"] if prior else 0
            if actual != expected_revision:
                raise StateConflict("source binding revision changed")
            if conn.execute("SELECT 1 FROM state_attempts WHERE project_id=? LIMIT 1", (project_id,)).fetchone():
                raise StateConflict("source bindings cannot be retargeted after attempts exist; preserve old provenance")
            conn.execute("INSERT INTO state_source_bindings VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                         "revision=excluded.revision,repo_path=excluded.repo_path,common_dir=excluded.common_dir,"
                         "actor=excluded.actor,updated_at=excluded.updated_at",
                         (project_id, actual + 1, repo_path, common_dir, self.actor, now()))
            self.store._event(conn, project_id, None, "source_bound", {"revision": actual + 1,
                              "repo_path": repo_path, "common_dir": common_dir, "actor": self.actor})
            return dict(conn.execute("SELECT * FROM state_source_bindings WHERE project_id=?", (project_id,)).fetchone())

    def set_dispatch(self, project_id, dispatch, expected_revision, reason):
        if not isinstance(dispatch, str) or dispatch not in {"active", "hold", "archive"}:
            raise StateGraphError("dispatch must be active, hold or archive")
        integer(expected_revision, "expected_revision", 0)
        reason = text(reason, "dispatch reason", 4000)
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            old = project_policy(conn, project_id)
            if old["revision"] != expected_revision:
                raise StateConflict("project policy revision changed")
            conn.execute("INSERT INTO state_project_policy VALUES(?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET "
                         "revision=excluded.revision,dispatch=excluded.dispatch,reason=excluded.reason,"
                         "actor=excluded.actor,updated_at=excluded.updated_at",
                         (project_id, expected_revision + 1, dispatch, reason, self.actor, now()))
            self.store._event(conn, project_id, None, "dispatch_policy_changed",
                              {"dispatch": dispatch, "reason": reason, "revision": expected_revision + 1, "actor": self.actor})
            return project_policy(conn, project_id)

    def _hold(self, conn, project_id, node_key, held, expected_revision, reason):
        self.store._node(conn, project_id, node_key)
        if type(held) is not bool:
            raise StateGraphError("held must be a boolean")
        integer(expected_revision, "expected_revision", 0)
        reason = text(reason, "hold reason", 4000)
        old = node_hold(conn, project_id, node_key)
        if old["revision"] != expected_revision:
            raise StateConflict("node hold revision changed")
        conn.execute("INSERT INTO state_node_holds VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id,node_key) DO UPDATE SET "
                     "revision=excluded.revision,held=excluded.held,reason=excluded.reason,actor=excluded.actor,updated_at=excluded.updated_at",
                     (project_id, node_key, expected_revision + 1, int(held), reason, self.actor, now()))
        self.store._event(conn, project_id, node_key, "node_hold_changed",
                          {"held": held, "reason": reason, "revision": expected_revision + 1, "actor": self.actor})
        return node_hold(conn, project_id, node_key)

    def set_hold(self, project_id, node_key, held, expected_revision, reason):
        with self.store.transaction(write=True) as conn:
            return self._hold(conn, project_id, node_key, held, expected_revision, reason)

    def add_reference(self, project_id, node_key, reference_id, kind, ref, label, provenance_actor,
                      observed_status="historical", artifact_ref=None, report_sha256=None, protect=False):
        key(reference_id, "reference id")
        if not isinstance(kind, str) or kind not in {"run", "commit", "report", "note"}:
            raise StateGraphError("reference kind must be run, commit, report or note")
        ref = text(ref, "reference", 2000)
        label = text(label, "reference label", 2000)
        provenance_actor = text(provenance_actor, "provenance actor", 300)
        observed_status = text(observed_status, "observed status", 100)
        if type(protect) is not bool:
            raise StateGraphError("protect must be a boolean")
        for value, name, length in [(artifact_ref, "artifact_ref", (40, 64)), (report_sha256, "report_sha256", (64,))]:
            if value is not None and (not isinstance(value, str) or len(value) not in length or any(c not in "0123456789abcdef" for c in value)):
                raise StateGraphError(f"{name} must be an exact lowercase digest")
        payload = dict(node_key=node_key, kind=kind, ref=ref, label=label, provenance_actor=provenance_actor,
                       observed_status=observed_status, artifact_ref=artifact_ref, report_sha256=report_sha256, protection=int(protect))
        fingerprint = digest(payload)
        with self.store.transaction(write=True) as conn:
            node = self.store._node(conn, project_id, node_key)
            previous = conn.execute("SELECT * FROM state_history_links WHERE project_id=? AND reference_id=?", (project_id, reference_id)).fetchone()
            if previous:
                if previous["payload_hash"] != fingerprint:
                    raise StateConflict("reference id already records different history; append a new correction")
                return dict(previous)
            conn.execute("INSERT INTO state_history_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (project_id, reference_id, node_key, kind, ref, label, observed_status, artifact_ref,
                          report_sha256, provenance_actor, self.actor, node["revision"], int(protect), fingerprint, now()))
            if protect:
                hold = node_hold(conn, project_id, node_key)
                self._hold(conn, project_id, node_key, True, hold["revision"], "Existing external work: " + label)
            self.store._event(conn, project_id, node_key, "historical_reference_added", payload | {"reference_id": reference_id, "acceptance": False})
            return dict(conn.execute("SELECT * FROM state_history_links WHERE project_id=? AND reference_id=?", (project_id, reference_id)).fetchone())

    def references(self, project_id, node_key=None, after="", limit=100):
        integer(limit, "limit", 1, 200)
        if after:
            key(after, "after")
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            where, params = "project_id=? AND reference_id>?", [project_id, after]
            if node_key is not None:
                self.store._node(conn, project_id, node_key)
                where += " AND node_key=?"
                params.append(node_key)
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM state_history_links WHERE {where} ORDER BY reference_id LIMIT ?", (*params, limit + 1))]
            return {"references": rows[:limit], "next_after": rows[limit - 1]["reference_id"] if len(rows) > limit else None}

    def projects(self, repo_path=None, after="", limit=100):
        integer(limit, "limit", 1, 200)
        if after:
            key(after, "after")
        if repo_path is not None:
            repo_path = str(Path(text(repo_path, "repo_path", 4000)).expanduser().resolve())
        with self.store.transaction() as conn:
            rows = conn.execute("SELECT p.*,COUNT(n.node_key) AS node_count,"
                                "COALESCE(SUM(n.status='VERIFIED'),0) AS verified_count "
                                "FROM state_projects p LEFT JOIN state_nodes n ON n.project_id=p.project_id "
                                "WHERE p.project_id>? GROUP BY p.project_id ORDER BY p.project_id", (after,)).fetchall()
            result = []
            for row in rows:
                p = dict(row)
                source = self._source(conn, p)
                if repo_path and source["repo_path"] != repo_path:
                    continue
                result.append({**p, "source": source, "policy": project_policy(conn, p["project_id"])})
                if len(result) > limit:
                    break
            return {"projects": result[:limit], "next_after": result[limit - 1]["project_id"] if len(result) > limit else None}

    def overview(self, project_id):
        with self.store.transaction() as conn:
            view = self.store._graph_view(conn, project_id)
            latest = {r["node_key"]: dict(r) for r in conn.execute("SELECT a.* FROM state_attempts a "
                      "JOIN (SELECT node_key,MAX(seq) AS last FROM state_attempts WHERE project_id=? GROUP BY node_key) b "
                      "ON a.seq=b.last", (project_id,))}
            count = {r["node_key"]: r["n"] for r in conn.execute("SELECT node_key,COUNT(*) AS n FROM state_attempts WHERE project_id=? GROUP BY node_key", (project_id,))}
            evidence = {}
            for row in conn.execute("SELECT e.attempt_id,e.criterion_id,e.verdict FROM state_evidence e JOIN "
                                    "(SELECT e.attempt_id,e.criterion_id,MAX(e.seq) AS last FROM state_evidence e "
                                    "JOIN state_attempts a ON a.attempt_id=e.attempt_id WHERE a.project_id=? "
                                    "GROUP BY e.attempt_id,e.criterion_id) v ON e.seq=v.last", (project_id,)):
                evidence.setdefault(row["attempt_id"], Counter())[row["verdict"]] += 1
            nodes = []
            for n in view["nodes"]:
                a = latest.get(n["node_key"])
                entry = {k: n[k] for k in ("node_key", "revision", "contract_hash", "status", "priority", "dependencies", "blocked_by", "readiness", "hold", "node_hold")}
                entry.update(title=n["goal"].splitlines()[0][:180], domain=n["node_key"].split(".")[0],
                             criteria_count=len(n["acceptance"]), attempt_count=count.get(n["node_key"], 0),
                             latest_attempt={k: a[k] for k in ATTEMPT_COLUMNS} if a else None,
                             latest_evidence=dict(evidence.get(a["attempt_id"], {})) if a else {})
                nodes.append(entry)
            seq = conn.execute("SELECT COALESCE(MAX(seq),0) FROM state_events WHERE project_id=?", (project_id,)).fetchone()[0]
            return {"project": view["project"], "source": self._source(conn, view["project"]),
                    "policy": project_policy(conn, project_id), "nodes": nodes,
                    "counts": dict(Counter(n["status"] for n in nodes)), "readiness_counts": dict(Counter(n["readiness"] for n in nodes)),
                    "event_seq": seq, "observed_at": now(), "run_state_mode": "persisted; explicit refresh observes workflow outcomes"}

    def project_attempts(self, project_id, after=0, limit=30):
        integer(after, "after", 0, 2**63-1)
        integer(limit, "limit", 1, 100)
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            cols = ",".join("a." + c for c in ATTEMPT_COLUMNS)
            rows = [dict(r) for r in conn.execute(f"SELECT {cols},n.goal FROM state_attempts a "
                    "JOIN state_nodes n ON n.project_id=a.project_id AND n.node_key=a.node_key "
                    "WHERE a.project_id=? AND (?=0 OR a.seq<?) ORDER BY a.seq DESC LIMIT ?", (project_id, after, after, limit + 1))]
            for row in rows:
                row["title"] = row.pop("goal").splitlines()[0][:180]
            return {"attempts": rows[:limit], "next_after": rows[limit - 1]["seq"] if len(rows) > limit else None}

    def run_owners(self, run_id):
        key(run_id, "run_id")
        with self.store.transaction() as conn:
            rows = [dict(r) for r in conn.execute("SELECT a.project_id,p.title,a.node_key,a.attempt_id,'attempt' AS relation "
                    "FROM state_attempts a JOIN state_projects p ON p.project_id=a.project_id WHERE a.run_id=?", (run_id,))]
            rows += [dict(r) for r in conn.execute("SELECT h.project_id,p.title,h.node_key,h.reference_id,'reference' AS relation "
                     "FROM state_history_links h JOIN state_projects p ON p.project_id=h.project_id WHERE h.kind='run' AND h.ref=?", (run_id,))]
            return {"links": rows, "run_id": run_id}

    def attempt_detail(self, attempt_id):
        with self.store.transaction() as conn:
            from core.state_attempts import StateAttempts, _public
            a = StateAttempts._attempt(conn, attempt_id)
            rows = [dict(r) for r in conn.execute("SELECT * FROM state_evidence WHERE attempt_id=? ORDER BY seq DESC LIMIT 201", (attempt_id,))]
            return {"attempt": _public(a), "evidence": list(reversed(rows[:200])), "evidence_truncated": len(rows) > 200,
                    "receipts": [dict(r) for r in conn.execute("SELECT * FROM state_acceptances WHERE attempt_id=? ORDER BY created_at DESC LIMIT 100", (attempt_id,))]}
