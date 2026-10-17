"""Actual host lifecycle composes Coding Mode ownership and native admission.

The shared fixture creates genuine workspaces/plans through the controller.
No guard, SDK method, run row or ownership index is replaced. The model fixture
is deterministic and no model call is made. Native refusal controls live in
this module's paired unit cohort, including calls inside controller context.
"""
import ast
import uuid
from collections import Counter
from pathlib import Path

import pytest
from skillflow import SkillFlowError

from core import coding_workspace as cw
from core.skillflow_host import AItelierSkillFlow
from test_coding_workspace_integration import (
    env, project, session, _engine_rows, _recovery_view,
)


def _plan(env, project):
    pid, _ = project(dirty=False)
    sid = session(env)
    req = cw.CodingRequest(env['db'], cw.authorize_entry(
        env['db'], session_id=sid, project_id=pid, writer='owned-lifecycle-test'))
    try:
        ws = req.workspace()
        started = req.plan_start('Retain this genuine plan for lifecycle controls')
        rid = started['run_id']
        assert req.plan_submit('plan', {'plan': 'Actual retained plan'})['status'] == 'paused'
        assert cw._index_get(ws.run_id)['role'] == 'owner'
        assert cw._index_get(rid)['role'] == 'plan'
        return pid, sid, rid
    finally:
        req.release()


def _invoke(sf, name, rid):
    if name == 'approve_checkpoint':
        return sf.approve_checkpoint(
            rid, expected_checkpoint=sf.get_checkpoint(rid)['expected_checkpoint'],
            decision_id='owned-' + uuid.uuid4().hex)
    if name == 'reject_checkpoint':
        return sf.reject_checkpoint(rid, 'plan', 'Please revise this retained plan')
    return getattr(sf, name)(rid)


def _snapshot(env):
    sf = env['sf']
    with sf._ro() as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'skillflow_%' ORDER BY name")]
        rows = {t: [dict(r) for r in conn.execute(f'SELECT * FROM "{t}" ORDER BY rowid')]
                for t in tables}
    files = {str(p.relative_to(cw._root())): p.read_bytes()
             for p in sorted(cw._root().rglob('*')) if p.is_file()}
    return rows, files


def test_generic_workspace_creation_refuses_before_sdk_effects(env, project):
    pid, _ = project(dirty=False)
    held = _snapshot(env)
    with pytest.raises(SkillFlowError, match='Coding Mode controller'):
        env['sf'].create_run(cw.GRAPH, project_id=pid)
    assert _snapshot(env) == held
    # The actual controller still creates and binds an authentic workspace.
    sid = session(env)
    req = cw.CodingRequest(env['db'], cw.authorize_entry(
        env['db'], session_id=sid, project_id=pid, writer='owned-lifecycle-test'))
    try:
        ws = req.workspace()
        assert ws.claimed.step_id == 'work'
        assert env['sf'].get_run(ws.run_id)['status'] == 'running'
    finally:
        req.release()


@pytest.mark.parametrize('name', [
    'approve_checkpoint', 'reject_checkpoint', 'resume_run', 'reactivate_run',
])
def test_governed_control_refuses_generic_call_but_controller_still_works(env, project, name):
    pid, sid, rid = _plan(env, project)
    sf = env['sf']
    if name == 'reactivate_run':
        lock = cw.ProjectLock(pid).acquire()
        try:
            with cw._trusted('decide'):
                sf.stop_run(rid, 'owned terminal fixture')
        finally:
            lock.release()
    held = _snapshot(env)
    with pytest.raises(SkillFlowError, match='Coding Mode'):
        _invoke(sf, name, rid)
    assert _snapshot(env) == held
    # Same genuine run, inside the existing controller context and project lock.
    lock = cw.ProjectLock(pid).acquire()
    try:
        with cw._trusted('decide'):
            _invoke(sf, name, rid)
    finally:
        lock.release()
    assert _snapshot(env) != held
    assert sf.get_run(rid)['status'] == 'running'


@pytest.mark.parametrize('name', [
    'approve_checkpoint', 'reject_checkpoint', 'resume_run', 'reactivate_run',
])
def test_ordinary_non_coding_run_keeps_public_lifecycle(env, project, name):
    from skillflow.plugins.skill_runner import RunnerService
    pid, _ = project(dirty=False)
    runner = RunnerService(env['sf'])
    rid = runner.start('coding_task', project_id=pid, seeds={'task.md': 'ordinary host run'})['run_id']
    assert runner.submit(rid, 'plan', {'plan': 'Ordinary retained plan'})['status'] == 'paused'
    assert cw._index_get(rid) is None
    if name == 'reactivate_run':
        env['sf'].stop_run(rid, 'ordinary terminal fixture')
    held = _recovery_view(_engine_rows(env, rid))
    _invoke(env['sf'], name, rid)
    assert _recovery_view(_engine_rows(env, rid)) != held
    assert env['sf'].get_run(rid)['status'] == 'running'


def test_host_class_has_one_effective_definition_per_method():
    import core.skillflow_host as host
    cls = next(n for n in ast.parse(Path(host.__file__).read_text()).body
               if isinstance(n, ast.ClassDef) and n.name == 'AItelierSkillFlow')
    counts = Counter(n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    assert {n: c for n, c in counts.items() if c > 1} == {}
