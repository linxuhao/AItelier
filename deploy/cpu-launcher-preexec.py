#!/usr/bin/env python3
"""Decide the host CPU launcher's socket path before the unit starts it.

docs/repository-host-launcher.md states the rule: the launcher refuses any
already-existing socket path, including a live listener, and does not unlink a
peer's listener to replace it, because a stale path needs deliberate operator
diagnosis. This pre-start step performs exactly that diagnosis, mechanically:

  * nothing at the path              -> proceed, create nothing
  * connect() succeeds               -> a live listener owns it; refuse, touch nothing
  * connect() refused / path gone    -> the path is dead, unlink it and proceed
  * any other OSError                -> refuse without unlinking (ownership unknown)

It exists because an unclean stop leaves the socket file behind: the launcher
would then refuse to bind, so without this step the supervised unit could not
start after every unclean stop or reboot.

Usage: cpu-launcher-preexec.py [SOCKET_PATH]
Default socket path: $HOME/.AItelier/cpu-launcher.sock (the installer's default).
"""
import errno
import os
from pathlib import Path
import socket
import sys

DEFAULT_SOCKET = Path.home() / ".AItelier/cpu-launcher.sock"


def decide(path: Path) -> int:
    # The launcher refuses a path that exists OR is a symlink; lexists covers both.
    if not os.path.lexists(path):
        print(f"no socket path at {path}", flush=True)
        return 0
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(2.0)
        probe.connect(str(path))
    except OSError as exc:
        if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            print(f"cannot decide ownership of {path}: {exc}", file=sys.stderr, flush=True)
            return 1
        try:
            os.unlink(path)
        except OSError as unlink_exc:
            print(f"dead socket path {path} could not be removed: {unlink_exc}",
                  file=sys.stderr, flush=True)
            return 1
        print(f"removed dead socket path {path}", flush=True)
        return 0
    finally:
        probe.close()
    print(f"a live listener holds {path}; refusing to replace it", file=sys.stderr, flush=True)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(f"usage: {Path(argv[0]).name} [SOCKET_PATH]", file=sys.stderr)
        return 2
    return decide(Path(argv[1]) if len(argv) == 2 else DEFAULT_SOCKET)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
