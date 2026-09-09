"""Persistent project facts, not workflow execution state.

A state node is a revisioned goal with a contract. SkillFlow owns attempts and
steps; this store owns dependency validity. All writes are serialized SQLite
transactions. Revisions and events are append-only, including after supersession.
No default database, agent calls, Git mutations or production imports live here.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
KINDS = frozenset({"test", "review", "human", "artifact", "integration"})
# A facet says what a node IS, so the graph can say what may be built on it.
# Only contracts and design facts may be depended upon; an implementation is
# nobody else's business until an integration node composes it. NULL = legacy,
# exempt, so a graph migrates one chain at a time. See design/state_facets.md.
FACETS = frozenset({"design", "contract", "test", "content", "integration"})
BUILDABLE = frozenset({"design", "contract"})
FACET_SUFFIXES = {".contract": "contract", ".test": "test"}
MAX_NODES = 1000
READY_ACTIONS = ("candidate_review", "new_attempt")


def ready_next_action(status: str, readiness: str) -> str | None:
    if readiness != "ready":
        return None
    if status == "CANDIDATE":
        return "candidate_review"
    if status in {"OPEN", "STALE"}:
        return "new_attempt"
    return None


def ready_action_counts(nodes: list[dict]) -> dict[str, int]:
    counts = {action: 0 for action in READY_ACTIONS}
    for node in nodes:
        action = node.get("next_action")
        if action in counts:
            counts[action] += 1
    return counts


class StateGraphError(ValueError):
    """Invalid graph operation; callers must not treat it as a successful write."""


class StateConflict(StateGraphError):
    """Stale revision or an operation incompatible with current project facts."""


class StateNotFound(StateGraphError):
    """An exact project/node/attempt identity does not exist."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def key(value: str, label: str = "key") -> str:
    if not isinstance(value, str) or not KEY.fullmatch(value):
        raise StateGraphError(f"{label} must be 1-128 letters/digits/._-, starting with a letter or digit")
    return value


