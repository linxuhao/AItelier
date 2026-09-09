"""Small read-only design queries, not a semantic or execution engine.

Reuse immutable revisions/baselines. Scan the selected candidate text in Python
for Unicode-safe substring matching; no new index, database or model dependency.
Every response identifies its exact selection and explicitly limits its claims.
"""
from __future__ import annotations

import json

from core.state_graph import StateGraphError, canonical, digest, integer, text


SEARCH_FIELDS = (('design_id', 8), ('title', 4), ('statement', 2), ('rationale', 1))
SNIPPET_CHARS = 240


def _baseline(design, conn, project_id, baseline_id):
    design.store._project(conn, project_id)
    if baseline_id is None:
        row = conn.execute('SELECT baseline_id FROM state_design_heads WHERE project_id=?', (project_id,)).fetchone()
        baseline_id = row[0] if row else None
    baseline = design._baseline(conn, project_id, baseline_id) if baseline_id is not None else None
    selected = baseline['manifest']['selected_revisions'] if baseline else []
    return baseline, selected


def _baseline_label(baseline):
    return {'baseline_id': baseline['baseline_id'] if baseline else None,
            'manifest_hash': baseline['manifest_hash'] if baseline else None}


def _candidate_rows(conn, project_id, selected, include_latest):
    # The max-revision grouping is project-local. A newer draft never replaces
    # an older revision that the requested/current baseline actually selected.
    return conn.execute('''
        WITH latest AS (
            SELECT design_id,MAX(revision) revision FROM state_design_revisions
            WHERE project_id=? GROUP BY design_id
        ), candidates AS (
            SELECT json_extract(value,'$.design_id') design_id,
                   json_extract(value,'$.revision') revision FROM json_each(?)
            UNION SELECT design_id,revision FROM latest WHERE ?
        )
        SELECT r.*, (r.revision=l.revision) is_latest
        FROM candidates c JOIN state_design_revisions r
          ON r.project_id=? AND r.design_id=c.design_id AND r.revision=c.revision
        JOIN latest l ON l.design_id=r.design_id
        ORDER BY r.design_id,r.revision
    ''', (project_id, canonical(selected), int(include_latest), project_id))


def search(design, project_id, query, baseline_id=None, scope=None, limit=20, offset=0):
    """All whitespace-separated literal terms must match (case-insensitive).

    No SQL/FTS expression language, stemming, synonym inference or scope algebra.
    Scope is an optional exact dimension/value filter, not automatic exclusion.
    """
    query = text(query, 'search query', 200)
    terms = list(dict.fromkeys(query.casefold().split()))
    if len(terms) > 8:
        raise StateGraphError('search accepts at most 8 whitespace-separated literal terms')
    integer(limit, 'limit', 1, 100)
    integer(offset, 'offset', 0, 1000000)
    if scope is not None:
        from core.state_design import _scope
        scope = _scope(scope)
    with design.store.transaction() as conn:
        baseline, selected = _baseline(design, conn, project_id, baseline_id)
        pins = {(r['design_id'], r['revision']) for r in selected}
        matches, inventory = [], []
        for row in _candidate_rows(conn, project_id, selected, include_latest=baseline_id is None):
            content = json.loads(row['payload_json'])
            adopted = (row['design_id'], row['revision']) in pins
            inventory.append((row['design_id'], row['revision'], row['content_hash'], bool(row['is_latest'])))
            if scope and any(content['scope'].get(k) != v for k, v in scope.items()):
                continue
            fields = {'design_id': row['design_id'], **{k: content[k] for k in ('title', 'statement', 'rationale')}}
            folded = {k: v.casefold() for k, v in fields.items()}
            if not all(any(term in value for value in folded.values()) for term in terms):
                continue
            # One strongest field per term: long/repetitive prose cannot bury an
            # exact ID/title match by merely repeating a word many times.
            score = sum(max(weight for field, weight in SEARCH_FIELDS if term in folded[field]) for term in terms)
            matched = {field: [term for term in terms if term in folded[field]]
                       for field, _ in SEARCH_FIELDS if any(term in folded[field] for term in terms)}
            body = content['statement']
            matches.append({'design_id': row['design_id'], 'revision': row['revision'], 'content_hash': row['content_hash'],
                            'title': content['title'], 'lifecycle_status': content['lifecycle_status'], 'kind': content['kind'],
                            'scope': content['scope'], 'adopted': adopted, 'is_latest': bool(row['is_latest']),
                            'snippet': body[:SNIPPET_CHARS], 'snippet_truncated': len(body) > SNIPPET_CHARS,
                            'matched_fields': matched, 'score': score})
        matches.sort(key=lambda r: (-r['score'], not r['adopted'], r['design_id'], -r['revision']))
        total = len(matches)
        return {'project_id': project_id, 'query': query, **_baseline_label(baseline),
                'selection': 'explicit_baseline' if baseline_id is not None else 'current_baseline_and_latest',
                'selection_hash': digest({'baseline': _baseline_label(baseline), 'revisions': inventory}),
                'searched_revisions': len(inventory), 'items': matches[offset:offset+limit], 'total': total,
                'offset': offset, 'limit': limit, 'truncated': offset > 0 or offset+limit < total,
                'next_offset': offset+limit if offset+limit < total else None,
                'scope_filter': scope,
                'coverage': 'Literal candidate search only. No semantic completeness or conflict-free guarantee.'}
