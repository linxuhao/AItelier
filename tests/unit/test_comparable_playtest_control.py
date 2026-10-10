"""Native report contract from emitted frames/nodes, not presence-only proxies."""
import importlib.util
import json
import math
from pathlib import Path
import pytest

p=Path(__file__).resolve().parents[2]/'docker/godot/godot_harness.py'
s=importlib.util.spec_from_file_location('gh_comparable_control',p)
g=importlib.util.module_from_spec(s);s.loader.exec_module(g)


def request():
    return {'scenarios':[{'name':'driven','execution_budget':{'max_frames':50,'timeout_seconds':.3},
                         'timeline':[{'at':10,'actions':['advance']},{'at':20,'assert':{'N.x':'x == 1'}}]}]}


def native_report(frames,nodes,complete=True):
    # Actual injected _finish shape. Empty nodes is a real observed empty tree;
    # missing nodes is never substituted for it.
    return {'frames':frames,'nodes':nodes,'complete':complete,'asserts':[],
            'captures':[],'spec_errors':[],'scheduled_asserts':0,'asserts_omitted':0,
            'asserts_missing':0,'baseline_missing':{},'before_captures':{},
            'asserts_incomplete':0,'captures_unobserved':[],
            'timing':{'frames_stepped':frames,'game_usec':int(frames/60*1000000)}}


def exercise(monkeypatch,tmp_path,control,driven=None):
    calls=[]
    def once(dst,path,frames,timeout,env,**kwargs):
        timeline=json.loads(Path(env['AITELIER_PROBE_SPEC']).read_text())['timeline']
        calls.append(timeline)
        kwargs.get('timing',{}).update(game_usec=int(frames/60*1000000))
        if not timeline:return control,[],False
        result=native_report(frames,{'N':{'x':True}}) if driven is None else driven
        result['asserts']=[{'passed':True,'measurement':'ok'}]
        return result,[],False
    monkeypatch.setattr(g,'_run_probe',once)
    result=g._playtest_spec(tmp_path/'project',request(),20,.5)
    return result,calls


@pytest.mark.parametrize('malformed',[7,False,3.5,{},'timeline'])
def test_bad_timeline_type_is_structured_refusal_before_copy_import(monkeypatch,tmp_path,malformed):
    (tmp_path/'project.godot').write_text('[application]\n')
    effects=[]
    monkeypatch.setattr(g,'_copy_project',lambda *_:effects.append('copy'))
    monkeypatch.setattr(g,'_import_resources',lambda *_:effects.append('import'))
    data=request();data['scenarios'][0]['timeline']=malformed
    result=g.playtest_project(str(tmp_path),spec=data,timeout=.5)
    assert not result['passed'] and result['spec_errors'] and effects==[]
    assert any('timeline' in e for e in result['spec_errors'])


@pytest.mark.parametrize('control',[{}, {'frames':50,'complete':True},
    {'frames':10,'nodes':{'N':{'x':False}},'asserts':[]},
    {'frames':10,'nodes':{'N':{'x':False}},'complete':True},
    {'nodes':{'N':{'x':False}},'complete':True},
    {'frames':math.inf,'nodes':{'N':{'x':False}}},
    {'frames':50,'nodes':{'N':[]}},
    {'frames':50,'nodes':{'N':{'x':False}},'timing':{'frames_stepped':10}},
    {'frames':50,'nodes':{'N':{'x':False}},'complete':1}])
def test_control_deletion_partial_and_proxy_mutants_cannot_buy_green(monkeypatch,tmp_path,control):
    result,calls=exercise(monkeypatch,tmp_path,control)
    assert not result['passed']
    assert any('no-input control' in e for e in result['spec_errors'])
    assert not result['behavior']['scenarios'][0]['input_dead']


@pytest.mark.parametrize('legacy',[False,True])
def test_real_native_shaped_complete_comparable_control_remains_positive(monkeypatch,tmp_path,legacy):
    control=native_report(50,{'N':{'x':False}})
    if legacy:control.pop('complete')
    result,calls=exercise(monkeypatch,tmp_path,control)
    assert result['passed'] and not result['spec_errors']
    assert not result['behavior']['scenarios'][0]['input_dead'] and len(calls)==2


def test_unchanged_real_control_preserves_actual_input_dead_gate(monkeypatch,tmp_path):
    result,_=exercise(monkeypatch,tmp_path,native_report(50,{'N':{'x':True}}))
    assert not result['passed'] and result['behavior']['scenarios'][0]['input_dead']


def test_observed_empty_tree_is_not_missing_snapshot_and_still_compares(monkeypatch,tmp_path):
    # Real empty no-input tree versus a driven script tree proves an effect.
    result,_=exercise(monkeypatch,tmp_path,native_report(50,{}))
    assert result['passed'] and not result['spec_errors']
    # Real empty trees on both sides prove unchanged input, never bypass by
    # the false truthiness of the empty digest.
    driven=native_report(50,{})
    result,_=exercise(monkeypatch,tmp_path,native_report(50,{}),driven=driven)
    assert not result['passed'] and result['behavior']['scenarios'][0]['input_dead']


@pytest.mark.parametrize('driven',[{'frames':50,'complete':True},
    {'frames':10,'nodes':{'N':{'x':True}},'asserts':[]}])
def test_both_sides_require_comparable_observed_extent(monkeypatch,tmp_path,driven):
    result,calls=exercise(monkeypatch,tmp_path,native_report(50,{'N':{'x':False}}),driven)
    assert not result['passed'] and any('driven state' in e for e in result['spec_errors'])
    assert len(calls)==1