def text(value: str, label: str, maximum: int = 20000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StateGraphError(f"{label} must be nonempty text of at most {maximum} characters")
    return value.strip()


def integer(value: int, label: str, low: int = 0, high: int = 1000000) -> int:
    if type(value) is not int or not low <= value <= high:
        raise StateGraphError(f"{label} must be an integer between {low} and {high}")
    return value


def contract(value: list[dict]) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise StateGraphError("acceptance requires 1-64 explicit checks; an empty contract cannot pass")
    checks, seen = [], set()
    for check in value:
        if not isinstance(check, dict) or set(check) - {"id", "kind", "description"}:
            raise StateGraphError("each check must contain only id, kind, description")
        cid = key(check.get("id"), "check id")
        kind = check.get("kind")
        if not isinstance(kind, str) or kind not in KINDS or cid in seen:
            raise StateGraphError("check kinds must be supported and IDs unique")
        seen.add(cid)
        checks.append({"id": cid, "kind": kind,
                       "description": text(check.get("description"), "check description", 2000)})
    return sorted(checks, key=lambda check: check["id"])


def dependencies(value: list[str]) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_NODES:
        raise StateGraphError("dependencies must be a bounded list")
    result = [key(v, "dependency") for v in value]
    if len(set(result)) != len(result):
        raise StateGraphError("duplicate dependencies")
    return sorted(result)


def _validate_dag(graph: dict[str, list[str]]) -> None:
    """Kahn traversal avoids recursion limits and rejects dangling references."""
    if len(graph) > MAX_NODES:
        raise StateGraphError(f"a project supports at most {MAX_NODES} nodes")
    degree, children = {}, {k: [] for k in graph}
    for node, deps in graph.items():
        if node in deps:
            raise StateGraphError("a node cannot depend on itself")
        for dep in deps:
            if dep not in graph:
                raise StateGraphError(f"unknown dependency {dep!r} in this project")
            children[dep].append(node)
        degree[node] = len(deps)
    queue = deque(k for k, count in degree.items() if not count)
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for child in children[node]:
            degree[child] -= 1
            if degree[child] == 0:
                queue.append(child)
    if visited != len(graph):
        raise StateGraphError("dependency cycle; use a workflow loop, not a cyclic state dependency")


def check_facet(value, label: str = "facet"):
    if value is None:
        return None
    if not isinstance(value, str) or value not in FACETS:
        raise StateGraphError(f"{label} must be one of {sorted(FACETS)}")
    return value


def facet_violations(facets: dict[str, str | None], graph: dict[str, list[str]]) -> list[str]:
    """Every rule about facets, as messages that name the fix.

    A rule the director cannot repair from its error text costs one rework
    round per violation forever, so each message says what to create or
    re-point. Pure: the store calls it inside a transaction, `facet_lint`
    calls it on a snapshot, and the migration dry-run calls it on a plan.
    """
    problems = []
    for node, own in facets.items():
        for suffix, expected in FACET_SUFFIXES.items():
            if node.endswith(suffix) and own is not None and own != expected:
                problems.append(f"`{node}` ends with {suffix} so its facet must be {expected}, not {own}")
        if own is None:
            continue
        stem = node[:-5] if node.endswith(".test") else node
        deps = set(graph.get(node, ()))
        for dep in sorted(deps):
            target = facets.get(dep)
            if target is None:
                problems.append(f"`{node}` ({own}) depends on `{dep}`, which has no facet yet; "
                                f"set_node_facet on `{dep}` first — dependencies are faceted bottom-up")
            elif own == "integration" or target in BUILDABLE:
                continue
            elif dep in (node + ".test", node + ".contract") or (node.endswith(".test") and dep == stem + ".contract"):
                continue
            else:
                problems.append(f"`{node}` ({own}) cannot depend on `{dep}` ({target}): only contract/design nodes may be "
                                f"built on. Create `{dep}.contract` (facet contract) holding {dep}'s interface + stub/fake and "
                                f"point this edge at it, or make `{node}` facet integration if it truly needs {dep}'s implementation")
        if own in {"content", "test"}:
            for sibling in (stem + ".contract",) + ((node + ".test",) if own == "content" else ()):
                if sibling in facets and sibling != node and sibling not in deps:
                    problems.append(f"`{node}` has `{sibling}` but does not depend on it; add the dependency so "
                                    f"contract → test → implementation is the accepted order")
    return problems


def facet_stem(node_key: str, facets: dict[str, str | None]) -> str:
    """`x.contract` / `x.test` belong to `x`; everything else is its own goal."""
    if facets.get(node_key) == "contract" and node_key.endswith(".contract"):
        return node_key[:-len(".contract")]
    if facets.get(node_key) == "test" and node_key.endswith(".test"):
        return node_key[:-len(".test")]
    return node_key


def shipping_gaps(facets: dict[str, str | None], graph: dict[str, list[str]]) -> dict[str, list[str]]:
    """R3 — what each integration node composes but forgot to depend on.

    R1 turns `B → A` into `B → A.contract`, and with it the old transitive
    "everything below must be built" property disappears: a release gate that
    used to reach twelve implementations through implementation chains now
    reaches their contracts and nothing else, and R1/R2 cannot see it (found by
    the director's review, 2026-09-09). The rule that restores it: an
    integration node depends directly on the implementation of every contract
    in its transitive closure. Reported as a warning, not a rejection — an
    integration node is edited one edge at a time, and a rule that cannot be
    satisfied incrementally is a rule that gets worked around.
    """
    gaps = {}
    for node, own in facets.items():
        if own != "integration":
            continue
        seen, stack = set(), list(graph.get(node, ()))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, ()))
        direct = set(graph.get(node, ()))
        missing = sorted({facet_stem(c, facets) for c in seen if facets.get(c) == "contract"
                          and facet_stem(c, facets) in facets and facets.get(facet_stem(c, facets)) != "contract"
                          and facet_stem(c, facets) != node and facet_stem(c, facets) not in direct})
        if missing:
            gaps[node] = missing
    return gaps


