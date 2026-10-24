"""Normal operator installer for the host CPU config's coding-entry pin.

The operator must hold normal admission/all CPU slots, establish an idle
launcher, and exclude Source consumers and foreign writers before calling.
The directory flock serializes participating installers only; the final read
and rename are not a compare-and-swap against unlocked foreign writers.
This installs config bytes only, without restarting or admitting any service.
"""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

from core.repository_host_launcher import LaunchRefused, _validate_config


@contextmanager
def _parent(path):
    """Pin the directory, refusing symlinks in every path component."""
    path = Path(path).absolute()
    with ExitStack() as stack:
        directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, directory)
        for part in path.parts[1:-1]:
            directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
            stack.callback(os.close, directory)
        yield directory, path.name


def _read_regular(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise LaunchRefused("pin inputs must be regular non-symlink files")
        raw = stream.read()
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mode,
                              s.st_uid, s.st_gid, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after):
            raise LaunchRefused("pin input changed while reading")
        return raw, identity(after)


def install_coding_entry_pin(config_path, expected_config_sha256,
                             entry_path, expected_entry_sha256):
    """Replace only the JSON pin; return the installed config's SHA256.

    Both expected digests describe exact input bytes. Failures before rename
    leave the target untouched; a failed parent fsync after rename reports an
    error even though replacement has occurred. Other JSON fields retain their
    values, while whitespace is normalized. Existing config validation applies.
    """
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in (expected_config_sha256, expected_entry_sha256)):
        raise LaunchRefused("expected hashes must be lowercase SHA256 digests")
    with _parent(config_path) as (directory, name), _parent(entry_path) as (source, entry):
        try:
            fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LaunchRefused("another participating pin installer holds this directory") from exc
        original = _read_regular(directory, name)
        accepted = _read_regular(source, entry)
        if hashlib.sha256(original[0]).hexdigest() != expected_config_sha256:
            raise LaunchRefused("config differs from expected old bytes")
        if hashlib.sha256(accepted[0]).hexdigest() != expected_entry_sha256:
            raise LaunchRefused("entry differs from accepted Source bytes")
        cfg = json.loads(original[0])
        _validate_config(cfg)
        if (os.path.abspath(cfg["executor_entry"]) != os.path.abspath(entry_path)
                or original[1][:2] == accepted[1][:2]):
            raise LaunchRefused("entry must be the configured executor, separate from config")
        cfg["coding_shell_entry_sha256"] = expected_entry_sha256
        desired = (json.dumps(cfg, indent=2) + "\n").encode()
        metadata = original[1]
        temporary = "." + name + "." + secrets.token_hex(12) + ".pending"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(desired)
                current = os.fstat(fd)
                if (current.st_uid, current.st_gid) != metadata[4:6]:
                    os.fchown(fd, metadata[4], metadata[5])
                os.fchmod(fd, stat.S_IMODE(metadata[3]))
                stream.flush()
                os.fsync(fd)
            if (_read_regular(directory, name) != original
                    or _read_regular(source, entry) != accepted):
                raise LaunchRefused("config or accepted entry changed before replacement")
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            temporary = None
            os.fsync(directory)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return hashlib.sha256(desired).hexdigest()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--entry", required=True, type=Path)
    parser.add_argument("--expected-entry-sha256", required=True)
    args = parser.parse_args()
    try:
        result = install_coding_entry_pin(args.config, args.expected_config_sha256,
                                          args.entry, args.expected_entry_sha256)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Pin installation refused or failed: {exc}\n")
    print(json.dumps({"config_sha256": result,
                      "coding_shell_entry_sha256": args.expected_entry_sha256}))


if __name__ == "__main__":
    main()
