from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from core.dpe_pipeline import PipelineEngine
from core.prompt_assembler import WORKSPACE_LAYOUT


def engine(root):
    e=object.__new__(PipelineEngine)
    e._output_target='code';e._output_fixed={};e._code_path=root;e._config_name='configured_graph'
    e._output_dir=str(root)
    return e


def test_graph_name_is_not_deduced_from_worktree_parent(tmp_path):
    e=engine(tmp_path/'worktrees/run-id')
    assert e._draft_graph_name()=='configured_graph'
    e._config_name=''
    with pytest.raises(RuntimeError,match='explicit config_name'):e._draft_graph_name()


def test_resume_finds_code_in_worktree_without_touching_artifact_draft(tmp_path):
    e=engine(tmp_path);(tmp_path/'source.py').write_text('source')
    def no_draft(*a):raise AssertionError('code does not have a draft')
    ws=SimpleNamespace(_draft_dir=no_draft)
    assert e._output_file_path(ws,'project','step','source.py')==tmp_path/'source.py'
    assert e._output_file_path(ws,'project','step','source.py').exists()


def test_mixed_resume_checks_each_slots_destination(tmp_path):
    repo=tmp_path/'repo';repo.mkdir();art=tmp_path/'artifacts';art.mkdir()
    e=engine(repo);e._output_target='artifact'
    e._output_fixed={'plan':'plan.md','manifest':{'file':'manifest.json','target':'code'}}
    ws=SimpleNamespace(_draft_dir=lambda *a:art)
    assert e._output_file_path(ws,'p','s','manifest.json')==repo/'manifest.json'
    assert e._output_file_path(ws,'p','s','plan.md')==art/'plan.md'


def test_json_code_output_uses_the_same_admitted_tool_dispatch(tmp_path):
    e=engine(tmp_path);calls=[]
    e._tool_schemas={'write':{}}
    e._exec_tool=lambda action:calls.append(action) or {'written':action['params']['file']}
    ws=SimpleNamespace(write_draft=lambda *a,**kw:pytest.fail('source was written to artifact draft'))
    e._write_output_file(ws,'p','s','code.py','source')
    assert calls==[{'tool':'write','params':{'file':'code.py','content':'source'}}]


def test_multifile_outputs_are_individually_accounted():
    assert PipelineEngine._written_names({'written':['a.png','b.png']})==['a.png','b.png']
    assert PipelineEngine._written_names({'written':'a.py'})==['a.py']
    assert PipelineEngine._written_names({'error':'failed','written':['a.py']})==['a.py']


def test_layout_does_not_claim_code_has_a_second_copy():
    assert 'no code staging' in WORKSPACE_LAYOUT
    assert 'artifact-only' in WORKSPACE_LAYOUT
    assert 'REPLACES this directory wholesale' not in WORKSPACE_LAYOUT


@pytest.mark.asyncio
async def test_legacy_code_claim_is_refused_before_any_workspace_or_agent_work():
    from aitelier.runner import AgentStepRunner
    from skillflow.exceptions import IsolationUnavailable
    claim = SimpleNamespace(inputs={"_legacy_code_staging": True})
    runner = AgentStepRunner(db_manager=None, workspace_manager=None)
    with pytest.raises(IsolationUnavailable, match="pinned to a legacy code-staging graph"):
        await runner.execute(claim)


def test_active_code_tool_descriptions_do_not_advertise_removed_overlay():
    import yaml
    root = Path(__file__).resolve().parents[2] / "aitelier/tools"
    for name in ("gen_audio_asset", "gen_image_asset", "godot_playtest_scenario"):
        schema = yaml.safe_load((root / name / "tool.yaml").read_text())
        description = schema["description"]
        assert "worktree" in description
        assert "through promotion + repo_apply" not in description
        assert "this step's staging dir" not in description
        assert "use_staged" not in schema.get("parameters", {})
