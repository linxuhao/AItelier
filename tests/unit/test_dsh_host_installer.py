"""Actual fresh/repeated filesystem installation without production data."""
import importlib.util
import json
import os
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
    for name in ('DPE_DB_PATH', 'AITELIER_SECRETS_DIR', 'AITELIER_HOME',
                 'AITELIER_UID', 'AITELIER_GID', 'AITELIER_STEWARD_SECRETS_HOST_DIR'):
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


def test_install_creates_the_bind_sources_and_names_the_container_user(paths, monkeypatch):
    """Overfit review 2026-11-24 (重-13, 重-14): every directory docker-compose.yml
    bind-mounts from HOME exists, as this user, before Docker can create it as
    root; and .env names this user's uid/gid for a bare `docker compose`. A
    second run changes nothing, and values already in .env are kept."""
    import os
    repo, home, secrets = paths
    monkeypatch.delenv('AITELIER_STEWARD_SECRETS_HOST_DIR', raising=False)
    installer.prepare(repo, home, secrets)
    for rel in ('.AItelier/godot-control', '.local/share/dsh-director/media',
                '.local/share/aitelier/media-staging', '.local/share/aitelier/steward-mcp-secrets'):
        directory = home / rel
        assert directory.is_dir(), rel
        assert directory.stat().st_uid == os.getuid(), rel
    env = (repo / '.env').read_text().splitlines()
    assert f'AITELIER_UID={os.getuid()}' in env and f'AITELIER_GID={os.getgid()}' in env
    before = (repo / '.env').read_bytes()
    assert not installer.prepare(repo, home, secrets)
    assert (repo / '.env').read_bytes() == before

    (repo / '.env').write_text('AITELIER_UID=4242\nAITELIER_DRIVER_IDENTITY=on\n')
    # Only AITELIER_GID is appended: identity was already on, so prepare() does not
    # report it (the uid/gid recreate note is container_user_note's, tested below).
    assert not installer.prepare(repo, home, secrets)
    env = (repo / '.env').read_text().splitlines()
    assert 'AITELIER_UID=4242' in env and f'AITELIER_UID={os.getuid()}' not in env
    assert f'AITELIER_GID={os.getgid()}' in env


def test_a_pdf_reader_install_failure_is_a_note_not_an_aborted_install(paths, monkeypatch, capsys):
    """Overfit fix round: requirements-pdf.txt pins the x86_64 wheel's hash only, so
    pip fails on aarch64/musl. The installer goes on (unit, runtime) and says so,
    as it does for a missing ffprobe."""
    import subprocess
    repo, _, _ = paths
    calls = []
    def failing_run(*args, **kwargs):
        calls.append([str(a) for a in args])
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(installer, 'run', failing_run)
    assert installer.install_pdf_reader(repo / '.venv/bin/python', repo) is False
    assert calls and calls[0][-2:] == ['-r', str(repo / 'requirements-pdf.txt')]
    assert 'pypdfium2' in capsys.readouterr().out
    monkeypatch.setattr(installer, 'run', lambda *a, **k: None)
    assert installer.install_pdf_reader(repo / '.venv/bin/python', repo) is True


def test_the_default_api_url_follows_aitelier_port(paths, monkeypatch):
    """Overfit fix round (次-5): compose publishes the backend on AITELIER_PORT, so the
    unit's AITELIER_DSH_API_URL defaults to the same port, 4444 when unset."""
    repo, _, _ = paths
    monkeypatch.delenv('AITELIER_PORT', raising=False)
    assert installer.default_api_url(repo) == 'http://127.0.0.1:4444'
    (repo / '.env').write_text('AITELIER_PORT=5555\n')
    assert installer.default_api_url(repo) == 'http://127.0.0.1:5555'


