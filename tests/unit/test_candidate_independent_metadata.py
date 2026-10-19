"""Additional real-Git ownership controls absent from the authored test matrix."""
import sys,json,os
from pathlib import Path
import pytest
sys.path.insert(0,'/workspace/tests/unit')
from test_candidate_derived_churn import world,cache,call,git

@pytest.mark.parametrize('unsafe',['head-tracked-index-removed','index-hardlink','exclude-symlink','exclude-hardlink','config-symlink','config-hardlink'])
def test_independent_metadata_ownership_keeps_payload(tmp_path,unsafe):
    repo,out=world(tmp_path);root=cache(repo)
    if unsafe=='head-tracked-index-removed':
        git(repo,'add','-f','.zvec-grep/manifest.json');git(repo,'commit','-qm','tracked before index removal');git(repo,'rm','--cached','.zvec-grep/manifest.json')
    elif unsafe=='index-hardlink':os.link(repo/'.git/index',tmp_path/'index-alias')
    else:
        name='info/exclude' if unsafe.startswith('exclude-') else 'config'
        f=repo/'.git'/name;target=tmp_path/('external-'+name.replace('/','-'))
        if unsafe.endswith('symlink'):f.rename(target);f.symlink_to(target)
        else:os.link(f,target)
    before=call(repo,out,'before');assert before['passed'],before
    baseline=json.loads((out/'before/candidate_snapshot.json').read_text())
    present=any(e['path']=='.zvec-grep/manifest.json' for e in baseline['entries'])
    (root/'manifest.json').write_text('changed real bytes under ambiguous Git metadata\n')
    after=call(repo,out,'after')
    print('INDEPENDENT_METADATA_CONTROL',json.dumps({'case':unsafe,'cache_payload_in_baseline':present,'verify':after}),flush=True)
    assert present and not after['passed'] and '.zvec-grep/manifest.json' in after.get('changed_paths',[])
