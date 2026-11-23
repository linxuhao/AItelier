"""The backend container sees the media library read-only and the staging dir read-write.

Step 24.2b (design-steward 7.8.12a, "显示 / 文件怎么找", "暂存", "隐私", "要改的地方"):

  * the DSH hosts' media root (core/dsh_driver.media_root()) is mounted into the
    `aitelier` service `:ro`, so the conversation media route can serve a stored
    attachment but never change the library its hosts write;
  * the dashboard uploads' staging directory (core/media_library.staging_dir())
    is mounted into the `aitelier` service read-write, since the upload route
    writes it and the fetch/ack paths delete from it;
  * both at the IDENTICAL absolute path, because the container's HOME is the
    host's HOME (compose `HOME: ${HOME}`), so the two helpers resolve unchanged
    inside the container and no extra env is needed;
  * no other service in this compose file mounts either path, or a directory
    above or below it (aitelier-zg, the godot sidecar, ...). dev_mcp lives in
    another compose project and is out of this file's reach.

The test reads the repository's real docker-compose.yml, resolves compose's
`${VAR}` / `${VAR:-default}` interpolation with a fake HOME and nothing else
set (so every override falls back to its default), and compares the volume
entries to what the two helpers answer under that same HOME.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest
import yaml

from core import dsh_driver, media_library

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
BACKEND = "aitelier"
FAKE_HOME = "/home/compose-media-tester"

# Innermost `${NAME}` or `${NAME:-default}` (default holds no nested `${`).
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^${}]*))?\}")


def _interpolate(text: str, env: dict[str, str]) -> str:
    while True:
        new = _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), text)
        if new == text:
            return new
        text = new


def _mounts(service: dict) -> list[tuple[str, str, bool]]:
    """(source, target, read_only) for each volume entry, interpolated."""
    env = {"HOME": FAKE_HOME}
    out = []
    for entry in service.get("volumes") or []:
        if isinstance(entry, dict):
            source = _interpolate(str(entry.get("source", "")), env)
            target = _interpolate(str(entry.get("target", "")), env)
            out.append((source, target, bool(entry.get("read_only"))))
            continue
        parts = _interpolate(str(entry), env).split(":")
        source, target = parts[0], parts[1] if len(parts) > 1 else parts[0]
        mode = parts[2] if len(parts) > 2 else ""
        out.append((source, target, "ro" in mode.split(",")))
    return out


def _services() -> dict:
    data = yaml.safe_load(COMPOSE_PATH.read_text())
    assert isinstance(data, dict) and BACKEND in (data.get("services") or {})
    return data["services"]


@pytest.fixture()
def paths(monkeypatch):
    monkeypatch.setenv("HOME", FAKE_HOME)
    monkeypatch.delenv("AITELIER_MEDIA_ROOT", raising=False)
    media = dsh_driver.media_root()
    staging = str(media_library.staging_dir())
    assert media == f"{FAKE_HOME}/.local/share/dsh-director/media"
    assert staging == f"{FAKE_HOME}/.local/share/aitelier/media-staging"
    return media, staging


def _related(a: str, b: str) -> bool:
    """a and b are the same path, or one contains the other."""
    pa, pb = PurePosixPath(a), PurePosixPath(b)
    return pa == pb or pa in pb.parents or pb in pa.parents


def test_backend_home_is_the_hosts_home():
    # The identical-path mounts rely on this: Path.home() in the container is the host's.
    assert _services()[BACKEND]["environment"]["HOME"] == "${HOME}"


def test_backend_mounts_the_media_root_read_only_at_the_same_path(paths):
    media, _ = paths
    found = [m for m in _mounts(_services()[BACKEND]) if m[0] == media]
    assert found == [(media, media, True)], found


def test_backend_mounts_staging_read_write_at_the_same_path(paths):
    _, staging = paths
    found = [m for m in _mounts(_services()[BACKEND]) if m[0] == staging]
    assert found == [(staging, staging, False)], found


def test_no_backend_mount_targets_either_path_from_elsewhere(paths):
    # Nothing else in the backend lands on (or over, or under) the two paths, so
    # the read-only library cannot be shadowed by a writable mount.
    media, staging = paths
    for source, target, _ in _mounts(_services()[BACKEND]):
        if (source, target) in ((media, media), (staging, staging)):
            continue
        for path in (media, staging):
            assert not _related(target, path), (source, target, path)


def test_no_other_service_mounts_either_path(paths):
    media, staging = paths
    for name, service in _services().items():
        if name == BACKEND:
            continue
        for source, target, _ in _mounts(service):
            for path in (media, staging):
                for side in (source, target):
                    if side.startswith("/"):
                        assert not _related(side, path), (name, source, target, path)


def test_the_cli_creates_both_mount_sources_as_the_user(tmp_path, monkeypatch):
    # Docker creates a missing bind source as root; the CLI start creates them first
    # (cli/server._ensure_host_dirs), at the paths the helpers resolve, mode 0700.
    import cli.server as srv
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AITELIER_MEDIA_ROOT", raising=False)
    monkeypatch.setenv("AITELIER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AITELIER_SECRETS_DIR", str(tmp_path / "secrets"))
    srv._ensure_host_dirs()
    for path in (Path(dsh_driver.media_root()), media_library.staging_dir()):
        assert path.is_dir() and str(path).startswith(str(home)), path
        assert path.stat().st_mode & 0o777 == 0o700, oct(path.stat().st_mode)
