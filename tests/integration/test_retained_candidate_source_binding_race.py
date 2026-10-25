"""Actual public first-receiver binding change during normal retained import."""
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from core.state_graph import StateConflict
from tests.integration.test_state_git_artifact_durability import fixture, git, node, prune_producer


@pytest.mark.parametrize('rebind_to_original', [False, True])
def test_first_receiver_cannot_admit_against_a_retargeted_source(tmp_path, monkeypatch, rebind_to_original):
    monkeypatch.setenv('AITELIER_HOME', str(tmp_path / 'home'))
    service, source, producer, db, prior, commit, tree, payload, bundle = fixture(tmp_path / 'owned')
    prune_producer(source, producer, commit)
    a, b = tmp_path / 'source-a', tmp_path / 'source-b'
    for dest in [a, b]:
        git(tmp_path, 'clone', '--no-hardlinks', str(source), str(dest))
    service.create_project('receiver', 'Fresh portable receiver')
    service.store.add_nodes('receiver', [node('review')])
    service.bind_source('receiver', str(a))
    imported, resume = Event(), Event()
    original = service._materialize_external_base

    def pause_after_real_import(*args, **kwargs):
        result = original(*args, **kwargs)
        assert git(a, 'rev-parse', commit + '^{commit}') == commit
        imported.set()
        assert resume.wait(10), 'Test controller did not release its own import'
        return result

    monkeypatch.setattr(service, '_materialize_external_base', pause_after_real_import)
    with ThreadPoolExecutor(max_workers=1) as pool:
        work = pool.submit(service.start_external_attempt, 'receiver', 'review', 1,
                           'fixture', 'fresh', 'fresh', base_sha=commit)
        try:
            assert imported.wait(10), 'Normal retained import did not reach the actual checkpoint'
            rebound = service.bind_source('receiver', str(b), expected_revision=1)
            assert rebound['revision'] == 2
            if rebind_to_original:
                rebound = service.bind_source('receiver', str(a), expected_revision=2)
                assert rebound['revision'] == 3
        finally:
            resume.set()
        try:
            result = work.result(timeout=10)
        except StateConflict as exc:
            assert 'source binding changed during artifact preflight' in str(exc)
            print('ADMISSION_REFUSAL=' + str(exc), flush=True)
            result = None
    current = service.portfolio.source_binding('receiver')
    absent = subprocess.run(['git', '-C', str(b), 'cat-file', '-e', commit], capture_output=True).returncode != 0
    with service.store.transaction() as conn:
        owners = conn.execute("SELECT count(*) FROM state_external_owners WHERE project_id='receiver' AND status='active'").fetchone()[0]
    print('ACTUAL_BINDING_RACE=' + json.dumps({'binding': current, 'commit_absent_from_b': absent,
          'owner_count': owners, 'admitted': result is not None}), flush=True)
    assert current['repo_path'] == str(a if rebind_to_original else b) and absent
    assert result is None and owners == 0, 'Admission imported into A but bound its first owner to unmaterialized B'
