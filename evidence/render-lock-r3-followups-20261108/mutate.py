import sys, shutil, subprocess, os
from pathlib import Path

SRC = Path("/home/linxuhao/.AItelier/dsh-drivers/dsh-c61eed4a13ba4aaaae96/work/c8-renderlock-r3-20261108")
HARNESS_REL = "docker/godot/godot_harness.py"

def copy_tree(dst):
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    subprocess.run(["rsync","-a","--exclude=.git",str(SRC)+"/",str(dst)+"/"],check=True)

def patch(dst, old, new, label):
    p = Path(dst)/HARNESS_REL
    s = p.read_text()
    assert s.count(old) == 1, (label, s.count(old))
    p.write_text(s.replace(old, new))

def m_v1(dst):
    old = '''    def beat() -> None:
        while not stop.wait(RENDER_OWNER_HEARTBEAT_INTERVAL_SEC):
            try:
                heartbeat_render_owner(owner_id, generation)
            except sqlite3.OperationalError as exc:
                print(f"[harness] render owner heartbeat failed, retrying: {exc}",
                      flush=True)
'''
    new = '''    fails = []

    def beat() -> None:
        while not stop.wait(RENDER_OWNER_HEARTBEAT_INTERVAL_SEC):
            try:
                heartbeat_render_owner(owner_id, generation)
            except sqlite3.OperationalError as exc:
                fails.append(1)
                print(f"IGNITION-V1 consecutive_operational_failures={len(fails)}",
                      flush=True)
                if len(fails) >= 2:
                    return
'''
    patch(dst, old, new, "V1")

def m_v5(dst):
    old = '''        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
        except RenderOwnerConflict as exc:
            fence_held = False
'''
    new = '''        except sqlite3.OperationalError as exc:
            print(f"IGNITION-V5 swallowed_non_locked={exc!r}", flush=True)
        except RenderOwnerConflict as exc:
            fence_held = False
'''
    patch(dst, old, new, "V5")

def m_v6(dst):
    old = '''        if updated != 1:
            raise RuntimeError("render owner is no longer active")
'''
    new = '''        if updated != 1:
            print("IGNITION-V6 row_is_no_longer_active", flush=True)
'''
    patch(dst, old, new, "V6")

def m_p6(dst):
    old = '''                with _RenderLockWait():
                    process_locked = self._RENDER_LOCK.acquire(
                        timeout=RENDER_PROCESS_LOCK_WAIT_SEC)
'''
    new = '''                with _RenderLockWait():
                    print("IGNITION-P6 unbounded_process_lock_acquire", flush=True)
                    process_locked = self._RENDER_LOCK.acquire()
'''
    patch(dst, old, new, "P6")

def m_p7(dst):
    old = '''    if wait_timeout is not None and wait_timeout <= 0:
        return _acquire_render_owner_without_queuing(
            project_id, run_id, operation_id, wait_timeout=wait_timeout)
'''
    new = '''    if False and wait_timeout is not None and wait_timeout <= 0:
        return _acquire_render_owner_without_queuing(
            project_id, run_id, operation_id, wait_timeout=wait_timeout)
'''
    patch(dst, old, new, "P7a")
    old2 = '''                if wait_timeout is not None and wait_timeout > 0 and elapsed > wait_timeout:
'''
    new2 = '''                if wait_timeout is not None and wait_timeout > 0 and elapsed > wait_timeout:
'''
    old3 = '''                if wait_timeout is not None and wait_timeout > 0 and elapsed > wait_timeout:
                    release_reason'''
    new3 = '''                print(f"IGNITION-P7 recheck elapsed={elapsed:.4f} limit={wait_timeout}",
                      flush=True)
                if wait_timeout is not None and elapsed > wait_timeout:
                    release_reason'''
    patch(dst, old3, new3, "P7b")

MUTS = {"v1":m_v1,"v5":m_v5,"v6":m_v6,"p6":m_p6,"p7":m_p7}
name = sys.argv[1]
dst = f"/tmp/rqmut/{name}"
copy_tree(dst)
MUTS[name](dst)
subprocess.run(["git","-C",str(SRC),"diff","--stat"],check=True)
print("mutant ready:", dst)