def test_a_container_user_other_than_the_compose_default_asks_for_a_backend_recreate(paths, monkeypatch):
    """Overfit fix round 2 (compose f4): docker-compose.yml interpolates
    `user: ${AITELIER_UID:-1000}:${AITELIER_GID:-1000}` only when it creates the
    container, so a backend created before .env named another user keeps running as
    1000:1000. main() says so whenever the effective user is not the compose
    default, and does not call that enabling driver identity."""
    repo, home, secrets = paths
    for name in ('AITELIER_UID', 'AITELIER_GID'):
        monkeypatch.delenv(name, raising=False)
    assert installer.container_user_note(repo) is None
    (repo / '.env').write_text('AITELIER_UID=1000\nAITELIER_GID=1000\n')
    assert installer.container_user_note(repo) is None
    # An empty value is compose's ${VAR:-1000}: still the default user, no note.
    (repo / '.env').write_text('AITELIER_UID=\nAITELIER_GID=\n')
    assert installer.container_user_note(repo) is None
    (repo / '.env').write_text('AITELIER_DRIVER_IDENTITY=on\nAITELIER_UID=4242\nAITELIER_GID=1000\n')
    note = installer.container_user_note(repo)
    assert '4242:1000' in note and 'recreate' in note and 'identity' not in note.lower()
    monkeypatch.setenv('AITELIER_GID', '77')
    assert '4242:77' in installer.container_user_note(repo)


def test_empty_uid_gid_assignments_are_filled_from_the_running_process(paths):
    """Compose's ${VAR:-1000} treats AITELIER_UID=/AITELIER_GID= as unset, so
    prepare() must write this process's real uid/gid instead of leaving an empty
    line that silently keeps the backend running as 1000:1000. A real value is
    left untouched."""
    repo, home, secrets = paths
    (repo / '.env').write_text('AITELIER_DRIVER_IDENTITY=on\nAITELIER_UID=\nAITELIER_GID=\n')
    installer.prepare(repo, home, secrets)
    lines = (repo / '.env').read_text().splitlines()
    assert 'AITELIER_UID=' not in lines and 'AITELIER_GID=' not in lines
    assert f'AITELIER_UID={os.getuid()}' in lines
    assert f'AITELIER_GID={os.getgid()}' in lines
    assert installer.setting(repo, 'AITELIER_UID', 'unset') == str(os.getuid())
    assert installer.setting(repo, 'AITELIER_GID', 'unset') == str(os.getgid())

    (repo / '.env').write_text('AITELIER_DRIVER_IDENTITY=on\nAITELIER_UID=4321\nAITELIER_GID=\n')
    installer.prepare(repo, home, secrets)
    lines = (repo / '.env').read_text().splitlines()
    assert 'AITELIER_UID=4321' in lines
    assert f'AITELIER_UID={os.getuid()}' not in lines
    assert 'AITELIER_GID=' not in lines
    assert f'AITELIER_GID={os.getgid()}' in lines
    assert installer.setting(repo, 'AITELIER_UID', 'unset') == '4321'


def test_identity_report_says_nothing_for_empty_values_but_names_real_ones(paths):
    """The lines main() prints come from identity_notes(): an empty assignment
    resolves to compose's default and must not produce the false sentence, while
    AITELIER_UID=4321 must still be named in the report."""
    repo, _, _ = paths
    (repo / '.env').write_text('AITELIER_UID=\nAITELIER_GID=\n')
    report = installer.identity_notes(repo)
    assert report == []
    assert not any('the containers run as the .env value' in line for line in report)
    assert not any('AITELIER_UID=' in line for line in report)

    (repo / '.env').write_text('AITELIER_UID=4321\nAITELIER_GID=\n')
    report = installer.identity_notes(repo)
    assert any('AITELIER_UID=4321' in line for line in report)
    assert not any('AITELIER_GID=' in line for line in report)


def test_empty_path_settings_fall_back_to_the_host_defaults(paths):
    """The same empty-means-unset rule also stops DPE_DB_PATH and
    AITELIER_STEWARD_SECRETS_HOST_DIR from resolving to the current directory."""
    repo, home, secrets = paths
    (repo / '.env').write_text('DPE_DB_PATH=\nAITELIER_STEWARD_SECRETS_HOST_DIR=\n')
    assert installer.setting(repo, 'DPE_DB_PATH', 'default db') == 'default db'
    assert installer.setting(repo, 'AITELIER_STEWARD_SECRETS_HOST_DIR', 'default secrets') == 'default secrets'
    installer.prepare(repo, home, secrets)
    assert (home / '.local/share/aitelier/steward-mcp-secrets').is_dir()
    unit = installer.render_unit(repo, home, home / 'runtimes/release', secrets,
                                 'http://127.0.0.1:5444')
    assignments = [shlex.split(line.split('=', 1)[1])[0].replace('%%', '%')
                   for line in unit.splitlines() if line.startswith('Environment=')]
    env = dict(value.split('=', 1) for value in assignments)
    assert env['DPE_DB_PATH'] == str(home / '.AItelier/aitelier.db')
