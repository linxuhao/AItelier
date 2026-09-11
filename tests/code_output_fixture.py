"""Shared fixtures for real direct-code delivery; no fake repo_apply success."""
from pathlib import Path
from skillflow.output_targets import git


def init_code_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Code output tests')
    git(root, 'config', 'user.email', 'code-output@test.invalid')
    git(root, 'commit', '--allow-empty', '-qm', 'fixture baseline')
    return root


def write_claim_code(sf, run_id, claim):
    root=sf._workspace.get_project_code_path('p',run_id=run_id)
    file=root/'impl.py'
    n=int(git(root,'rev-list','--count','HEAD').strip())
    content=f'x = {n}\n'
    params={'file':'impl.py'}
    if file.exists():
        tool='edit';params.update(old_str=file.read_text(),new_str=content)
    else:
        tool='create';params['content']=content
    result=sf.execute_tool(tool,params,run_id=run_id,step_id=claim.step_id,
        step_instance_id=claim.token.step_instance_id,claim_epoch=claim.token.claim_epoch)
    assert not result.get('error'),result
    assert file.read_text()==content


def commit_count(sf,run_id):
    root=sf._workspace.get_project_code_path('p',run_id=run_id)
    return int(git(root,'rev-list','--count','HEAD').strip())-1
