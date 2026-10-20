"""Normal installer implementation; launcher-only mode never executes a launcher."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys


def read_regular(path, *, dir_fd=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Not a regular file: {path}")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read()
        after = os.fstat(fd)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mode,
                              s.st_uid, s.st_gid, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after):
            raise ValueError(f"File changed while reading: {path}")
        return raw, identity(after)
    finally:
        os.close(fd)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def replace_launcher(source, directory, expected_old, expected_source):
    desired, _ = read_regular(source / "gate_run.sh")
    if digest(desired) != expected_source:
        raise ValueError("Source launcher hash mismatch")
    old, identity = read_regular("gate_run.sh", dir_fd=directory)
    if digest(old) != expected_old:
        raise ValueError("Installed launcher hash mismatch")
    temporary = ".gate_run.sh." + secrets.token_hex(12) + ".pending"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(desired)
            current = os.fstat(stream.fileno())
            if (current.st_uid, current.st_gid) != identity[4:6]:
                os.fchown(stream.fileno(), identity[4], identity[5])
            os.fchmod(stream.fileno(), stat.S_IMODE(identity[3]))
            stream.flush()
            os.fsync(stream.fileno())
        if read_regular("gate_run.sh", dir_fd=directory) != (old, identity):
            raise ValueError("Installed launcher changed before replacement")
        os.replace(temporary, "gate_run.sh", src_dir_fd=directory, dst_dir_fd=directory)
        temporary = None
        os.fsync(directory)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    print(json.dumps(dict(old_sha256=expected_old, sha256=expected_source,
                          mode=stat.S_IMODE(identity[3]), uid=identity[4], gid=identity[5])))


def main():
    source = Path(sys.argv[1])
    args = sys.argv[2:]
    launcher_only = bool(args and args[0] == "--launcher-only")
    if launcher_only:
        if len(args) not in (3, 4) or (len(args) == 4 and not args[3]) or any(re.fullmatch(r"[0-9a-f]{64}", v) is None for v in args[1:3]):
            raise ValueError("Usage: install_gate_run.sh --launcher-only EXPECTED_OLD_SHA256 EXPECTED_SOURCE_SHA256 [DEST]")
        destination = Path(args[3]) if len(args) == 4 else Path.home() / ".AItelier/bin"
    else:
        if len(args) > 1 or (args and args[0].startswith("--")):
            raise ValueError("Usage: install_gate_run.sh [DEST]")
        destination = Path(args[0]) if args and args[0] else Path.home() / ".AItelier/bin"
        destination.mkdir(parents=True, exist_ok=True)
    directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        lock = os.open(".gate_run.install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(lock).st_mode):
                raise ValueError("Installer lock is not a regular file")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if launcher_only:
                replace_launcher(source, directory, args[1], args[2])
            else:
                for name in ("gate_run.sh", "gate_binding.py"):
                    subprocess.run(["install", "-m", "755", str(source / name), str(destination / name)], check=True)
                subprocess.run(["sha256sum", str(destination / "gate_run.sh"), str(destination / "gate_binding.py")], check=True)
        finally:
            os.close(lock)
    finally:
        os.close(directory)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f"Installer refused or failed: {error}")
