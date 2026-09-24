"""Custom-harness lifecycle and schema upgrade on actual isolated SQLite."""
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from core.db_manager import DBManager
from core.state_graph import StateGraphStore, StateConflict, StateGraphError
from core.state_attempts import StateAttempts, SCHEMA
from core.state_external import ExternalAttempts

ARTIFACT='a'*64
REPORT='b'*64
EVIDENCE_REPORT='c'*64

def candidate_report(attempt, observation_id='completion-1'):
    payload = json.dumps({'attempt':attempt['attempt_id'], 'observation':observation_id,
                          'status':'candidate', 'settled':True, 'usable':True},
                         sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def spec(k,deps=None):
    return {'key':k,'goal':'Deliver '+k,'dependencies':deps or [],'acceptance':[
        {'id':'behaviour','kind':'test','description':'Required actual behaviour passes'},
        {'id':'review','kind':'review','description':'Subagent independently reviews the artifact'}]}


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path/'home'))
    s=StateGraphStore(DBManager(str(tmp_path/'state.db')),project_read_trusted=True)
    s.create_project('game','Game');s.add_nodes('game',[spec('a'),spec('b',['a']),spec('independent')])
    a=StateAttempts(s);e=ExternalAttempts(a,'director@example')
    e._test_report_dir = tmp_path/'terminal-reports'
    e._test_report_dir.mkdir()
    return s,a,e


def register(system,node='a',rid='job-1',request='req-1',revision=1):
    return system[2].register('game',node,revision,'my-harness',rid,request)


def report(system,a,status='candidate',oid='completion-1',version=None,**kwargs):
    terminal_path = system[2]._test_report_dir/(oid+'.json')
    terminal_bytes = json.dumps({'attempt':a['attempt_id'],'observation':oid,
                                 'status':status, 'settled':True, 'usable':True},
                                sort_keys=True).encode()
    if status in {'candidate','failed'}:
        terminal_path.write_bytes(terminal_bytes)
    body=dict(attempt_id=a['attempt_id'],observation_id=oid,expected_version=a['observation_version'] if version is None else version,
        context_hash=a['context_hash'],status=status,
        report_ref=str(terminal_path) if status in {'candidate','failed'} else 'reports/'+oid+'.json',
        report_sha256=hashlib.sha256(terminal_bytes).hexdigest() if status in {'candidate','failed'} else REPORT,
        quiescent=status in {'candidate','failed'},artifact=ARTIFACT if status=='candidate' else None,
        artifact_kind='sha256' if status=='candidate' else None,detail='Real fixture harness declaration')
    body.update(kwargs)
    return system[2].observe(**body)


def accept(system,a):
    for check in ['behaviour','review']:
        system[1].record_evidence(a['attempt_id'],a['attempt_id']+'-'+check,check,'pass',ARTIFACT,
            'reports/'+check,EVIDENCE_REPORT,'verifier-subagent')
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
    assert provenance['report_sha256']==candidate_report(r) and provenance['quiescent'] is True
    assert provenance['observation_id']=='completion-1'
    assert a.verify('game','a',1,r['attempt_id'],'director')==rec


def test_zero_byte_local_terminal_report_is_refused_before_candidate(system, tmp_path):
    attempt = register(system)
    report_path = tmp_path / "interrupted-report.json"
    report_path.write_bytes(b"")
    with pytest.raises(StateConflict, match="empty|interrupted"):
        report(system, attempt, report_ref=str(report_path),
               report_sha256=hashlib.sha256(b"").hexdigest())
    report_path.write_bytes(b'{"partial":')
    with pytest.raises(StateConflict, match="partial|malformed"):
        report(system, attempt, report_ref=str(report_path),
               report_sha256=hashlib.sha256(b'{"partial":').hexdigest())
    with pytest.raises(StateConflict, match="do not match"):
        report(system, attempt, report_ref=str(report_path),
               report_sha256=hashlib.sha256(b'{"complete":true}').hexdigest())
    missing = tmp_path / "transport-reset-report.json"
    with pytest.raises(StateConflict, match="unavailable"):
        report(system, attempt, report_ref=str(missing),
               report_sha256=hashlib.sha256(b"lost").hexdigest())
    assert system[1].get(attempt["attempt_id"])["status"] == "running"