def facet_warnings(facets: dict[str, str | None], graph: dict[str, list[str]]) -> list[str]:
    return [f"`{node}` (integration) builds on the contracts of {', '.join('`' + m + '`' for m in missing)} but does not "
            f"depend on their implementations; add those edges or the release closure silently drops them"
            for node, missing in shipping_gaps(facets, graph).items()]


SCHEMA = """
CREATE TABLE IF NOT EXISTS state_projects (
    project_id TEXT PRIMARY KEY, title TEXT NOT NULL,
    source_project_id TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_nodes (
    project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    goal TEXT NOT NULL, contract_json TEXT NOT NULL, contract_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN','CANDIDATE','VERIFIED','STALE','SUPERSEDED')),
    priority INTEGER NOT NULL DEFAULT 0, verified_receipt TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,node_key),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TABLE IF NOT EXISTS state_dependencies (
    project_id TEXT NOT NULL, node_key TEXT NOT NULL, dependency_key TEXT NOT NULL,
    PRIMARY KEY(project_id,node_key,dependency_key),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key),
    FOREIGN KEY(project_id,dependency_key) REFERENCES state_nodes(project_id,node_key),
    CHECK(node_key != dependency_key)
);
CREATE TABLE IF NOT EXISTS state_node_revisions (
    project_id TEXT NOT NULL, node_key TEXT NOT NULL, revision INTEGER NOT NULL,
    goal TEXT NOT NULL, contract_json TEXT NOT NULL, contract_hash TEXT NOT NULL,
    dependencies_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,node_key,revision),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key)
);
CREATE TABLE IF NOT EXISTS state_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    node_key TEXT, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE INDEX IF NOT EXISTS state_events_project ON state_events(project_id,seq);
CREATE INDEX IF NOT EXISTS state_dependencies_reverse ON state_dependencies(project_id,dependency_key);
CREATE TRIGGER IF NOT EXISTS state_events_no_update BEFORE UPDATE ON state_events
BEGIN SELECT RAISE(ABORT,'state events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_events_no_delete BEFORE DELETE ON state_events
BEGIN SELECT RAISE(ABORT,'state events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_revisions_no_update BEFORE UPDATE ON state_node_revisions
BEGIN SELECT RAISE(ABORT,'state revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_revisions_no_delete BEFORE DELETE ON state_node_revisions
BEGIN SELECT RAISE(ABORT,'state revisions are append-only'); END;
"""


