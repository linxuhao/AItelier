"""Actual fresh/repeated filesystem installation without production data."""
import importlib.util
import json
from pathlib import Path
import shlex
import sqlite3

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('dsh_host_installer', ROOT / 'deploy/install_dsh_host.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    for name in ('DPE_DB_PATH', 'AITELIER_SECRETS_DIR', 'AITELIER_HOME'):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / 'checkout space %test'; repo.mkdir()
    (repo / 'deploy').mkdir()
    (repo / 'deploy/aitelier-dsh-host.service').write_bytes((ROOT / 'deploy/aitelier-dsh-host.service').read_bytes())
    (repo / '.env.example').write_text('# preserved example\nEXAMPLE=value\n')
    home = tmp_path / 'fresh home'; home.mkdir()
    return repo, home, home / '.aitelier-secrets'


def test_first_and_repeated_install_preserve_identity_and_raw_history(paths):
    repo, home, secrets = paths
    assert installer.prepare(repo, home, secrets)
    pepper = secrets / 'AITELIER_DRIVER_TOKEN_PEPPER'
    assert len(pepper.read_text().strip()) >= 32
    assert pepper.stat().st_mode & 0o777 == 0o600
    assert json.loads((home / '.AItelier/dsh-drivers/runtime.json').read_text()) == {'mode': 'host'}
    (repo / '.env').write_text((repo / '.env').read_text() + 'AITELIER_ADMIN_TOKEN="owner-secret # retained"\n')
    directory = home / '.AItelier/dsh-drivers/own'; directory.mkdir()
    for name, content in [('token', 'retained-token'), ('cursor.json', '{"after":123}'), ('session.jsonl', 'precompact-history')]:
        (directory / name).write_text(content)
    files = [pepper, repo / '.env', *directory.iterdir()]
    before = {p: (p.read_bytes(), p.stat().st_ino) for p in files}
    assert not installer.prepare(repo, home, secrets)
    assert {p: (p.read_bytes(), p.stat().st_ino) for p in files} == before


def test_missing_pepper_for_existing_identities_refuses_without_rekeying(paths):
    repo, home, secrets = paths
    state = home / '.AItelier'; state.mkdir()
    with sqlite3.connect(state / 'aitelier.db') as db:
        db.execute('CREATE TABLE drivers(token_hash TEXT)')
        db.execute('INSERT INTO drivers VALUES (?)', ('existing-hash',))
    with pytest.raises(ValueError, match='original AITELIER_DRIVER_TOKEN_PEPPER'):
        installer.prepare(repo, home, secrets)
    assert not (secrets / 'AITELIER_DRIVER_TOKEN_PEPPER').exists()
    assert not (repo / '.env').exists()
    assert not (state / 'dsh-drivers/runtime.json').exists()


def test_live_container_controller_is_not_silently_replaced(paths):
    repo, home, secrets = paths
    state = home / '.AItelier'; state.mkdir()
    with sqlite3.connect(state / 'aitelier.db') as db:
        db.execute('CREATE TABLE dsh_drivers(enabled INTEGER)')
        db.execute('INSERT INTO dsh_drivers VALUES (1)')
    with pytest.raises(ValueError, match='Stop the enabled container drivers'):
        installer.prepare(repo, home, secrets)
    assert not (state / 'dsh-drivers/runtime.json').exists()
    # Once already in host mode, reinstallation preserves active sessions.
    config = state / 'dsh-drivers/runtime.json'; config.parent.mkdir()
    config.write_text('{"mode":"host"}')
    installer.prepare(repo, home, secrets)
    assert installer.count_rows(state / 'aitelier.db', 'dsh_drivers', 'enabled=1') == 1


def test_rendered_service_uses_actual_paths_and_host_credentials(paths, monkeypatch):
    repo, home, secrets = paths
    (repo / '.env').write_text('export DPE_DB_PATH="' + str(home / 'state custom.db') + '"\n')
    runtime = home / 'runtimes/release'; monkeypatch.setenv('PATH', '/usr/bin:/bin')
    unit = installer.render_unit(repo, home, runtime, secrets, 'http://127.0.0.1:5444')
    assert 'linxuhao' not in unit
    assert '@REPO@' not in unit
    workdir = next(line.split('=', 1)[1] for line in unit.splitlines() if line.startswith('WorkingDirectory='))
    assert workdir.replace('%%', '%') == str(repo)
    assert 'ExecStart=' + installer.unit_quote(repo / '.venv/bin/python') + ' -m core.dsh_host' in unit
    assignments = [shlex.split(line.split('=', 1)[1])[0].replace('%%', '%')
                   for line in unit.splitlines() if line.startswith('Environment=')]
    env = dict(value.split('=', 1) for value in assignments)
    assert env['HOME'] == str(home)
    assert env['DPE_DB_PATH'] == str(home / 'state custom.db')
    assert env['AITELIER_DSH_BIN'] == str(runtime / 'node_modules/.bin/dsh')
    assert shlex.split(env['GIT_CONFIG_VALUE_0']) == [str(repo / 'docker/git-credential-helper.sh')]
    assert env['AITELIER_DSH_API_URL'] == 'http://127.0.0.1:5444'
    assert 'KillMode=control-group' in unit
