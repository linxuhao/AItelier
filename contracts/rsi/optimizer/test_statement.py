"""STATEMENT/fake/SDK compile only. Never run the native evaluator or model."""
import importlib.metadata
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from skillflow.graph import PipelineGraph, GraphResolver
from contracts.rsi.evaluator.contract import digest
from contracts.rsi.pipeline_versioning.contract import version_id_of
from core.pipeline_optimization import evaluate_matrix
from .contract import PORTS, ROLES, RoundRequest, RoundView, Hypothesis, Edit, OptimizerError, graph_result
from .fake import FakeRound, literal
from .conformance import SCENARIOS, run_scenario, edit_fixture
from .profiles import for_scenario

ROOT=Path(__file__).parent

def evidence(name,obj):
    folder=os.getenv('OPTIMIZER_EVIDENCE')
    if folder: Path(folder,name+'.json').write_text(json.dumps(obj,indent=2)+'\n')


@pytest.mark.parametrize('vector',SCENARIOS,ids=lambda v:v['name'])
def test_shared_round_scenarios(vector):
    # The same predeclared profile the native entrypoint selects, before prepare.
    profile=for_scenario(vector['name'])
    evidence('scenario-'+vector['name'],run_scenario(lambda:FakeRound(profile=profile),vector))


def test_actual_sdk_graph_order_checkpoint_and_tool_ports():
    graph=PipelineGraph.from_yaml(ROOT/'round.graph.json');resolver=GraphResolver(graph)
    assert graph.validate()==resolver.validate()==[]
    assert {n.id for n in graph.steps}==set(PORTS)
    assert all(n.step_type=='tool' and n.tool_name=='rsi_opt_'+n.id and n.max_retries==0
               and not n.context and not n.capability for n in graph.steps)
    assert {n.id for n in graph.steps if n.checkpoint}=={'hypothesis','train_compare','adoption_review'}
    for node,next_node in [('freeze','train_reference'),('train_reference','diagnose'),
                           ('diagnose','hypothesis'),('candidate','admissibility'),
                           ('admissibility','train_candidate'),('train_candidate','train_compare'),
                           ('holdout_seal','holdout'),('holdout','compare'),('compare','decision')]:
        assert resolver.next_node(node,{'continue':True},{})==next_node
        assert resolver.next_node(node,{}, {})=='stop'
    for node,target,flags in [('hypothesis','candidate',{'continue':True}),
                               ('train_compare','holdout_seal',{'continue':True}),
                               ('adoption_review','adopt',{'action':'adopt'}),
                               ('adoption_review','source_patch',{'action':'source_patch'})]:
        assert resolver.resolve_transition(node,flags,{},checkpoint_approved=True)[1]==target
        assert resolver.resolve_transition(node,flags,{},checkpoint_approved=False)[1]!=target
    v=FakeRound().view()
    assert resolver.next_node('decision',graph_result(v), {})=='stop'  # manual default
    for action in ['adopt','source_patch']:
        flags=graph_result(v.model_copy(update={'phase':'adoption_review','action':action}))
        assert resolver.next_node('decision',flags,{})=='adoption_review'
        assert resolver.resolve_transition('adoption_review',flags,{},checkpoint_approved=True)[1]==action
    assert ROLES['candidate']=='proposer' and ROLES['train_candidate']=='coordinator'
    assert ROLES['holdout_seal']==ROLES['adopt']=='owner'
    assert importlib.metadata.version('skillflow-py')=='1.5.94'
    evidence('graph-compile',{'schema_valid':True,'sdk':'1.5.94',
        'tool_ports':{n.id:n.tool_name for n in graph.steps},'roles':ROLES,
        'native_graph_run_or_dispatch_proven':False})


def test_strict_schema_unknown_inputs_and_no_spend_grant():
    f=FakeRound();q=f.prepare();payload=q.model_dump(mode='json')
    for changed in [dict(payload,unknown=True),dict(payload,round_number=True),
                    dict(payload,mode='autonomous'),dict(payload,design='wrong')]:
        with pytest.raises(ValidationError):RoundRequest.model_validate_json(json.dumps(changed))
    f.actor='coordinator';before=f.snapshot()
    with pytest.raises(OptimizerError) as e:f.invoke('unknown',{})
    assert e.value.code=='UNKNOWN_PORT' and f.snapshot()==before
    with pytest.raises(OptimizerError):f.set_fault('unknown')
    f.authorized=False
    with pytest.raises(OptimizerError) as e:f.invoke('freeze',payload)
    assert e.value.code=='AUTHORITY' and not f.events
    evidence('wire-schemas',{c.__name__:c.model_json_schema() for c in [RoundRequest,RoundView,Hypothesis,Edit]})


def test_literal_useful_edit_is_not_a_common_token_blacklist():
    f=literal();assert version_id_of(f['logical_reference'])==f['reference_version']
    fake=FakeRound();content=fake._check_edit(Edit.model_validate_json(json.dumps(edit_fixture('useful'))))
    assert content['roles'][f['logical_role']]['prompt'] != f['logical_reference']['roles'][f['logical_role']]['prompt']
    # A second ordinary useful proposal shares task/report/repository words and
    # remains admissible. Only exact frozen case IDs/literals are excluded.
    edit={'prompts':{f['prompt_key']:'Read the public repository, cite evidence, and write the required findings report.\n'}}
    assert version_id_of(fake._check_edit(Edit.model_validate_json(json.dumps(edit))))!=f['reference_version']
    for kind,code in [('case_id','CONTAMINATION'),('case_literal','CONTAMINATION'),
                       ('topology','FORBIDDEN_EDIT'),('route','FORBIDDEN_EDIT')]:
        before=fake.snapshot()
        with pytest.raises(OptimizerError) as e:fake._check_edit(Edit.model_validate_json(json.dumps(edit_fixture(kind))))
        assert e.value.code==code and fake.snapshot()==before


def test_all_new_python_compiles_and_no_native_provider_invoked():
    import sys
    for path in ROOT.glob('*.py'):compile(path.read_bytes(),str(path),'exec')
    assert 'core.rsi_evaluator' not in sys.modules
    assert 'contracts.rsi.evaluator.native_fixture' not in sys.modules
    assert evaluate_matrix.__module__=='core.pipeline_optimization'
    evidence('scope',{'fake_only':True,'native_evaluator_invoked':False,
                      'numeric_module':evaluate_matrix.__module__,'codec':version_id_of.__module__})