@pytest.mark.parametrize("name,body", [
    ("empty.txt", b"{}"),
    ("interrupted.md", b"Criterion started but interrupted mid-stream"),
    ("unsettled.json", b'{"status":"completed","settled":false,"usable":true}'),
    ("unusable.json", b'{"status":"completed","settled":true,"usable":false}'),
])
def test_terminal_envelope_is_structured_settled_and_usable_regardless_extension(
        system, tmp_path, name, body):
    attempt = register(system)
    path = tmp_path / name
    path.write_bytes(body)
    with pytest.raises(StateConflict, match="structured|settled|usable"):
        report(system, attempt, report_ref=str(path),
               report_sha256=hashlib.sha256(body).hexdigest())


def test_valid_structured_terminal_envelope_is_accepted_with_markdown_extension(
        system, tmp_path):
    attempt = register(system)
    body = json.dumps({"attempt": attempt["attempt_id"], "status": "candidate",
                       "settled": True, "usable": True}, sort_keys=True).encode()
    path = tmp_path / "terminal.md"
    path.write_bytes(body)
    completed = report(system, attempt, report_ref=str(path),
                       report_sha256=hashlib.sha256(body).hexdigest())
    assert completed["status"] == "candidate"


def test_report_descriptor_refuses_check_open_symlink_swap(system, tmp_path, monkeypatch):
    from core.state_report_integrity import retain_report

    victim = tmp_path / "review.json"
    victim.write_bytes(b'{"status":"completed","settled":true,"usable":true,"verdict":"fail"}')
    target_body = b'{"status":"completed","settled":true,"usable":true,"verdict":"pass","criterion_id":"review"}'
    target = tmp_path / "target.json"
    target.write_bytes(target_body)
    original = __import__("os").open
    swapped = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if str(path) == victim.name and kwargs.get("dir_fd") is not None and not swapped:
            victim.unlink()
            victim.symlink_to(target)
            swapped = True
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr("core.state_report_integrity.os.open", racing_open)
    with pytest.raises(StateConflict, match="symlink|unavailable"):
        retain_report(str(victim), hashlib.sha256(target_body).hexdigest(),
                      completed=True)
    assert swapped


def test_intermediate_report_store_symlink_cannot_escape_evidence_root(
        system, tmp_path, monkeypatch):
    from core import datadir
    from core.state_report_integrity import retain_report

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    body = json.dumps({"status": "candidate", "settled": True,
                       "usable": True}).encode()
    source = tmp_path / "report.json"
    source.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    store = datadir.state_report_store_dir()
    store.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (store / digest[:2]).symlink_to(outside, target_is_directory=True)

    with pytest.raises(StateConflict, match="store directory is a symlink"):
        retain_report(str(source), digest, completed=True)
    assert list(outside.iterdir()) == []


def test_report_store_uses_descriptor_relative_no_follow_open(monkeypatch, system, tmp_path):
    from core import datadir
    from core.state_report_integrity import retain_report

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    body = json.dumps({"status": "candidate", "settled": True,
                       "usable": True}).encode()
    source = tmp_path / "report.json"
    source.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    store = datadir.state_report_store_dir()
    store.mkdir(parents=True)
    shard = store / digest[:2]
    shard.mkdir()
    original_open = __import__("os").open
    swapped = False

    def swap_after_root_open(path, flags, *args, **kwargs):
        nonlocal swapped
        result = original_open(path, flags, *args, **kwargs)
        if path == shard.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            shard.rename(tmp_path / "renamed-shard")
            shard.symlink_to(tmp_path / "outside", target_is_directory=True)
        return result

    (tmp_path / "outside").mkdir()
    monkeypatch.setattr("core.state_report_integrity.os.open", swap_after_root_open)
    with pytest.raises(StateConflict, match="changed|symlink"):
        retain_report(str(source), digest, completed=True)
    assert swapped
    assert list((tmp_path / "outside").iterdir()) == []


