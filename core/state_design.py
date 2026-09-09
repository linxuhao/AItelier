"""Immutable design facts and generated views, using the existing State store.

Design references do not create execution dependencies. A baseline is an exact
selection, never a latest-version query. Binding a new selection is an explicit
node revision; existing attempts and receipts retain their original context.
"""
from __future__ import annotations

import hashlib
import json

from core.state_graph import StateConflict, StateGraphError, StateNotFound, canonical, digest, integer, key, now, text

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_design_revisions (
    project_id TEXT NOT NULL, design_id TEXT NOT NULL, revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,design_id,revision),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TABLE IF NOT EXISTS state_design_baselines (
    project_id TEXT NOT NULL, baseline_id TEXT NOT NULL, manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(project_id,baseline_id),
    FOREIGN KEY(project_id) REFERENCES state_projects(project_id)
);
CREATE TABLE IF NOT EXISTS state_design_heads (
    project_id TEXT PRIMARY KEY, baseline_id TEXT NOT NULL,
    FOREIGN KEY(project_id,baseline_id) REFERENCES state_design_baselines(project_id,baseline_id)
);
CREATE TABLE IF NOT EXISTS state_design_bindings (
    project_id TEXT NOT NULL, node_key TEXT NOT NULL, node_revision INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
    PRIMARY KEY(project_id,node_key,node_revision),
    FOREIGN KEY(project_id,node_key) REFERENCES state_nodes(project_id,node_key)
);
"""
for _table in ('state_design_revisions', 'state_design_baselines', 'state_design_bindings'):
    for _operation in ('UPDATE', 'DELETE'):
        SCHEMA += f"CREATE TRIGGER IF NOT EXISTS {_table}_no_{_operation.lower()} BEFORE {_operation} ON {_table} BEGIN SELECT RAISE(ABORT,'design facts are immutable'); END;\n"


def initialize(db):
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def _scope(value):
    # Scope is explicit metadata, not a predicate language or an overlap solver.
    if not isinstance(value, dict) or not 1 <= len(value) <= 16:
        raise StateGraphError('scope must contain 1-16 explicit dimension/value pairs')
    return {key(k, 'scope dimension'): key(v, 'scope value') for k, v in sorted(value.items())}


def _ref(value):
    if not isinstance(value, dict) or set(value) != {'design_id', 'revision'}:
        raise StateGraphError('reference must contain only design_id and exact revision')
    return {'design_id': key(value['design_id'], 'design id'),
            'revision': integer(value['revision'], 'design revision', 1)}


def binding_snapshot(conn, project_id, node_key):
    row = conn.execute('SELECT snapshot_json FROM state_design_bindings WHERE project_id=? AND node_key=? '
                       'ORDER BY node_revision DESC LIMIT 1', (project_id, node_key)).fetchone()
    return json.loads(row[0]) if row else None


class StateDesign:
    def __init__(self, store, actor):
        self.store, self.actor = store, actor

    @staticmethod
    def _revision(conn, project_id, design_id, revision):
        row = conn.execute('SELECT * FROM state_design_revisions WHERE project_id=? AND design_id=? AND revision=?',
                           (project_id, key(design_id, 'design id'), integer(revision, 'design revision', 1))).fetchone()
        if row is None:
            raise StateNotFound('exact design revision not found')
        result = dict(row)
        result['content'] = json.loads(result.pop('payload_json'))
        return result

    @staticmethod
    def _baseline(conn, project_id, baseline_id):
        row = conn.execute('SELECT * FROM state_design_baselines WHERE project_id=? AND baseline_id=?',
                           (project_id, key(baseline_id, 'baseline id'))).fetchone()
        if row is None:
            raise StateNotFound('exact design baseline not found')
        result = dict(row)
        result['manifest'] = json.loads(result.pop('manifest_json'))
        return result

    def search(self, project_id, query, baseline_id=None, scope=None, limit=20, offset=0):
        from core.state_design_queries import search
        return search(self, project_id, query, baseline_id, scope, limit, offset)

    def get_revision(self, project_id, design_id, revision):
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            return self._revision(conn, project_id, design_id, revision)

    def create_revision(self, project_id, design_id, expected_revision, title, statement, rationale,
                        open_questions, scope, lifecycle_status='draft', kind='rule', relations=None):
        key(design_id, 'design id')
        integer(expected_revision, 'expected revision')
        if lifecycle_status not in ('draft', 'approved', 'historical') or kind not in ('rule', 'decision', 'explanatory'):
            raise StateGraphError('unsupported design status or kind')
        if not isinstance(open_questions, list) or len(open_questions) > 64:
            raise StateGraphError('open_questions must be a bounded list')
        questions = [text(q, 'open question', 2000) for q in open_questions]
        if questions and lifecycle_status == 'approved':
            raise StateGraphError('unresolved questions cannot be approved; split unresolved work into a draft item')
        payload = {'title': text(title, 'title', 400), 'statement': text(statement, 'statement'),
                   'rationale': text(rationale, 'rationale'), 'open_questions': questions,
                   'scope': _scope(scope), 'lifecycle_status': lifecycle_status, 'kind': kind,
                   'parent_revision': expected_revision or None, 'relations': []}
        if relations is None:
            relations = []
        if not isinstance(relations, list) or len(relations) > 64:
            raise StateGraphError('relations must be a bounded list')
        for rel in relations:
            if not isinstance(rel, dict) or set(rel) != {'type', 'target', 'rationale'}:
                raise StateGraphError('relations require type, exact target and rationale; partial-scope supersedes is unsupported')
            if rel['type'] not in ('depends_on', 'references', 'supersedes'):
                raise StateGraphError('unsupported design relation')
            payload['relations'].append({'type': rel['type'], 'target': _ref(rel['target']),
                                         'rationale': text(rel['rationale'], 'relation rationale', 2000)})
        payload['relations'].sort(key=canonical)
        if len({canonical(r['target']) + r['type'] for r in payload['relations']}) != len(payload['relations']):
            raise StateGraphError('duplicate design relation')
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            latest = conn.execute('SELECT COALESCE(MAX(revision),0) FROM state_design_revisions '
                                  'WHERE project_id=? AND design_id=?', (project_id, design_id)).fetchone()[0]
            if latest != expected_revision:
                raise StateConflict('design revision changed; reload before editing')
            for rel in payload['relations']:
                target = self._revision(conn, project_id, **rel['target'])
                if rel['type'] == 'supersedes' and target['content']['scope'] != payload['scope']:
                    raise StateGraphError('supersedes supports only complete items with identical scope; split partial overrides')
            revision = latest + 1
            conn.execute('INSERT INTO state_design_revisions VALUES(?,?,?,?,?,?,?)',
                         (project_id, design_id, revision, canonical(payload), digest(payload), self.actor, now()))
            self.store._event(conn, project_id, None, 'design_revision_created',
                              {'design_id': design_id, 'revision': revision, 'content_hash': digest(payload), 'actor': self.actor})
            return self._revision(conn, project_id, design_id, revision)

    def create_baseline(self, project_id, baseline_id, selected_revisions, expected_baseline_id=None):
        key(baseline_id, 'baseline id')
        if expected_baseline_id is not None:
            key(expected_baseline_id, 'expected baseline id')
        if not isinstance(selected_revisions, list) or not 1 <= len(selected_revisions) <= 1000:
            raise StateGraphError('baseline requires 1-1000 exact revisions')
        refs = sorted((_ref(r) for r in selected_revisions), key=lambda r: r['design_id'])
        selected = {r['design_id']: r['revision'] for r in refs}
        if len(selected) != len(refs):
            raise StateGraphError('a baseline selects exactly one revision per design ID')
        with self.store.transaction(write=True) as conn:
            self.store._project(conn, project_id)
            if conn.execute('SELECT 1 FROM state_design_baselines WHERE project_id=? AND baseline_id=?',
                            (project_id, baseline_id)).fetchone():
                raise StateConflict('baseline ID is immutable and cannot be reused')
            head = conn.execute('SELECT baseline_id FROM state_design_heads WHERE project_id=?', (project_id,)).fetchone()
            if (head[0] if head else None) != expected_baseline_id:
                raise StateConflict('current baseline changed; reload before selecting')
            revisions = [self._revision(conn, project_id, **r) for r in refs]
            replaced = set()
            selected_pins = {canonical(r) for r in refs}
            for rev in revisions:
                ancestors = set()
                pending = [rev]
                while pending:
                    source = pending.pop()
                    for rel in source['content']['relations']:
                        target = rel['target']
                        target_revision = self._revision(conn, project_id, **target)
                        if source is rev and rel['type'] != 'supersedes':
                            if selected.get(target['design_id']) != target['revision']:
                                raise StateGraphError('design reference must resolve to the exact revision selected in this baseline')
                            if (rel['type'] == 'depends_on' and rev['content']['lifecycle_status'] == 'approved'
                                    and target_revision['content']['lifecycle_status'] != 'approved'):
                                raise StateGraphError('approved design cannot depend on a non-approved requirement')
                        if rel['type'] == 'supersedes':
                            pin = canonical(target)
                            if pin not in ancestors:
                                ancestors.add(pin)
                                pending.append(target_revision)
                if ancestors & (selected_pins | replaced):
                    raise StateConflict('conflicting full-item supersession in baseline')
                replaced.update(ancestors)
            manifest = {'selected_revisions': [{'design_id': r['design_id'], 'revision': r['revision'],
                         'content_hash': r['content_hash']} for r in revisions]}
            conn.execute('INSERT INTO state_design_baselines VALUES(?,?,?,?,?,?)',
                         (project_id, baseline_id, canonical(manifest), digest(manifest), self.actor, now()))
            conn.execute('INSERT INTO state_design_heads VALUES(?,?) ON CONFLICT(project_id) '
                         'DO UPDATE SET baseline_id=excluded.baseline_id', (project_id, baseline_id))
            self.store._event(conn, project_id, None, 'design_baseline_selected',
                              {'baseline_id': baseline_id, 'previous_baseline_id': expected_baseline_id,
                               'manifest_hash': digest(manifest), 'actor': self.actor})
            return self._baseline(conn, project_id, baseline_id)

    def get_baseline(self, project_id, baseline_id):
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            return self._baseline(conn, project_id, baseline_id)

    def catalog(self, project_id):
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            head = conn.execute('SELECT baseline_id FROM state_design_heads WHERE project_id=?', (project_id,)).fetchone()
            return {'current_baseline_id': head[0] if head else None,
                    'revisions': [dict(r) for r in conn.execute('SELECT design_id,revision,content_hash FROM state_design_revisions '
                        'WHERE project_id=? ORDER BY design_id,revision', (project_id,))],
                    'baselines': [dict(r) for r in conn.execute('SELECT baseline_id,manifest_hash FROM state_design_baselines '
                        'WHERE project_id=? ORDER BY baseline_id', (project_id,))]}

    def bind_node(self, project_id, node_key, expected_revision, baseline_id, bindings, reason):
        if not isinstance(bindings, list) or not 1 <= len(bindings) <= 64:
            raise StateGraphError('bindings require 1-64 explicit items')
        with self.store.transaction(write=True) as conn:
            self.store._node(conn, project_id, node_key)
            baseline = self._baseline(conn, project_id, baseline_id)
            selected = {r['design_id']: r['revision'] for r in baseline['manifest']['selected_revisions']}
            prepared = []
            for binding in bindings:
                if not isinstance(binding, dict) or set(binding) != {'design_id', 'revision', 'purpose', 'coverage_scope'}:
                    raise StateGraphError('binding requires exact design_id/revision, purpose and coverage_scope')
                ref = _ref({k: binding[k] for k in ('design_id', 'revision')})
                purpose = binding['purpose']
                if purpose not in ('implements', 'verifies', 'context'):
                    raise StateGraphError('binding purpose must be implements, verifies or context')
                if selected.get(ref['design_id']) != ref['revision']:
                    raise StateGraphError('binding revision is not selected in this baseline')
                revision = self._revision(conn, project_id, **ref)
                coverage = _scope(binding['coverage_scope'])
                if coverage != revision['content']['scope']:
                    raise StateGraphError('MVP binding covers a complete item scope; split partial coverage into separate items')
                if purpose != 'context' and revision['content']['lifecycle_status'] != 'approved':
                    raise StateGraphError('implements/verifies requires an approved design revision')
                prepared.append({**ref, 'purpose': purpose, 'coverage_scope': coverage, 'design': revision})
            prepared.sort(key=canonical)
            if len({(b['design_id'], b['purpose']) for b in prepared}) != len(prepared):
                raise StateGraphError('duplicate design binding')
            # Include only bound items and their declared depends_on closure.
            # The manifest pins the remaining baseline without copying its prose.
            dependencies = {}
            pending = [b['design'] for b in prepared]
            seen = {(b['design_id'], b['revision']) for b in prepared}
            while pending:
                source = pending.pop()
                for rel in source['content']['relations']:
                    pin = (rel['target']['design_id'], rel['target']['revision'])
                    if rel['type'] == 'depends_on' and pin not in seen:
                        seen.add(pin)
                        dependency = self._revision(conn, project_id, **rel['target'])
                        dependencies[pin] = dependency
                        pending.append(dependency)
            snapshot = {'baseline_id': baseline_id, 'manifest_hash': baseline['manifest_hash'],
                        'manifest': baseline['manifest'], 'bindings': prepared,
                        'design_dependencies': [dependencies[pin] for pin in sorted(dependencies)]}
            # Reuse the existing contract revision and invalidation transaction.
            # No design edge is written into state_dependencies.
            result = self.store._revise(conn, project_id, node_key, expected_revision, reason=reason)
            conn.execute('INSERT INTO state_design_bindings VALUES(?,?,?,?,?)',
                         (project_id, node_key, result['revision'], canonical(snapshot), digest(snapshot)))
            self.store._event(conn, project_id, node_key, 'node_design_bound',
                              {'node_revision': result['revision'], 'baseline_id': baseline_id,
                               'binding_snapshot_hash': digest(snapshot), 'actor': self.actor})
            return {**result, 'design_context': snapshot, 'binding_snapshot_hash': digest(snapshot)}

    def node_bindings(self, project_id, node_key):
        with self.store.transaction() as conn:
            self.store._node(conn, project_id, node_key)
            snapshot = binding_snapshot(conn, project_id, node_key)
            head = conn.execute('SELECT baseline_id FROM state_design_heads WHERE project_id=?', (project_id,)).fetchone()
            return {'design_context': snapshot, 'binding_snapshot_hash': digest(snapshot) if snapshot else None,
                    'current_baseline_id': head[0] if head else None,
                    'baseline_review_required': bool(snapshot and head and snapshot['baseline_id'] != head[0])}

    def export_markdown(self, project_id, baseline_id):
        with self.store.transaction() as conn:
            self.store._project(conn, project_id)
            baseline = self._baseline(conn, project_id, baseline_id)
            revisions = [self._revision(conn, project_id, r['design_id'], r['revision'])
                         for r in baseline['manifest']['selected_revisions']]
        lines = ['# Design baseline ' + baseline_id, '',
                 '> GENERATED READ-ONLY VIEW. Edit through State design commands; never import this Markdown.', '',
                 'Project: ' + project_id, 'Baseline: ' + baseline_id,
                 'Manifest SHA-256: ' + baseline['manifest_hash'], '']
        for revision in revisions:
            p = revision['content']
            lines += ['## ' + revision['design_id'] + ' @ ' + str(revision['revision']) + ': ' + p['title'], '',
                      'Status: ' + p['lifecycle_status'] + '; kind: ' + p['kind'],
                      'Scope: `' + canonical(p['scope']) + '`', 'Content SHA-256: ' + revision['content_hash'], '',
                      '### ' + ('Approved statement' if p['lifecycle_status'] == 'approved' else 'Non-approved statement'),
                      '', p['statement'], '', '### Rationale', '', p['rationale'], '',
                      '### Open questions (not approved requirements)', '']
            lines += ['- ' + q for q in p['open_questions']] or ['None.']
            lines += ['', '### Exact relations', '']
            lines += ['- ' + r['type'] + ': ' + r['target']['design_id'] + ' @ ' + str(r['target']['revision']) +
                      ' — ' + r['rationale'] for r in p['relations']] or ['None.']
            lines += ['']
        markdown = '\n'.join(lines) + '\n'
        return {'baseline_id': baseline_id, 'manifest_hash': baseline['manifest_hash'],
                'markdown': markdown, 'markdown_sha256': hashlib.sha256(markdown.encode('utf-8')).hexdigest()}

    def check_markdown(self, project_id, baseline_id, markdown):
        if not isinstance(markdown, str) or len(markdown) > 32 * 1024 * 1024:
            raise StateGraphError('markdown must be bounded text')
        exported = self.export_markdown(project_id, baseline_id)
        return {'matches': markdown == exported['markdown'], 'baseline_id': baseline_id,
                'expected_sha256': exported['markdown_sha256'],
                'actual_sha256': hashlib.sha256(markdown.encode('utf-8')).hexdigest()}
