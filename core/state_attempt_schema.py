"""Atomic, preserving upgrades of the attempt tables.

Two upgrades need a SQLite table REBUILD (a CHECK constraint cannot be altered
in place): the original NOT NULL workflow/execution-project columns
(executor-neutral attempts), and the multi-driver P3 terminal status
``abandoned`` on ``state_attempts`` and ``state_external_owners``
(design/multi-driver-coop.md §4.4, §9.2). A rebuild copies every existing row
column-for-column, keeps the autoincrement high-water mark, recreates every
index and trigger that hung off the table, verifies counts and foreign keys,
and runs inside one write transaction; corrupt or partial input fails without
touching the original rows. Referenced evidence and immutable receipts stay
byte-for-byte intact. No production path is resolved.
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
    status TEXT NOT NULL CHECK(status IN ('reserved','launching','running','paused','unknown','candidate','failed','superseded','abandoned')),
    artifact_ref TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    execution_kind TEXT NOT NULL DEFAULT 'skillflow' CHECK(execution_kind IN ('skillflow','external')),
    harness TEXT, external_id TEXT, reporting_actor TEXT,
    observation_version INTEGER NOT NULL DEFAULT 0 CHECK(observation_version >= 0),
    artifact_kind TEXT CHECK(artifact_kind IN ('git-sha1','sha256')),
    terminal_observation_id TEXT,
    abandon_kind TEXT CHECK(abandon_kind IN ('confirmed_stopped','unknown')),
    owner_driver_id TEXT,
    owner_fence INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    last_heartbeat_at TEXT,
    UNIQUE(project_id,node_key,request_key),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key),
    CHECK ((execution_kind='skillflow' AND workflow IS NOT NULL AND execution_project_id IS NOT NULL
            AND harness IS NULL AND external_id IS NULL AND reporting_actor IS NULL)
        OR (execution_kind='external' AND workflow IS NULL AND execution_project_id IS NULL
            AND run_id IS NULL AND graph_version IS NULL AND graph_digest IS NULL
            AND harness IS NOT NULL AND external_id IS NOT NULL AND reporting_actor IS NOT NULL))
)
"""

LEASE_COLUMNS = (
    ('owner_driver_id', 'TEXT'),
    ('owner_fence', 'INTEGER NOT NULL DEFAULT 0'),
    ('lease_expires_at', 'TEXT'),
    ('last_heartbeat_at', 'TEXT'),
)

