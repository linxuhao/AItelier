"""Private acknowledgement policy, actual quorum/history and read boundaries."""
import ast,copy,importlib.util,subprocess
from pathlib import Path
import pytest
from core.state_commands import ProjectPrivate,execute
from core.state_database import StateDatabase
from core.state_service import StateService
from core.state_graph import StateGraphStore
from core.state_privacy import PRIVATE_STATE_TABLES,PUBLIC_STATE_TABLES
from core.director_messaging_protocol import DirectorMessageError
from tests.unit.test_driver_p2 import peers,send

TABLES={'state_director_delivery_acks','state_director_legacy_acks'}
# Multi-driver P3 (rebased onto this policy) classifies its own tables: handoff
# packages private, the enforcement switch public.
P3_PRIVATE={'state_handoffs'}
P3_PUBLIC={'state_project_enforcement'}
# The retained Git bundle store holds the same private bytes as state_git_artifacts.
BUNDLE_STORE_PRIVATE={'state_git_bundle_store'}
# Audited retirement of cross-project context run protection (3f817f6fe7): the
# retirement rows name the project, reference and node a protection was lifted
# from — the same private fields as the protections themselves — so the table is
# PRIVATE and has its own denied-reader canary in tests/support/state_canaries.py.
CONTEXT_PROTECTION_PRIVATE={'state_context_run_protection_retirements'}
BASE='f367c46f2cd29d4d3306aa6ca540bf8c54ebbeb9'

def old_sets():
    root=Path(__file__).resolve().parents[2]
    tree=ast.parse(subprocess.check_output(['git','-C',str(root),'show',BASE+':core/state_privacy.py']))
    return {n.targets[0].id:set(ast.literal_eval(n.value.args[0])) for n in tree.body
            if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)
            and n.targets[0].id in {'PRIVATE_STATE_TABLES','PUBLIC_STATE_TABLES'}}

@pytest.mark.docker  # needs a frozen Source WITH history: `git show BASE:core/state_privacy.py` is the pre-policy baseline, and a linked worktree mounted alone has no repository
def test_policy_adds_only_two_private_ack_tables_and_preserves_public_set():
    old=old_sets()
    assert set(PRIVATE_STATE_TABLES)-old['PRIVATE_STATE_TABLES']==TABLES|P3_PRIVATE|BUNDLE_STORE_PRIVATE|CONTEXT_PROTECTION_PRIVATE
    assert not old['PRIVATE_STATE_TABLES']-set(PRIVATE_STATE_TABLES)
    assert set(PUBLIC_STATE_TABLES)-old['PUBLIC_STATE_TABLES']==P3_PUBLIC
    assert not old['PUBLIC_STATE_TABLES']-set(PUBLIC_STATE_TABLES)
    assert not (TABLES|P3_PRIVATE|BUNDLE_STORE_PRIVATE|CONTEXT_PROTECTION_PRIVATE) & set(PUBLIC_STATE_TABLES)

def stored_director_event(service,project_id,event_type,delivery_id):
    """The one stored state_events row of this type for this delivery, payload decoded."""
    matches=[e for e in service.store.events(project_id,limit=500)
             if e['event_type']==event_type and e['payload'].get('delivery_id')==delivery_id]
    assert len(matches)==1,[(e['event_type'],e['payload']) for e in matches]
    return matches[0]['payload']

def test_own_ack_and_resolve_events_carry_the_acting_driver_actor(peers):
    # The echo filter in integrations/dsh-driver/runtime.mjs (ownEchoOnly) decides
    # "own" only from the event's own `actor`, so the emitter must stamp it. Drive the
    # real ack and resolve paths (not hand-built payloads) and read what State stored.
    _,s=peers
    d=send(s['ada'],ack_quorum=2)['result']['deliveries'][0]
    s['bob'].director_messages.acknowledge_director_message('beta',d['delivery_id'],1,'bob-ack',protocol_version='v3')
    acked=stored_director_event(s['bob'],'beta','director_message_acked',d['delivery_id'])
    assert acked['actor']=='driver:bob' and acked['driver_id']=='bob'
    assert set(acked)=={'delivery_id','driver_id','actor','acks'}
    resolved_delivery=send(s['ada'],request_key='to-resolve',ack_quorum=2)['result']['deliveries'][0]
    s['bob'].director_messages.acknowledge_director_message('beta',resolved_delivery['delivery_id'],1,'bob-ack-2',protocol_version='v3')
    s['bob'].director_messages.resolve_director_message('beta',resolved_delivery['delivery_id'],2,'bob-resolve',protocol_version='v3')
    resolved=stored_director_event(s['bob'],'beta','director_message_resolved',resolved_delivery['delivery_id'])
    assert resolved['actor']=='driver:bob' and resolved['driver_id']=='bob'
    assert set(resolved)=={'delivery_id','driver_id','actor','acks'}

