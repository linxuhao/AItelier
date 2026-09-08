"""Private actual-run history, separate from legacy execution-project summaries."""
from fastapi import APIRouter, Depends, Query, Request
from api.auth import get_optional_user
from api.authz import require_writer
from api.dependencies import get_db_manager, get_skillflow, owner_filter

router = APIRouter(prefix="/api", tags=["Run history"], dependencies=[Depends(require_writer)])


@router.get("/run-history")
def run_history(request: Request, q: str = Query("", max_length=200),
                status: str = Query("", max_length=40), workflow: str = Query("", max_length=128),
                state_project_id: str = Query("", max_length=128),
                offset: int = Query(0, ge=0, le=100000), limit: int = Query(50, ge=1, le=100),
                db=Depends(get_db_manager), sf=Depends(get_skillflow), user=Depends(get_optional_user)):
    """Every real SkillFlow run once, including repeat/authoring/repo-less runs.

    A projection only: never initialize State tables, reconcile or launch work.
    Restrict by the host's owner scope before adding private node identities.
    Filtering precedes paging; input/output/trace payloads are not returned.
    """
    owner = owner_filter(user, request)
    with db.get_connection() as conn:
        projects = {row['project_id']: dict(row) for row in conn.execute(
            'SELECT project_id,name,repo_path,owner_email FROM runs')}
        linked = {}
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'state_attempts' in tables:
            linked = {r['run_id']: dict(r) for r in conn.execute(
                'SELECT run_id,project_id,node_key,attempt_id FROM state_attempts WHERE run_id IS NOT NULL')}
    rows = []
    query = q.casefold().strip()
    for run in sf.list_runs():
        pid = run.get('project_id')
        project = projects.get(pid, {})
        if owner is not None and project.get('owner_email') != owner:
            continue
        link = linked.get(run['id'], {})
        if state_project_id and link.get('project_id') != state_project_id:
            continue
        if status and run.get('status') != status:
            continue
        if workflow and run.get('graph_name') != workflow:
            continue
        row = {field: run.get(field) for field in ('id','project_id','status','current_node','created_at','updated_at','started_at','completed_at','graph_version')}
        row.update(config_name=run.get('graph_name'), execution_name=project.get('name') or pid or run['id'],
                   repo_path=project.get('repo_path'), state_project_id=link.get('project_id'),
                   state_node_key=link.get('node_key'), attempt_id=link.get('attempt_id'))
        if query and query not in ' '.join(str(v or '') for v in row.values()).casefold():
            continue
        rows.append(row)
    rows.sort(key=lambda r: (r['created_at'] or '', r['id']), reverse=True)
    page = rows[offset:offset+limit]
    return {'runs': page, 'total': len(rows), 'next_offset': offset+limit if offset+limit<len(rows) else None,
            'scope': 'actual workflow runs; not execution-project summaries'}