class StateGraphStore:
    def __init__(self, db):
        """Use an explicitly supplied DBManager; never resolve a production path."""
        self.db = db
        with db.get_connection() as conn:
            from core.state_metadata import SCHEMA as METADATA_SCHEMA
            conn.executescript(SCHEMA + METADATA_SCHEMA)
            # Additive: existing rows stay NULL (legacy, exempt from facet rules).
            if "facet" not in {r["name"] for r in conn.execute("PRAGMA table_info(state_nodes)")}:
                conn.execute("ALTER TABLE state_nodes ADD COLUMN facet TEXT "
                             "CHECK(facet IN ('design','contract','test','content','integration'))")
            conn.commit()

    @contextmanager
    def transaction(self, *, write: bool = False):
        with self.db.get_connection() as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            before_event = conn.execute("SELECT COALESCE(MAX(seq),0) FROM state_events").fetchone()[0] if write else None
            try:
                yield conn
                after_event = conn.execute("SELECT COALESCE(MAX(seq),0) FROM state_events").fetchone()[0] if write else None
                conn.commit()
                if write and after_event != before_event:
                    from core.state_changes import notify
                    notify(self.db.db_path)
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _project(conn, project_id):
        row = conn.execute("SELECT * FROM state_projects WHERE project_id=?", (key(project_id),)).fetchone()
        if row is None:
            raise StateNotFound(f"state project {project_id!r} not found")
        return dict(row)

    @staticmethod
    def _node(conn, project_id, node_key):
        row = conn.execute("SELECT * FROM state_nodes WHERE project_id=? AND node_key=?",
                           (key(project_id), key(node_key))).fetchone()
        if row is None:
            raise StateNotFound(f"state node {project_id}/{node_key} not found")
        return dict(row)

    @staticmethod
    def _event(conn, project_id, node_key, event_type, payload):
        conn.execute("INSERT INTO state_events(project_id,node_key,event_type,payload_json,created_at) "
                     "VALUES(?,?,?,?,?)", (project_id, node_key, event_type, canonical(payload), now()))

    @staticmethod
    def _graph(conn, project_id):
        nodes = {r["node_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM state_nodes WHERE project_id=?", (project_id,))}
        edges = {k: [] for k in nodes}
        for row in conn.execute("SELECT node_key,dependency_key FROM state_dependencies WHERE project_id=? "
                                "ORDER BY dependency_key", (project_id,)):
            edges[row["node_key"]].append(row["dependency_key"])
        return nodes, edges

    @staticmethod
    def _snapshot_revision(conn, node, deps):
        conn.execute("INSERT INTO state_node_revisions VALUES(?,?,?,?,?,?,?,?)",
                     (node["project_id"], node["node_key"], node["revision"], node["goal"],
                      node["contract_json"], node["contract_hash"], canonical(deps), now()))

    def create_project(self, project_id: str, title: str, source_project_id: str | None = None) -> dict:
        key(project_id)
        title = text(title, "title", 400)
        if source_project_id is not None:
            key(source_project_id, "source project")
        with self.transaction(write=True) as conn:
            existing = conn.execute("SELECT * FROM state_projects WHERE project_id=?", (project_id,)).fetchone()
            if existing:
                if existing["title"] != title or existing["source_project_id"] != source_project_id:
                    raise StateConflict("project identity already exists with different settings")
                return dict(existing)
            conn.execute("INSERT INTO state_projects VALUES(?,?,?,?)", (project_id, title, source_project_id, now()))
            self._event(conn, project_id, None, "project_created", {"title": title, "source_project_id": source_project_id})
            return self._project(conn, project_id)

    def list_projects(self) -> list[dict]:
        with self.transaction() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM state_projects ORDER BY project_id")]

    def get_project(self, project_id: str) -> dict:
        with self.transaction() as conn:
            return self._project(conn, project_id)

    def _add(self, conn, project_id, specs):
        self._project(conn, project_id)
        if not isinstance(specs, list) or not 1 <= len(specs) <= 200:
            raise StateGraphError("add_nodes requires 1-200 nodes per transaction")
        old, graph = self._graph(conn, project_id)
        facets = {k: n.get("facet") for k, n in old.items()}
        prepared = []
        for spec in specs:
            if not isinstance(spec, dict) or set(spec) - {"key", "goal", "acceptance", "dependencies", "priority", "facet"}:
                raise StateGraphError("unknown node fields; workflow progress is not a state-node property")
            nk = key(spec.get("key"), "node key")
            if nk in graph:
                raise StateConflict(f"duplicate node key {nk!r}")
            goal = text(spec.get("goal"), "goal")
            checks = contract(spec.get("acceptance"))
            deps = dependencies(spec.get("dependencies", []))
            priority = integer(spec.get("priority", 0), "priority", -100000, 100000)
            own = check_facet(spec.get("facet"))
            if any(old.get(d, {}).get("status") == "SUPERSEDED" for d in deps):
                raise StateConflict("cannot depend on a superseded node")
            graph[nk] = deps
            facets[nk] = own
            prepared.append((nk, goal, checks, deps, priority, own))
        _validate_dag(graph)
        self._check_facets(facets, graph)
        # All nodes first: forward references in a batch are legitimate.
        for nk, goal, checks, deps, priority, own in prepared:
            stamp = now()
            conn.execute("INSERT INTO state_nodes(project_id,node_key,revision,goal,contract_json,contract_hash,status,"
                         "priority,verified_receipt,created_at,updated_at,facet) VALUES(?,?,?,?,?,?,'OPEN',?,NULL,?,?,?)",
                         (project_id, nk, 1, goal, canonical(checks), digest(checks), priority, stamp, stamp, own))
        for nk, goal, checks, deps, priority, own in prepared:
            conn.executemany("INSERT INTO state_dependencies VALUES(?,?,?)", [(project_id, nk, dep) for dep in deps])
            self._snapshot_revision(conn, self._node(conn, project_id, nk), deps)
            self._event(conn, project_id, nk, "node_created", {"revision": 1, "dependencies": deps,
                                                                "contract_hash": digest(checks), "facet": own})
        return [nk for nk, *_ in prepared]

    @staticmethod
    def _check_facets(facets, graph):
        problems = facet_violations(facets, graph)
        if problems:
            raise StateGraphError("facet rules: " + " | ".join(problems[:5]) +
                                  (f" | (+{len(problems) - 5} more; see facet_lint)" if len(problems) > 5 else ""))

    def set_node_facet(self, project_id: str, node_key: str, facet: str) -> dict:
        """Label a node. No revision: a label is not a contract change.

        NULL → value is the migration path. Re-labelling is allowed only while
        nothing has been ACCEPTED under the old label — a verified receipt was
        granted against what the node was, and re-judging that is a new node's
        job — and only if every edge in the graph is still legal afterwards,
        so a node others already build on cannot quietly become `content`.
        (The first version refused any re-label; the director's review then
        found a package gate migrated as `content`, and superseding a node to
        fix a label is the wrong size of correction.)
        """
        own = check_facet(facet)
        if own is None:
            raise StateGraphError("facet is required")
        with self.transaction(write=True) as conn:
            node = self._node(conn, project_id, node_key)
            previous = node.get("facet")
            if previous == own:
                return {"key": node_key, "facet": own, "changed": False}
            if previous is not None and node.get("verified_receipt"):
                raise StateConflict(f"`{node_key}` was accepted as {previous}; a verified node keeps its facet — "
                                    f"supersede it and create the {own} node")
            nodes, graph = self._graph(conn, project_id)
            facets = {k: n.get("facet") for k, n in nodes.items()}
            facets[node_key] = own
            # Dependents were accepted against the old label (or none); every
            # edge must still be legal under the new one.
            self._check_facets(facets, graph)
            conn.execute("UPDATE state_nodes SET facet=?,updated_at=? WHERE project_id=? AND node_key=?",
                         (own, now(), project_id, node_key))
            self._event(conn, project_id, node_key, "node_facet_set",
                        {"facet": own, "previous": previous, "revision": node["revision"]})
            return {"key": node_key, "facet": own, "previous": previous, "changed": True}

    def facet_lint(self, project_id: str) -> dict:
        """The same rules, read-only, over the whole project including legacy nodes."""
        with self.transaction() as conn:
            self._project(conn, project_id)
            nodes, graph = self._graph(conn, project_id)
        facets = {k: n.get("facet") for k, n in nodes.items()}
        return {"violations": facet_violations(facets, graph), "warnings": facet_warnings(facets, graph),
                "shipping_gaps": shipping_gaps(facets, graph),
                "faceted": sum(1 for f in facets.values() if f), "legacy": sum(1 for f in facets.values() if not f),
                "facets": {k: f for k, f in sorted(facets.items()) if f}}

    def add_nodes(self, project_id: str, nodes: list[dict]) -> dict:
        with self.transaction(write=True) as conn:
            keys = self._add(conn, project_id, nodes)
        return {"created": keys}

    @staticmethod
    def _invalidate(conn, project_id, node_key):
        rows = conn.execute("WITH RECURSIVE affected(k) AS (SELECT ? UNION "
                            "SELECT d.node_key FROM state_dependencies d JOIN affected a ON d.dependency_key=a.k "
                            "WHERE d.project_id=?) SELECT k FROM affected", (node_key, project_id)).fetchall()
        keys = sorted(r[0] for r in rows)
        conn.executemany("UPDATE state_nodes SET status='STALE',verified_receipt=NULL,updated_at=? "
                         "WHERE project_id=? AND node_key=? AND status!='SUPERSEDED'",
                         [(now(), project_id, k) for k in keys])
        return keys

    def _revise(self, conn, project_id, node_key, expected_revision, *, reason, goal=None, acceptance=None, deps=None):
        old = self._node(conn, project_id, node_key)
        integer(expected_revision, "expected_revision", 1)
        reason = text(reason, "revision reason", 4000)
        if old["revision"] != expected_revision:
            raise StateConflict("node revision changed; reload before editing")
        if old["status"] == "SUPERSEDED":
            raise StateConflict("a superseded node cannot be revised")
        nodes, graph = self._graph(conn, project_id)
        if deps is not None:
            graph[node_key] = dependencies(deps)
        if any(nodes[d]["status"] == "SUPERSEDED" for d in graph[node_key] if d in nodes):
            raise StateConflict("cannot depend on a superseded node")
        _validate_dag(graph)
        self._check_facets({k: n.get("facet") for k, n in nodes.items()}, graph)
        new_goal = text(goal, "goal") if goal is not None else old["goal"]
        checks = contract(acceptance) if acceptance is not None else json.loads(old["contract_json"])
        revision = expected_revision + 1
        affected = self._invalidate(conn, project_id, node_key)
        conn.execute("UPDATE state_nodes SET revision=?,goal=?,contract_json=?,contract_hash=?,updated_at=? "
                     "WHERE project_id=? AND node_key=?",
                     (revision, new_goal, canonical(checks), digest(checks), now(), project_id, node_key))
        conn.execute("DELETE FROM state_dependencies WHERE project_id=? AND node_key=?", (project_id, node_key))
        conn.executemany("INSERT INTO state_dependencies VALUES(?,?,?)", [(project_id, node_key, dep) for dep in graph[node_key]])
        self._snapshot_revision(conn, self._node(conn, project_id, node_key), graph[node_key])
        self._event(conn, project_id, node_key, "node_revised", {"revision": revision, "reason": reason, "invalidated": affected})
        return {"key": node_key, "revision": revision, "invalidated": affected}

    def revise_node(self, project_id: str, node_key: str, expected_revision: int, reason: str,
                    *, goal: str | None = None, acceptance: list[dict] | None = None,
                    dependencies: list[str] | None = None) -> dict:
        with self.transaction(write=True) as conn:
            return self._revise(conn, project_id, node_key, expected_revision, reason=reason,
                                goal=goal, acceptance=acceptance, deps=dependencies)

    def split_node(self, project_id: str, node_key: str, expected_revision: int,
                   children: list[dict], reason: str) -> dict:
        """Add child goals and make the parent depend on them, atomically."""
        with self.transaction(write=True) as conn:
            self._node(conn, project_id, node_key)
            new_keys = self._add(conn, project_id, children)
            _, graph = self._graph(conn, project_id)
            result = self._revise(conn, project_id, node_key, expected_revision, reason=reason,
                                  deps=sorted(set(graph[node_key]) | set(new_keys)))
            return {**result, "children": new_keys}

    def supersede_node(self, project_id: str, node_key: str, expected_revision: int, reason: str) -> dict:
        with self.transaction(write=True) as conn:
            old = self._node(conn, project_id, node_key)
            integer(expected_revision, "expected_revision", 1)
            reason = text(reason, "supersession reason", 4000)
            if old["revision"] != expected_revision:
                raise StateConflict("node revision changed")
            if old["status"] == "SUPERSEDED":
                return {"key": node_key, "status": "SUPERSEDED", "changed": False}
            affected = self._invalidate(conn, project_id, node_key)
            conn.execute("UPDATE state_nodes SET status='SUPERSEDED',updated_at=? WHERE project_id=? AND node_key=?",
                         (now(), project_id, node_key))
            self._event(conn, project_id, node_key, "node_superseded", {"reason": reason, "invalidated": affected})
            return {"key": node_key, "status": "SUPERSEDED", "invalidated": affected}

    @staticmethod
    def dependency_snapshot(conn, project_id, node_key) -> dict:
        rows = conn.execute("SELECT n.node_key,n.revision,n.contract_hash,n.status,n.verified_receipt "
                            "FROM state_nodes n JOIN state_dependencies d "
                            "ON n.project_id=d.project_id AND n.node_key=d.dependency_key "
                            "WHERE d.project_id=? AND d.node_key=? ORDER BY n.node_key",
                            (project_id, node_key)).fetchall()
        return {r["node_key"]: dict(r) for r in rows}

    def _graph_view(self, conn, project_id):
        from core.state_metadata import project_policy
        project = self._project(conn, project_id)
        nodes, graph = self._graph(conn, project_id)
        policy = project_policy(conn, project_id)
        holds = {r["node_key"]: dict(r) for r in conn.execute(
            "SELECT * FROM state_node_holds WHERE project_id=?", (project_id,))}
        active = set()
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_attempts'").fetchone():
            active = {r[0] for r in conn.execute("SELECT node_key FROM state_attempts WHERE project_id=? "
                      "AND status IN ('reserved','launching','running','paused','unknown')", (project_id,))}
        result = []
        for nk, node in nodes.items():
            blocked_by = [d for d in graph[nk] if nodes[d]["status"] != "VERIFIED" or not nodes[d]["verified_receipt"]]
            own_hold = holds.get(nk, {"revision": 0, "held": 0, "reason": ""})
            hold = ({"scope": "project", **policy} if policy["dispatch"] != "active" else
                    {"scope": "node", **own_hold} if own_hold["held"] else None)
            item = dict(node)
            item["acceptance"] = json.loads(item.pop("contract_json"))
            item["dependencies"] = graph[nk]
            item["blocked_by"] = blocked_by
            item["hold"], item["node_hold"] = hold, own_hold
            item["readiness"] = ("closed" if node["status"] in {"VERIFIED", "SUPERSEDED"}
                                 else "in_progress" if nk in active else "held" if hold
                                 else "blocked" if blocked_by else "ready")
            item["next_action"] = ready_next_action(node["status"], item["readiness"])
            result.append(item)
        return {"project": project, "nodes": sorted(result, key=lambda n: (-n["priority"], n["node_key"]))}

    def get_graph(self, project_id: str) -> dict:
        with self.transaction() as conn:
            return self._graph_view(conn, project_id)

    def get_node(self, project_id: str, node_key: str) -> dict:
        key(node_key)
        for node in self.get_graph(project_id)["nodes"]:
            if node["node_key"] == node_key:
                return node
        raise StateNotFound(f"state node {project_id}/{node_key} not found")

    def frontier(self, project_id: str, limit: int = 30) -> dict:
        integer(limit, "limit", 1, 200)
        ready = [n for n in self.get_graph(project_id)["nodes"] if n["readiness"] == "ready"]
        # Frontier is a selection surface, not a dump of every acceptance
        # document. Pull get_node only for the goal the driver chooses.
        summary = []
        for node in ready[:limit]:
            entry = {field: node[field] for field in ("node_key", "revision", "status", "priority",
                                                     "contract_hash", "dependencies", "readiness", "next_action")}
            entry["goal"] = node["goal"][:1200]
            entry["goal_truncated"] = len(node["goal"]) > 1200
            summary.append(entry)
        return {"nodes": summary, "total": len(ready), "truncated": len(ready) > limit,
                "ready_action_counts": ready_action_counts(ready)}

    def events(self, project_id: str, after: int = 0, limit: int = 100) -> list[dict]:
        integer(after, "after", 0, 2**63-1)
        integer(limit, "limit", 1, 500)
        with self.transaction() as conn:
            self._project(conn, project_id)
            result = []
            for row in conn.execute("SELECT * FROM state_events WHERE project_id=? AND seq>? ORDER BY seq LIMIT ?",
                                    (project_id, after, limit)):
                item = dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result
