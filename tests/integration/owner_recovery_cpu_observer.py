"""Host-only evidence collector for the owned normal-transport integration test.

This collector never launches or cancels an effect. The existing normal host
launcher owns execution and cleanup. Filtered Docker events and exact-container
inspection expose physical identity/settlement to the disposable caller, which
receives neither Docker CLI nor socket. Run separately with --output and the
same unique --project-id used by the owned fixture State database.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import threading
import time


def _pid_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def _inspect(container):
    done = subprocess.run(["docker", "inspect", container],
                          capture_output=True, text=True, timeout=10)
    if done.returncode == 0:
        return json.loads(done.stdout)[0]
    if "No such object" in done.stderr and container in done.stderr:
        return None
    raise RuntimeError(f"exact container inspection failed: {done.stderr}")


def _observe(container, ticket, events):
    deadline = time.monotonic() + 30
    effect = ticket / "effect"
    while time.monotonic() < deadline:
        if (effect / "started").exists() and (effect / "child-pid").exists():
            info = _inspect(container)
            if info is None or not info["State"]["Running"]:
                return
            pid = info["State"]["Pid"]
            start = _pid_start(pid)
            assert start is not None
            (effect / "host-observed").write_text(json.dumps({
                "container": container, "running": True, "host_pid": pid,
                "host_pid_start": start,
                "bash_pid": int((effect / "child-pid").read_text()),
                "observed_at_monotonic": time.monotonic(),
            }))
            while time.monotonic() < deadline:
                if (effect / "owner-killed").exists():
                    info = _inspect(container)
                    gone = _pid_start(pid) != start
                    recorded = events.get(container, [])
                    died = [e for e in recorded if e["Action"] == "die"]
                    destroyed = [e for e in recorded if e["Action"] == "destroy"]
                    if info is None and gone and died and destroyed:
                        (effect / "physical-settlement").write_text(json.dumps({
                            "container": container, "container_absent": True,
                            "original_host_pid": pid, "original_host_pid_gone": True,
                            "exit_code": int(died[-1]["Actor"]["Attributes"]["exitCode"]),
                            "die_event": died[-1], "destroy_event": destroyed[-1],
                            "observed_at_monotonic": time.monotonic(),
                        }))
                        return
                time.sleep(0.02)
            return
        time.sleep(0.02)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    events = {}
    proc = subprocess.Popen([
        "docker", "events", "--filter", "type=container", "--filter",
        "label=aitelier.project_id=" + args.project_id, "--format", "{{json .}}",
    ], stdout=subprocess.PIPE, text=True)
    (args.output / "observer-events.pid").write_text(str(proc.pid))
    for line in proc.stdout:
        event = json.loads(line)
        container = event["Actor"]["ID"]
        events.setdefault(container, []).append(event)
        with (args.output / "owned-docker-events.jsonl").open("a") as stream:
            stream.write(line)
        if event["Action"] == "start":
            info = _inspect(container)
            if info is None:
                continue
            with (args.output / "owned-container-inspect.jsonl").open("a") as stream:
                stream.write(json.dumps(info) + "\n")
            for mount in info["Mounts"]:
                ticket = Path(mount["Source"])
                if mount.get("RW") and ticket.name.startswith("rt-"):
                    threading.Thread(target=_observe,
                                     args=(container, ticket, events), daemon=True).start()


if __name__ == "__main__":
    main()
