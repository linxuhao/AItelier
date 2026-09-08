"""Migration rehearsals never promote history into active/verified work."""
import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
from core.db_manager import DBManager
from core.state_service import StateService
from core.state_graph import StateConflict, StateGraphError
from core.state_migration import dry_run, stage_shadow, validate_manifest, verify_inputs


@pytest.fixture
def bundle(tmp_path):
    repo = tmp_path / 'repo';repo.mkdir()
    subprocess.run(['git','init','-q','-b','main'],cwd=repo,check=True)
    (repo/'source.py').write_text('VALUE=1\n')
    subprocess.run(['git','add','source.py'],cwd=repo,check=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','source'],cwd=repo,check=True)
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    report=tmp_path/'report.json';report.write_text('{"passed": false}\n')
    return {'format_version':1,'project':{'project_id':'preview','title':'Migration preview'},
        'source':{'repo_path':str(repo),'head_sha':sha},'dispatch':'hold','dispatch_reason':'Review first',
        'nodes':[{'key':'a','goal':'Goal A','acceptance':[{'id':'test','kind':'test','description':'Actual test'}]},
                 {'key':'b','goal':'Goal B','dependencies':['a'],'acceptance':[{'id':'test','kind':'test','description':'Actual test'}]}],
        'references':[{'reference_id':'external','node_key':'b','kind':'run','ref':'external-run','label':'Existing paused run',
                       'provenance_actor':'retained handoff','observed_status':'paused','protect':True}],
        'excluded_runs':[{'run_id':'cancelled-run','reason':'Explicitly cancelled; do not reopen'}],
        'file_pins':[{'path':str(report),'sha256':hashlib.sha256(report.read_bytes()).hexdigest()}]}


def test_rehearsal_uses_temporary_db_zero_attempts_zero_acceptance(bundle):
    result=dry_run(bundle)
    assert result['result']=='PASS' and result['production_database_written'] is False
    assert result['receipt']['nodes']==2 and result['receipt']['ready']==0
    assert result['receipt']['attempts']==result['receipt']['verified']==0
    assert result['idempotent_replay'] is True
    assert all(n['status']=='OPEN' and n['readiness']=='held' for n in result['overview']['nodes'])


def test_repeat_manifest_is_idempotent_but_edits_or_releasing_hold_refuse(bundle,tmp_path):
    svc=StateService(DBManager(str(tmp_path/'shadow.db')))
    stage_shadow(svc,bundle)
    before=svc.store.events('preview');assert stage_shadow(svc,bundle)['idempotent'] is True
    assert svc.store.events('preview')==before
    changed=copy.deepcopy(bundle);changed['nodes'][0]['goal']='Changed'
    with pytest.raises(StateConflict):stage_shadow(svc,changed)
    svc.portfolio.set_dispatch('preview','active',1,'Explicit fixture change')
    with pytest.raises(StateConflict):stage_shadow(svc,bundle)


def test_existing_project_is_not_implicitly_overwritten(bundle,tmp_path):
    svc=StateService(DBManager(str(tmp_path/'shadow.db')));svc.create_project('preview','An existing project')
    with pytest.raises(StateConflict,match='without a complete'):stage_shadow(svc,bundle)
    assert svc.store.get_project('preview')['title']=='An existing project'


@pytest.mark.parametrize('field,value',[('dispatch','active'),('format_version',2),('format_version',True)])
def test_active_or_unknown_format_cannot_be_prepared(bundle,field,value):
    bundle[field]=value
    with pytest.raises(StateGraphError):dry_run(bundle)


def test_user_cannot_smuggle_verified_status_or_unknown_node_fields(bundle):
    bundle['nodes'][0]['status']='VERIFIED'
    with pytest.raises(StateGraphError):dry_run(bundle)


def test_cycles_and_missing_dependencies_fail(bundle):
    bundle['nodes'][0]['dependencies']=['b']
    with pytest.raises(StateGraphError,match='cycle'):dry_run(bundle)
    bundle['nodes'][0]['dependencies']=['missing']
    with pytest.raises(StateGraphError,match='unknown dependency'):dry_run(bundle)


def test_active_external_work_requires_protection_and_excluded_run_cannot_reappear(bundle):
    bundle['references'][0]['protect']=False
    with pytest.raises(StateGraphError,match='protection'):validate_manifest(bundle)
    bundle['references'][0]['protect']=True
    bundle['references'][0]['ref']='cancelled-run'
    with pytest.raises(StateGraphError,match='excluded'):validate_manifest(bundle)


def test_changed_evidence_or_source_head_refuses(bundle):
    report=Path(bundle['file_pins'][0]['path']);report.write_text('changed')
    with pytest.raises(StateConflict,match='evidence changed'):verify_inputs(bundle)
    bundle['file_pins']=[];bundle['source']['head_sha']='0'*40
    with pytest.raises(StateConflict,match='HEAD changed'):verify_inputs(bundle)


def test_reference_is_only_history_and_protection_survives_global_release(bundle,tmp_path):
    svc=StateService(DBManager(str(tmp_path/'shadow.db')))
    stage_shadow(svc,bundle)
    assert svc.portfolio.run_owners('external-run')['links'][0]['relation']=='reference'
    svc.portfolio.set_dispatch('preview','active',1,'Review complete in fixture')
    assert svc.store.get_node('preview','b')['readiness']=='held'
    assert svc.attempts.list('preview','b')==[]
    with pytest.raises(StateConflict,match='held'):
        svc.attempts.reserve('preview','b',1,'fixture','new')


def test_cli_rehearsal_has_no_production_apply_or_database_flags(bundle,tmp_path):
    import sys
    script=Path(__file__).resolve().parents[2]/'scripts/preview_state_migration.py'
    source=tmp_path/'bundle.json';source.write_text(json.dumps(bundle))
    report=tmp_path/'result.json'
    out=subprocess.run([sys.executable,str(script),str(source),'--report',str(report)],capture_output=True,text=True,timeout=30)
    assert out.returncode==0,out.stdout+out.stderr
    assert json.loads(report.read_text())['production_database_written'] is False
    forbidden=subprocess.run([sys.executable,str(script),str(source),'--report',str(tmp_path/'new.json'),'--apply'],capture_output=True,text=True)
    assert forbidden.returncode!=0 and not (tmp_path/'new.json').exists()
    repeat=subprocess.run([sys.executable,str(script),str(source),'--report',str(report)],capture_output=True,text=True)
    assert repeat.returncode!=0


def test_dirty_source_is_not_mistaken_for_the_pinned_commit(bundle):
    (Path(bundle["source"]["repo_path"])/"source.py").write_text("VALUE=2\n")
    with pytest.raises(StateConflict,match="uncommitted"):
        verify_inputs(bundle)
