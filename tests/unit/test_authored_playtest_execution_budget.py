"""Actual platform admission and owned CPU process behavior; no Godot execution."""
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest

HARNESS=Path(__file__).resolve().parents[2]/'docker/godot/godot_harness.py'
spec=importlib.util.spec_from_file_location('gh_execution_budget',HARNESS)
gh=importlib.util.module_from_spec(spec);spec.loader.exec_module(gh)


def authored(last=48120, ceiling=None, seconds=0.5):
    scenario={'name':'late','timeline':[
        {'at':last-2,'capture_before':[{'id':'b','node':'N','attr':'x','action_frame':last-1}]},
        {'at':last-1,'actions':['advance']},
        {'at':last,'assert':[{'node':'N','attr':'x','mode':'changed','before':'b'}]}]}
    if ceiling is not None:scenario['execution_budget']={'max_frames':ceiling,'timeout_seconds':seconds}
    return {'scenarios':[scenario]}


def recorder(monkeypatch):
    calls=[]
    def probe(dst,state_path,frames,timeout,extra,**kwargs):
        data=json.loads(Path(extra['AITELIER_PROBE_SPEC']).read_text())
        calls.append({'frames':frames,'timeout':timeout,'data':data})
        kwargs.get('timing',{}).update(game_usec=int(frames/60*1000000))
        assertions=[{'name':str(i),'passed':True} for i in range(sum(len(e.get('assert',[])) for e in data['timeline']))]
        return {'frames':frames,'complete':True,'nodes':{'N':{'x':bool(data['timeline'])}},'asserts':assertions},[],False
    monkeypatch.setattr(gh,'_run_probe',probe)
    return calls


@pytest.mark.parametrize('last',[4275,48080,48120])
def test_explicit_budget_reaches_every_original_input_assert_before_event(monkeypatch,tmp_path,last):
    calls=recorder(monkeypatch);request=authored(last,last+30)
    report=gh._playtest_spec(tmp_path/'project',request,180,0.75)
    assert report['passed'],report
    scenario=calls[0]
    assert scenario['frames']==last+30 and scenario['timeout']==0.5
    timeline=scenario['data']['timeline']
    assert [e['at'] for e in timeline]==[last-2,last-1,last]
    assert timeline[0]['capture_before']==request['scenarios'][0]['timeline'][0]['capture_before']
    assert timeline[1]['press']=='advance'
    assert timeline[2]['assert'][0]['before']=='b'
    # Genuine no-input control gets the SAME frame AND wall budget.
    assert calls[1]['frames']==last+30 and calls[1]['timeout']==0.5 and calls[1]['data']['timeline']==[]


@pytest.mark.parametrize('case_spec',[authored(),authored(48120,48149),
    {'frames':5000,'scenarios':[{'name':'floor','timeline':[{'at':1,'assert':{'N.x':'x==1'}}]}]}])
def test_default_undersized_and_frame_floor_never_execute_partial(monkeypatch,tmp_path,case_spec):
    calls=recorder(monkeypatch)
    report=gh._playtest_spec(tmp_path/'project',case_spec,180,0.75)
    assert not report['passed'] and report['spec_errors']
    assert calls==[]
    assert all(not s['ran'] for s in report['behavior']['scenarios'])


@pytest.mark.parametrize('budget',[{}, {'max_frames':48150}, {'max_frames':48150,'timeout_seconds':0.5,'other':1},
    {'max_frames':True,'timeout_seconds':0.5}, {'max_frames':0,'timeout_seconds':0.5},
    {'max_frames':gh._MAX_JSON_FRAME+1,'timeout_seconds':0.5},
    {'max_frames':48150,'timeout_seconds':math.inf},{'max_frames':48150,'timeout_seconds':math.nan},
    {'max_frames':48150,'timeout_seconds':-1},{'max_frames':48150,'timeout_seconds':True},
    {'max_frames':48150,'timeout_seconds':0.8}])
def test_invalid_and_unsupported_budget_refuses_whole_spec_before_copy_import(monkeypatch,tmp_path,budget):
    (tmp_path/'project.godot').write_text('[application]\n')
    calls=[]
    monkeypatch.setattr(gh,'_copy_project',lambda *_:calls.append('copy'))
    monkeypatch.setattr(gh,'_import_resources',lambda *_:calls.append('import'))
    request=authored(48120,48150);request['scenarios'][0]['execution_budget']=budget
    report=gh.playtest_project(str(tmp_path),spec=request,timeout=0.75)
    assert not report['passed'] and report['spec_errors'] and calls==[]


