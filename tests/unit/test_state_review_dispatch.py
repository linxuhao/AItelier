"""Real typed State boundary: declared candidate review is external-only."""
import hashlib
import json
import sqlite3
import pytest
from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService
from core.state_claims import ClaimError
from core.state_graph import StateConflict, StateGraphError

ART='a'*64

def call(s, action, **kw):
    return execute(s, action, kw, allow_write=True)

def service(db, who, admin=False):
    return StateService(StateDatabase(str(db)), actor='driver:'+who,
                        project_read_trusted=True, driver_id=who, is_admin=admin)

def claim(s, purpose='review', request='review', **kw):
    return call(s,'claim_node',project_id='p',node_key='n',purpose=purpose,
                expected_revision=1,request_key=request,**kw)

def start(s,c,request='review-start',**kw):
    args=dict(project_id='p',node_key='n',expected_revision=1,harness='h',
              external_id=request,request_key=request,claim_id=c['claim_id'],fence=c['fence'])
    args.update(kw)
    return call(s,'start_external_attempt',**args)

def file(tmp_path,name,d):
    p=tmp_path/name;b=json.dumps(d).encode();p.write_bytes(b)
    return str(p),hashlib.sha256(b).hexdigest()

def settle(s,a,tmp_path,label):
    ref,h=file(tmp_path,label+'.json',dict(status='candidate',settled=True,usable=True,artifact=ART,attempt_id=a['attempt_id'],observation=label))
    return call(s,'report_external_attempt',attempt_id=a['attempt_id'],observation_id=label,
                expected_version=a['observation_version'],context_hash=a['context_hash'],status='candidate',
                report_ref=ref,report_sha256=h,quiescent=True,artifact=ART,artifact_kind='sha256',fence=a['owner_fence'])

@pytest.fixture
def rig(tmp_path,monkeypatch):
    monkeypatch.setenv('AITELIER_HOME',str(tmp_path/'home'))
    db=tmp_path/'state.db';owner=service(db,'owner',True)
    call(owner,'create_project',project_id='p',title='P')
    call(owner,'add_nodes',project_id='p',nodes=[dict(key='n',goal='G',acceptance=[dict(id='c',kind='test',description='C')])])
    call(owner,'set_claim_enforcement',project_id='p',claim_enforcement='on',expected_revision=0,reason='fixture')
    author=service(db,'author');c=claim(author,'implement','author');a=start(author,c,'author-start')
    settle(author,a,tmp_path,'author-terminal')
    return db,service(db,'reviewer'),owner

def counts(db):
    with sqlite3.connect(db) as c:
        return tuple(c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ['state_attempts','state_external_owners'])

def test_real_review_receipt_lifecycle_terminal_and_each(rig,tmp_path):
    db,s,owner=rig;c=claim(s,subagent='reviewer/check')
    assert c['purpose']=='review' and c['status']=='live'
    assert claim(s,subagent='reviewer/check')['claim_id']==c['claim_id']
    a=start(s,c);assert a['owner_driver_id']=='reviewer' and a['owner_fence']==1
    assert a['context']['review_claim']=={'claim_id':c['claim_id'],'fence':c['fence'],'purpose':'review'}
    assert start(s,c)['attempt_id']==a['attempt_id']
    bound=call(s,'get_claim',project_id='p',claim_id=c['claim_id'])['claim']
    assert bound['attempt_id']==a['attempt_id']
    renewed=call(s,'heartbeat',project_id='p',claims=[dict(claim_id=c['claim_id'],fence=c['fence'])],attempts=[dict(attempt_id=a['attempt_id'],fence=1)])
    assert len(renewed['renewed'])==2 and not renewed['refused']
    done=settle(s,a,tmp_path,'review-terminal');assert done['status']=='candidate'
    assert call(s,'get_claim',project_id='p',claim_id=c['claim_id'])['claim']['status']=='released'
    ref,h=file(tmp_path,'each.json',dict(status='completed',settled=True,usable=True,criterion_id='c',artifact=ART,verdict='pass'))
    row=call(s,'record_evidence',attempt_id=a['attempt_id'],evidence_id='each',criterion_id='c',verdict='pass',artifact=ART,report_ref=ref,report_sha256=h)
    assert row['verdict']=='pass'
    call(owner,'verify_node',project_id='p',node_key='n',expected_revision=1,attempt_id=a['attempt_id'])
    assert start(s,c)['attempt_id']==a['attempt_id']  # terminal replay is not new dispatch
    other=claim(s,request='other-review')
    with pytest.raises(ClaimError):start(s,other)

