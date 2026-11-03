"""Host-only resident DSH supervisor. The backend remains in Docker.

The existing enabled rows are the desired state. A short-lived process marker
lets the container observe host liveness without a Docker socket or another API.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import signal
import time

from core.dsh_driver import DshDrivers


def publish(runtime, row):
    path = runtime.root / row["driver_id"] / "host-process.json"
    proc = runtime.processes.get(row["project_id"])
    if proc is None or proc.returncode is not None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": proc.pid, "session_id": row["session_id"],
                                     "seen_at": time.time()}))
    temporary.replace(path)


async def supervise(runtime, stopped, *, interval=1):
    """One controller per installation; systemd owns its entire process group.

    One project's failure must not stop the others. This loop is the only thing
    keeping every resident driver alive, so each row is handled inside its own
    exception boundary: `rows()` reads a multi-gigabyte SQLite database that can
    report "database is locked", and `publish()` writes a file that can fail on a
    full disk. Either used to propagate out of the loop, and the supervisor then
    exited and took every driver offline at once, not just the failing project.
    """
    runtime.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (runtime.root / "host-supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        retry_at = {}
        failures = {}
        try:
            while not stopped.is_set():
                try:
                    rows = runtime.rows()
                except Exception as exc:  # a locked database must not end supervision
                    print(f"DSH host: cannot read drivers: {exc}", flush=True)
                    rows = []
                for row in rows:
                    project = row["project_id"]
                    try:
                        if not row["enabled"]:
                            retry_at.pop(project, None)
                            failures.pop(project, None)
                            if project in runtime.processes:
                                await runtime.stop(project)
                        elif not runtime.running(project) and time.monotonic() >= retry_at.get(project, 0):
                            # Back off per project: a driver that cannot start at
                            # all must not be respawned every interval forever.
                            delay = min(300, 15 * 2 ** failures.get(project, 0))
                            retry_at[project] = time.monotonic() + delay
                            try:
                                await runtime.start(project, actor="dsh-host-recovery")
                            except Exception as exc:
                                failures[project] = failures.get(project, 0) + 1
                                print(f"DSH host {project}: {exc}", flush=True)
                            else:
                                failures.pop(project, None)
                        publish(runtime, row)
                    except Exception as exc:  # one project's failure stays local
                        print(f"DSH host {project}: {exc}", flush=True)
                try:
                    await asyncio.wait_for(stopped.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await runtime.close()
            for row in runtime.rows():
                publish(runtime, row)


async def main():
    from core.datadir import aitelier_home, db_path
    from core.drivers import registry_for
    from core.state_database import StateDatabase
    db = StateDatabase(db_path())
    runtime = DshDrivers(db, registry_for(db), aitelier_home() / "dsh-drivers", host_controller=True)
    if json.loads((runtime.root / "runtime.json").read_text()).get("mode") != "host":
        raise ValueError("installation is not configured for the host runtime")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    await supervise(runtime, stopped)


if __name__ == "__main__":
    asyncio.run(main())
