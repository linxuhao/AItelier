"""Custom-harness lifecycle and schema upgrade on actual isolated SQLite."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from core.db_manager import DBManager
from core.state_graph import StateGraphStore, StateConflict, StateGraphError
from core.state_attempts import StateAttempts, SCHEMA
from core.state_external import ExternalAttempts

ARTIFACT='a'*64
REPORT='b'*64


def spec(k,deps=None):
    return {'key':k,'goal':'Deliver '+k,'dependencies':deps or [],'acceptance':[
        {'id':'behaviour','kind':'test','description':'Required actual behaviour passes'},
        {'id':'review','kind':'review','description':'Subagent independently reviews the artifact'}]}


@pytest.fixture
def system(tmp_path):
    s=StateGraphStore(DBManager(str(tmp_path/'state.db')))
    s.create_project('game','Game');s.add_nodes('game',[spec('a'),spec('b',['a']),spec('independent')])
    a=StateAttempts(s);e=ExternalAttempts(a,'director@example')
    return s,a,e


def register(system,node='a',rid='job-1',request='req-1',revision=1):
    return system[2].register('game',node,revision,'my-harness',rid,request)


def report(system,a,status='candidate',oid='completion-1',version=None,**kwargs):
    body=dict(attempt_id=a['attempt_id'],observation_id=oid,expected_version=a['observation_version'] if version is None else version,
        context_hash=a['context_hash'],status=status,report_ref='reports/'+oid+'.json',report_sha256=REPORT,
        quiescent=status in {'candidate','failed'},artifact=ARTIFACT if status=='candidate' else None,
        artifact_kind='sha256' if status=='candidate' else None,detail='Real fixture harness declaration')
    body.update(kwargs)
    return system[2].observe(**body)


def accept(system,a):
    for check in ['behaviour','review']:
        system[1].record_evidence(a['attempt_id'],a['attempt_id']+'-'+check,check,'pass',ARTIFACT,
            'reports/'+check,REPORT,'verifier-subagent')
    return system[1].verify('game',a['node_key'],a['node_revision'],a['attempt_id'],'director-acceptance')


def test_external_attempt_has_real_identity_no_workflow_or_execution_project(system):
    s,a,e=system;r=register(system)
    assert r['execution_kind']=='external' and r['harness']=='my-harness'
    assert r['external_id']=='job-1' and r['reporting_actor']=='director@example'
    for name in ['run_id','workflow','execution_project_id','graph_version','graph_digest']:assert r[name] is None
    with s.db.get_connection() as c:assert c.execute('SELECT COUNT(*) FROM runs').fetchone()[0]==0
    assert s.get_node('game','a')['readiness']=='in_progress'
    assert r==register(system)


def test_external_completion_shares_candidate_evidence_and_acceptance_semantics(system):
    s,a,e=system;r=report(system,register(system))
    assert r['status']=='candidate';assert s.get_node('game','b')['readiness']=='blocked'
    with pytest.raises(StateConflict,match='evidence'):a.verify('game','a',1,r['attempt_id'],'director')
    rec=accept(system,r)
    assert s.get_node('game','b')['readiness']=='ready'
    provenance=json.loads(rec['provenance_json'])
    assert provenance['execution_kind']=='external' and provenance['external_id']=='job-1'
    assert provenance['report_sha256']==REPORT and provenance['quiescent'] is True
    assert provenance['observation_id']=='completion-1'
    assert a.verify('game','a',1,r['attempt_id'],'director')==rec


def test_failing_or_missing_criterion_never_verifies(system):
    s,a,_=system;r=report(system,register(system))
    a.record_evidence(r['attempt_id'],'bad-test','behaviour','fail',ARTIFACT,'reports/failed',REPORT,'test-agent')
    a.record_evidence(r['attempt_id'],'good-review','review','pass',ARTIFACT,'reports/review',REPORT,'review-agent')
    with pytest.raises(StateConflict,match='behaviour'):a.verify('game','a',1,r['attempt_id'],'director')
    assert s.get_node('game','b')['readiness']=='blocked'


def test_report_retry_is_idempotent_but_mutated_or_out_of_order_reports_fail(system):
    s,_,e=system;r=register(system)
    observed=report(system,r);events=s.events('game')
    replay=report(system,r)
    assert replay['idempotent'] and replay['observation']['seq']==observed['observation']['seq']
    assert s.events('game')==events
    with pytest.raises(StateConflict,match='different payload'):report(system,r,detail='edited old report')
    with pytest.raises(StateConflict,match='version'):report(system,r,status='failed',oid='retraction',version=0)
    with pytest.raises(StateConflict,match='immutable'):report(system,observed,oid='replace-artifact')


def test_paused_unknown_and_unquiescent_completion_retain_active_ownership(system):
    s,a,e=system;r=register(system)
    r=report(system,r,status='paused',oid='pause');assert r['status']=='paused'
    with pytest.raises(StateConflict):a.reserve('game','a',1,'a-workflow','competing-skillflow')
    r=report(system,r,status='unknown',oid='timeout');assert r['status']=='unknown'
    with pytest.raises(StateConflict,match='quiescent'):report(system,r,quiescent=False)
    with pytest.raises(StateConflict):register(system,rid='another',request='other')
    r=report(system,r,status='failed',oid='settled');assert r['status']=='failed'
    assert register(system,rid='new-job',request='new')['status']=='running'


def test_same_external_identity_and_request_key_conflicts_do_not_duplicate(system):
    _,a,e=system;report(system,register(system),status='failed')
    with pytest.raises(StateConflict):register(system,rid='job-1',request='new-key')
    with pytest.raises(StateConflict):register(system,rid='another-job',request='req-1')
    assert len(a.list('game','a'))==1


def test_holds_and_dependency_guard_are_shared(system):
    from core.state_portfolio import StatePortfolio
    s,a,e=system;p=StatePortfolio(s)
    p.set_dispatch('game','hold',0,'Owner paused scheduling')
    with pytest.raises(StateConflict,match='held'):register(system)
    p.set_dispatch('game','active',1,'Resume')
    p.set_hold('game','a',True,0,'Protected work')
    with pytest.raises(StateConflict,match='held'):register(system)
    with pytest.raises(StateConflict,match='dependencies'):register(system,node='b')
    assert a.list('game','a')==[]


def test_hold_does_not_pretend_existing_external_workers_are_cancelled(system):
    from core.state_portfolio import StatePortfolio
    s,_,_=system;r=register(system)
    StatePortfolio(s).set_dispatch('game','hold',0,'No new admissions')
    r=report(system,r)
    assert r['status']=='candidate'
    assert s.get_node('game','a')['readiness']=='held'


def test_owner_kind_and_context_cannot_be_spoofed(system):
    s,a,e=system;r=register(system)
    other=ExternalAttempts(a,'different-authenticated-principal')
    with pytest.raises(StateConflict,match='reporter'):
        other.observe(r['attempt_id'],'stolen',0,r['context_hash'],'failed','reports/x',REPORT,quiescent=True)
    with pytest.raises(StateConflict,match='context'):report(system,r,context_hash='c'*64)
    sf=a.reserve('game','independent',1,'workflow','skillflow')
    with pytest.raises(StateConflict,match='SkillFlow'):report(system,sf)
    with pytest.raises(StateConflict,match='external'):a.bind_run(r['attempt_id'],'made-up-run',object())
    with pytest.raises(StateConflict,match='external'):a.reconcile(r['attempt_id'],object())


def test_old_revision_result_does_not_certify_new_goal(system):
    s,a,e=system;r=register(system)
    s.revise_node('game','a',1,'Owner changes goal')
    r=report(system,r)
    assert r['status']=='superseded' and r['stale_inputs']
    with pytest.raises(StateConflict):accept(system,r)
    assert register(system,rid='v2',request='v2',revision=2)['node_revision']==2


def test_dependency_changes_and_retractions_invalidate_accepted_facts(system):
    s,a,e=system;first=report(system,register(system));accept(system,first)
    second=report(system,register(system,node='b',rid='dependent'));accept(system,second)
    report(system,first,status='failed',oid='upstream-error-found')
    assert s.get_node('game','a')['status']=='STALE' and s.get_node('game','b')['status']=='STALE'
    with pytest.raises(StateConflict):accept(system,second)
    with s.db.get_connection() as c:assert c.execute('SELECT COUNT(*) FROM state_acceptances').fetchone()[0]==2


def test_version_race_has_one_winner_and_one_append_only_record(system):
    s,a,e=system;r=register(system)
    def observe(i):
        try:return report(system,r,status='paused',oid='progress-'+str(i))['observation_version']
        except StateConflict:return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(observe,[1,2]))
    assert sorted(str(x) for x in results)==['1','None']
    with s.db.get_connection() as c:
        assert c.execute('SELECT COUNT(*) FROM state_external_observations').fetchone()[0]==1
        for query in ['DELETE FROM state_external_observations','UPDATE state_external_observations SET detail=detail']:
            with pytest.raises(sqlite3.IntegrityError,match='append-only'):c.execute(query)


@pytest.mark.parametrize('field,value',[
    ('status','VERIFIED'),('quiescent',1),('report_sha256','not-a-hash'),('context_hash','bad'),
    ('artifact','x'*64),('artifact_kind','file-path'),('report_ref',''),('expected_version',True),
])
def test_invalid_external_reports_are_rejected_without_partial_write(system,field,value):
    s,_,_=system;r=register(system);events=s.events('game')
    with pytest.raises(StateGraphError):report(system,r,**{field:value})
    assert s.events('game')==events


def legacy_db(tmp_path):
    s=StateGraphStore(DBManager(str(tmp_path/'legacy.db')));s.create_project('old','Old');s.add_nodes('old',[spec('goal')])
    with s.db.get_connection() as c:
        c.executescript(SCHEMA)
        node=dict(c.execute('SELECT * FROM state_nodes').fetchone())
        c.execute("INSERT INTO state_attempts(seq,attempt_id,project_id,node_key,node_revision,contract_hash,dependency_snapshot,context_json,"
                  "request_key,request_hash,workflow,execution_project_id,run_id,status,artifact_ref,created_at,updated_at) "
                  "VALUES(42,'old-a','old','goal',1,?,'{}','{}','key','hash','real-workflow','exec','real-run','candidate',?,'then','then')",
                  (node['contract_hash'],ARTIFACT))
        c.execute("INSERT INTO state_evidence(evidence_id,attempt_id,criterion_id,kind,verdict,artifact_ref,report_ref,report_sha256,reviewer,detail,payload_hash,created_at) "
                  "VALUES('e','old-a','check','test','pass',?,'report',?,'old-reviewer','original','ph','then')",(ARTIFACT,REPORT))
        c.execute("INSERT INTO state_acceptances VALUES('receipt','old','goal',1,'old-a',?,?,'{}','[\"e\"]','old-reviewer','then')",(ARTIFACT,node['contract_hash']))
        c.execute("UPDATE state_nodes SET status='VERIFIED',verified_receipt='receipt'")
        c.execute('CREATE INDEX custom_old_attempt_index ON state_attempts(created_at)')
        c.commit()
    return s


def test_atomic_upgrade_preserves_all_legacy_rows_ids_receipts_and_indexes(tmp_path):
    s=legacy_db(tmp_path)
    tables=['state_attempts','state_evidence','state_acceptances','state_nodes','state_events']
    with s.db.get_connection() as c:before={t:[dict(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
    StateAttempts(s);StateAttempts(s)
    with s.db.get_connection() as c:
        for t in tables:
            after=[dict(r) for r in c.execute('SELECT * FROM '+t)]
            assert len(after)==len(before[t])
            for old,new in zip(before[t],after):assert all(new[k]==v for k,v in old.items())
        cols={r['name']:r for r in c.execute('PRAGMA table_info(state_attempts)')}
        assert cols['workflow']['notnull']==cols['execution_project_id']['notnull']==0
        assert c.execute('SELECT execution_kind FROM state_attempts').fetchone()[0]=='skillflow'
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert c.execute("SELECT 1 FROM sqlite_master WHERE name='custom_old_attempt_index'").fetchone()
    assert s.get_node('old','goal')['verified_receipt']=='receipt'


def test_concurrent_initializers_migrate_once_safely(tmp_path):
    s=legacy_db(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:StateAttempts(s),range(4)))
    with s.db.get_connection() as c:
        assert c.execute('SELECT COUNT(*) FROM state_attempts').fetchone()[0]==1
        assert c.execute('SELECT seq FROM state_attempts').fetchone()[0]==42


def test_migration_failure_leaves_old_schema_and_rows_untouched(tmp_path):
    s=legacy_db(tmp_path)
    with s.db.get_connection() as c:
        c.execute('PRAGMA ignore_check_constraints=ON')
        c.execute("UPDATE state_attempts SET status='damaged-legacy-status'");c.commit()
    with pytest.raises(sqlite3.IntegrityError):StateAttempts(s)
    with s.db.get_connection() as c:
        assert 'execution_kind' not in [r['name'] for r in c.execute('PRAGMA table_info(state_attempts)')]
        assert c.execute('SELECT status FROM state_attempts').fetchone()[0]=='damaged-legacy-status'
        assert c.execute('SELECT receipt_id FROM state_acceptances').fetchone()[0]=='receipt'
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='state_attempts_executor_upgrade'").fetchone()


def test_migration_preserves_autoincrement_watermark(tmp_path):
    s=legacy_db(tmp_path)
    with s.db.get_connection() as c:
        c.execute("UPDATE sqlite_sequence SET seq=999 WHERE name='state_attempts'");c.commit()
    a=StateAttempts(s);s.add_nodes('old',[spec('fresh')])
    r=ExternalAttempts(a,'owner').register('old','fresh',1,'harness','fresh-job','new')
    assert r['seq']==1000


def test_sql_cannot_relabel_external_attempt_as_a_workflow(system):
    s,_,_=system;r=register(system)
    with s.db.get_connection() as c:
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            c.execute("UPDATE state_attempts SET execution_kind='skillflow',workflow='fake',execution_project_id='fake' WHERE attempt_id=?",(r['attempt_id'],))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE state_attempts SET run_id='fake' WHERE attempt_id=?",(r['attempt_id'],))


def test_workflow_and_external_reservations_compete_for_same_goal(system):
    _,a,e=system
    a.reserve('game','a',1,'workflow','first-workflow')
    with pytest.raises(StateConflict):register(system)
    assert len(a.list('game','a'))==1