@pytest.mark.parametrize('timeout',[0,-1,True,math.inf,math.nan,1e100,'120'])
def test_invalid_caller_timeout_is_structured_preexecution_refusal(monkeypatch,tmp_path,timeout):
    calls=recorder(monkeypatch)
    report=gh._playtest_spec(tmp_path/'project',authored(10,40,0.1),180,timeout)
    assert not report['passed'] and any('caller timeout' in e for e in report['spec_errors']) and calls==[]


def test_one_bad_scenario_refuses_every_scenario_and_unknown_fields(monkeypatch,tmp_path):
    calls=recorder(monkeypatch)
    request=authored(20,50);request['scenarios'].append({'name':'unknown','timeline':[{'at':5,'notes':{'assert':{'N.x':'true'}}}]})
    report=gh._playtest_spec(tmp_path/'project',request,180,0.75)
    assert not report['passed'] and calls==[]
    assert any('unknown' in e for e in report['spec_errors'])


def test_capture_allocation_uses_bounded_capture_count_not_frame_count():
    captures=gh._capture_frames(gh._MAX_JSON_FRAME,[{'at':gh._MAX_JSON_FRAME-30,'assert':[{}]}],limit=gh._MAX_CAPTURES)
    assert 0<len(captures)<=gh._MAX_CAPTURES
    assert gh._MAX_JSON_FRAME-30 in captures
    assert int(float(gh._MAX_JSON_FRAME))==gh._MAX_JSON_FRAME
    assert not gh._valid_frame_count(gh._MAX_JSON_FRAME+1)


def test_a_real_timeout_cannot_be_erased_by_headless_fallback(monkeypatch,tmp_path):
    calls=[]
    def once(args,env,path,timeout,render,**kwargs):
        calls.append((timeout,render));return {},[],True
    monkeypatch.setattr(gh,'_probe_once',once)
    report,errs,timed_out=gh._run_probe(tmp_path,tmp_path/'state',20,0.2,{})
    assert timed_out and report=={} and len(calls)==1


def test_non_timeout_renderer_failure_uses_only_remaining_shared_deadline(monkeypatch,tmp_path):
    calls=[]
    def once(args,env,path,timeout,render,**kwargs):
        calls.append((timeout,render))
        if render:time.sleep(0.03);return {},[],False
        return {'frames':20},[],False
    monkeypatch.setattr(gh,'_probe_once',once)
    report,errs,timed_out=gh._run_probe(tmp_path,tmp_path/'state',20,0.2,{})
    assert not timed_out and report['render_mode']=='headless'
    assert len(calls)==2 and 0<calls[1][0]<calls[0][0]-0.02


def alive(pid):
    path=Path('/proc')/str(pid)/'stat'
    return path.exists() and path.read_text().split()[2]!='Z'


def test_actual_timeout_kills_only_owned_group_and_cleans_descendant_stdout(monkeypatch,tmp_path):
    wrapper=tmp_path/'owned.py';pidfile=tmp_path/'owned-child.pid';foreignfile=tmp_path/'foreign-finished'
    wrapper.write_text('#!/usr/bin/env python3\nimport subprocess,sys,time,os\nfrom pathlib import Path\nchild=subprocess.Popen([sys.executable,"-c","import time;time.sleep(3)"],stdout=sys.stdout,stderr=sys.stderr)\nPath(os.environ["OWNED_PID_FILE"]).write_text(str(child.pid))\ntime.sleep(3)\n');wrapper.chmod(0o755)
    monkeypatch.setattr(gh,'GODOT_BIN',str(wrapper))
    foreign=subprocess.Popen([sys.executable,'-c','import time;from pathlib import Path;time.sleep(.5);Path('+repr(str(foreignfile))+').write_text("done")'],start_new_session=True)
    try:
        start=time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):gh._run([],0.15,{'OWNED_PID_FILE':str(pidfile)})
        assert time.monotonic()-start<1.0
        assert pidfile.exists() and not alive(int(pidfile.read_text()))
        assert foreign.poll() is None
        assert foreign.wait(timeout=2)==0 and foreignfile.read_text()=='done'
    finally:
        if foreign.poll() is None:foreign.kill();foreign.wait(timeout=2)


def test_reader_and_selection_preserve_budget_without_widening(tmp_path):
    from aitelier.tools.godot_playtest.impl import read_spec,select_scenarios
    folder=tmp_path/'playtest';folder.mkdir()
    (folder/'_common.yaml').write_text('scene: res://main.tscn\n')
    (folder/'late.yaml').write_text('name: late\nexecution_budget: {max_frames: 48150, timeout_seconds: 120}\ntimeline:\n- at: 48120\n  assert: {N.x: x == 1}\n')
    loaded,info=read_spec(tmp_path);assert not info['errors']
    narrowed,unknown=select_scenarios(loaded,['late']);assert unknown==[]
    assert narrowed['scenarios'][0]['execution_budget']=={'max_frames':48150,'timeout_seconds':120}
    assert 'timeout' not in narrowed
