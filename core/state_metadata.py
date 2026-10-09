"""Additive State Project metadata, independent of workflow-engine tables."""
SCHEMA = """
CREATE TABLE IF NOT EXISTS state_project_policy (
    project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
    dispatch TEXT NOT NULL CHECK(dispatch IN ('active','hold','archive')),
    reason TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
-- Multi-driver P3 (design §7.3): claims are ENFORCED only behind this second
-- switch (owner decision 2026-10-09: set_multi_driver alone keeps P1's
-- record-only behaviour). Its own row, so the policy table keeps its shape;
-- it shares the policy revision for the CAS. No row = off.
CREATE TABLE IF NOT EXISTS state_project_enforcement (
    project_id TEXT PRIMARY KEY,
    claim_enforcement TEXT NOT NULL CHECK(claim_enforcement IN ('off','on')),
    reason TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TABLE IF NOT EXISTS state_node_holds (
    project_id TEXT NOT NULL, node_key TEXT NOT NULL, revision INTEGER NOT NULL,
    held INTEGER NOT NULL CHECK(held IN (0,1)), reason TEXT NOT NULL,
    actor TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id,node_key),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key)
);
CREATE TABLE IF NOT EXISTS state_source_bindings (
    project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
    repo_path TEXT NOT NULL, common_dir TEXT NOT NULL,
    actor TEXT NOT NULL, updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TABLE IF NOT EXISTS state_history_links (
    project_id TEXT NOT NULL, reference_id TEXT NOT NULL, node_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('run','commit','report','note')),
    ref TEXT NOT NULL, label TEXT NOT NULL, observed_status TEXT NOT NULL,
    artifact_ref TEXT, report_sha256 TEXT, provenance_actor TEXT NOT NULL,
    recorded_by TEXT NOT NULL, node_revision INTEGER NOT NULL,
    protection INTEGER NOT NULL CHECK(protection IN (0,1)),
    payload_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,reference_id),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key)
);
CREATE INDEX IF NOT EXISTS state_history_by_run ON state_history_links(kind,ref);
CREATE TRIGGER IF NOT EXISTS state_history_no_update BEFORE UPDATE ON state_history_links
BEGIN SELECT RAISE(ABORT,'historical references are append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_history_no_delete BEFORE DELETE ON state_history_links
BEGIN SELECT RAISE(ABORT,'historical references are append-only'); END;
"""


def project_policy(conn, project_id):
    row = conn.execute("SELECT * FROM state_project_policy WHERE project_id=?", (project_id,)).fetchone()
    policy = dict(row) if row else {"project_id": project_id, "revision": 0,
                                    "dispatch": "active", "reason": "", "actor": "", "updated_at": None,
                                    "multi_driver": "off"}
    policy.setdefault("multi_driver", "off")
    policy["claim_enforcement"] = claim_enforcement(conn, project_id)
    return policy


def claim_enforcement(conn, project_id) -> str:
    """off | on; off unless multi_driver is on too (the switch has no meaning alone)."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_project_enforcement'").fetchone():
        return "off"
    row = conn.execute("SELECT claim_enforcement FROM state_project_enforcement WHERE project_id=?",
                       (project_id,)).fetchone()
    return row[0] if row else "off"


def node_hold(conn, project_id, node_key):
    row = conn.execute("SELECT * FROM state_node_holds WHERE project_id=? AND node_key=?",
                       (project_id, node_key)).fetchone()
    return dict(row) if row else {"project_id": project_id, "node_key": node_key, "revision": 0,
                                 "held": 0, "reason": "", "actor": "", "updated_at": None}


def dispatch_block(conn, project_id, node_key):
    policy = project_policy(conn, project_id)
    if policy["dispatch"] != "active":
        return {"scope": "project", **policy}
    hold = node_hold(conn, project_id, node_key)
    if hold["held"]:
        return {"scope": "node", **hold}
    return None


def require_dispatch(conn, project_id, node_key):
    from core.state_graph import StateConflict
    block = dispatch_block(conn, project_id, node_key)
    if block:
        raise StateConflict(f"dispatch is held at {block['scope']} level: {block['reason']}")