@pytest.mark.parametrize('bad',['foreign','fence','missing-fence','expired','released','wrong-node','wrong-purpose','revision'])
def test_refused_registration_has_no_new_attempt_or_binding(rig,tmp_path,bad):
    db,s,owner=rig;c=claim(s)
    target=s;extra={}
    if bad=='foreign':target=service(db,'foreign')
    if bad=='fence':extra['fence']=c['fence']+1
    if bad=='missing-fence':extra['fence']=None
    if bad=='expired':
        with sqlite3.connect(db) as conn:conn.execute("UPDATE state_node_claims SET lease_expires_at='2000-01-01T00:00:00.000000+00:00' WHERE claim_id=?",(c['claim_id'],))
    if bad=='released':call(s,'release_claim',project_id='p',claim_id=c['claim_id'],fence=c['fence'],reason='done')
    if bad=='wrong-node':extra['node_key']='missing'
    if bad=='wrong-purpose':
        call(s,'release_claim',project_id='p',claim_id=c['claim_id'],fence=c['fence'],reason='replace')
        c=claim(s,'investigate','investigate')
    if bad=='revision':extra['expected_revision']=2
    before=counts(db)
    with pytest.raises(StateGraphError):start(target,c,**extra)
    assert counts(db)==before
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT attempt_id FROM state_node_claims WHERE claim_id=?',(c['claim_id'],)).fetchone()[0] is None


def test_review_does_not_authorize_workflow_implicit_implementation_or_second_active(rig):
    db,s,owner=rig;c=claim(s)
    with pytest.raises(ClaimError):
        s.attempts.reserve('p','n',1,'dpe_default','workflow',owner_driver_id='reviewer',claim_id=c['claim_id'],fence=c['fence'])
    with pytest.raises(ClaimError):start(s,c,'implicit',claim_id=None,fence=None)
    first=start(s,c)
    second=claim(service(db,'foreign'),request='foreign-review')
    with pytest.raises(StateConflict):
        start(service(db,'foreign'),second,'second')
    assert call(s,'get_claim',project_id='p',claim_id=second['claim_id'])['claim']['attempt_id'] is None
    implement=claim(service(db,'implementer'),'implement','implement')
    with pytest.raises(ClaimError):claim(service(db,'other'),'implement','other-implement')
    assert call(s,'get_claim',project_id='p',claim_id=implement['claim_id'])['claim']['driver_id']=='implementer'


def test_review_claim_on_open_node_cannot_dispatch(rig):
    db,s,owner=rig
    call(owner,'add_nodes',project_id='p',nodes=[dict(key='open',goal='G',acceptance=[dict(id='c',kind='test',description='C')])])
    c=call(s,'claim_node',project_id='p',node_key='open',purpose='review',expected_revision=1,request_key='open-review')
    before=counts(db)
    with pytest.raises(ClaimError):start(s,c,'open-start',node_key='open')
    assert counts(db)==before


def test_active_review_replay_cannot_change_claim_or_fence(rig):
    db,s,owner=rig;c=claim(s);a=start(s,c)
    replacement=claim(s,request='replacement')
    for bad in [replacement,{**c,'fence':c['fence']+1}]:
        with pytest.raises(ClaimError):start(s,bad)
    assert call(s,'get_claim',project_id='p',claim_id=c['claim_id'])['claim']['attempt_id']==a['attempt_id']
    assert call(s,'get_claim',project_id='p',claim_id=replacement['claim_id'])['claim']['attempt_id'] is None
