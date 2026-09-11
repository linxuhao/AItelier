"""Actual Git recovery: code bytes are not copied through artifact folders."""
import json
from pathlib import Path

import pytest
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode
from skillflow.output_targets import CodeOutput, git
from core.code_relay import inventory, recovery_commit, require_quiet


def setup(root, config):
    root.mkdir(); git(root, 'init', '-q')
    (root / 'old.py').write_text('before\n')
    (root / 'deleted.py').write_text('delete me\n')
    git(root, 'add', '--', 'old.py', 'deleted.py'); git(root, 'commit', '-qm', 'base')
    journal = CodeOutput(root, config / '.code-output/run/implement.json')
    journal.prepare('run', 1, 'card')
    (root / 'old.py').write_text('after\n')
    (root / 'deleted.py').unlink()
    (root / 'new image.bin').write_bytes(b'\x00\xff\x01')
    journal.record(['old.py', 'deleted.py', 'new image.bin'])
    return journal


def test_recovery_keeps_source_branch_index_and_worktree_unchanged(tmp_path):
    root, config = tmp_path/'repo', tmp_path/'artifacts'
    setup(root, config)
    head = git(root, 'rev-parse', 'HEAD').strip()
    index = (root/'.git/index').read_bytes()
    status = git(root, 'status', '--porcelain')
    manifest = inventory(root, config, 'run')
    assert manifest['unowned'] == []
    got = recovery_commit(root, config, 'run', head, manifest, 'attempt-2')
    assert got != head
    assert git(root, 'rev-parse', 'HEAD').strip() == head
    assert (root/'.git/index').read_bytes() == index
    assert git(root, 'status', '--porcelain') == status
    assert 'UNVALIDATED' in git(root, 'show', '-s', '--format=%s', got)
    recovered = tmp_path/'next'; git(root, 'worktree', 'add', '--detach', str(recovered), got)
    assert (recovered/'old.py').read_text() == 'after\n'
    assert not (recovered/'deleted.py').exists()
    assert (recovered/'new image.bin').read_bytes() == b'\x00\xff\x01'
    assert not list(config.rglob('*.py')) and not list(config.rglob('*.bin'))
    assert recovery_commit(root, config, 'run', head, manifest, 'attempt-2') == got
    # Pending inherited files still participate in validation and the receipt.
    next_journal = CodeOutput(recovered, tmp_path/'next-artifacts/journal.json')
    next_journal.prepare('next-run', 2, 'card', inherited={
        'recovery_commit':got, 'base_commit':head, 'paths':list(manifest['files'])})
    report = next_journal.commit(tmp_path/'next-artifacts/receipt.json', 'verified candidate')
    assert set(report['files']) == set(manifest['files'])


def test_changed_inventory_is_not_published(tmp_path):
    root, config=tmp_path/'repo',tmp_path/'artifacts';setup(root,config)
    head=git(root,'rev-parse','HEAD').strip(); manifest=inventory(root,config,'run')
    (root/'old.py').write_text('another write')
    with pytest.raises(ValueError,match='changed'):
        recovery_commit(root,config,'run',head,manifest,'attempt-2')
    assert not git(root,'for-each-ref','refs/aitelier/recovery').strip()


def test_unreported_dirty_files_are_not_absorbed(tmp_path):
    root, config=tmp_path/'repo',tmp_path/'artifacts';setup(root,config)
    (root/'unowned').write_text('operator file')
    manifest=inventory(root,config,'run');assert manifest['unowned']==['unowned']
    with pytest.raises(ValueError,match='Unowned'):
        recovery_commit(root,config,'run',git(root,'rev-parse','HEAD').strip(),manifest,'attempt-2')


def test_recovery_requires_terminal_and_no_admitted_operations():
    sf=SkillFlow(':memory:')
    sf.register_graph(PipelineGraph(name='g',begin='s',steps=[StepNode(id='s')]))
    rid=sf.create_run('g');sf.start_run(rid)
    with pytest.raises(ValueError):require_quiet(sf,rid)
    operation=sf._admit_op('test-recovery',rid)
    sf.fail_run(rid,'failed while operation still admitted')
    with pytest.raises(ValueError):require_quiet(sf,rid)
    sf._retire_op(operation)
    require_quiet(sf,rid)