def ack_ledger(db):
    with db.get_connection() as c:
        return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')]
                for t in TABLES|{'state_director_deliveries','state_director_idempotency','state_events'}}

def deny_rows(service,table):
    for handle in [service.db,service.store.db]:
        with pytest.raises(ProjectPrivate):
            with handle.get_connection() as c:c.execute('SELECT * FROM '+table).fetchall()

def test_actual_quorum_history_trusted_positive_untrusted_and_foreign_acker_negative(peers):
    _,s=peers
    d=send(s['ada'],ack_quorum=2)['result']['deliveries'][0]
    before=ack_ledger(s['bob'].db)
    with pytest.raises(DirectorMessageError):
        s['ada'].director_messages.acknowledge_director_message('beta',d['delivery_id'],1,'foreign',protocol_version='v3')
    assert ack_ledger(s['bob'].db)==before
    first=s['bob'].director_messages.acknowledge_director_message('beta',d['delivery_id'],1,'bob',protocol_version='v3')['result']
    assert first['acked_count']==1 and not first['quorum_met']
    replay=s['bob'].director_messages.acknowledge_director_message('beta',d['delivery_id'],1,'again',protocol_version='v3')['result']
    assert replay['replayed'] and replay['acked_count']==1
    second=s['carol'].director_messages.acknowledge_director_message('beta',d['delivery_id'],2,'carol',protocol_version='v3')['result']
    assert second['acked_count']==2 and second['quorum_met'] and second['delivery']['status']=='resolved'
    with s['bob'].db.get_connection() as c:
        rows=c.execute('SELECT * FROM state_director_delivery_acks WHERE delivery_id=? ORDER BY driver_id',(d['delivery_id'],)).fetchall()
        assert [r['driver_id'] for r in rows]==['bob','carol'] and all(r['acked_at'] and r['ack_event_seq'] for r in rows)
    owner=StateService(s['bob'].db,actor='owner:synthetic@example.test',is_admin=True,project_read_trusted=True)
    item=owner.director_messages.list_director_messages('beta',protocol_version='v3')['result']['items'][0]
    assert item['acked_count']==2
    s['bob'].open_project('beta')
    before=ack_ledger(s['bob'].db)
    anonymous=StateService(s['bob'].db,actor='anonymous',project_read_trusted=False)
    for table in TABLES:deny_rows(anonymous,table)
    with pytest.raises(ProjectPrivate):
        execute(anonymous,'list_director_messages',{'project_id':'beta','protocol_version':'v3'})
    assert ack_ledger(s['bob'].db)==before

@pytest.mark.docker  # needs a frozen Source WITH history: `git show 100a1886:core/director_messaging.py` is the pre-ack mailbox, and a linked worktree mounted alone has no repository
def test_actual_legacy_migration_retains_acknowledged_history_and_stays_private(tmp_path):
    root=Path(__file__).resolve().parents[2]
    original=tmp_path/'old_director_mailbox.py'
    original.write_bytes(subprocess.check_output(['git','-C',str(root),'show','100a1886f435c3da76aab2bcf2f28c46649bb037:core/director_messaging.py']))
    spec=importlib.util.spec_from_file_location('original_private_mailbox',original)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    db=StateDatabase(str(tmp_path/'legacy.sqlite'));store=StateGraphStore(db,project_read_trusted=True)
    store.create_project('oldsender','oldsender');store.create_project('oldtarget','oldtarget')
    old=module.SQLiteDirectorMessaging(store,'legacy-fixture',project_read_trusted=True)
    d=old.send_director_message('oldsender','fixture','old','old','retained',target_project_id='oldtarget')['result']['deliveries'][0]
    old.acknowledge_director_message('oldtarget',d['delivery_id'],1,'old-ack')
    with db.get_connection() as c:
        before=[tuple(r) for r in c.execute('SELECT * FROM state_director_deliveries')]
    from core.director_messaging import initialize
    initialize(db);initialize(db)
    with db.get_connection() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM state_director_deliveries')]==before
        assert c.execute('SELECT delivery_id FROM state_director_legacy_acks').fetchone()[0]==d['delivery_id']
        assert not c.execute('SELECT * FROM state_director_delivery_acks').fetchall()
    owner=StateService(db,actor='owner:synthetic@example.test',project_read_trusted=True,is_admin=True)
    item=owner.director_messages.list_director_messages('oldtarget',protocol_version='v3')['result']['items'][0]
    assert item['legacy_ack'] and item['quorum_met'] and item['acked_count']==0
    owner.open_project('oldtarget');before=ack_ledger(db)
    anonymous=StateService(db,actor='anonymous',project_read_trusted=False)
    for table in TABLES:deny_rows(anonymous,table)
    assert ack_ledger(db)==before
