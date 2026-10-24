"""Real owned-temp filesystem checks; execution belongs to the director."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading

import pytest

from core import repository_host_config as installer
from core.repository_host_launcher import LaunchRefused, load_config


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def inputs(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    database = home / "state.db"
    database.write_bytes(b"untouched existing State storage")
    entry = tmp_path / "entry.py"
    entry.write_bytes(b"# accepted entry Source\n")
    config = tmp_path / "cpu.json"
    config.write_text(json.dumps({
        "allowed_roots": [str(tmp_path)], "aitelier_home": str(home),
        "state_db": str(database), "test_image": "sha256:" + "a" * 64,
        "executor_entry": str(entry), "allowed_uids": [os.getuid()],
        "coding_shell_entry_sha256": "b" * 64,
        "operator_extension": {"nested": [True, 17, "é"], "keep": None},
    }))
    config.chmod(0o640)
    (tmp_path / "neighbor").write_bytes(b"leave me alone\n")
    return config, entry


def _install(inputs, **overrides):
    config, entry = inputs
    args = {"config_path": config, "expected_config_sha256": _sha(config),
            "entry_path": entry, "expected_entry_sha256": _sha(entry)}
    args.update(overrides)
    return installer.install_coding_entry_pin(**args)


def test_install_preserves_fields_metadata_and_neighbors_and_fsyncs(inputs, monkeypatch):
    config, entry = inputs
    old = load_config(config)
    metadata = config.stat()
    neighbor = config.parent / "neighbor"
    untouched = {path: path.read_bytes() for path in (entry, neighbor, Path(old["state_db"]))}
    names = set(config.parent.iterdir())
    synced = []
    replaced = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        synced.append(stat.S_IFMT(os.fstat(fd).st_mode))
        return real_fsync(fd)

    def replace(source, destination, **kwargs):
        assert kwargs["src_dir_fd"] == kwargs["dst_dir_fd"]
        assert config.read_bytes() == original
        real_replace(source, destination, **kwargs)
        replaced.append(load_config(config))

    monkeypatch.setattr(installer.os, "fsync", fsync)
    monkeypatch.setattr(installer.os, "replace", replace)
    original = config.read_bytes()
    digest = _install(inputs)
    expected = {**old, "coding_shell_entry_sha256": _sha(entry)}
    assert load_config(config) == expected
    assert replaced == [expected]
    assert digest == _sha(config)
    after = config.stat()
    assert (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) == (
        metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
    assert after.st_ino != metadata.st_ino
    assert synced == [stat.S_IFREG, stat.S_IFDIR]
    assert set(config.parent.iterdir()) == names
    assert {path: path.read_bytes() for path in untouched} == untouched


@pytest.mark.parametrize("override", [
    {"expected_config_sha256": "0" * 64}, {"expected_entry_sha256": "0" * 64},
    {"expected_config_sha256": "not a digest"}, {"expected_entry_sha256": None},
])
def test_hash_refusals_have_no_target_or_neighbor_effects(inputs, override):
    config, _ = inputs
    before = {p: p.read_bytes() for p in config.parent.iterdir() if p.is_file()}
    names = set(config.parent.iterdir())
    with pytest.raises(LaunchRefused):
        _install(inputs, **override)
    assert set(config.parent.iterdir()) == names
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("raw", [b"{bad json", b"[]", b'{"allowed_roots": []}'])
def test_invalid_config_is_refused_before_replacement(inputs, raw):
    config, _ = inputs
    config.write_bytes(raw)
    names = set(config.parent.iterdir())
    with pytest.raises(ValueError):
        _install(inputs)
    assert config.read_bytes() == raw
    assert set(config.parent.iterdir()) == names


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_nonregular_and_symlink_inputs_are_refused(inputs, which, kind):
    config, entry = inputs
    old_hashes = (_sha(config), _sha(entry))
    path = inputs[which]
    backup = path.with_suffix(".backup")
    path.rename(backup)
    if kind == "symlink":
        path.symlink_to(backup)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    before = backup.read_bytes()
    config_before = config.read_bytes() if which == 1 else None
    names = set(config.parent.iterdir())
    with pytest.raises((OSError, LaunchRefused)):
        installer.install_coding_entry_pin(config, old_hashes[0], entry, old_hashes[1])
    assert backup.read_bytes() == before
    assert set(config.parent.iterdir()) == names
    if which == 1:
        assert config.read_bytes() == config_before


def test_symlink_parent_and_unconfigured_entry_are_refused(inputs):
    config, entry = inputs
    alias = config.parent / "alias"
    alias.symlink_to(config.parent, target_is_directory=True)
    original = config.read_bytes()
    with pytest.raises(OSError):
        _install(inputs, config_path=alias / config.name)
    other = config.parent / "other-entry.py"
    other.write_bytes(entry.read_bytes())
    with pytest.raises(LaunchRefused, match="configured executor"):
        _install(inputs, entry_path=other)
    assert config.read_bytes() == original


@pytest.mark.parametrize("which", [0, 1])
@pytest.mark.parametrize("change", ["inplace", "replace", "symlink", "metadata"])
def test_actual_late_input_changes_are_not_overwritten(inputs, monkeypatch, which, change):
    config, _ = inputs
    target = inputs[which]
    original_config = config.read_bytes()
    real_fsync = os.fsync
    observed = []

    def fsync(fd):
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode) and not observed:
            if change == "inplace":
                target.write_bytes(b"foreign late content\n")
            elif change == "replace":
                replacement = target.with_suffix(".foreign")
                replacement.write_bytes(target.read_bytes())
                os.replace(replacement, target)
            elif change == "symlink":
                backup = target.with_suffix(".foreign")
                target.rename(backup)
                target.symlink_to(backup)
            else:
                target.chmod(0o600 if stat.S_IMODE(target.stat().st_mode) != 0o600 else 0o640)
            observed.append((target.lstat(), target.read_bytes()))

    monkeypatch.setattr(installer.os, "fsync", fsync)
    with pytest.raises((OSError, LaunchRefused)):
        _install(inputs)
    assert observed
    info, raw = observed[0]
    after = target.lstat()
    assert (after.st_dev, after.st_ino, after.st_mode) == (info.st_dev, info.st_ino, info.st_mode)
    assert target.read_bytes() == raw
    assert not list(config.parent.glob(".*.pending"))
    if which == 1:
        assert config.read_bytes() == original_config


def test_participating_installers_contend_on_real_directory_lock(inputs, monkeypatch):
    config, entry = inputs
    old_hash, entry_hash = _sha(config), _sha(entry)
    reached, release = threading.Event(), threading.Event()
    real_fsync = os.fsync
    outcome = []

    def fsync(fd):
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            reached.set()
            if not release.wait(10):
                raise RuntimeError("test did not release the first installer")

    def first():
        try:
            outcome.append(installer.install_coding_entry_pin(config, old_hash, entry, entry_hash))
        except BaseException as exc:
            outcome.append(exc)

    monkeypatch.setattr(installer.os, "fsync", fsync)
    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert reached.wait(10)
        with pytest.raises(LaunchRefused, match="participating"):
            installer.install_coding_entry_pin(config, old_hash, entry, entry_hash)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive()
    assert outcome == [_sha(config)]
    with pytest.raises(LaunchRefused, match="expected old"):
        installer.install_coding_entry_pin(config, old_hash, entry, entry_hash)


def test_pending_name_collision_does_not_delete_neighbor(inputs, monkeypatch):
    config, _ = inputs
    monkeypatch.setattr(installer.secrets, "token_hex", lambda _: "fixed")
    collision = config.parent / ("." + config.name + ".fixed.pending")
    collision.write_bytes(b"preexisting neighbor")
    original = config.read_bytes()
    with pytest.raises(FileExistsError):
        _install(inputs)
    assert collision.read_bytes() == b"preexisting neighbor"
    assert config.read_bytes() == original


def test_normal_module_cli_and_existing_serve_cli(inputs):
    config, entry = inputs
    result = subprocess.run([
        sys.executable, "-m", "core.repository_host_config", "--config", str(config),
        "--expected-config-sha256", _sha(config), "--entry", str(entry),
        "--expected-entry-sha256", _sha(entry),
    ], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == {"config_sha256": _sha(config),
                                       "coding_shell_entry_sha256": _sha(entry)}
    assert load_config(config)["coding_shell_entry_sha256"] == _sha(entry)
    help_result = subprocess.run([sys.executable, "-m", "core.repository_host_launcher", "--help"],
                                 text=True, capture_output=True, check=True)
    assert "--socket" in help_result.stdout and "--config" in help_result.stdout