def test_report_store_root_replacement_fails_before_retention(monkeypatch, system, tmp_path):
    from core import datadir
    from core.state_report_integrity import retain_report

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    body = json.dumps({"status": "candidate", "settled": True,
                       "usable": True}).encode()
    source = tmp_path / "report.json"
    source.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    store = datadir.state_report_store_dir()
    store.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open = __import__("os").open
    swapped = False

    def swap_after_root_open(path, flags, *args, **kwargs):
        nonlocal swapped
        result = original_open(path, flags, *args, **kwargs)
        if path == store.name and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            store.rename(tmp_path / "renamed-root")
            store.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr("core.state_report_integrity.os.open", swap_after_root_open)
    with pytest.raises(StateConflict, match="directory changed|symlink"):
        retain_report(str(source), digest, completed=True)
    assert swapped
    assert list(outside.iterdir()) == []
    assert list((tmp_path / "renamed-root").iterdir()) == []


def test_report_store_post_link_root_replacement_fails_and_cleans_new_digest(
        monkeypatch, system, tmp_path):
    from core import datadir
    from core.state_report_integrity import retain_report

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    body = json.dumps({"status": "candidate", "settled": True,
                       "usable": True}).encode()
    source = tmp_path / "report.json"
    source.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    store = datadir.state_report_store_dir()
    store.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_fsync = __import__("os").fsync
    calls = 0

    def swap_on_shard_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            store.rename(tmp_path / "renamed-root")
            store.symlink_to(outside, target_is_directory=True)
        return original_fsync(fd)

    monkeypatch.setattr("core.state_report_integrity.os.fsync", swap_on_shard_fsync)
    with pytest.raises(StateConflict, match="changed|symlink"):
        retain_report(str(source), digest, completed=True)
    assert calls >= 4  # publication, digest cleanup, and temp cleanup fsyncs
    assert list(outside.iterdir()) == []
    assert list((tmp_path / "renamed-root" / digest[:2]).iterdir()) == []


def test_report_store_root_replacement_during_final_temp_cleanup_fsync_is_rejected(
        monkeypatch, system, tmp_path):
    from core import datadir
    from core.state_report_integrity import retain_report

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    body = json.dumps({"status": "candidate", "settled": True,
                       "usable": True}).encode()
    source = tmp_path / "report.json"
    source.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    store = datadir.state_report_store_dir()
    store.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_fsync = __import__("os").fsync
    calls = 0

    def swap_during_final_temp_cleanup(fd):
        nonlocal calls
        calls += 1
        if calls == 4:
            store.rename(tmp_path / "renamed-root")
            store.symlink_to(outside, target_is_directory=True)
        return original_fsync(fd)

    monkeypatch.setattr("core.state_report_integrity.os.fsync",
                        swap_during_final_temp_cleanup)
    with pytest.raises(StateConflict, match="changed|symlink"):
        retain_report(str(source), digest, completed=True)
    assert calls >= 4
    assert list(outside.iterdir()) == []
    assert list((tmp_path / "renamed-root" / digest[:2]).iterdir()) == [
        tmp_path / "renamed-root" / digest[:2] / digest
    ]


