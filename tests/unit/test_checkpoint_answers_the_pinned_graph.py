"""Run-keyed checkpoint views preserve the actual pinned graph and pause."""
import pytest
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from api import meta_routers


@pytest.fixture
def runs(tmp_path, monkeypatch):
    sf = SkillFlow(str(tmp_path / 'sdk.db'), workspace_base=str(tmp_path / 'ws'),
                   projects_base=str(tmp_path / 'projects'))
    sf.register_graph(PipelineGraph(name='pinned_checkpoint', begin='a', steps=[
        StepNode(id='a', transitions=[Transition(to='b')]),
        StepNode(id='b', checkpoint=True, checkpoint_label='Original B review',
                 transitions=[Transition(to='done')]),
        StepNode(id='done', transitions=[])]))
    old = sf.create_run('pinned_checkpoint', project_id='pinned-proof')
    sf.start_run(old)
    for name in ['a', 'b']:
        sf.advance_run(old)
        claim = sf.claim_next_step(old)
        assert claim.step_id == name
        sf.confirm_step(claim.token, StepResult(outputs={'owned': name}))
    sf.advance_run(old)
    original = sf.get_checkpoint(old)
    sf.register_graph(PipelineGraph(name='pinned_checkpoint', begin='a', steps=[
        StepNode(id='a', checkpoint=True, checkpoint_label='New A review',
                 transitions=[Transition(to='b')]),
        StepNode(id='b', transitions=[Transition(to='done')]),
        StepNode(id='done', transitions=[])]))
    new = sf.create_run('pinned_checkpoint', project_id='pinned-proof')
    sf.start_run(new)
    claim = sf.claim_next_step(new)
    sf.confirm_step(claim.token, StepResult(outputs={'owned': 'new-a'}))
    sf.advance_run(new)
    newer = sf.get_checkpoint(new)
    assert original['graph_digest'] != newer['graph_digest']
    assert sf._get_resolver('pinned_checkpoint').get_node('b').checkpoint is False
    monkeypatch.setattr(meta_routers, 'get_skillflow', lambda: sf)
    return sf, old, new, {old: original, new: newer}


def test_it_asks_the_pinned_graph_not_the_current_one(runs):
    _, old, _, retained = runs
    step, label, rid, graph, instance = meta_routers._get_checkpoint_info('pinned-proof', old)
    assert (step, label, rid, graph, instance) == (
        'b', 'Original B review', old, 'pinned_checkpoint',
        retained[old]['expected_checkpoint']['step_instance_id'])


def test_it_answers_about_the_run_it_was_given(runs):
    sf, old, new, retained = runs
    selected = sf.get_run_by_project('pinned-proof')['id']
    wanted = new if selected == old else old
    observed = retained[wanted]
    result = meta_routers._get_checkpoint_info('pinned-proof', wanted)
    assert result[2] == wanted != selected
    assert (result[0], result[1], result[4]) == (
        observed['step_id'], observed['label'], observed['expected_checkpoint']['step_instance_id'])


def test_without_a_run_id_it_still_resolves_from_the_project(runs):
    sf, _, _, retained = runs
    selected = sf.get_run_by_project('pinned-proof')['id']
    observed = retained[selected]
    result = meta_routers._get_checkpoint_info('pinned-proof')
    assert (result[0], result[1], result[2], result[4]) == (
        observed['step_id'], observed['label'], selected,
        observed['expected_checkpoint']['step_instance_id'])
