import importlib.util
import importlib.metadata
import io
import json
import os
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.drivers import DriverRegistry
from core.state_database import StateDatabase

ROOT=Path(__file__).resolve().parents[2]

def test_exact_source_and_sdk_binding():
    import core.drivers,api.authz,api.driver_routers,cli.client
    assert importlib.metadata.version('skillflow-py') == '1.5.88'
    for module in [core.drivers,api.authz,api.driver_routers,cli.client]:
        assert Path(module.__file__).resolve().is_relative_to(ROOT)

@pytest.fixture
def world(tmp_path,monkeypatch):
    from api import authz,driver_routers
    db=StateDatabase(str(tmp_path/'onboarding.sqlite'))
    reg=DriverRegistry(db,'synthetic-onboarding-pepper-'*3)
    owner='synthetic-onboarding-owner'
    reg.seed(owner)
    monkeypatch.setattr(authz,'driver_registry',lambda:reg)
    monkeypatch.setattr(authz,'gate_enabled',lambda:True)
    monkeypatch.setattr(authz.cf_access,'email_from_request_headers',lambda *args:None)
    spec=importlib.util.spec_from_file_location('actual_onboarding',ROOT/'scripts/driver_token.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    app=FastAPI();app.include_router(driver_routers.router)
    with TestClient(app) as client:
        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self,*args): self.close()
        def transport(request,timeout):
            body = json.loads(request.data) if request.data is not None else None
            response=client.request(request.method,request.full_url,json=body,headers=dict(request.header_items()))
            if response.status_code>=400:
                raise module.urllib.error.HTTPError(request.full_url,response.status_code,'synthetic isolated response',{},io.BytesIO(response.content))
            return Reply(response.content)
        monkeypatch.setattr(module.urllib.request,'urlopen',transport)
        call=module._call
        yield module,client,reg,call,{'X-AItelier-Admin-Token':owner}

def test_current_real_self_register_idempotence_rotate_and_private_file(world,tmp_path,capsys):
    module,client,reg,call,headers=world
    first=module.self_register('new-client','New client',home=tmp_path,call=call,owner_headers=lambda:headers)
    assert first['status']=='registered'
    path=Path(first['token_file']);token=path.read_text().strip()
    assert path.stat().st_mode&0o777==0o600 and path.parent.stat().st_mode&0o777==0o700
    assert reg.lookup_token(token)['driver_id']=='new-client'
    assert reg.get('new-client')['is_admin'] is False
    assert token not in repr(first)
    again=module.self_register('new-client','New client',home=tmp_path,call=call,owner_headers=lambda:headers)
    assert again['status']=='already_registered'
    rotated=module.self_register('new-client','New client',home=tmp_path,rotate=True,call=call,owner_headers=lambda:headers)
    assert rotated['status']=='rotated' and reg.lookup_token(token) is None
    assert reg.lookup_token(path.read_text().strip())['driver_id']=='new-client'
    assert token not in json.dumps(reg.audit())
    assert not capsys.readouterr().out

@pytest.mark.parametrize('kind',['nonadmin','wrong','reserved','existing','stale'])
def test_current_real_self_register_unauthorized_inverse(world,tmp_path,kind):
    module,client,reg,call,owner=world
    before=reg.audit()
    driver='refused-client';headers=owner
    if kind=='nonadmin':
        token=reg.register('nonadmin','Nonadmin',actor='driver:owner-cli')['token']
        headers={'X-AItelier-Driver-Token':token};before=reg.audit()
    elif kind=='wrong':headers={'X-AItelier-Admin-Token':'synthetic-wrong-owner'}
    elif kind=='reserved':driver='public'
    elif kind=='existing':
        reg.register(driver,'Existing',actor='driver:owner-cli');before=reg.audit()
    else:
        module.write_token_file(driver,'synthetic-stale-own-file',home=tmp_path)
        reg.register(driver,'Existing',actor='driver:owner-cli');before=reg.audit()
    with pytest.raises(SystemExit):
        module.self_register(driver,'Refused',home=tmp_path,call=call,owner_headers=lambda:headers)
    assert reg.audit()==before


def test_absent_driver_get_is_404_auth_and_input_refusals_stay_distinct(world):
    module,client,reg,call,headers=world
    assert client.get('/api/drivers/absent',headers=headers).status_code==404
    assert client.get('/api/drivers/owner-cli',headers=headers).status_code==200
    assert client.get('/api/drivers/absent',headers={'X-AItelier-Admin-Token':'synthetic-wrong'}).status_code==403
    assert client.post('/api/drivers',headers=headers,json={'driver_id':'bad id','display_name':'Bad'}).status_code==422
