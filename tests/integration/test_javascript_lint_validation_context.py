"""Actual installed SDK wheel -> host loader -> owned fixture launcher -> baked ESLint.

LINT_REAL_ROOT is an explicitly mounted disposable test fixture, never production.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid

import pytest
import skillflow
from skillflow import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.workspace import WorkspaceManager
from skillflow.step_validation import StepValidator
from skillflow.lint_backends import _backends
from core.tool_routes import AItelierToolLoader
from aitelier.lint_backends import register_all


@pytest.fixture(autouse=True)
def registry_snapshot():
    before=dict(_backends)
    register_all()
    yield
    _backends.clear();_backends.update(before)


@pytest.fixture
def owned_root(monkeypatch):
    root=Path(os.environ['LINT_REAL_ROOT'])
    monkeypatch.setenv('AITELIER_HOME',str(root/'home'))
    return root


def loader():
    return AItelierToolLoader(Path(skillflow.__file__).parent/'tools')


def create_binding(root,run,repo):
    with sqlite3.connect(root/'fixture-ownership.sqlite') as c:
        c.execute('INSERT INTO run_isolation VALUES(?,?,?,?,?)',(run,'direct','',str(repo),'owned-js-fixture'))


def repository(root):
    repo=root/'projects'/uuid.uuid4().hex;repo.mkdir(parents=True)
    for args in [['init','-q'],['config','user.name','owned-fixture'],['config','user.email','fixture@test.invalid']]:
        subprocess.run(['git','-C',str(repo),*args],check=True)
    (repo/'baseline').write_text('fixture baseline')
    subprocess.run(['git','-C',str(repo),'add','baseline'],check=True)
    subprocess.run(['git','-C',str(repo),'commit','-qm','fixture'],check=True)
    (repo/'linter_manifest.json').write_text(json.dumps({'.js':'eslint','.mjs':'eslint','.cjs':'eslint'}))
    return repo


@pytest.mark.parametrize('valid',[True,False])
def test_real_engine_context_native_loader_and_owned_parser(owned_root,valid):
    root=owned_root;repo=repository(root)
    specs=[{'tool':'lint','files':['candidate.js'],'manifest_path':'linter_manifest.json',
            'run_id':'foreign','step_id':'foreign','project_root':'/outside'}]
    sf=SkillFlow(':memory:');sf._tool_loader=loader()
    sf._workspace=WorkspaceManager(str(root/'artifacts'/uuid.uuid4().hex),code_path_resolver=lambda project_id,run_id=None:repo)
    node=StepNode(id='implement',output_mode='write',output_target='code',output_allow_full_write=True,validation=specs,transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name='owned_lint',begin=node.id,steps=[node]))
    run=sf.create_run('owned_lint',{'project_id':'owned-js-fixture'},project_id='owned-js-fixture');sf.start_run(run);sf.advance_run(run);claim=sf.claim_next_step(run)
    create_binding(root,run,repo)
    (repo/'candidate.js').write_text('const answer=42;' if valid else 'const answer = ;')
    sf._code_output(run,node.id).record(['candidate.js'])
    result=sf._validate_outputs(claim.token,node)
    assert result['passed'] is valid,result
    if not valid:assert 'Parsing error' in str(result)


def test_empty_match_through_real_step_validator_is_unmeasured(owned_root):
    repo=repository(owned_root);run='empty-'+uuid.uuid4().hex;create_binding(owned_root,run,repo)
    validator=StepValidator(loader(),repo,tool_context={'run_id':run,'step_id':'validate','project_root':str(repo)})
    result=validator.validate([{'tool':'lint','files':['**/*.js'],'manifest_path':'linter_manifest.json'}])
    assert result['passed'] is False,result
    assert 'no lint measurement' in str(result)


@pytest.mark.parametrize('suffix,code,passed',[('js','',True),('mjs','export const value=1;',True),
    ('cjs',"module.exports=require('node:path');",True),('cjs',"import path from 'node:path';",False)])
def test_actual_supported_extensions_and_empty_parsed_program(owned_root,suffix,code,passed):
    repo=repository(owned_root);run='mode-'+uuid.uuid4().hex;create_binding(owned_root,run,repo)
    (repo/('input.'+suffix)).write_text(code)
    fn=loader().load_fn('lint')
    result=fn(['input.'+suffix],workspace_root=str(repo),manifest_path='linter_manifest.json',run_id=run,step_id='s',project_root=str(repo))
    assert len(result['results'])==1 and result['all_passed'] is passed,result


@pytest.mark.parametrize('attack',['missing-context','unbound','artifact','foreign-root'])
def test_real_native_lint_context_refusals_are_not_green(owned_root,attack):
    repo=repository(owned_root);run='refuse-'+uuid.uuid4().hex
    (repo/'candidate.js').write_text('const value=1;')
    if attack!='unbound':create_binding(owned_root,run,repo)
    workspace=repo
    context={'run_id':run,'step_id':'s','project_root':str(repo)}
    if attack=='missing-context':context={}
    if attack=='foreign-root':context['project_root']=str(repo.parent)
    if attack=='artifact':
        workspace=owned_root/'artifact-target'/uuid.uuid4().hex;workspace.mkdir(parents=True)
        (workspace/'candidate.js').write_text('const value=1;');(workspace/'linter_manifest.json').write_text('{".js":"eslint"}')
    result=StepValidator(loader(),workspace,tool_context=context).validate([{'tool':'lint','files':['candidate.js'],'manifest_path':'linter_manifest.json','run_id':run,'step_id':'s','project_root':str(repo)}])
    assert result['passed'] is False,result
    assert 'unavailable' in str(result)
