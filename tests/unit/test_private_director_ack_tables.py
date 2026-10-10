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
BASE='f367c46f2cd29d4d3306aa6ca540bf8c54ebbeb9'

def old_sets():
    root=Path(__file__).resolve().parents[2]
    tree=ast.parse(subprocess.check_output(['git','-C',str(root),'show',BASE+':core/state_privacy.py']))
    return {n.targets[0].id:set(ast.literal_eval(n.value.args[0])) for n in tree.body
            if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)
            and n.targets[0].id in {'PRIVATE_STATE_TABLES','PUBLIC_STATE_TABLES'}}

def test_policy_adds_only_two_private_ack_tables_and_preserves_public_set():
    old=old_sets()
    assert set(PRIVATE_STATE_TABLES)-old['PRIVATE_STATE_TABLES']==TABLES
    assert not old['PRIVATE_STATE_TABLES']-set(PRIVATE_STATE_TABLES)
    assert set(PUBLIC_STATE_TABLES)==old['PUBLIC_STATE_TABLES']
    assert not TABLES & set(PUBLIC_STATE_TABLES)

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
