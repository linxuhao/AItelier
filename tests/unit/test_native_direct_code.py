"""Real native loop + real SkillFlow dispatch/journal, only the LLM is fake."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.output_targets import git
from skillflow.tool_loader import ToolLoader
from core.dpe_pipeline import PipelineEngine


def run_fixture(tmp_path, monkeypatch, tools=None):
    root=tmp_path/'code';root.mkdir();git(root,'init','-q')
    (root/'baseline.py').write_text('baseline=True');git(root,'add','--','baseline.py');git(root,'commit','-qm','base')
    sf=SkillFlow(str(tmp_path/'sf.db'),tool_loader=ToolLoader(Path(skillflow.__file__).parent/'tools'),
                workspace_base=str(tmp_path/'artifacts'),code_path_resolver=lambda pid,run_id=None:root)
    for name, (schema, fn) in (tools or {}).items():
        sf._tool_loader.register_dynamic_tool(name, schema, fn)
    node=StepNode(id='implement',output_mode='write',output_target='code',output_allow_full_write=True,
                  config={'extra_tools': list(tools or {})},
                  context=[{'from':'repository','mode':'tool'}],transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name='g',begin=node.id,steps=[node]))
    rid=sf.create_run('g',project_id='p');sf.start_run(rid);sf.advance_run(rid);claim=sf.claim_next_step(rid)
    monkeypatch.setattr('api.dependencies.get_skillflow',lambda:sf)
    return sf,rid,claim,root


def host(sf,rid,claim,root,turn):
    agent=SimpleNamespace(turn=turn,system_prompt='Test native code output.',
                          gateway=SimpleNamespace(last_usage={}))
    e=PipelineEngine()
    e.factory=SimpleNamespace(is_native=lambda _:True,get_native_agent=lambda _:agent,
        get_max_retries=lambda _:1,get_max_tool_turns=lambda _:8,get_fallback_to_json=lambda _:False)
    e.assembler=SimpleNamespace(assemble=lambda *a,**kw:'Write code directly, then finish.')
    e._get_project_path=lambda *a:root.parent/'artifacts/p'
    e._get_code_path=lambda *a:root
    e._preamble_steps=lambda _:[]
    e._trace_cb=lambda cat,event,payload:sf.trace(rid,cat,event,payload,step_id=claim.step_id,
                                               step_instance_id=claim.token.step_instance_id)
    def never(*a,**kw):raise AssertionError('code tried to create/clean an artifact draft')
    ws=SimpleNamespace(clean_draft_dir=never,_draft_dir=never)
    return e,ws


def response(name,**args):
    return SimpleNamespace(text='',reasoning_content='',truncated=False,tool_calls=[
        {'id':name,'type':'function','function':{'name':name,'arguments':json.dumps(args)}}])


def execute(e,ws,rid,claim,resolved_context=None):
    return e.run_step(1,claim.step_id,ws,'p',agent_config_name='fake',run_id=rid,
        step_instance_id=claim.token.step_instance_id,claim_epoch=claim.token.claim_epoch,
        tool_schemas=claim.inputs['_tool_schemas'],output_dir=claim.inputs['_output_dir'],
        output_target=claim.inputs['_output_target'],output_fixed=claim.inputs['_output_fixed'],
        config_name=claim.inputs['_config_name'],artifact_dir=claim.inputs['_artifact_dir'],
        resolved_context=resolved_context)


def test_native_code_write_read_and_finish_same_worktree(tmp_path,monkeypatch):
    sf,rid,claim,root=run_fixture(tmp_path,monkeypatch);turns=[]
    def turn(messages,**kwargs):
        turns.append(messages)
        if len(turns)==1:return response('create',file='new.py',content='value = 42\n')
        assert (root/'new.py').read_text()=='value = 42\n'
        if len(turns)==2:return response('read',path='new.py')
        assert 'value = 42' in messages[-1]['content']
        return response('finish_step',summary='done')
    e,ws=host(sf,rid,claim,root,turn)
    assert execute(e,ws,rid,claim)
    sf.confirm_step(claim.token,StepResult())
    assert len(turns)==3 and sf.get_steps(rid)[0]['status']=='completed'
    assert not (Path(claim.inputs['_artifact_dir'])/'new.py').exists()
    assert not list((root.parent/'artifacts').rglob('implement.tmp'))
    assert git(root,'status','--porcelain').strip()==''


def test_native_restart_reconstructs_trace_and_keeps_direct_code(tmp_path,monkeypatch):
    sf,rid,claim,root=run_fixture(tmp_path,monkeypatch);calls=[]
    def before(messages,**kwargs):
        calls.append('before')
        if len(calls)==1:return response('create',file='kept.py',content='retained = 1\n')
        raise KeyboardInterrupt('simulated host crash')
    e,ws=host(sf,rid,claim,root,before)
    with pytest.raises(KeyboardInterrupt):execute(e,ws,rid,claim)
    assert (root/'kept.py').read_text()=='retained = 1\n'
    def after(messages,**kwargs):
        assert any(m.get('role')=='tool' and 'kept.py' in (m.get('content') or '') for m in messages)
        assert (root/'kept.py').read_text()=='retained = 1\n'
        return response('finish_step',summary='resume completed')
    restored,ws2=host(sf,rid,claim,root,after)
    assert execute(restored,ws2,rid,claim)
    sf.confirm_step(claim.token,StepResult())
    assert sf.get_steps(rid)[0]['status']=='completed'
    assert git(root,'status','--porcelain').strip()==''


def test_native_report_refresh_invalidates_reads_without_becoming_code(tmp_path, monkeypatch):
    def diagnostic(version, out_dir=""):
        target = Path(out_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "report.txt").write_text(version + "X" * 5000)
        return {"written": "report.txt"}
    tools = {"diagnostic": ({"output": {"target": "artifact"},
                            "parameters": {"version": {"type": "string", "required": True}}}, diagnostic)}
    sf, rid, claim, root = run_fixture(tmp_path, monkeypatch, tools)
    calls = []
    def turn(messages, **kwargs):
        calls.append(1)
        n = len(calls)
        if n == 1:
            return response("diagnostic", version="FIRST")
        if n in (2, 4):
            return response("read", source="self", path="report.txt")
        if n == 3:
            assert "FIRST" in messages[-1]["content"]
            return response("diagnostic", version="SECOND")
        if n == 5:
            assert "SECOND" in messages[-1]["content"], messages[-1]
            assert "repeat_of_earlier_call" not in messages[-1]["content"]
            return response("create", file="new.py", content="value = 42\n")
        return response("finish_step", summary="done")
    e, ws = host(sf, rid, claim, root, turn)
    assert execute(e, ws, rid, claim)
    sf.confirm_step(claim.token, StepResult())
    assert sf.get_steps(rid)[0]["status"] == "completed"
    assert not (root / "report.txt").exists()
    artifact = Path(claim.inputs["_artifact_dir"])
    assert (artifact / "report.txt").read_text().startswith("SECOND")
    assert json.loads((artifact / "code_changes.json").read_text())["files"] == ["new.py"]


def test_coding_impl_relay_acknowledges_retained_bytes_before_targeted_work(tmp_path, monkeypatch):
    sf, rid, claim, root = run_fixture(tmp_path, monkeypatch)
    retained_bytes = (root / "baseline.py").stat().st_size
    state = {
        "instruction": "Finish baseline wiring and add the targeted test; do not re-ground.",
        "relay": {
            "run_id": "prior-budget-run",
            "error": "native turn budget exhausted (32/32)",
            "code_changes": {"files": {
                "baseline.py": {"bytes": retained_bytes},
            }},
        },
    }
    seed = "# State goal attempt\n\n" + json.dumps(state) + "\n\n## Relay\ncontinue\n"
    calls = []

    def turn(messages, **kwargs):
        calls.append(messages)
        n = len(calls)
        if n == 1:
            assert "Relay Progress Contract" in messages[1]["content"]
            return response("list_tree")  # must be refused before acknowledgement
        if n == 2:
            assert "relay acknowledgement required" in messages[-1]["content"]
            return response("acknowledge_relay", retained_bytes=retained_bytes,
                            incomplete_items=["finish baseline wiring", "add targeted test"])
        if n in (3, 4, 5, 6):
            return response("read", path="baseline.py")
        if n == 7:
            assert "targeted-read limit reached" in messages[-1]["content"]
            return response("create", file="relay_done.py", content="done = True\n")
        return response("finish_step", summary="continued retained work")

    e, ws = host(sf, rid, claim, root, turn)
    e._draft_graph_name = lambda: "coding_impl"
    e.factory.get_max_tool_turns = lambda _: 10
    assert execute(e, ws, rid, claim, {"coding_impl/plan.md": seed})
    events = [row[0] for row in sf._conn.execute(
        "SELECT event FROM skillflow_trace WHERE run_id = ? ORDER BY seq", (rid,))]
    assert "relay_operation_refused_before_ack" in events
    assert "relay_progress_acknowledged" in events
    assert "relay_broad_survey_refused" in events
    assert "early_progress_intervention" in events
    assert "implementation_first_write" in events
    assert (root / "relay_done.py").read_text() == "done = True\n"
