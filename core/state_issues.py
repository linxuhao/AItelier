"""Project-scoped issue backlog beside the State DAG.

A node is a claim about product state with an acceptance contract; an issue is
an observation (a defect, a coverage gap, a hand-off from another project, an
open question) that has not been triaged into one. Reporting an issue never
touches nodes, revisions, readiness or the frontier. Only a resolution can
point at the DAG, and each resolution must name the State record that absorbed
it, so "fixed" is checkable rather than asserted:

- absorbed:  a revision of an existing node, created AFTER the issue was
             reported (the contract changed in response to it);
- promoted:  a node whose first revision was created after the report;
- duplicate: another issue, or an existing node that already covers it;
- rejected:  a reason only.

An OPEN defect linked to a VERIFIED or CANDIDATE node contradicts that
acceptance; reads flag it as `contradicts_acceptance` instead of hiding it.
"""
from __future__ import annotations

import json
import uuid

from core.state_graph import StateConflict, StateGraphError, StateNotFound, canonical, digest, key, now, text

KINDS = ("defect", "gap", "handoff", "question")
RESOLUTIONS = ("absorbed", "promoted", "duplicate", "rejected")
MAX_LINKS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_issues (
    issue_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('defect','gap','handoff','question')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT,
    status TEXT NOT NULL CHECK(status IN ('open','absorbed','promoted','duplicate','rejected')),
    version INTEGER NOT NULL CHECK(version > 0),
    resolution_json TEXT,
    reported_by_actor TEXT NOT NULL,
    reported_by_director TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, request_key),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TABLE IF NOT EXISTS state_issue_nodes (
    issue_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    node_key TEXT NOT NULL,
    PRIMARY KEY(issue_id, node_key),
    FOREIGN KEY(issue_id) REFERENCES state_issues(issue_id),
    FOREIGN KEY(project_id, node_key) REFERENCES state_nodes(project_id, node_key)
);
CREATE INDEX IF NOT EXISTS state_issues_project ON state_issues(project_id, status, created_at);
CREATE INDEX IF NOT EXISTS state_issue_nodes_node ON state_issue_nodes(project_id, node_key);
"""


class StateIssues:
    def __init__(self, store, actor: str):
        self.store = store
        self.actor = text(actor, "authenticated actor", 320)
        with store.db.get_connection() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _node_keys(conn, project_id, node_keys) -> list[str]:
        if not isinstance(node_keys, list) or len(node_keys) > MAX_LINKS:
            raise StateGraphError(f"node_keys must be a list of at most {MAX_LINKS} node keys")
        keys = sorted({key(k, "node_key") for k in node_keys})
        for k in keys:
            if conn.execute("SELECT 1 FROM state_nodes WHERE project_id=? AND node_key=?",
                            (project_id, k)).fetchone() is None:
                raise StateNotFound(f"state node {project_id}/{k} not found")
        return keys

    @staticmethod
    def _issue(conn, project_id, issue_id):
        row = conn.execute("SELECT * FROM state_issues WHERE project_id=? AND issue_id=?",
                           (project_id, key(issue_id, "issue_id"))).fetchone()
        if row is None:
            raise StateNotFound(f"issue {project_id}/{issue_id} not found")
        return row

    @staticmethod
    def _links(conn, issue_id) -> list[str]:
        return [r[0] for r in conn.execute(
            "SELECT node_key FROM state_issue_nodes WHERE issue_id=? ORDER BY node_key", (issue_id,))]

    def _view(self, conn, row, *, full: bool) -> dict:
        links = self._links(conn, row["issue_id"])
        statuses = {r["node_key"]: r["status"] for r in conn.execute(
            "SELECT n.node_key,n.status FROM state_nodes n JOIN state_issue_nodes l "
            "ON l.project_id=n.project_id AND l.node_key=n.node_key WHERE l.issue_id=?", (row["issue_id"],))}
        item = {
            "issue_id": row["issue_id"], "project_id": row["project_id"], "kind": row["kind"],
            "title": row["title"], "status": row["status"], "version": row["version"],
            "nodes": [{"node_key": k, "status": statuses.get(k)} for k in links],
            "contradicts_acceptance": sorted(
                k for k in links if row["status"] == "open" and row["kind"] == "defect"
                and statuses.get(k) in {"VERIFIED", "CANDIDATE"}),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        if full:
            item.update(body=row["body"], source=row["source"],
                        resolution=json.loads(row["resolution_json"]) if row["resolution_json"] else None,
                        reported_by={"actor": row["reported_by_actor"],
                                     "director_identity": row["reported_by_director"]})
        return item

    # -- reads -----------------------------------------------------------
    def get(self, project_id: str, issue_id: str) -> dict:
        project_id = key(project_id, "project_id")
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            return self._view(conn, self._issue(conn, project_id, issue_id), full=True)

    def list(self, project_id: str, statuses: list[str] | None = None, kinds: list[str] | None = None,
             node_key: str | None = None, after: int = 0, limit: int = 50) -> dict:
        """Bounded summaries (no bodies), oldest first; continue with next_after."""
        project_id = key(project_id, "project_id")
        clauses, args = ["i.project_id=?", "i.rowid>?"], [project_id, after]
        for column, values, allowed in (("status", statuses, ("open",) + RESOLUTIONS), ("kind", kinds, KINDS)):
            if values is not None:
                if set(values) - set(allowed):
                    raise StateGraphError(f"{column} filter accepts only {', '.join(allowed)}")
                clauses.append(f"i.{column} IN (" + ",".join("?" for _ in values) + ")")
                args.extend(values)
        if node_key is not None:
            clauses.append("EXISTS (SELECT 1 FROM state_issue_nodes l WHERE l.issue_id=i.issue_id AND l.node_key=?)")
            args.append(key(node_key, "node_key"))
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            rows = conn.execute("SELECT i.rowid AS seq,i.* FROM state_issues i WHERE " + " AND ".join(clauses) +
                                " ORDER BY i.rowid LIMIT ?", [*args, limit + 1]).fetchall()
            issues = [self._view(conn, r, full=False) for r in rows[:limit]]
            counts = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status,COUNT(*) AS n FROM state_issues WHERE project_id=? GROUP BY status", (project_id,))}
        return {"project_id": project_id, "issues": issues, "status_counts": counts,
                "has_more": len(rows) > limit,
                "next_after": rows[limit - 1]["seq"] if len(rows) > limit else (rows[-1]["seq"] if rows else after)}

    def open_for_node(self, conn, project_id: str, node_key: str) -> list[dict]:
        rows = conn.execute("SELECT i.* FROM state_issues i JOIN state_issue_nodes l ON l.issue_id=i.issue_id "
                            "WHERE l.project_id=? AND l.node_key=? AND i.status='open' ORDER BY i.rowid",
                            (project_id, node_key)).fetchall()
        return [self._view(conn, r, full=False) for r in rows]

    # -- writes ----------------------------------------------------------
    def report(self, project_id: str, request_key: str, kind: str, title: str, body: str,
               director_identity: str, node_keys: list[str] | None = None, source: str | None = None) -> dict:
        project_id = key(project_id, "project_id")
        request_key = text(request_key, "request_key", 200)
        if kind not in KINDS:
            raise StateGraphError(f"kind must be one of {', '.join(KINDS)}")
        title = text(title, "title", 300)
        body = text(body, "body", 20000)
        director_identity = text(director_identity, "director_identity", 320)
        if source is not None:
            source = text(source, "source", 400)
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            links = self._node_keys(conn, project_id, node_keys or [])
            request_hash = digest({"kind": kind, "title": title, "body": body,
                                   "node_keys": links, "source": source})
            existing = conn.execute("SELECT * FROM state_issues WHERE project_id=? AND request_key=?",
                                    (project_id, request_key)).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise StateConflict("request_key was already used for a different issue report")
                return {**self._view(conn, existing, full=True), "created": False}
            issue_id = "iss-" + uuid.uuid4().hex[:16]
            timestamp = now()
            conn.execute("INSERT INTO state_issues VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (issue_id, project_id, request_key, request_hash, kind, title, body, source,
                          "open", 1, None, self.actor, director_identity, timestamp, timestamp))
            conn.executemany("INSERT INTO state_issue_nodes VALUES(?,?,?)",
                             [(issue_id, project_id, k) for k in links])
            self.store._event(conn, project_id, None, "issue_reported", {
                "issue_id": issue_id, "kind": kind, "title": title, "node_keys": links})
            return {**self._view(conn, self._issue(conn, project_id, issue_id), full=True), "created": True}

    def _open_for_write(self, conn, project_id, issue_id, expected_version):
        self.store._project(conn, project_id)
        row = self._issue(conn, project_id, issue_id)
        if row["version"] != expected_version:
            raise StateConflict(f"issue version conflict: expected {expected_version}, current {row['version']}; "
                                "read the issue and retry")
        if row["status"] != "open":
            raise StateConflict(f"issue is already {row['status']}; report a new issue instead of reopening")
        return row

    def link(self, project_id: str, issue_id: str, expected_version: int, node_keys: list[str],
             reason: str) -> dict:
        """Replace the linked node set of an OPEN issue. Nodes are not changed."""
        project_id = key(project_id, "project_id")
        reason = text(reason, "reason", 4000)
        with self.store.transaction(write=True) as conn:
            row = self._open_for_write(conn, project_id, issue_id, expected_version)
            links = self._node_keys(conn, project_id, node_keys)
            conn.execute("DELETE FROM state_issue_nodes WHERE issue_id=?", (row["issue_id"],))
            conn.executemany("INSERT INTO state_issue_nodes VALUES(?,?,?)",
                             [(row["issue_id"], project_id, k) for k in links])
            conn.execute("UPDATE state_issues SET version=version+1,updated_at=? WHERE issue_id=?",
                         (now(), row["issue_id"]))
            self.store._event(conn, project_id, None, "issue_linked", {
                "issue_id": row["issue_id"], "node_keys": links, "reason": reason})
            return self._view(conn, self._issue(conn, project_id, issue_id), full=True)

    def resolve(self, project_id: str, issue_id: str, expected_version: int, resolution: str,
                reason: str, director_identity: str, node_key: str | None = None,
                node_revision: int | None = None, duplicate_of: str | None = None) -> dict:
        project_id = key(project_id, "project_id")
        if resolution not in RESOLUTIONS:
            raise StateGraphError(f"resolution must be one of {', '.join(RESOLUTIONS)}")
        reason = text(reason, "reason", 4000)
        director_identity = text(director_identity, "director_identity", 320)
        shape = {"absorbed": {"node_key", "node_revision"}, "promoted": {"node_key"},
                 "rejected": set()}.get(resolution)
        given = {n for n, v in (("node_key", node_key), ("node_revision", node_revision),
                                ("duplicate_of", duplicate_of)) if v is not None}
        if resolution == "duplicate":
            if len(given) != 1 or given == {"node_revision"}:
                raise StateGraphError("duplicate takes exactly one of duplicate_of (an issue) or node_key")
        elif given != shape:
            raise StateGraphError(f"{resolution} takes exactly: {', '.join(sorted(shape)) or 'reason only'}")
        with self.store.transaction(write=True) as conn:
            row = self._open_for_write(conn, project_id, issue_id, expected_version)
            record = {"resolution": resolution, "reason": reason, "actor": self.actor,
                      "director_identity": director_identity}
            if node_key is not None:
                node = self.store._node(conn, project_id, node_key)
                wanted = node_revision if resolution == "absorbed" else 1
                if resolution != "duplicate":
                    rev = conn.execute("SELECT created_at FROM state_node_revisions "
                                       "WHERE project_id=? AND node_key=? AND revision=?",
                                       (project_id, node["node_key"], wanted)).fetchone()
                    if rev is None:
                        raise StateNotFound(f"node {node['node_key']} has no revision {wanted}")
                    if rev["created_at"] < row["created_at"]:
                        what = "that revision" if resolution == "absorbed" else "that node"
                        raise StateConflict(
                            f"{what} predates the issue, so it cannot be the change made for it; "
                            "revise the contract (absorbed) or create the node (promoted) first, "
                            "or resolve as duplicate if the node already covered it")
                    record["node_revision"] = wanted
                record["node_key"] = node["node_key"]
                conn.execute("INSERT OR IGNORE INTO state_issue_nodes VALUES(?,?,?)",
                             (row["issue_id"], project_id, node["node_key"]))
            if duplicate_of is not None:
                if duplicate_of == row["issue_id"]:
                    raise StateGraphError("an issue cannot duplicate itself")
                record["duplicate_of"] = self._issue(conn, project_id, duplicate_of)["issue_id"]
            conn.execute("UPDATE state_issues SET status=?,version=version+1,resolution_json=?,updated_at=? "
                         "WHERE issue_id=?", (resolution, canonical(record), now(), row["issue_id"]))
            self.store._event(conn, project_id, record.get("node_key"), "issue_resolved", {
                "issue_id": row["issue_id"], **{k: v for k, v in record.items() if k != "actor"}})
            return self._view(conn, self._issue(conn, project_id, issue_id), full=True)
