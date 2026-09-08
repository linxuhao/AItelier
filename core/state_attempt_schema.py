"""Atomic, preserving upgrade from workflow-only to executor-neutral attempts.

The original NOT NULL workflow/execution-project columns require a SQLite table
rebuild. Only this table is rebuilt, in one write transaction; referenced evidence
and immutable receipts stay byte-for-byte intact. No production path is resolved.
"""
from __future__ import annotations
import sqlite3

ATTEMPT_TABLE = """
CREATE TABLE {name} (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL, node_key TEXT NOT NULL, node_revision INTEGER NOT NULL,
    contract_hash TEXT NOT NULL, dependency_snapshot TEXT NOT NULL, context_json TEXT NOT NULL,
    request_key TEXT NOT NULL, request_hash TEXT NOT NULL, workflow TEXT,
    execution_project_id TEXT UNIQUE, run_id TEXT UNIQUE,
    graph_version INTEGER, graph_digest TEXT,
    status TEXT NOT NULL CHECK(status IN ('reserved','launching','running','paused','unknown','candidate','failed','superseded')),
    artifact_ref TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    execution_kind TEXT NOT NULL DEFAULT 'skillflow' CHECK(execution_kind IN ('skillflow','external')),
    harness TEXT, external_id TEXT, reporting_actor TEXT,
    observation_version INTEGER NOT NULL DEFAULT 0 CHECK(observation_version >= 0),
    artifact_kind TEXT CHECK(artifact_kind IN ('git-sha1','sha256')),
    terminal_observation_id TEXT,
    UNIQUE(project_id,node_key,request_key),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key),
    CHECK ((execution_kind='skillflow' AND workflow IS NOT NULL AND execution_project_id IS NOT NULL
            AND harness IS NULL AND external_id IS NULL AND reporting_actor IS NULL)
        OR (execution_kind='external' AND workflow IS NULL AND execution_project_id IS NULL
            AND run_id IS NULL AND graph_version IS NULL AND graph_digest IS NULL
            AND harness IS NOT NULL AND external_id IS NOT NULL AND reporting_actor IS NOT NULL))
)
"""

EXTRA_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS state_attempt_external_identity
ON state_attempts(project_id,node_key,harness,external_id) WHERE execution_kind='external';
CREATE TRIGGER IF NOT EXISTS state_attempt_executor_immutable
BEFORE UPDATE OF execution_kind,harness,external_id,reporting_actor,workflow,execution_project_id ON state_attempts
WHEN NEW.execution_kind IS NOT OLD.execution_kind OR NEW.harness IS NOT OLD.harness
  OR NEW.external_id IS NOT OLD.external_id OR NEW.reporting_actor IS NOT OLD.reporting_actor
  OR NEW.workflow IS NOT OLD.workflow OR NEW.execution_project_id IS NOT OLD.execution_project_id
BEGIN SELECT RAISE(ABORT,'attempt execution identity is immutable'); END;
CREATE TABLE IF NOT EXISTS state_external_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL,
    observation_id TEXT NOT NULL, version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','paused','unknown','candidate','failed')),
    resulting_status TEXT NOT NULL,
    quiescent INTEGER NOT NULL CHECK(quiescent IN (0,1)),
    artifact_ref TEXT, artifact_kind TEXT,
    report_ref TEXT NOT NULL, report_sha256 TEXT NOT NULL,
    actor TEXT NOT NULL, detail TEXT NOT NULL, payload_hash TEXT NOT NULL,
    context_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(attempt_id,observation_id), UNIQUE(attempt_id,version),
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
CREATE TRIGGER IF NOT EXISTS state_external_observations_no_update BEFORE UPDATE ON state_external_observations
BEGIN SELECT RAISE(ABORT,'external observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS state_external_observations_no_delete BEFORE DELETE ON state_external_observations
BEGIN SELECT RAISE(ABORT,'external observations are append-only'); END;
"""


def _statements(script):
    """Execute DDL without executescript's implicit transaction commit."""
    pending = ''
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        raise ValueError('Incomplete internal schema statement')


def initialize(db, existing_schema: str) -> None:
    with db.get_connection() as conn:
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('BEGIN IMMEDIATE')
        try:
            before = {r['name']: dict(r) for r in conn.execute('PRAGMA table_info(state_attempts)')}
            if before and 'execution_kind' not in before:
                # Reject existing corruption rather than accidentally normalizing it.
                for table in ('state_attempts','state_evidence','state_acceptances'):
                    if conn.execute(f'PRAGMA foreign_key_check({table})').fetchone():
                        raise sqlite3.IntegrityError('Existing state foreign-key violations; inspect before migration')
                sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='state_attempts'").fetchone()
                previous_watermark = sequence[0] if sequence else 0
                custom = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name='state_attempts' "
                                      "AND sql IS NOT NULL AND type IN ('index','trigger')").fetchall()
                conn.execute(ATTEMPT_TABLE.format(name='state_attempts_executor_upgrade'))
                columns = ','.join('"'+name+'"' for name in before)
                conn.execute(f'INSERT INTO state_attempts_executor_upgrade({columns}) SELECT {columns} FROM state_attempts')
                old_count = conn.execute('SELECT COUNT(*) FROM state_attempts').fetchone()[0]
                new_count = conn.execute('SELECT COUNT(*) FROM state_attempts_executor_upgrade').fetchone()[0]
                if old_count != new_count:
                    raise sqlite3.IntegrityError('Attempt migration count mismatch')
                conn.execute('DROP TABLE state_attempts')
                conn.execute('ALTER TABLE state_attempts_executor_upgrade RENAME TO state_attempts')
                conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='state_attempts'", (previous_watermark,))
                for row in custom:
                    conn.execute(row['sql'])
            elif not before:
                conn.execute(ATTEMPT_TABLE.format(name='state_attempts'))
            columns_now = {r['name'] for r in conn.execute('PRAGMA table_info(state_attempts)')}
            if not {'execution_kind','harness','external_id','reporting_actor','observation_version',
                    'artifact_kind','terminal_observation_id'} <= columns_now:
                raise sqlite3.IntegrityError('Incomplete executor-neutral attempt schema; refusing partial upgrade')
            # Existing table creation is IF NOT EXISTS; keep its evidence/receipt
            # schema and triggers, indexes and explicit legacy compatibility.
            for statement in _statements(existing_schema):
                conn.execute(statement)
            receipt_cols = {r['name'] for r in conn.execute('PRAGMA table_info(state_acceptances)')}
            if 'provenance_json' not in receipt_cols:
                conn.execute("ALTER TABLE state_acceptances ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}'")
            for statement in _statements(EXTRA_SCHEMA):
                conn.execute(statement)
            for table in ('state_attempts','state_evidence','state_acceptances','state_external_observations'):
                if conn.execute(f'PRAGMA foreign_key_check({table})').fetchone():
                    raise sqlite3.IntegrityError('State migration failed foreign-key validation')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute('PRAGMA foreign_keys=ON')
