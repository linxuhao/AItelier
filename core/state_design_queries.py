"""Small read-only design queries, not a semantic or execution engine.

Reuse immutable revisions/baselines. Scan the selected candidate text in Python
for Unicode-safe substring matching; no new index, database or model dependency.
Every response identifies its exact selection and explicitly limits its claims.
"""
from __future__ import annotations

from collections import deque
import json

from core.state_graph import StateConflict, StateGraphError, canonical, digest, integer, text


SEARCH_FIELDS = (('design_id', 8), ('title', 4), ('statement', 2), ('rationale', 1))
SNIPPET_CHARS = 240
PATH_LIMIT = 32


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


def _pin(revision):
    return revision['design_id'], revision['revision']


def _ref(pin):
    return {'design_id': pin[0], 'revision': pin[1]}


def _page(items, limit, *, complete=True):
    return {'items': items[:limit], 'total': len(items), 'total_is_exact': complete,
            'truncated': not complete or len(items) > limit}


def _binding_impacts(conn, project_id, subject, baseline_id, limit):
    """Read latest binding per node once, not one query per design or node.

    Match bound/dependency bodies, NEVER every pin in the copied baseline
    manifest. Older replaced bindings are retained in storage but are not
    current impacts. The latest snapshot may itself target an older baseline.
    """
    rows = conn.execute('''
        SELECT b.node_key,b.node_revision,b.snapshot_json,b.snapshot_hash,
               n.revision current_revision,n.status,n.verified_receipt
        FROM state_design_bindings b JOIN state_nodes n
          ON n.project_id=b.project_id AND n.node_key=b.node_key
        WHERE b.project_id=? AND b.node_revision=(
            SELECT MAX(newer.node_revision) FROM state_design_bindings newer
            WHERE newer.project_id=b.project_id AND newer.node_key=b.node_key
              AND newer.node_revision<=n.revision)
        ORDER BY b.node_key
    ''', (project_id,))
    affected, scanned = [], 0
    for row in rows:
        scanned += 1
        snapshot = json.loads(row['snapshot_json'])
        direct = [b['purpose'] for b in snapshot['bindings'] if _pin(b) == subject]
        dependency = any(_pin(d) == subject for d in snapshot['design_dependencies'])
        if not direct and not dependency:
            continue
        affected.append({'node_key': row['node_key'], 'node_revision': row['current_revision'], 'status': row['status'],
                         'verified_receipt': row['verified_receipt'], 'review_only': True,
                         'reason': 'direct_binding' if direct else 'bound_dependency', 'purposes': sorted(direct),
                         'binding_node_revision': row['node_revision'], 'binding_snapshot_hash': row['snapshot_hash'],
                         'binding_baseline_id': snapshot['baseline_id'], 'binding_manifest_hash': snapshot['manifest_hash'],
                         'matches_requested_baseline': snapshot['baseline_id'] == baseline_id,
                         'binding_matches_node_revision': row['node_revision'] == row['current_revision']})
    return {**_page(affected, limit), 'bindings_scanned': scanned,
            'scope': 'Latest binding snapshots only, including older-baseline bindings; not historical binding versions.'}


def impact(design, project_id, design_id, revision, baseline_id=None, limit=50, max_visits=1000):
    """Exact-version review hints. Nothing here edits relations, goals or runs.

    Incoming edges are restricted to selected baseline versions. Direct outgoing
    assertions of the requested revision stay visible even if it is not selected.
    Reverse depends_on traversal never follows conflicts/references/supersedes.
    """
    integer(limit, 'limit', 1, 100)
    integer(max_visits, 'max_visits', 1, 1000)
    with design.store.transaction() as conn:
        baseline, selected = _baseline(design, conn, project_id, baseline_id)
        if baseline is None:
            raise StateConflict('design_impact requires an explicit or current baseline; no selection exists')
        subject = design._revision(conn, project_id, design_id, revision)
        root = _pin(subject)
        bodies = {_pin(r): json.loads(r['payload_json'])
                  for r in _candidate_rows(conn, project_id, selected, include_latest=False)}
        pins = set(bodies)
        source_pins = {root, *pins}
        bodies[root] = subject['content']
        direct, reverse = [], {}
        for source in sorted(source_pins):
            for rel in bodies[source]['relations']:
                target = _pin(rel['target'])
                if source in pins and rel['type'] == 'depends_on':
                    reverse.setdefault(target, []).append(source)
                if source != root and target != root:
                    continue
                direct.append({'type': rel['type'], 'source': _ref(source), 'target': _ref(target),
                               'direction': 'outgoing' if source == root else 'incoming',
                               'source_selected': source in pins, 'target_selected': target in pins,
                               'rationale': rel['rationale'][:360], 'rationale_truncated': len(rel['rationale']) > 360})
        direct.sort(key=lambda r: (r['type'], r['direction'], r['source']['design_id'], r['source']['revision'],
                                  r['target']['design_id'], r['target']['revision']))
        # One deterministic shortest reverse-dependency path per visited design.
        parents, queue, clipped = {root: None}, deque([root]), False
        while queue:
            target = queue.popleft()
            for child in sorted(reverse.get(target, [])):
                if child in parents:
                    continue
                if len(parents) >= max_visits:
                    clipped = True
                    continue
                parents[child] = target
                queue.append(child)
        dependents = []
        for pin in sorted(set(parents) - {root}):
            path, cursor = [], pin
            while cursor is not None:
                path.append(_ref(cursor))
                cursor = parents[cursor]
            path.reverse()
            dependents.append({**_ref(pin), 'title': bodies[pin]['title'], 'lifecycle_status': bodies[pin]['lifecycle_status'],
                               'path': path if len(path) <= PATH_LIMIT else path[:PATH_LIMIT//2]+path[-PATH_LIMIT//2:],
                               'path_length': len(path), 'path_truncated': len(path) > PATH_LIMIT,
                               'path_direction': 'changed_design_to_dependent', 'review_only': True})
        declared_conflicts = [{**edge, 'message': 'Declared direct conflict; review the recorded rationale and applicable scope.',
                               'both_selected': edge['source_selected'] and edge['target_selected']}
                              for edge in direct if edge['type'] == 'conflicts_with']
        return {'project_id': project_id, **_baseline_label(baseline),
                'subject': {**_ref(root), 'content_hash': subject['content_hash'], 'title': subject['content']['title'],
                            'scope': subject['content']['scope'], 'lifecycle_status': subject['content']['lifecycle_status'],
                            'selected': root in pins},
                'direct_relations': _page(direct, limit), 'declared_conflicts': _page(declared_conflicts, limit),
                'dependent_designs': _page(dependents, limit, complete=not clipped),
                'affected_nodes': _binding_impacts(conn, project_id, root, baseline['baseline_id'], limit),
                'traversal': {'visited': len(parents), 'max_visits': max_visits, 'truncated': clipped,
                              'path_limit': PATH_LIMIT},
                'coverage': 'Review hints from recorded exact relations, not semantic proof. Conflicts are symmetric to query, never propagated. '
                            'Design edges never change State requires edges or execution. Latest node bindings are a live snapshot, not part of the historical baseline.'}
