"""Read-only related-run summary for a State project.

Membership comes from explicit attempt/run references, never shared repo paths.
No reconciliation, task dispatch or State verification happens here. Exact run
IDs and usage events are deduplicated before summation. Finished means workflow
completed, not an accepted State fact. External jobs have no synthetic run/usage.
"""
from __future__ import annotations
from collections import Counter

from core.state_graph import now

# trace_query deliberately accepts only SELECT, not a WITH-prefixed query.
# Nested SELECTs make malformed JSON and missing/provider-unknown counters safe.
USAGE_SQL = """
SELECT COUNT(*) AS turns,
 COALESCE(SUM(CASE WHEN token_valid THEN 1 ELSE 0 END),0) AS token_turns,
 COALESCE(SUM(CASE WHEN cache_valid THEN 1 ELSE 0 END),0) AS cache_turns,
 COALESCE(SUM(CASE WHEN token_valid THEN prompt ELSE 0 END),0) AS prompt,
 COALESCE(SUM(CASE WHEN token_valid THEN completion ELSE 0 END),0) AS completion,
 COALESCE(SUM(CASE WHEN cache_valid THEN hit ELSE 0 END),0) AS hit,
 COALESCE(SUM(CASE WHEN cache_valid THEN miss ELSE 0 END),0) AS miss
FROM (
 SELECT json_extract(p,'$.prompt_tokens') AS prompt,
        json_extract(p,'$.completion_tokens') AS completion,
        json_extract(p,'$.cache_hit_tokens') AS hit,
        json_extract(p,'$.cache_miss_tokens') AS miss,
        (json_type(p,'$.prompt_tokens')='integer' AND json_extract(p,'$.prompt_tokens')>=0
         AND json_type(p,'$.completion_tokens')='integer' AND json_extract(p,'$.completion_tokens')>=0) AS token_valid,
        (json_type(p,'$.cache_hit_tokens')='integer' AND json_extract(p,'$.cache_hit_tokens')>=0
         AND json_type(p,'$.cache_miss_tokens')='integer' AND json_extract(p,'$.cache_miss_tokens')>=0) AS cache_valid
 FROM (SELECT CASE WHEN json_valid(payload_json) THEN payload_json ELSE '{}' END AS p
       FROM skillflow_trace WHERE run_id=? AND category='usage' AND event='token_usage')
)
"""


def project_run_summary(service, project_id):
    """Private projection; returns identities ONLY for currently running runs."""
    with service.store.transaction() as conn:
        service.store._project(conn, project_id)
        membership = {}
        for row in conn.execute("SELECT run_id,node_key FROM state_attempts WHERE project_id=? AND run_id IS NOT NULL",
                                (project_id,)):
            membership.setdefault(row['run_id'], set()).add(row['node_key'])
        for row in conn.execute("SELECT ref,node_key FROM state_history_links WHERE project_id=? AND kind='run'",
                                (project_id,)):
            membership.setdefault(row['ref'], set()).add(row['node_key'])
        external = conn.execute("SELECT COUNT(*) FROM state_attempts WHERE project_id=? AND execution_kind='external'",
                                (project_id,)).fetchone()[0]
    counts = {'total':len(membership),'running':0,'finished':0,'failed':0,'other':0,'unavailable':0}
    sums = Counter()
    running = []
    usage_runs = 0
    errors = 0
    engine = service.sf
    engine_unavailable = False
    if membership and engine is None:
        try:
            if service.runtime_factory is None:
                raise RuntimeError('No workflow read adapter configured')
            engine, _ = service.runtime_factory()
            if engine is None:
                raise RuntimeError('Workflow observer unavailable')
        except Exception:
            # Project graph still works when its optional executor is down.
            # Do not report zero activity as if it had actually been observed.
            engine_unavailable = True
    for run_id, node_keys in sorted(membership.items()):
        if engine_unavailable:
            counts['unavailable'] += 1
            continue
        try:
            row = engine.get_run(run_id)
            if not isinstance(row, dict) or row.get('id') != run_id:
                raise LookupError('Exact associated run no longer resolves')
            status = row.get('status')
            if status not in {'pending','running','paused','completed','failed'}:
                raise LookupError('Unknown workflow status')
        except Exception:
            counts['unavailable'] += 1
            continue
        if status == 'running':
            counts['running'] += 1
            running.append({'run_id':run_id,'workflow':row.get('graph_name') or '',
                            'current_node':row.get('current_node'), 'node_keys':sorted(node_keys),
                            'started_at':row.get('started_at'),'status':'running'})
        elif status == 'completed':
            counts['finished'] += 1
        elif status == 'failed':
            counts['failed'] += 1
        else:
            counts['other'] += 1
        try:
            stats = engine.trace_query(run_id, USAGE_SQL, (run_id,))
            if len(stats) != 1:
                raise ValueError('Incomplete usage result')
            values = {key:int(stats[0][key]) for key in ('turns','token_turns','cache_turns','prompt','completion','hit','miss')}
            if any(value<0 for value in values.values()):
                raise ValueError('Invalid usage totals')
            sums.update(values)
            if values['token_turns']:
                usage_runs += 1
        except Exception:
            errors += 1
    running.sort(key=lambda row:(row['started_at'] or '',row['run_id']),reverse=True)
    covered = sums['hit']+sums['miss']
    no_runs = not membership
    usage = {
        'total_tokens':sums['prompt']+sums['completion'] if sums['token_turns'] or no_runs else None,
        'prompt_tokens':sums['prompt'] if sums['token_turns'] or no_runs else None,
        'completion_tokens':sums['completion'] if sums['token_turns'] or no_runs else None,
        'cache_hit_tokens':sums['hit'] if sums['cache_turns'] else None,
        'cache_miss_tokens':sums['miss'] if sums['cache_turns'] else None,
        'cache_hit_ratio':sums['hit']/covered if covered else None,
        'cache_covered_tokens':covered,
        'usage_turns':sums['turns'],'token_reported_turns':sums['token_turns'],
        'cache_reported_turns':sums['cache_turns'],'runs_with_token_usage':usage_runs,
        'runs_without_token_usage':len(membership)-usage_runs,
        'usage_errors':errors,
        'partial':bool(membership) and (usage_runs!=len(membership) or sums['token_turns']!=sums['turns']),
    }
    return {'project_id':project_id,'counts':counts,'running_runs':running,'usage':usage,
            'external_attempts_excluded':external,'observed_at':now(),
            'runtime_unavailable':engine_unavailable,
            'scope':'Distinct workflow runs bound to State attempts or explicit run references; external executions are not synthetic runs.'}
