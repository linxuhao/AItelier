"""The calculate sandbox's fixed wrapper (step 24.4b, design-steward 7.8.24 "十" 2).

The image's entrypoint. The model's program is /job/in/main.py (the host wrote it there; it is
never on a command line), its input files /job/in/f1.<ext>, f2.<ext>, ... in the order given.
This runs it as a child, `python -I -B /job/in/main.py` with the working directory /job/out,
and then writes ONE JSON frame to its own stdout:

    {"v": 1, "exit_code": int, "timed_out": bool, "stdout": str, "stdout_truncated": bool,
     "stderr_tail": str, "files": [{"name": str, "data": base64}], "skipped": int}

- stdout is read as it comes: the first STDOUT_BYTES are kept, the rest read and dropped
  (stdout_truncated); stderr keeps its last STDERR_BYTES.
- After CHILD_SECONDS the child's whole process group is killed (timed_out; exit_code -9).
- The files are the regular files directly in /job/out (lstat: a symlink, a FIFO, a device or
  a directory is never opened), by name, at most MAX_FILES of at most FILE_BYTES each; any
  other entry is counted in `skipped`.

The host trusts nothing in the frame: every bound is enforced again on its side (the model's
program runs as the same uid and could write a frame of its own).
"""
import base64
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading

IN_DIR = "/job/in"
OUT_DIR = "/job/out"
STDOUT_BYTES = 16 * 1024
STDERR_BYTES = 2 * 1024
CHILD_SECONDS = 25
MAX_FILES = 4
FILE_BYTES = 10 * 1024 * 1024
# matplotlib's font cache, made when the image was built; copied into the writable /tmp so
# the first plot does not rebuild it (MPLCONFIGDIR is /tmp/matplotlib, the image's ENV).
MPL_CACHE = "/opt/calc/mpl"


def _keep_head(stream, limit, out):
    """Read `stream` to its end, keeping its first `limit` bytes in out["data"]. `out` is
    updated as each chunk is read, so a join that times out still has what was read."""
    kept = b""
    seen = 0
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            break
        seen += len(chunk)
        if len(kept) < limit:
            kept += chunk[: limit - len(kept)]
            out["data"] = kept
        out["truncated"] = seen > limit


def _keep_tail(stream, limit, out):
    """Read `stream` to its end, keeping its last `limit` bytes in out["data"] (updated as each
    chunk is read)."""
    kept = b""
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            break
        kept = (kept + chunk)[-limit:]
        out["data"] = kept


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_child(main, cwd, seconds=CHILD_SECONDS, stdout_bytes=STDOUT_BYTES, stderr_bytes=STDERR_BYTES):
    """Run `python -I -B main` in `cwd`: (exit_code, timed_out, stdout, stdout_truncated, stderr_tail)."""
    proc = subprocess.Popen([sys.executable, "-I", "-B", main], cwd=cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    out, err = {}, {}
    readers = [threading.Thread(target=_keep_head, args=(proc.stdout, stdout_bytes, out), daemon=True),
               threading.Thread(target=_keep_tail, args=(proc.stderr, stderr_bytes, err), daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        code = proc.wait()
    # A grandchild still holding the pipes open is killed with its group.
    _kill_group(proc)
    for reader in readers:
        reader.join(timeout=5)
    return (code, timed_out, out.get("data", b""), out.get("truncated", False), err.get("data", b""))


def _read_regular(path, limit):
    """The bytes of a regular file at most `limit` long, or None (never follows a link)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        return data if len(data) <= limit else None
    finally:
        os.close(fd)


def collect(out_dir, max_files=MAX_FILES, file_bytes=FILE_BYTES):
    """The regular files directly in `out_dir`, by name: ([(name, bytes)], skipped)."""
    files, skipped = [], 0
    try:
        names = sorted(os.listdir(out_dir))
    except OSError:
        return files, skipped
    for name in names:
        path = os.path.join(out_dir, name)
        try:
            info = os.lstat(path)
        except OSError:
            skipped += 1
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > file_bytes or len(files) >= max_files:
            skipped += 1
            continue
        data = _read_regular(path, file_bytes)
        if data is None:
            skipped += 1
            continue
        files.append((name, data))
    return files, skipped


def frame(code, timed_out, stdout, truncated, stderr, files, skipped):
    """The one JSON frame the host reads."""
    return json.dumps({
        "v": 1, "exit_code": code, "timed_out": timed_out,
        "stdout": stdout.decode("utf-8", "replace"), "stdout_truncated": truncated,
        "stderr_tail": stderr.decode("utf-8", "replace"),
        "files": [{"name": name, "data": base64.b64encode(data).decode("ascii")} for name, data in files],
        "skipped": skipped,
    }, ensure_ascii=True)


def main():
    if os.path.isdir(MPL_CACHE):
        try:
            shutil.copytree(MPL_CACHE, os.environ.get("MPLCONFIGDIR", "/tmp/matplotlib"), dirs_exist_ok=True)
        except OSError:
            pass
    code, timed_out, stdout, truncated, stderr = run_child(os.path.join(IN_DIR, "main.py"), OUT_DIR)
    files, skipped = collect(OUT_DIR)
    sys.stdout.write(frame(code, timed_out, stdout, truncated, stderr, files, skipped))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
