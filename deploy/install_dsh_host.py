#!/usr/bin/env python3
"""Install host DSH from this checkout; no running AItelier container required."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import tempfile


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def setting(repo, name, default):
    if name in os.environ:
        return os.environ[name]
    path = repo / '.env'
    for line in reversed(path.read_text().splitlines() if path.exists() else []):
        match = re.match(r'\s*(?:export\s+)?' + re.escape(name) + r'\s*=\s*(.*)', line)
        if match:
            return ' '.join(shlex.split(match[1], comments=True))
    return default


def atomic_text(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return False
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def count_rows(db, table, where='1'):
    if not db.exists():
        return 0
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
        if not conn.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone():
            return 0
        return conn.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}').fetchone()[0]


def prepare(repo, home, secrets_dir):
    """Preserve identities and running controllers before changing install mode."""
    state = home / '.AItelier'
    db = Path(setting(repo, 'DPE_DB_PATH', str(state / 'aitelier.db'))).expanduser().resolve()
    config = state / 'dsh-drivers/runtime.json'
    host = config.exists() and json.loads(config.read_text()).get('mode') == 'host'
    if not host and count_rows(db, 'dsh_drivers', 'enabled=1'):
        raise ValueError('Stop the enabled container drivers in Dashboard before installing host mode; their sessions will be retained.')
    pepper = secrets_dir / 'AITELIER_DRIVER_TOKEN_PEPPER'
    if not pepper.exists() and count_rows(db, 'drivers', 'token_hash IS NOT NULL'):
        raise ValueError('Existing driver identities need their original AITELIER_DRIVER_TOKEN_PEPPER. Restore it; installation will not rotate credentials.')
    if pepper.exists() and len(pepper.read_text().strip()) < 32:
        raise ValueError('Existing AITELIER_DRIVER_TOKEN_PEPPER is too short; restore the original secret.')
    for directory in (state, state / 'dsh-drivers', secrets_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not pepper.exists():
        fd = os.open(pepper, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(48) + '\n')
    pepper.chmod(0o600)
    env = repo / '.env'
    original = env.read_text() if env.exists() else (repo / '.env.example').read_text()
    lines = original.splitlines()
    found = False
    for index, line in enumerate(lines):
        if re.match(r'\s*(?:export\s+)?AITELIER_DRIVER_IDENTITY\s*=', line):
            lines[index] = 'AITELIER_DRIVER_IDENTITY=on'
            found = True
    if not found:
        lines.append('AITELIER_DRIVER_IDENTITY=on')
    changed = atomic_text(env, '\n'.join(lines) + '\n')
    values = json.loads(config.read_text()) if config.exists() else {}
    values['mode'] = 'host'
    atomic_text(config, json.dumps(values) + '\n')
    return changed


def install_runtime(repo, destination):
    """Use the same Dockerfile stage and npm lockfile as backend builds."""
    lock = (repo / 'integrations/dsh-driver/package-lock.json').read_bytes()
    fingerprint = hashlib.sha256(lock + (repo / 'Dockerfile').read_bytes()).hexdigest()[:20]
    release = destination / 'releases' / fingerprint
    expected = json.loads((repo / 'integrations/dsh-driver/package.json').read_text())['dependencies']['@deepseek-ai/dsh']
    if not release.exists():
        release.parent.mkdir(parents=True, exist_ok=True)
        image = 'aitelier-dsh-runtime:' + fingerprint
        run('docker', 'build', '--target', 'dsh-runtime', '-t', image, str(repo))
        container = run('docker', 'create', '--entrypoint', '/bin/true', image,
                        capture_output=True, text=True).stdout.strip()
        try:
            with tempfile.TemporaryDirectory(dir=release.parent, prefix='.install-') as temporary:
                staging = Path(temporary) / 'runtime'
                (staging / 'bin').mkdir(parents=True)
                run('docker', 'cp', container + ':/usr/local/bin/node', staging / 'bin/node')
                run('docker', 'cp', container + ':/opt/aitelier-dsh/node_modules', staging / 'node_modules')
                (staging / 'package-lock.json').write_bytes(lock)
                verify_runtime(staging, expected)
                staging.rename(release)
        finally:
            run('docker', 'rm', container, stdout=subprocess.DEVNULL)
    verify_runtime(release, expected)
    return release


def verify_runtime(release, expected):
    node = release / 'bin/node'
    version = run(node, '--version', capture_output=True, text=True).stdout.strip()
    if not version.startswith('v24.'):
        raise ValueError('Host driver requires the Node 24 runtime from the Dockerfile')
    manifest = release / 'node_modules/@deepseek-ai/dsh/package.json'
    if json.loads(manifest.read_text())['version'] != expected:
        raise ValueError('Installed DSH version does not match the committed package manifest')
    if not (release / 'node_modules/.bin/dsh').is_file():
        raise ValueError('Installed DSH executable is missing')


def unit_quote(value):
    value = str(value)
    if '\n' in value or '\r' in value:
        raise ValueError('Service paths/settings must not contain newlines')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render_unit(repo, home, runtime, secrets_dir, api_url):
    values = {
        'REPO': repo, 'PYTHON': repo / '.venv/bin/python', 'HOME_ENV': 'HOME=' + str(home),
        'PATH_ENV': 'PATH=' + ':'.join([str(runtime / 'bin'), str(home / '.npm-global/bin'),
            str(home / '.local/bin'), os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')]),
        'DSH_BIN_ENV': 'AITELIER_DSH_BIN=' + str(runtime / 'node_modules/.bin/dsh'),
        'STATE_ENV': 'AITELIER_HOME=' + str(home / '.AItelier'),
        'DB_ENV': 'DPE_DB_PATH=' + str(Path(setting(repo, 'DPE_DB_PATH', str(home / '.AItelier/aitelier.db'))).expanduser().resolve()),
        'SECRETS_ENV': 'AITELIER_SECRETS_DIR=' + str(secrets_dir),
        'API_ENV': 'AITELIER_DSH_API_URL=' + api_url,
        'GIT_HELPER_ENV': 'GIT_CONFIG_VALUE_0=' + shlex.quote(str(repo / 'docker/git-credential-helper.sh')),
        'GITHUB_TOKEN_ENV': 'AITELIER_GITHUB_TOKEN_FILE=' + str(secrets_dir / 'GITHUB_TOKEN'),
    }
    template = (repo / 'deploy/aitelier-dsh-host.service').read_text()
    def substitute(match):
        value = values[match[1]]
        quoted = unit_quote(value)
        # WorkingDirectory consumes a single path, not shell-style words.
        # Quotes there become literal filename bytes; inner spaces are valid.
        return str(value).replace('%', '%%') if match[1] == 'REPO' else quoted
    return re.sub(r'@([A-Z_]+)@', substitute, template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--runtime-dir', type=Path, help='Default: ~/.local/share/aitelier-dsh')
    parser.add_argument('--api-url', default='http://127.0.0.1:4444')
    parser.add_argument('--service-name', default='aitelier-dsh-host.service')
    parser.add_argument('--no-start', action='store_true', help='Prepare files without enabling/starting the service (initial install before backend startup)')
    args = parser.parse_args()
    if sys.version_info < (3, 12) or sys.platform != 'linux':
        parser.error('Requires Linux with Python 3.12+, Docker and a systemd user session')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+\.service', args.service_name):
        parser.error('--service-name must be a plain .service name')
    repo, home = args.repo.expanduser().resolve(), Path.home().resolve()
    secrets_dir = Path(os.path.expandvars(setting(repo, 'AITELIER_SECRETS_DIR', str(home / '.aitelier-secrets')))).expanduser().resolve()
    if secrets_dir == Path('/run/aitelier-secrets'):
        parser.error('AITELIER_SECRETS_DIR must be the host directory, not /run/aitelier-secrets')
    if setting(repo, 'AITELIER_HOME', str(home / '.AItelier')) != str(home / '.AItelier'):
        parser.error('This installer follows docker-compose.yml: AITELIER_HOME must be $HOME/.AItelier')
    run('docker', 'info', stdout=subprocess.DEVNULL)
    if not args.no_start:
        run('systemctl', '--user', 'show-environment', stdout=subprocess.DEVNULL)
    changed = prepare(repo, home, secrets_dir)
    python = repo / '.venv/bin/python'
    has_pip = python.exists() and subprocess.run([str(python), '-m', 'pip', '--version'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not has_pip:
        if importlib.util.find_spec('ensurepip') is None:
            raise ValueError('This Python has no ensurepip. Install its venv package (Ubuntu/Debian: sudo apt install python3.12-venv), then rerun; an incomplete .venv will be repaired.')
        run(sys.executable, '-m', 'venv', repo / '.venv')
    run(python, '-m', 'pip', 'install', '-e', repo)
    runtime = install_runtime(repo, (args.runtime_dir or home / '.local/share/aitelier-dsh').expanduser().resolve())
    unit = Path(os.environ.get('XDG_CONFIG_HOME', str(home / '.config'))) / 'systemd/user' / args.service_name
    unit_changed = atomic_text(unit, render_unit(repo, home, runtime, secrets_dir, args.api_url), mode=0o644)
    if not args.no_start:
        was_running = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', args.service_name]).returncode == 0
        run('systemctl', '--user', 'daemon-reload')
        run('systemctl', '--user', 'enable', '--now', unit)
        run('systemctl', '--user', 'is-active', args.service_name)
        if was_running and unit_changed:
            print('Existing driver processes were preserved. Apply the updated unit when ready:')
            print('systemctl --user restart ' + args.service_name)
    print('Installed Node 24 / DSH ' + json.loads((repo / 'integrations/dsh-driver/package.json').read_text())['dependencies']['@deepseek-ai/dsh'])
    print('Service: ' + str(unit))
    if changed:
        print('Driver identity enabled in .env. Start/recreate the backend with this configuration before starting project drivers.')
    print('Backend lifecycle remains separate; this installer does not restart Docker services.')
    print('For boot startup without login: loginctl enable-linger "$USER"')
    if args.no_start:
        print('After starting the Docker backend: systemctl --user enable --now ' + shlex.quote(str(unit)))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit('DSH host installation failed: ' + str(error))
