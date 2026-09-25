"""Use up this uid's inotify instances, then run a command while holding them.

Opens inotify instances with the real `inotify_init1` until the kernel refuses
one, prints how many were held and the errno of the refusal, runs the command
with AITELIER_TABLE_INOTIFY_EXHAUSTED=1, and closes every instance afterwards.
Run it as a uid nothing else on the host uses: the limit
(`fs.inotify.max_user_instances`) is per uid, host-wide.
"""
import ctypes
import os
import subprocess
import sys

libc = ctypes.CDLL(None, use_errno=True)
held = []
err = 0
while len(held) < 100000:
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0:
        err = ctypes.get_errno()
        break
    held.append(fd)
with open("/proc/sys/fs/inotify/max_user_instances") as fh:
    limit = fh.read().strip()
print(f"EXHAUSTED uid={os.getuid()} inotify instances held by this probe="
      f"{len(held)} errno={err} ({os.strerror(err) if err else '-'}) "
      f"max_user_instances={limit}", flush=True)
rc = subprocess.call(sys.argv[1:], env=dict(
    os.environ, AITELIER_TABLE_INOTIFY_EXHAUSTED="1"))
for fd in held:
    os.close(fd)
print(f"RELEASED {len(held)} instances; command rc={rc}", flush=True)
sys.exit(rc)
