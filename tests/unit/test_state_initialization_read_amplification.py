"""Control reads inspect reference keys, not every row's retained report/context.

These are owned Linux/SQLite fixtures, not a production latency benchmark.
"""
import concurrent.futures
import json
import sqlite3
import time
from pathlib import Path

import pytest

from core.state_database import StateDatabase
from core.state_service import StateService


def _rchar():
    return int(dict(line.split(':', 1) for line in Path('/proc/self/io').read_text().splitlines())['rchar'])


@pytest.fixture
def populated(tmp_path, monkeypatch):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path / 'home'))
    database = StateDatabase(str(tmp_path / 'state.sqlite'))
    service = StateService(database, actor='fixture', project_read_trusted=True)
    service.create_project('p', 'Owned scaled fixture')
    service.store.add_nodes('p', [{'key': 'n', 'goal': 'N', 'acceptance': [
        {'id': 'a', 'kind': 'test', 'description': 'D'}]}])
    first = service.start_external_attempt('p', 'n', 1, 'fixture', 'first', 'first')
    attempt_id = first['attempt_id']
    count = 3500
    with database.get_connection() as conn:
        template = dict(conn.execute('SELECT * FROM state_attempts WHERE attempt_id=?', (attempt_id,)).fetchone())
        # Every generated historical attempt is terminal, with a matching owner.
        template.pop('seq')
        columns = list(template)
        rows = []
        for i in range(count):
            row = dict(template, attempt_id=f'history-{i}', request_key=f'k-{i}',
                       external_id=f'e-{i}', status='candidate', context_json='x' * 3500)
            rows.append(tuple(row[c] for c in columns))
        conn.executemany('INSERT INTO state_attempts(' + ','.join(columns) + ') VALUES(' +
                         ','.join('?' for _ in columns) + ')', rows)
        conn.executemany('INSERT INTO state_external_owners('
                         'attempt_id,project_id,node_key,harness,external_id,status,admitted_at,updated_at) '
                         "VALUES(?,'p','n','fixture',?,'settled','t','t')",
                         [(f'history-{i}', f'e-{i}') for i in range(count)])
        conn.executemany('INSERT INTO state_external_observations('
                         'attempt_id,observation_id,version,status,resulting_status,quiescent,'
                         'report_ref,report_sha256,actor,detail,payload_hash,context_hash,created_at) '
                         "VALUES(?,?,0,'candidate','candidate',1,'r','h','fixture',?,'h','h','t')",
                         [(f'history-{i}', f'obs-{i}', 'y' * 3500) for i in range(count)])
        conn.commit()
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    return database, attempt_id, count * 7000


def test_scaled_control_read_does_not_reread_retained_payloads(populated):
    database, attempt_id, payload_bytes = populated
    measurements = []
    # Reconstruct the actual per-request service, rather than a substitute query.
    for _ in range(2):
        before = _rchar()
        started = time.monotonic()
        reader = StateService(database, actor='fixture', project_read_trusted=True)
        assert reader.get_attempt(attempt_id)['attempt_id'] == attempt_id
        measurements.append({'read_syscall_bytes': _rchar() - before,
                             'elapsed': time.monotonic() - started})
    print('OWNED_READ_AMPLIFICATION ' + json.dumps({'payload_bytes': payload_bytes, 'reads': measurements}))
    # 24.5 MB of retained context/report content must not be walked on every
    # request. rchar counts SQLite read syscalls even when the OS cache is warm.
    assert max(m['read_syscall_bytes'] for m in measurements) < payload_bytes // 5