OWNERS_TABLE = """
CREATE TABLE {name} (
    attempt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    harness TEXT NOT NULL, external_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','unknown','settled','abandoned')),
    admitted_at TEXT NOT NULL, updated_at TEXT NOT NULL, settled_at TEXT,
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
)
"""

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS state_git_artifacts (
    commit_sha TEXT PRIMARY KEY, tree_sha TEXT NOT NULL,
    bundle_sha256 TEXT NOT NULL, retained_ref TEXT NOT NULL,
    bundle_bytes BLOB NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS state_git_artifacts_no_update
BEFORE UPDATE ON state_git_artifacts
BEGIN SELECT RAISE(ABORT,'recoverable Git artifacts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS state_git_artifacts_no_delete
BEFORE DELETE ON state_git_artifacts
BEGIN SELECT RAISE(ABORT,'recoverable Git artifacts are retained'); END;
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
    late_after_abandon INTEGER NOT NULL DEFAULT 0 CHECK(late_after_abandon IN (0,1)),
    fence INTEGER,
    UNIQUE(attempt_id,observation_id), UNIQUE(attempt_id,version),
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
CREATE TRIGGER IF NOT EXISTS state_external_observations_no_update BEFORE UPDATE ON state_external_observations
BEGIN SELECT RAISE(ABORT,'external observations are append-only'); END;
CREATE TABLE IF NOT EXISTS state_external_report_blobs (
    report_sha256 TEXT PRIMARY KEY, report_bytes BLOB NOT NULL,
    retained_ref TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS state_external_report_blobs_no_update
BEFORE UPDATE ON state_external_report_blobs
BEGIN SELECT RAISE(ABORT,'external report bytes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS state_external_report_blobs_no_delete
BEFORE DELETE ON state_external_report_blobs
BEGIN SELECT RAISE(ABORT,'external report bytes are immutable'); END;
CREATE TABLE IF NOT EXISTS state_external_owners (
    attempt_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, node_key TEXT NOT NULL,
    harness TEXT NOT NULL, external_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','unknown','settled','abandoned')),
    admitted_at TEXT NOT NULL, updated_at TEXT NOT NULL, settled_at TEXT,
    FOREIGN KEY(attempt_id) REFERENCES state_attempts(attempt_id)
);
CREATE INDEX IF NOT EXISTS state_external_owners_status
ON state_external_owners(status, project_id, node_key);
CREATE TRIGGER IF NOT EXISTS state_external_owners_no_delete
BEFORE DELETE ON state_external_owners
BEGIN SELECT RAISE(ABORT,'external owner lifecycle is append-only'); END;
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


def _rebuild(conn, table: str, ddl: str, fk_tables: tuple) -> None:
    """Replace ``table`` by ``ddl`` (a ``{name}`` template), keeping every row.

    Every column of the old table must exist in the new one; rows are copied by
    name, the autoincrement high-water mark (if any) is carried over, and every
    index/trigger that hung off the old table is recreated verbatim. Counts are
    compared and the named tables are foreign-key checked before the caller
    commits. Runs with ``PRAGMA foreign_keys=OFF`` inside the caller's
    transaction, like the executor-neutral upgrade always did.
    """
    scratch = table + '_rebuild'
    for name in (table, *fk_tables):
        if conn.execute(f"SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() \
                and conn.execute(f'PRAGMA foreign_key_check({name})').fetchone():
            raise sqlite3.IntegrityError('Existing state foreign-key violations; inspect before migration')
    before = [r['name'] for r in conn.execute(f'PRAGMA table_info({table})')]
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
    previous_watermark = sequence[0] if sequence else None
    custom = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? "
                          "AND sql IS NOT NULL AND type IN ('index','trigger')", (table,)).fetchall()
    conn.execute(ddl.format(name=scratch))
    new_columns = {r['name'] for r in conn.execute(f'PRAGMA table_info({scratch})')}
    missing = [c for c in before if c not in new_columns]
    if missing:
        raise sqlite3.IntegrityError(f'{table} rebuild would drop columns {missing}; refusing')
    columns = ','.join('"' + name + '"' for name in before)
    conn.execute(f'INSERT INTO {scratch}({columns}) SELECT {columns} FROM {table}')
    old_count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    new_count = conn.execute(f'SELECT COUNT(*) FROM {scratch}').fetchone()[0]
    if old_count != new_count:
        raise sqlite3.IntegrityError(f'{table} migration count mismatch')
    conn.execute(f'DROP TABLE {table}')
    conn.execute(f'ALTER TABLE {scratch} RENAME TO {table}')
    if previous_watermark is not None:
        conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?", (previous_watermark, table))
    for row in custom:
        conn.execute(row['sql'])


def _table_sql(conn, table: str) -> str | None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row[0] if row else None


# Tables whose rows reference state_attempts; checked after any rebuild.
_ATTEMPT_DEPENDENTS = ('state_evidence', 'state_acceptances', 'state_external_observations',
                       'state_external_owners', 'driver_subagents', 'state_handoffs')


def initialize(db, existing_schema: str) -> None:
    with db.get_connection() as conn:
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('BEGIN IMMEDIATE')
        try:
            before = {r['name']: dict(r) for r in conn.execute('PRAGMA table_info(state_attempts)')}
            if before and 'execution_kind' not in before:
                # Reject existing corruption rather than accidentally normalizing it.
                _rebuild(conn, 'state_attempts', ATTEMPT_TABLE, ('state_evidence', 'state_acceptances'))
            elif not before:
                conn.execute(ATTEMPT_TABLE.format(name='state_attempts'))
            elif "'abandoned'" not in (_table_sql(conn, 'state_attempts') or ''):
                # Multi-driver P3: the terminal status `abandoned` (design §4.4)
                # widens the CHECK, which SQLite cannot alter in place. Same
                # preserving rebuild; every historical row, the seq high-water
                # mark, indexes and triggers survive, and a damaged table fails
                # the copy instead of being normalized.
                _rebuild(conn, 'state_attempts', ATTEMPT_TABLE, _ATTEMPT_DEPENDENTS)
            columns_now = {r['name'] for r in conn.execute('PRAGMA table_info(state_attempts)')}
            if not {'execution_kind','harness','external_id','reporting_actor','observation_version',
                    'artifact_kind','terminal_observation_id'} <= columns_now:
                raise sqlite3.IntegrityError('Incomplete executor-neutral attempt schema; refusing partial upgrade')
            # Multi-driver leases (design/multi-driver-coop.md §4.2, P1): additive
            # columns only. Existing rows keep NULL owner/lease = legacy_unleased,
            # which never expires; nothing is backfilled or guessed. (A table the
            # P3 rebuild produced already has them.)
            for column, ddl in LEASE_COLUMNS:
                if column not in columns_now:
                    conn.execute(f'ALTER TABLE state_attempts ADD COLUMN {column} {ddl}')
            if 'abandon_kind' not in columns_now:
                conn.execute("ALTER TABLE state_attempts ADD COLUMN abandon_kind TEXT "
                             "CHECK(abandon_kind IN ('confirmed_stopped','unknown'))")
            if "'abandoned'" not in (_table_sql(conn, 'state_attempts') or ''):
                raise sqlite3.IntegrityError('state_attempts lacks the abandoned status after migration; refusing')
            conn.execute('CREATE INDEX IF NOT EXISTS state_attempts_lease ON state_attempts(project_id,lease_expires_at) '
                         'WHERE lease_expires_at IS NOT NULL')
            # Existing table creation is IF NOT EXISTS; keep its evidence/receipt
            # schema and triggers, indexes and explicit legacy compatibility.
            for statement in _statements(existing_schema):
                conn.execute(statement)
            evidence_cols = {r['name'] for r in conn.execute('PRAGMA table_info(state_evidence)')}
            if 'director_identity' not in evidence_cols:
                conn.execute("ALTER TABLE state_evidence ADD COLUMN director_identity TEXT")
            receipt_cols = {r['name'] for r in conn.execute('PRAGMA table_info(state_acceptances)')}
            if 'provenance_json' not in receipt_cols:
                conn.execute("ALTER TABLE state_acceptances ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}'")
            observation_cols = {r['name'] for r in conn.execute('PRAGMA table_info(state_external_observations)')}
            if observation_cols and 'late_after_abandon' not in observation_cols:
                # P3 (design §9.1): a report filed after its attempt was abandoned
                # is kept and marked; the fence it presented is recorded.
                conn.execute("ALTER TABLE state_external_observations ADD COLUMN late_after_abandon INTEGER NOT NULL "
                             "DEFAULT 0 CHECK(late_after_abandon IN (0,1))")
            if observation_cols and 'fence' not in observation_cols:
                conn.execute("ALTER TABLE state_external_observations ADD COLUMN fence INTEGER")
            owners_sql = _table_sql(conn, 'state_external_owners')
            if owners_sql is not None and "'abandoned'" not in owners_sql:
                # P3: the owner lifecycle gains `abandoned` too (design §9.1).
                _rebuild(conn, 'state_external_owners', OWNERS_TABLE, ())
            for statement in _statements(EXTRA_SCHEMA):
                conn.execute(statement)
            conn.execute(
                "INSERT OR IGNORE INTO state_external_owners("
                "attempt_id,project_id,node_key,harness,external_id,status,admitted_at,updated_at,settled_at) "
                "SELECT attempt_id,project_id,node_key,harness,external_id,"
                "CASE WHEN status IN ('candidate','failed','superseded') THEN 'settled' "
                "WHEN status='abandoned' THEN 'abandoned' "
                "WHEN status='paused' THEN 'paused' WHEN status='unknown' THEN 'unknown' ELSE 'active' END,"
                "created_at,updated_at,CASE WHEN status IN ('candidate','failed','superseded','abandoned') THEN updated_at END "
                "FROM state_attempts WHERE execution_kind='external'")
            for table in ('state_attempts', *_ATTEMPT_DEPENDENTS):
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() \
                        and conn.execute(f'PRAGMA foreign_key_check({table})').fetchone():
                    raise sqlite3.IntegrityError('State migration failed foreign-key validation')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.execute('PRAGMA foreign_keys=ON')
