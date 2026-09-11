"""Container test harness: reap orphan grandchildren like a functioning init.

Only this process becomes a Linux subreaper. No host settings or production
processes are changed. The pytest child and all tested code are unmodified.
"""
import ctypes
import os
import sys

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
    raise OSError(ctypes.get_errno(), 'Cannot enable test-only subreaper')
pid = os.fork()
if pid == 0:
    os.execv(sys.executable, [sys.executable, '-m', 'pytest', *sys.argv[1:]])
while True:
    child, status = os.waitpid(-1, 0)
    if child == pid:
        exit_code = os.waitstatus_to_exitcode(status)
        break
# Reap any already-exited test-only orphans without killing other processes.
while True:
    try:
        child, _ = os.waitpid(-1, os.WNOHANG)
        if not child:
            break
    except ChildProcessError:
        break
raise SystemExit(exit_code if exit_code >= 0 else 128 - exit_code)