def test_scaled_control_read_progresses_while_writer_holds_lock(populated):
    database, attempt_id, _ = populated
    holder = sqlite3.connect(database.db_path)
    holder.execute('BEGIN IMMEDIATE')
    holder.execute("UPDATE state_projects SET title='uncommitted' WHERE project_id='p'")
    def request():
        reader = StateService(database, actor='fixture', project_read_trusted=True)
        return reader.get_attempt(attempt_id)['attempt_id']
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(request) for _ in range(2)]
            assert [f.result(timeout=3) for f in futures] == [attempt_id, attempt_id]
        assert holder.in_transaction
    finally:
        holder.rollback()
        holder.close()
    with database.get_connection() as conn:
        assert conn.execute("SELECT title FROM state_projects WHERE project_id='p'").fetchone()[0] == 'Owned scaled fixture'
        assert conn.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('parent,child,parent_rows,child_rows', [
    ('CREATE TABLE p(id INTEGER PRIMARY KEY)',
     'CREATE TABLE c(f TEXT REFERENCES p(id))', [(1,)], [('001',), (None,)]),
    ('CREATE TABLE p(id TEXT PRIMARY KEY)',
     'CREATE TABLE c(f INTEGER REFERENCES p(id))', [('01',)], [(1,)]),
    ('CREATE TABLE p(id TEXT COLLATE NOCASE PRIMARY KEY)',
     'CREATE TABLE c(f TEXT COLLATE BINARY REFERENCES p(id))', [('ABC',)], [('abc',)]),
    ('CREATE TABLE p(id TEXT COLLATE BINARY PRIMARY KEY)',
     'CREATE TABLE c(f TEXT COLLATE NOCASE REFERENCES p(id))', [('ABC',)], [('abc',)]),
    ('CREATE TABLE p(a TEXT COLLATE NOCASE,b INTEGER,PRIMARY KEY(a,b)) WITHOUT ROWID',
     'CREATE TABLE c(x TEXT,y TEXT,FOREIGN KEY(x,y) REFERENCES p)',
     [('A', 2)], [('a', '02'), ('missing', None)]),
    ('CREATE TABLE p(a TEXT,b INTEGER,PRIMARY KEY(a,b))',
     'CREATE TABLE c(x TEXT,y INTEGER,FOREIGN KEY(x,y) REFERENCES p(a,b))',
     [('A', 2)], [('A', 3), (None, 123)]),
    ('CREATE TABLE p("quoted""key" BLOB PRIMARY KEY)',
     'CREATE TABLE c("quoted""fk" TEXT REFERENCES p("quoted""key"))',
     [(1,)], [('1',)]),
    ('CREATE TABLE p(id INTEGER PRIMARY KEY)',
     'CREATE TABLE c(a INTEGER REFERENCES p(id),b INTEGER REFERENCES p(id))',
     [(1,)], [(1, 2)]),
    ('CREATE TABLE p(id INTEGER PRIMARY KEY)',
     'CREATE TABLE c(id INTEGER PRIMARY KEY,parent INTEGER REFERENCES c(id))',
     [(1,)], [(1, None), (2, 1)]),
])
def test_reference_key_checks_match_sqlite_fk_semantics(tmp_path, parent, child, parent_rows, child_rows):
    from core.state_attempt_schema import _foreign_key_violation
    with sqlite3.connect(tmp_path / 'references.sqlite') as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(parent)
        conn.execute(child)
        conn.executemany('INSERT INTO p VALUES(' + ','.join('?' for _ in parent_rows[0]) + ')', parent_rows)
        conn.executemany('INSERT INTO c VALUES(' + ','.join('?' for _ in child_rows[0]) + ')', child_rows)
        expected = bool(conn.execute('PRAGMA foreign_key_check(c)').fetchall())
        assert _foreign_key_violation(conn, 'c') == expected


def test_malformed_parent_key_is_not_accepted_as_a_current_schema(tmp_path):
    from core.state_attempt_schema import _foreign_key_violation
    with sqlite3.connect(tmp_path / 'mismatch.sqlite') as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript('CREATE TABLE p(id TEXT);CREATE TABLE c(f TEXT REFERENCES p(id));')
        with pytest.raises(sqlite3.OperationalError, match='foreign key mismatch'):
            conn.execute('PRAGMA foreign_key_check(c)').fetchall()
        with pytest.raises(sqlite3.OperationalError, match='foreign key mismatch'):
            _foreign_key_violation(conn, 'c')


def test_reference_check_observes_new_corruption_in_a_new_read_snapshot(populated):
    from core.state_attempt_schema import _foreign_key_violation
    database, _, _ = populated
    with database.get_connection() as conn:
        assert not _foreign_key_violation(conn, 'state_external_owners')
    with database.get_connection() as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute("INSERT INTO state_external_owners(attempt_id,project_id,node_key,harness,external_id,"
                     "status,admitted_at,updated_at) VALUES('missing-after-check','p','n','h','e','active','t','t')")
        conn.commit()
    # No schema cache or process stamp may bless later FK-OFF corruption.
    with pytest.raises(sqlite3.IntegrityError, match='foreign-key validation'):
        StateService(database, actor='fixture', project_read_trusted=True)
