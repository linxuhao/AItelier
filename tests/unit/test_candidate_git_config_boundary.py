"""Real effective config origin and alias observability for derived exemption."""
import json
import os
from pathlib import Path

import pytest
from core.semantic_index_control import git as closed_git
from test_candidate_derived_churn import world, cache, call, git


@pytest.mark.parametrize('case',['empty-config-alias','include-owned','include-alias','include-hardlink','include-external','include-nested-external','include-parent-alias','worktree-owned','worktree-alias','worktree-hardlink','worktree-include-external'])
def test_effective_config_origin_controls_public_candidate_boundary(tmp_path,case):
    repo,out=world(tmp_path);common=repo/'.git'
    if case.startswith('worktree-'):
        source=repo;repo=tmp_path/'linked'
        git(source,'worktree','add','-qb','linked',str(repo));git(source,'config','extensions.worktreeConfig','true')
        own=Path(git(repo,'rev-parse','--absolute-git-dir'));store=own/'config.worktree';store.write_text('[user]\n name = fixture-worktree\n')
    else:store=common/'config'
    target=tmp_path/'external-config';target.write_text('[user]\n name = external-fixture\n')
    if case=='empty-config-alias':store.unlink();target.write_text('');store.symlink_to(target)
    elif case in ('worktree-alias','worktree-hardlink'):
        if case.endswith('alias'):store.rename(tmp_path/'worktree-config');store.symlink_to(tmp_path/'worktree-config')
        else:os.link(store,tmp_path/'worktree-config')
    elif case.startswith('include-') or case=='worktree-include-external':
        included=common/'config-extra';included.write_text('[user]\n name = extra-fixture\n')
        if case=='include-alias':included.unlink();included.symlink_to(target)
        elif case=='include-hardlink':os.link(included,tmp_path/'external-alias')
        elif case in ('include-external','worktree-include-external'):included=target
        elif case=='include-nested-external':included.write_text('[include]\n path = '+str(target)+'\n')
        elif case=='include-parent-alias':
            directory=tmp_path/'outside-config-dir';directory.mkdir();(directory/'config').write_text('[user]\n name = alias-parent\n')
            (common/'linked-config-dir').symlink_to(directory,target_is_directory=True);included=common/'linked-config-dir/config'
        with store.open('a') as stream:stream.write('[include]\n path = '+str(included)+'\n')
    root=cache(repo)
    observed=closed_git(repo,'config','--includes','--null','--show-origin','--list')
    assert observed.returncode==0,observed.stderr
    facts={'case':case,'repository':str(repo),'store':str(store),'store_lstat_links':store.lstat().st_nlink,'store_is_symlink':store.is_symlink(),'store_resolved':str(store.resolve()),'effective_origin_records':observed.stdout.split('\0')}
    assert call(repo,out,'before')['passed']
    baseline=json.loads((out/'before/candidate_snapshot.json').read_text())
    facts['cache_manifest_in_snapshot']=any(e['path']=='.zvec-grep/manifest.json' for e in baseline['entries'])
    (root/'manifest.json').write_text('actual cache churn after config-origin measurement\n')
    result=call(repo,out,'after');facts['public_verify']=result
    facts['public_report']=json.loads((out/'after/candidate_integrity_report.json').read_text())
    print('CONFIG_ORIGIN_OBSERVABLE',json.dumps(facts),flush=True)
    if case in ('include-owned','worktree-owned'):
        assert not facts['cache_manifest_in_snapshot'] and result['passed'],facts
    else:
        assert facts['cache_manifest_in_snapshot'] and not result['passed'] and '.zvec-grep/manifest.json' in result['changed_paths'],facts