def test_report_store_cleanup_fsync_failure_is_reported_fail_closed(
        monkeypatch, system, tmp_path):
    from core import datadir
    from core.state_report_integrity import retain_report

    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    body = json.dumps({"status": "candidate", "settled": True,
                       "usable": True}).encode()
    source = tmp_path / "report.json"
    source.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    store = datadir.state_report_store_dir()
    store.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_fsync = __import__("os").fsync
    calls = 0

    def fail_cleanup_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            store.rename(tmp_path / "renamed-root")
            store.symlink_to(outside, target_is_directory=True)
        if calls == 3:
            raise OSError("simulated cleanup fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr("core.state_report_integrity.os.fsync", fail_cleanup_fsync)
    with pytest.raises(StateConflict, match="cleanup.*durably synced") as exc_info:
        retain_report(str(source), digest, completed=True)
    assert isinstance(exc_info.value.__cause__, StateConflict)
    assert list(outside.iterdir()) == []
    assert list((tmp_path / "renamed-root" / digest[:2]).iterdir()) == []


def test_relative_terminal_report_is_refused_and_retained_bytes_are_immutable(system, tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    attempt = register(system)
    with pytest.raises(StateConflict, match="absolute|inspectable"):
        report(system, attempt, report_ref="reports/missing.json")

    source = tmp_path / "complete.json"
    payload = json.dumps({"attempt": attempt["attempt_id"], "status": "candidate",
                          "settled": True, "usable": True}).encode()
    source.write_bytes(payload)
    completed = report(
        system, attempt, report_ref=str(source),
        report_sha256=hashlib.sha256(payload).hexdigest())
    retained = completed["observation"]["report_ref"]
    assert retained != str(source)
    source.unlink()
    with system[0].db.get_connection() as conn:
        row = conn.execute(
            "SELECT report_bytes FROM state_external_report_blobs WHERE report_sha256=?",
            (hashlib.sha256(payload).hexdigest(),),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE state_external_report_blobs SET report_bytes=? WHERE report_sha256=?",
                (b"changed", hashlib.sha256(payload).hexdigest()),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "DELETE FROM state_external_report_blobs WHERE report_sha256=?",
                (hashlib.sha256(payload).hexdigest(),),
            )
    assert bytes(row[0]) == payload
    assert Path(retained).read_bytes() == payload


def test_failing_or_missing_criterion_never_verifies(system):
    s,a,_=system;r=report(system,register(system))
    a.record_evidence(r['attempt_id'],'bad-test','behaviour','fail',ARTIFACT,'reports/failed',EVIDENCE_REPORT,'test-agent')
    a.record_evidence(r['attempt_id'],'good-review','review','pass',ARTIFACT,'reports/review',EVIDENCE_REPORT,'review-agent')
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
    s=StateGraphStore(DBManager(str(tmp_path/'legacy.db')),project_read_trusted=True);s.create_project('old','Old');s.add_nodes('old',[spec('goal')])
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


def test_candidate_report_digest_cannot_be_reused_by_a_later_attempt(system):
    first = report(system, register(system))
    second = register(system, rid="job-2", request="req-2")
    with pytest.raises(StateConflict, match="fresh report"):
        report(
            system,
            second,
            oid="completion-2",
            report_ref=first["observation"]["report_ref"],
            report_sha256=candidate_report(first),
        )
    assert system[1].get(second["attempt_id"])["status"] == "running"
    assert system[1].get(second["attempt_id"])["observation_version"] == 0


def test_candidate_or_old_observation_report_is_not_independent_evidence(system):
    first = report(system, register(system))
    with pytest.raises(StateConflict, match="independent"):
        system[1].record_evidence(
            first["attempt_id"], "self-review", "review", "pass", ARTIFACT,
            "reports/self-review.json", candidate_report(first), "verifier-subagent",
        )

    second = report(
        system,
        register(system, rid="job-2", request="req-2"),
        oid="completion-2",
    )
    with pytest.raises(StateConflict, match="independent"):
        system[1].record_evidence(
            second["attempt_id"], "stale-review", "review", "pass", ARTIFACT,
            "reports/copied-old-review.json", candidate_report(first),
            "verifier-subagent",
        )


def test_fresh_independent_report_can_cover_all_current_criteria(system):
    candidate = report(system, register(system))
    for criterion in ("behaviour", "review"):
        system[1].record_evidence(
            candidate["attempt_id"], f"fresh-{criterion}", criterion, "pass",
            ARTIFACT, "reports/current-independent.json", EVIDENCE_REPORT,
            "verifier-subagent",
        )
    receipt = system[1].verify(
        "game", "a", 1, candidate["attempt_id"], "director-acceptance"
    )
    assert receipt["artifact_ref"] == ARTIFACT
