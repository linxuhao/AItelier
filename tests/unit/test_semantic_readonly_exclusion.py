"""Useful real linked Git ownership with read-only original Source metadata."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

if os.environ.get('CONTROL_SCRIPT'):
    spec=importlib.util.spec_from_file_location('image_control',os.environ['CONTROL_SCRIPT'])
    control=importlib.util.module_from_spec(spec);spec.loader.exec_module(control)
else:
    from core import semantic_index_control as control


def git(root,*args):
    return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()


def setup(root,case='positive'):
    source=root/'source';source.mkdir(parents=True);runs=root/'runs';runs.mkdir();work=runs/'owned'
    git(source,'init','-q');git(source,'config','user.name','fixture');git(source,'config','user.email','fixture@localhost')
    (source/'code.py').write_text('value=42\n');git(source,'add','code.py');git(source,'commit','-qm','fixture')
    exclude=source/'.git/info/exclude';exclude.write_text('.zvec-grep/\n')
    if case=='tracked':
        cache=source/'.zvec-grep';cache.mkdir();(cache/'manifest.json').write_text('tracked deliverable')
        git(source,'add','-f','.zvec-grep/manifest.json');git(source,'commit','-qm','tracked fixture')
    git(source,'worktree','add','-qb','owned',str(work))
    outside=root/'outside';outside.mkdir();(outside/'exclude').write_text('.zvec-grep/\n')
    if case=='absent':exclude.unlink()
    if case=='symlink':exclude.unlink();exclude.symlink_to(outside/'exclude')
    if case=='hardlink':exclude.unlink();os.link(outside/'exclude',exclude)
    if case=='parent-symlink':
        exclude.unlink();exclude.parent.rmdir();exclude.parent.symlink_to(outside,target_is_directory=True)
    if case=='negated':exclude.write_text('.zvec-grep/\n!.zvec-grep/\n')
    if case=='cache-symlink':(work/'.zvec-grep').symlink_to(outside,target_is_directory=True)
    if case=='unknown':
        (work/'.git').write_text('gitdir: /nonexistent-owned-fixture\n')
    return source,runs,work,outside


def snapshot(root):
    return {str(p.relative_to(root)):('link',os.readlink(p)) if p.is_symlink() else
            ('file',hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mode&0o777) if p.is_file() else
            ('dir',p.stat().st_mode&0o777) for p in [root,*root.rglob('*')]}


def permissions(root,writable):
    # Actual UID1000 kernel permission checks, never os.access monkeypatching.
    for p in sorted([root,*root.rglob('*')],key=lambda x:len(x.parts),reverse=not writable):
        if not p.is_symlink():p.chmod((0o755 if writable else 0o555) if p.is_dir() else (0o644 if writable else 0o444))


def useful(control_dir,runs,work,source):
    ctl=control.IndexControl(control_dir,runs)
    rec={'run_id':'owned','worktree_path':str(work),'source_repo':str(source)}
    before=ctl.request(rec,'ready');calls=[]
    def client(command,**kwargs):
        calls.append(command)
        cache=work/'.zvec-grep';digest=hashlib.sha256((work/'code.py').read_bytes()).hexdigest()
        if command[1]=='index':cache.mkdir(exist_ok=True);(cache/'manifest.json').write_text(json.dumps({'code.py':digest}))
        else:assert json.loads((cache/'manifest.json').read_text())=={'code.py':digest}
    assert ctl.process_once(execute=client)==[{'run_id':'owned','outcome':'ready'}]
    after=ctl.get('owned');assert after['root']==str(work) and after['source']==str(source)
    assert after['revision']==before['revision']==after['done_revision'] and ctl.settled(after,'ready')
    assert [c[1] for c in calls]==['index','status']
    print('READONLY_ORIGINAL_NAMESPACE',json.dumps({'script':control.__file__,'source':str(source),'root':str(work),'before':before,'after':after}))


def test_existing_readonly_exclusion_permits_useful_owned_work(tmp_path,monkeypatch):
    assert os.geteuid()==1000
    source,runs,work,outside=setup(tmp_path)
    # Repo configuration and inherited routing cannot replace the closed helper.
    git(source,'config','core.fsmonitor',str(tmp_path/'untrusted-monitor'))
    monkeypatch.setenv('GIT_DIR',str(outside));monkeypatch.setenv('GIT_CONFIG_COUNT','1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0','core.excludesFile');monkeypatch.setenv('GIT_CONFIG_VALUE_0',str(outside/'exclude'))
    permissions(source,False);before=snapshot(source)
    try:
        assert not os.access(source/'.git/info/exclude',os.W_OK,effective_ids=True)
        useful(tmp_path/'control',runs,work,source)
        assert snapshot(source)==before
    finally:permissions(source,True)


@pytest.mark.parametrize('case',['absent','symlink','hardlink','parent-symlink','tracked','negated','cache-symlink','unknown'])
def test_readonly_unsafe_or_unknown_exclusion_refuses_without_source_writes(tmp_path,case):
    source,runs,work,outside=setup(tmp_path,case)
    permissions(source,False);before=snapshot(source);external=snapshot(outside)
    try:
        with pytest.raises((OSError,ValueError,RuntimeError)):
            control.IndexControl._exclude_storage(work)
        assert snapshot(source)==before and snapshot(outside)==external
    finally:permissions(source,True)


def test_writable_missing_exclusion_keeps_normal_behavior(tmp_path):
    source,runs,work,outside=setup(tmp_path,'absent')
    control.IndexControl._exclude_storage(work)
    assert (source/'.git/info/exclude').read_text().splitlines()==['.zvec-grep/']
    useful(tmp_path/'control',runs,work,source)
