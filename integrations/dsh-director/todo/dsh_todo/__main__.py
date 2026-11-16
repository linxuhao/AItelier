"""`python -m dsh_todo <command>`: the same entry point as `python -m dsh_todo.cli`."""
import signal
import sys

from dsh_todo.cli import main

if __name__ == "__main__":
    # A terminated CLI must unwind (so a running predicate command's process group is killed),
    # not vanish and leave a test or docker child behind. Guarded: importing this module (a
    # package walk, a test) must not run a command.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    sys.exit(main())
