"""A genuine restart preserves routed checkpoint identity.

The modern SDK owns checkpoint resolution. A NULL/empty pointer that disagrees
with the retained routed pause is corruption, not a reason to guess another
checkpoint. Keep that refusal separate from ordinary process recovery.
"""
import pytest
from skillflow.core import SkillFlow, SkillFlowError, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition


def paused_checkpoint(tmp_path):
    path = tmp_path / 'checkpoint.db'
    options = dict(workspace_base=str(tmp_path / 'ws'), projects_base=str(tmp_path / 'projects'))
    sf = SkillFlow(str(path), **options)
    sf.register_graph(PipelineGraph(name='restart_proof', begin='plan', steps=[
        StepNode(id='plan', checkpoint=True, checkpoint_label='Plan review',
                 transitions=[Transition(to='vision')]),
        StepNode(id='vision', checkpoint=True, checkpoint_label='Vision review',
                 transitions=[Transition(to='done')]),
        StepNode(id='done', transitions=[])]))
    rid = sf.create_run('restart_proof', project_id='restart-proof')
    sf.start_run(rid)
    claim = sf.claim_next_step(rid)
    sf.confirm_step(claim.token, StepResult(outputs={'plan': 'owned reviewed plan'}))
    sf.advance_run(rid)
    first = sf.get_checkpoint(rid)
    sf.approve_checkpoint(rid, expected_checkpoint=first['expected_checkpoint'], decision_id='plan-approved')
    sf.advance_run(rid)
    claim = sf.claim_next_step(rid)
    assert claim.step_id == 'vision'
    sf.confirm_step(claim.token, StepResult(outputs={'frames': 'owned reviewed frames'}))
    sf.advance_run(rid)
    retained = sf.get_checkpoint(rid)
    assert retained['step_id'] == 'vision'
    assert retained['expected_checkpoint'] != first['expected_checkpoint']
    return sf, rid, retained, path, options


def test_pointer_present_identifies_the_exact_checkpoint(tmp_path, monkeypatch):
    from api import meta_routers
    sf, rid, retained, _, _ = paused_checkpoint(tmp_path)
    monkeypatch.setattr(meta_routers, 'get_skillflow', lambda: sf)
    assert meta_routers._get_checkpoint_info('restart-proof') == (
        'vision', 'Vision review', rid, 'restart_proof', retained['expected_checkpoint']['step_instance_id'])


def test_fresh_engine_and_real_recovery_preserve_the_retained_pause(tmp_path, monkeypatch):
    from api import meta_routers
    _, rid, retained, path, options = paused_checkpoint(tmp_path)
    reopened = SkillFlow(str(path), **options)
    reopened.recover_stale_claims(run_id=rid)
    assert reopened.get_checkpoint(rid) == retained
    monkeypatch.setattr(meta_routers, 'get_skillflow', lambda: reopened)
    step, label, actual_run, _, instance = meta_routers._get_checkpoint_info('restart-proof', rid)
    assert (step, label, actual_run, instance) == (
        'vision', 'Vision review', rid, retained['expected_checkpoint']['step_instance_id'])
    reopened.approve_checkpoint(rid, expected_checkpoint=retained['expected_checkpoint'],
                                decision_id='vision-after-restart')
    reopened.advance_run(rid)
    assert reopened.claim_next_step(rid).step_id == 'done'


@pytest.mark.parametrize('pointer', [None, ''], ids=['null', 'empty'])
def test_corrupt_pointer_is_refused_without_guessing_a_checkpoint(tmp_path, monkeypatch, pointer):
    from api import meta_routers
    sf, rid, retained, _, _ = paused_checkpoint(tmp_path)
    with sf._tx() as conn:
        conn.execute('UPDATE skillflow_runs SET current_node=? WHERE id=?', (pointer, rid))
    monkeypatch.setattr(meta_routers, 'get_skillflow', lambda: sf)
    with pytest.raises(SkillFlowError, match='matching routed checkpoint pause'):
        meta_routers._get_checkpoint_info('restart-proof', rid)
    assert sf.get_run(rid)['status'] == 'paused'
    assert sf.get_run(rid)['current_node'] == pointer
    assert retained['step_id'] == 'vision'


def test_restart_keeps_the_most_recent_checkpoint_not_the_first(tmp_path, monkeypatch):
    from api import meta_routers
    _, rid, retained, path, options = paused_checkpoint(tmp_path)
    reopened = SkillFlow(str(path), **options)
    monkeypatch.setattr(meta_routers, 'get_skillflow', lambda: reopened)
    result = meta_routers._get_checkpoint_info('restart-proof', rid)
    assert result[0] == retained['step_id'] == 'vision'
    assert result[4] == retained['expected_checkpoint']['step_instance_id']
