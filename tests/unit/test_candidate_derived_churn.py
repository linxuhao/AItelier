"""Public candidate snapshot/verify over real Git and physical index churn."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from aitelier.tools.candidate_integrity.impl import candidate_integrity


def git(repo,*args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()


def world(tmp_path,*,git_owned=True):
    repo=tmp_path/'repo';repo.mkdir()
    for name,body in {'README.md':'# useful delivery\n','src/main.py':'VALUE=1\n','config.json':'{}\n','.hidden':'hidden payload\n','ignored.txt':'ignored payload\n','untracked.txt':'untracked payload\n','.gitignore':'ignored.txt\n'}.items():
        p=repo/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body)
    if git_owned:
        git(repo,'init','-q');git(repo,'config','user.name','fixture');git(repo,'config','user.email','fixture@example.invalid')
        git(repo,'add','README.md','src/main.py','config.json','.hidden','.gitignore');git(repo,'commit','-qm','real payload')
        (repo/'.git/info/exclude').write_text('.zvec-grep/\n')
    out=tmp_path/'reports';out.mkdir()
    return repo,out


def call(repo,out,phase):
    return candidate_integrity(project_root=str(repo),out_dir=str(out/phase),phase='snapshot' if phase=='before' else 'verify',baseline_step='before')


def cache(repo,placement='root'):
    root=repo/('.zvec-grep' if placement=='root' else 'nested/.zvec-grep')
    (root/'scratch').mkdir(parents=True);(root/'index.zvec').mkdir()
    (root/'manifest.json').write_text('{"generation":1}\n');(root/'scratch/current').write_bytes(b'before\0')
    (root/'index.zvec/CURRENT').write_text('000001\n');return root


def mutate(root):
    (root/'manifest.json').write_text('{"generation":2}\n');(root/'scratch/current').unlink();(root/'index.zvec/000002').write_bytes(b'new derived shard\0')


@pytest.mark.parametrize('placement',['root','nested'])
def test_derived_churn_keeps_useful_candidate_identity(tmp_path,placement):
    repo,out=world(tmp_path);root=cache(repo,placement)
    payload={n:hashlib.sha256((repo/n).read_bytes()).hexdigest() for n in ['README.md','src/main.py','config.json','.hidden','ignored.txt','untracked.txt']}
    assert call(repo,out,'before')['passed']
    mutate(root)
    assert {n:hashlib.sha256((repo/n).read_bytes()).hexdigest() for n in payload}==payload
    result=call(repo,out,'after');print('REAL_DERIVED_CHURN',json.dumps({'placement':placement,'product_hashes':payload,'verify':result}))
    assert result['passed'],result
    report=json.loads((out/'after/candidate_integrity_report.json').read_text())
    assert report['changed_paths']==[] and report['before']==report['after']


@pytest.mark.parametrize('operation',['create','remove'])
def test_derived_storage_lifetime_is_outside_payload(tmp_path,operation):
    repo,out=world(tmp_path)
    if operation=='remove':root=cache(repo)
    assert call(repo,out,'before')['passed']
    if operation=='create':cache(repo)
    else:shutil.rmtree(root)
    assert call(repo,out,'after')['passed']


@pytest.mark.parametrize('name',['src/main.py','README.md','config.json','ignored.txt','untracked.txt','.hidden'])
@pytest.mark.parametrize('operation',['change','delete','add'])
def test_real_payload_mutation_still_fails(tmp_path,name,operation):
    repo,out=world(tmp_path);cache(repo)
    if operation=='add':(repo/name).unlink()
    # README must exist at snapshot time; its addition is tested as replacement.
    if name=='README.md' and operation=='add':(repo/name).write_text('old README\n')
    assert call(repo,out,'before')['passed']
    p=repo/name
    if operation=='delete':p.unlink()
    else:p.write_text('mutated deliverable\n')
    result=call(repo,out,'after');assert not result['passed'],result
    assert result.get('error')
    if name!='README.md' or operation!='delete':
        report=json.loads((out/'after/candidate_integrity_report.json').read_text());assert name in report['changed_paths']


@pytest.mark.parametrize('placement',['root','nested'])
@pytest.mark.parametrize('operation',['change','delete','add'])
def test_tracked_cache_named_source_is_real_payload(tmp_path,placement,operation):
    repo,out=world(tmp_path);root=cache(repo,placement);name=(root/'manifest.json').relative_to(repo).as_posix()
    git(repo,'add','-f',name);git(repo,'commit','-qm','cache named deliverable')
    if operation=='add':(root/'manifest.json').unlink()
    assert call(repo,out,'before')['passed']
    if operation=='delete':(root/'manifest.json').unlink()
    else:(root/'manifest.json').write_text('changed tracked Source\n')
    result=call(repo,out,'after');assert not result['passed'] and name in result['changed_paths']


@pytest.mark.parametrize('unsafe',['non-git','not-ignored','git-symlink','cache-symlink','inner-symlink','fifo','unknown-git'])
def test_unsafe_or_unknown_storage_is_never_an_escape(tmp_path,unsafe):
    repo,out=world(tmp_path,git_owned=unsafe!='non-git');root=cache(repo)
    outside=tmp_path/'outside';outside.mkdir();(outside/'payload').write_text('outside')
    if unsafe=='not-ignored':(repo/'.git/info/exclude').write_text('')
    elif unsafe=='git-symlink':
        target=tmp_path/'metadata';(repo/'.git').rename(target);(repo/'.git').symlink_to(target,target_is_directory=True)
    elif unsafe=='cache-symlink':shutil.rmtree(root);root.symlink_to(outside,target_is_directory=True)
    elif unsafe=='inner-symlink':(root/'scratch/link').symlink_to(outside,target_is_directory=True)
    elif unsafe=='fifo':os.mkfifo(root/'pipe')
    elif unsafe=='unknown-git':(repo/'.git').rename(tmp_path/'metadata');(repo/'.git').write_text('gitdir: /no-such-fixture\n')
    before=call(repo,out,'before')
    if unsafe=='fifo':assert not before['passed'] and 'unsupported file type' in before['error'];return
    assert before['passed'],before
    if unsafe=='cache-symlink':root.unlink();root.symlink_to(tmp_path/'other-outside',target_is_directory=True)
    else:(root/'manifest.json').write_text('mutated real bytes under unsafe root\n')
    result=call(repo,out,'after');assert not result['passed'],result
    assert (outside/'payload').read_text()=='outside'


def test_closed_git_route_and_no_callback_preserve_real_ownership(tmp_path,monkeypatch):
    repo,out=world(tmp_path);root=cache(repo);other=tmp_path/'other';other.mkdir()
    git(repo,'config','core.fsmonitor',str(tmp_path/'should-not-run'))
    monkeypatch.setenv('GIT_DIR',str(other));monkeypatch.setenv('GIT_CONFIG_COUNT','1');monkeypatch.setenv('GIT_CONFIG_KEY_0','core.excludesFile');monkeypatch.setenv('GIT_CONFIG_VALUE_0','/not-this-repository')
    # A repository-selected fsmonitor must not be invoked by metadata inspection.
    assert call(repo,out,'before')['passed'];mutate(root);assert call(repo,out,'after')['passed']
    assert not (tmp_path/'should-not-run').exists()


def test_unchanged_payload_and_resolved_bindings(tmp_path):
    repo,out=world(tmp_path);assert call(repo,out,'before')['passed'];assert call(repo,out,'after')['passed']
    baseline=out/'before/candidate_snapshot.json';original=baseline.read_bytes()
    other=tmp_path/'other';shutil.copytree(repo,other)
    result=candidate_integrity(project_root=str(other),out_dir=str(out/'wrong'),phase='verify',baseline_step='before')
    assert not result['passed'] and 'different resolved project root' in result['error'];assert baseline.read_bytes()==original
    assert not candidate_integrity(project_root=str(repo),out_dir=str(repo/'inside'),phase='snapshot')['passed']
    assert not candidate_integrity(project_root=str(repo),out_dir=str(out/'bad'),phase='verify',baseline_step='../before')['passed']


def test_historical_snapshot_is_refused_without_rewriting(tmp_path):
    repo,out=world(tmp_path);assert call(repo,out,'before')['passed']
    path=out/'before/candidate_snapshot.json';old=json.loads(path.read_text());old['schema_version']=1;path.write_text(json.dumps(old));body=path.read_bytes()
    result=call(repo,out,'after');assert not result['passed'] and 'unsupported schema' in result['error'];assert path.read_bytes()==body


@pytest.mark.parametrize('transition',['tracked','no-ignore','symlink','unknown-index'])
def test_derived_boundary_cannot_hide_new_unsafe_state(tmp_path,transition):
    repo,out=world(tmp_path);root=cache(repo);assert call(repo,out,'before')['passed']
    if transition=='tracked':git(repo,'add','-f','.zvec-grep/manifest.json')
    elif transition=='no-ignore':(repo/'.git/info/exclude').write_text('')
    elif transition=='symlink':(root/'scratch/link').symlink_to(tmp_path/'external')
    else:(repo/'.git/index').unlink()
    result=call(repo,out,'after');assert not result['passed'] and result.get('changed_paths'),result


@pytest.mark.parametrize('unsafe',['nested-repository','cache-repository','inner-repository','missing-index','linked-index','linked-payload'])
def test_git_or_alias_ambiguity_keeps_cache_named_bytes_protected(tmp_path,unsafe):
    repo,out=world(tmp_path)
    if unsafe=='nested-repository':
        nested=repo/'nested';nested.mkdir();git(nested,'init','-q');(nested/'.git/info/exclude').write_text('.zvec-grep/\n');root=cache(repo,'nested')
    else:root=cache(repo)
    if unsafe=='cache-repository':git(root,'init','-q')
    elif unsafe=='inner-repository':git(root/'scratch','init','-q')
    elif unsafe=='missing-index':(repo/'.git/index').unlink()
    elif unsafe=='linked-index':
        target=tmp_path/'index';(repo/'.git/index').rename(target);(repo/'.git/index').symlink_to(target)
    elif unsafe=='linked-payload':os.link(root/'manifest.json',tmp_path/'external-alias')
    assert call(repo,out,'before')['passed'];(root/'manifest.json').write_text('changed ambiguous bytes\n')
    result=call(repo,out,'after');assert not result['passed'] and result.get('changed_paths'),result
