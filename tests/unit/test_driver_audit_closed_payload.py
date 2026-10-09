"""Closed audit facts: synthetic credentials cannot enter through caller data."""
import json
import pytest
from core import drivers
from core.state_database import StateDatabase

@pytest.fixture
def registry(tmp_path):
    reg=drivers.DriverRegistry(StateDatabase(str(tmp_path/'audit.sqlite')),'synthetic-pepper-for-audit-'*2)
    reg.seed('synthetic-owner-credential-for-audit')
    reg.register('review','Review',actor='driver:owner-cli')
    return reg

@pytest.mark.parametrize('payload',[
    {'nested':{'token':'synthetic-credential'}},
    {'kind':{'token_hash':'synthetic-hash'},'is_admin':False},
    {'kind':'lan','is_admin':'synthetic-credential'},
    {'kind':'lan','is_admin':False,'token_hash':'synthetic-hash'},
    {'kind':'lan','is_admin':False,'metadata':[{'token':'synthetic-credential'}]},
])
def test_recursive_unknown_keys_and_credential_values_are_refused(registry,payload):
    before=registry.audit()
    with registry._write() as conn, pytest.raises(drivers.DriverError):
        registry._audit(conn,'review','register',payload,'driver:owner-cli')
    assert registry.audit()==before


def test_known_hash_is_refused_as_an_operation_value(registry):
    digest=drivers.token_hash(registry._pepper,'synthetic-owner-credential-for-audit')
    before=registry.audit()
    with registry._write() as conn, pytest.raises(drivers.DriverError):
        registry._audit(conn,'review','register',{'kind':digest,'is_admin':False},'driver:owner-cli')
    assert registry.audit()==before


def test_public_admin_reason_is_not_persisted_or_logged(registry,monkeypatch,caplog):
    from api import authz,driver_routers
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    secret='synthetic-owner-credential-for-audit'
    monkeypatch.setattr(authz,'driver_registry',lambda:registry)
    monkeypatch.setattr(authz,'gate_enabled',lambda:True)
    monkeypatch.setattr(authz.cf_access,'email_from_request_headers',lambda *args:None)
    app=FastAPI();app.include_router(driver_routers.router)
    with TestClient(app) as client:
        headers={'X-AItelier-Admin-Token':secret}
        before=len(registry.audit())
        response=client.post('/api/drivers/review/admin',headers=headers,
                             json={'is_admin':True,'expected_revision':1,'reason':secret})
        assert response.status_code==200
        assert len(registry.audit())==before+1
        assert json.loads(registry.audit()[0]['payload_json'])=={'is_admin':True}
        response=client.post('/api/drivers/review/status',headers=headers,
                             json={'status':'suspended','expected_revision':2,'reason':secret})
        assert response.status_code==200
    digest=drivers.token_hash(registry._pepper,secret)
    retained=json.dumps(registry.audit())+caplog.text
    assert secret not in retained and digest not in retained


def test_membership_audit_uses_resolved_row_reference(registry):
    secret='synthetic-owner-credential-for-audit'
    row=registry.set_membership('fixture-project','review','member',0,secret,
                               actor='driver:owner-cli',project_exists=lambda p:True)
    payload=json.loads(registry.audit()[0]['payload_json'])
    with registry.db.get_connection() as conn:
        ref=conn.execute('SELECT rowid FROM project_drivers WHERE project_id=? AND driver_id=?',
                         ('fixture-project','review')).fetchone()[0]
    assert payload=={'membership_ref':ref,'status':'member'}
    assert secret not in json.dumps(registry.audit())
    assert row['project_id']=='fixture-project'


@pytest.mark.parametrize("field", ["subject", "actor"])
def test_known_credential_cannot_enter_audit_identity_columns(registry,field):
    before=registry.audit()
    secret='synthetic-owner-credential-for-audit'
    subject=secret if field=='subject' else 'review'
    actor=secret if field=='actor' else 'driver:owner-cli'
    with registry._write() as conn, pytest.raises(drivers.DriverError):
        registry._audit(conn,subject,'set_admin',{'is_admin':True},actor)
    assert registry.audit()==before
