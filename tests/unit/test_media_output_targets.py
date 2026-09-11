import importlib
from pathlib import Path
import pytest


@pytest.mark.parametrize('module,name,args,extension',[
    ('gen_image_asset','gen_image_asset',{'prompt':'test image','transparent':False,'verify':False},'png'),
    ('gen_audio_asset','gen_audio_asset',{'preset':'jump'},'wav')])
@pytest.mark.parametrize('target',['code','artifact'])
def test_media_bytes_follow_declared_destination_without_code_staging(tmp_path,monkeypatch,module,name,args,extension,target):
    m=importlib.import_module(f'aitelier.tools.{module}.impl')
    repo=tmp_path/'repo';repo.mkdir();artifact=tmp_path/'artifact';artifact.mkdir()
    destination=repo if target=='code' else artifact
    monkeypatch.setattr(m,'call_tool',lambda *a,**kw:'https://test.invalid/result')
    monkeypatch.setattr(m,'urls_in',lambda text:['https://test.invalid/result'])
    monkeypatch.setattr(m,'fetch',lambda _:b'\x00\xffmedia')
    result=getattr(m,name)(dest=f'assets/test.{extension}',project_root=str(repo),
        output_dir=str(destination),output_target=target,**args)
    assert not result.get('error'),result
    assert (destination/f'assets/test.{extension}').read_bytes()==b'\x00\xffmedia'
    other=artifact if target=='code' else repo
    assert not (other/f'assets/test.{extension}').exists()


def test_partial_image_failure_reports_files_that_really_landed(tmp_path,monkeypatch):
    from aitelier.tools.gen_image_asset import impl as m
    monkeypatch.setattr(m,'call_tool',lambda tool,*a,**kw:tool)
    monkeypatch.setattr(m,'urls_in',lambda text:['one','two'] if text=='slice_sheet' else ['image'])
    def fetch(url):
        if url=='two':raise m.MCPError('second image failed')
        return b'first image'
    monkeypatch.setattr(m,'fetch',fetch)
    got=m.gen_image_asset(prompt='sheet',dest='sprite.png',rows=1,cols=2,
        project_root=str(tmp_path),output_dir=str(tmp_path),output_target='code',verify=False,transparent=False)
    assert got['written']==['sprite_0.png'] and got['error']=='second image failed'
    assert (tmp_path/'sprite_0.png').read_bytes()==b'first image'


def test_code_media_rejects_a_different_injected_output_root(tmp_path):
    from aitelier.tools.gen_image_asset.impl import gen_image_asset
    repo=tmp_path/'repo';repo.mkdir();other=tmp_path/'other';other.mkdir()
    got=gen_image_asset(prompt='image',dest='x.png',project_root=str(repo),output_dir=str(other),output_target='code')
    assert got.get('error')
    assert list(repo.iterdir())==[] and list(other.iterdir())==[]


def test_explicit_artifact_output_never_falls_back_to_code(tmp_path):
    from aitelier.tools.gen_image_asset.impl import _target_root as image_root
    from aitelier.tools.gen_audio_asset.impl import _target_root as audio_root
    assert image_root("", "artifact", str(tmp_path)) is None
    assert audio_root("", "artifact", str(tmp_path)) is None
