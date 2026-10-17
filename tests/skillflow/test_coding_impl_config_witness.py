"""Adopted independent F1 regressions and bounded config-witness controls."""
import json
import os
from pathlib import Path
import pytest
from tests.skillflow import test_coding_impl_failed_candidate as fc
from core import coding_impl_outcome as outcome, closed_git


def ignored_config_rig(tmp_path, monkeypatch):
    original_app = fc.app
    def baseline(repo, green=False):
        original_app(repo, green)
        (repo / '.gitignore').write_text('pytest.ini\n')
        fc.git(repo, 'rm', '--cached', 'pytest.ini')
        fc.git(repo, 'add', '.gitignore')
        fc.git(repo, 'commit', '-qm', 'pytest configuration is intentionally not in tracked input')
    monkeypatch.setattr(fc, 'app', baseline)
    rig = fc.Rig(tmp_path, monkeypatch)
    # An ignored file may exist without appearing in HEAD or raw clean status.
    (rig.repo / 'pytest.ini').write_text('[pytest]\n')
    assert 'pytest.ini' not in closed_git.ls_tree(rig.repo, 'HEAD')
    assert closed_git.clean_against_head(rig.repo) == (True, 'clean')
    return rig


def test_ignored_untracked_pytest_ini_is_not_an_owned_config_witness(tmp_path, monkeypatch):
    rig = ignored_config_rig(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='config|tracked|owned'):
        outcome.owned_snapshot(rig.repo, rig.rid, original=True)


def test_changed_ignored_effective_config_refuses_special_failed_exit(tmp_path, monkeypatch):
    rig = ignored_config_rig(tmp_path, monkeypatch)
    assert rig.drive(stop_after_baseline=True) == ('running', 0)
    # Different effective config between authentic sessions, absent from tracked inventory.
    (rig.repo / 'pytest.ini').write_text('[pytest]\naddopts = --disable-warnings\n')
    status, count = rig.drive(fc.useful_change)
    report = rig.report()
    target = Path(os.environ.get('REVIEW_RESULTS', str(tmp_path / 'observations')))
    target.mkdir(exist_ok=True)
    sample = target / 'ignored-config-measurement'
    sample.mkdir(exist_ok=True)
    for step in ('baseline', 'test'):
        root = rig.ws.get_final_path('p', step, 'coding_impl')
        (sample/step).mkdir(exist_ok=True)
        for name in ('prechange_report.json', 'candidate_report.json', 'test_report.json'):
            if (root/name).exists():
                (sample/step/name).write_bytes((root/name).read_bytes())
    (sample/'observation.json').write_text(json.dumps({'status':status,'implement_count':count,
        'cpu_measurement_count':rig.calls,'config_tracked':False,'config_changed':True,
        'before_config':'[pytest]\n','after_config':(rig.repo/'pytest.ini').read_text(),
        'retained_outcome':report.get('coding_outcome')},indent=2))
    assert not report.get('coding_outcome'), (status, count, report.get('coding_outcome'))


@pytest.mark.parametrize('name', ['pytest.toml', '.pytest.toml', '.pytest.ini'])
def test_ambiguous_root_config_still_refuses(tmp_path, monkeypatch, name):
    rig = fc.Rig(tmp_path, monkeypatch)
    (rig.repo / name).write_text('[pytest]\n')
    fc.git(rig.repo, 'add', name)
    fc.git(rig.repo, 'commit', '-qm', 'ambiguous root configuration')
    with pytest.raises(ValueError, match='config'):
        outcome.owned_snapshot(rig.repo, rig.rid)


def test_linked_root_config_still_refuses(tmp_path, monkeypatch):
    rig = fc.Rig(tmp_path, monkeypatch)
    (rig.repo / 'pytest.ini').rename(rig.repo / 'actual.ini')
    (rig.repo / 'pytest.ini').symlink_to('actual.ini')
    fc.git(rig.repo, 'add', 'pytest.ini', 'actual.ini')
    fc.git(rig.repo, 'commit', '-qm', 'linked root configuration')
    with pytest.raises(ValueError, match='config'):
        outcome.owned_snapshot(rig.repo, rig.rid)


def test_tracked_witness_and_ignored_cache_allow_actual_retention(tmp_path, monkeypatch):
    original_app = fc.app
    def baseline(repo, green=False):
        original_app(repo, green)
        (repo / '.gitignore').write_text('.venv/\n__pycache__/\n.pytest_cache/\n')
        fc.git(repo, 'add', '.gitignore')
        fc.git(repo, 'commit', '-qm', 'ordinary ignored runtime directories')
    monkeypatch.setattr(fc, 'app', baseline)
    rig = fc.Rig(tmp_path, monkeypatch)
    for directory in ('.venv', '__pycache__', '.pytest_cache'):
        (rig.repo / directory).mkdir(exist_ok=True)
        (rig.repo / directory / 'ignored').write_text('runtime data')
    assert closed_git.clean_against_head(rig.repo) == (True, 'clean')
    assert rig.drive(fc.useful_change) == ('completed', 1)
    baseline_path = rig.ws.get_final_path('p', 'baseline', 'coding_impl') / 'prechange_report.json'
    before = json.loads(baseline_path.read_text())
    after = rig.measurement()
    tracked = closed_git.ls_tree(rig.repo, 'HEAD')['pytest.ini'].sha
    assert before['inventory']['pytest.ini'] == after['inventory']['pytest.ini'] == tracked
    assert rig.report()['coding_outcome']['tests_passed'] is False
    assert rig.report()['coding_outcome']['release_passed'] is False
