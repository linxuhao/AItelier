#!/usr/bin/env python3
"""Read-only cgroup memory-peak recorder for the live aitelier container.

WHAT QUESTION THIS ANSWERS

The backend-rss-1114 report ranked the container's residual growth as
allocator-shaped but could not name the holder: its probe ran for 15 idle
minutes against an empty database, while production runs ~68x faster.  What was
missing was an instrument that, at the moment the container's ``memory.peak``
moves, records WHO held the memory.  This script is that instrument.  Every time
``memory.peak`` takes a new value it appends exactly one JSON line to the output
JSONL carrying:

  * the wall clock, the sample interval and the cgroup the numbers came from;
  * ``memory.peak`` in bytes, in MiB and as a share of ``memory.max`` when a
    limit is set;
  * the ``memory.stat`` decomposition (anon, file, kernel, slab, ...) and
    ``pgfault``/``pgmajfault``, so the record says which *kind* of memory the
    peak was made of;
  * the ``memory.events`` counters (max, oom, oom_kill, ...), so a peak that
    grazed the limit is distinguishable from one that did not;
  * a ``processes`` list of EVERY pid in ``cgroup.procs`` with its ``comm``,
    its ``cmdline`` (truncated to 200 characters, flagged when it was) and its
    ``RssAnon``/``VmHWM`` from ``/proc/<pid>/status``, sorted by anon RSS
    descending so the holder is the first entry.

One record therefore answers "who held the memory at the peak" without having
to guess from a timeline.

READ-ONLY BY CONTRACT

Nothing here restarts, recreates, execs into, attaches to or injects into the
container.  The only docker invocation this script can ever make is the
identity lookup ``docker inspect -f {{.Id}} aitelier`` used to find the cgroup
when ``--cgroup`` is not given; with ``--cgroup`` the module never shells out at
all.  After that it only reads cgroup files and ``/proc``.

USAGE

    # default: resolve the 'aitelier' container by name, sample every 1 s forever
    python3 scripts/mem_peak_watch.py --out ~/.AItelier/logs/memory_peaks.jsonl

    # a bounded live window (what the report's next_fix asks for)
    python3 scripts/mem_peak_watch.py --container-id <id> --interval 1 \\
        --duration 600 --out /path/to/live-peaks.jsonl

    # one snapshot, for a test or an on-demand "who holds it now"
    python3 scripts/mem_peak_watch.py --cgroup /sys/fs/cgroup --once

Exit codes: 0 = normal end of the requested window; 2 = bad invocation;
3 = the cgroup disappeared while watching (recorded, then reported); 4 = the
cgroup could not be resolved.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")
DEFAULT_CONTAINER = "aitelier"
DEFAULT_OUT = Path("~/.AItelier/logs/memory_peaks.jsonl")
CMDLINE_LIMIT = 200

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CGROUP_GONE = 3
EXIT_RESOLVE = 4

# The memory.stat keys promoted to named fields in ``decomposition_b``.  The
# full file is kept verbatim under ``memory_stat`` so nothing is lost.
DECOMPOSITION_KEYS = (
    "anon",
    "file",
    "kernel",
    "kernel_stack",
    "pagetables",
    "slab",
    "sock",
    "shmem",
    "anon_thp",
    "file_mapped",
    "file_dirty",
    "file_writeback",
    "inactive_anon",
    "active_anon",
    "inactive_file",
    "active_file",
)
# memory.events counters worth carrying at the top level of the record.
EVENT_KEYS = (
    "low",
    "high",
    "max",
    "oom",
    "oom_kill",
    "oom_group_kill",
)

_DOCKER_ID = re.compile(r"docker-([0-9a-f]{12,64})\.scope$")


def _utc_now() -> str:
    """Current UTC time as an ISO-8601 second-resolution string."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_text(path: Path) -> str | None:
    """Read a small file; ``None`` when it is unreadable (process raced away)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    """Read a one-value integer file; ``None`` when missing or not an integer."""
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _read_kv(path: Path) -> dict[str, int]:
    """Parse a ``key value`` per-line file into an int map.

    Used for ``memory.stat``/``memory.events`` and for ``/proc/<pid>/status``
    (whose values carry a ``kB`` unit, hence the tolerance below).
    """
    text = _read_text(path)
    if not text:
        return {}
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        # memory.stat / memory.events are "key value"; /proc/<pid>/status adds a
        # unit ("RssAnon:\t 1234 kB"), so accept a trailing unit rather than
        # silently dropping every kB-valued field.
        if len(parts) < 2:
            continue
        try:
            out[parts[0].rstrip(":")] = int(parts[1])
        except ValueError:
            continue
    return out


def _read_memory_max(path: Path) -> int | None:
    """Read ``memory.max``; ``None`` means unlimited (the file says ``max``)."""
    text = _read_text(path)
    if text is None:
        return None
    text = text.strip()
    if not text or text == "max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def container_id_from_path(path: str | Path) -> str | None:
    """Recover a container id from a cgroup path, without asking docker."""
    text = str(path)
    match = _DOCKER_ID.search(text.rstrip("/"))
    if match:
        return match.group(1)
    # cgroup v1 layout: .../memory/docker/<id> and friends.
    match = re.search(r"/docker/([0-9a-f]{12,64})(?:/|$)", text)
    if match:
        return match.group(1)
    return None


def docker_container_id(name: str, timeout: float = 15.0) -> str:
    """Ask the docker CLI for a container's full id (identity lookup only)."""
    try:
        done = subprocess.run(
            ["docker", "inspect", "-f", "{{.Id}}", name],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ResolveError(f"could not run 'docker inspect' for {name!r}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit {done.returncode}"
        raise ResolveError(f"docker inspect -f '{{{{.Id}}}}' {name}: {reason}")
    container_id = done.stdout.strip()
    if not container_id:
        raise ResolveError(f"docker inspect returned no id for {name!r}")
    return container_id


class ResolveError(RuntimeError):
    """The container/cgroup could not be located; nothing was sampled."""


class CgroupGone(RuntimeError):
    """The cgroup path vanished while it was being watched."""


def candidate_cgroups(container_id: str) -> list[Path]:
    """Every layout this host might use for a container's cgroup directory."""
    short = container_id[:12]
    return [
        CGROUP_ROOT / "system.slice" / f"docker-{container_id}.scope",
        CGROUP_ROOT / "system.slice" / f"docker-{short}.scope",
        CGROUP_ROOT / "docker" / container_id,
        CGROUP_ROOT / "memory" / "docker" / container_id,
        CGROUP_ROOT / "memory" / "docker" / short,
    ]


def resolve_cgroup(
    container_id: str | None,
    explicit: str | None,
) -> tuple[Path, str | None]:
    """Return ``(cgroup directory, container id or None)``.

    With ``--cgroup`` this touches no docker: the id, when the path carries
    one, is recovered from the path string itself.
    """
    if explicit:
        path = Path(explicit)
        if not _is_cgroup_dir(path):
            raise ResolveError(
                f"--cgroup {explicit!r} is not a cgroup directory "
                f"(no memory.* files found there)"
            )
        return path, container_id or container_id_from_path(path)

    cid = container_id or docker_container_id(DEFAULT_CONTAINER)
    tried: list[str] = []
    for path in candidate_cgroups(cid):
        tried.append(str(path))
        if _is_cgroup_dir(path):
            return path, cid
    raise ResolveError(
        "no cgroup directory for container "
        f"{cid} (tried: {', '.join(tried)}); pass --cgroup to name it directly"
    )


def _is_cgroup_dir(path: Path) -> bool:
    """Whether ``path`` looks like a cgroup v2/v1 memory cgroup we can read."""
    return path.is_dir() and (
        (path / "memory.stat").exists()
        or (path / "memory.current").exists()
        or (path / "memory.peak").exists()
    )


def _cmdline(pid: int, proc_root: Path, limit: int = CMDLINE_LIMIT) -> tuple[str, bool]:
    """A process's argv joined by spaces, truncated to ``limit`` characters."""
    raw = None
    try:
        raw = (proc_root / str(pid) / "cmdline").read_bytes()
    except OSError:
        pass
    if not raw:
        return "", False
    argv = [part for part in raw.split(b"\0") if part]
    text = " ".join(part.decode("utf-8", "replace") for part in argv)
    if len(text) > limit:
        return text[:limit], True
    return text, False


def sample_processes(
    cgroup: Path,
    proc_root: Path = PROC_ROOT,
    cmdline_limit: int = CMDLINE_LIMIT,
) -> list[dict[str, Any]]:
    """One entry per pid in ``cgroup.procs``, biggest anonymous RSS first."""
    text = _read_text(cgroup / "cgroup.procs")
    if text is None:
        raise CgroupGone(f"{cgroup}/cgroup.procs is unreadable")
    processes: list[dict[str, Any]] = []
    for token in text.split():
        try:
            pid = int(token)
        except ValueError:
            continue
        proc_dir = proc_root / str(pid)
        comm = (_read_text(proc_dir / "comm") or "").strip()
        cmdline, truncated = _cmdline(pid, proc_root, cmdline_limit)
        status = _read_kv(proc_dir / "status") if (proc_dir / "status").exists() else {}
        processes.append(
            {
                "pid": pid,
                "comm": comm,
                "cmdline": cmdline,
                "cmdline_truncated": truncated,
                "rss_anon_kb": status.get("RssAnon"),
                "vm_hwm_kb": status.get("VmHWM"),
                "vm_rss_kb": status.get("VmRSS"),
                "observed": bool(status),
            }
        )
    processes.sort(
        key=lambda proc: (
            proc["rss_anon_kb"] is None,
            -(proc["rss_anon_kb"] or 0),
            proc["pid"],
        )
    )
    return processes


def sample(
    cgroup: Path,
    *,
    proc_root: Path = PROC_ROOT,
    interval: float | None = None,
    seq: int = 0,
    container_id: str | None = None,
    cmdline_limit: int = CMDLINE_LIMIT,
) -> dict[str, Any]:
    """Read the cgroup once and return the payload for one JSONL record.

    ``container_id`` (when the caller resolved one) wins over the id recovered
    from the path, so an explicit ``--cgroup`` on a non-docker layout still
    names the container it was asked about.
    """
    peak = _read_int(cgroup / "memory.peak")
    if peak is None:
        # memory.peak exists from Linux 5.19 on; a missing/unreadable file here
        # means the cgroup went away (or the kernel cannot answer at all).
        if not (cgroup / "memory.peak").exists():
            raise CgroupGone(f"{cgroup}/memory.peak does not exist")
        raise CgroupGone(f"{cgroup}/memory.peak is unreadable")

    stat = _read_kv(cgroup / "memory.stat")
    events = _read_kv(cgroup / "memory.events")
    mem_max = _read_memory_max(cgroup / "memory.max")
    current = _read_int(cgroup / "memory.current")
    processes = sample_processes(cgroup, proc_root, cmdline_limit)
    anon_total = sum(
        proc["rss_anon_kb"] or 0 for proc in processes if proc["observed"]
    )
    holder = next(
        (proc for proc in processes if (proc["rss_anon_kb"] or 0) > 0), None
    )

    record: dict[str, Any] = {
        "utc": _utc_now(),
        "epoch_s": time.time(),
        "seq": seq,
        "interval_s": interval,
        "cgroup": str(cgroup),
        "container_id": container_id or container_id_from_path(cgroup),
        "memory_current_b": current,
        "memory_peak_b": peak,
        "memory_peak_mib": round(peak / (1024 * 1024), 3),
        "memory_max_b": mem_max,
        "memory_peak_share_of_max": (
            round(peak / mem_max, 4) if mem_max else None
        ),
        "decomposition_b": {
            key: stat[key] for key in DECOMPOSITION_KEYS if key in stat
        },
        "pgfault": stat.get("pgfault"),
        "pgmajfault": stat.get("pgmajfault"),
        "memory_stat": stat,
        "memory_events": events,
        "event_counters": {key: events[key] for key in EVENT_KEYS if key in events},
        "process_count": len(processes),
        "rss_anon_total_kb": anon_total,
        "top_rss_anon": (
            {
                "pid": holder["pid"],
                "comm": holder["comm"],
                "cmdline": holder["cmdline"],
                "rss_anon_kb": holder["rss_anon_kb"],
            }
            if holder
            else None
        ),
        "processes": processes,
    }
    return record


def _append(out_path: Path, record: dict[str, Any]) -> None:
    """Append exactly one compact JSON object plus newline, and flush it."""
    line = json.dumps(record, separators=(",", ":"), default=str)
    if "\n" in line:  # json.dumps never emits bare newlines; guard the contract
        raise AssertionError("serialized record is not one line")
    with out_path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _gone_record(
    cgroup: Path,
    seq: int,
    interval: float | None,
    reason: str,
    container_id: str | None = None,
) -> dict[str, Any]:
    """A JSONL line that says the watched cgroup disappeared, and why."""
    return {
        "utc": _utc_now(),
        "epoch_s": time.time(),
        "seq": seq,
        "interval_s": interval,
        "cgroup": str(cgroup),
        "container_id": container_id or container_id_from_path(cgroup),
        "event": "cgroup_gone",
        "reason": reason,
    }


def watch(
    cgroup: Path,
    out_path: Path,
    *,
    interval: float = 1.0,
    duration: float = 0.0,
    once: bool = False,
    proc_root: Path = PROC_ROOT,
    container_id: str | None = None,
) -> tuple[int, int, int]:
    """Sample until the window ends; return ``(exit code, samples, records)``.

    A record is written exactly once per distinct ``memory.peak`` value, so a
    quiet hour adds no lines while a real rise adds one line naming its holder.
    ``--once`` writes the current state exactly once regardless of novelty.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists() and out_path.is_dir():
        raise ResolveError(f"--out {out_path} is a directory")

    samples = 0
    records = 0
    seq = 0
    started = time.monotonic()
    last_peak: int | None = None
    while True:
        if not cgroup.is_dir():
            return (
                _finish_gone(
                    cgroup, out_path, seq, interval, "directory deleted", container_id
                ),
                samples,
                records,
            )
        try:
            payload = sample(
                cgroup,
                proc_root=proc_root,
                interval=interval,
                seq=seq,
                container_id=container_id,
            )
        except CgroupGone as exc:
            return (
                _finish_gone(cgroup, out_path, seq, interval, str(exc), container_id),
                samples,
                records,
            )

        peak = payload["memory_peak_b"]
        samples += 1
        seq += 1
        if once or peak != last_peak:
            payload["peak_is_new"] = last_peak is None or peak > (last_peak or 0)
            if last_peak is not None:
                payload["peak_delta_b"] = peak - last_peak
            _append(out_path, payload)
            records += 1
            last_peak = peak
        if once:
            return EXIT_OK, samples, records

        elapsed = time.monotonic() - started
        if duration and elapsed >= duration:
            return EXIT_OK, samples, records
        target = started + samples * interval
        delay = target - time.monotonic()
        if duration:
            delay = min(delay, duration - elapsed)
        if delay > 0:
            time.sleep(delay)


def _finish_gone(
    cgroup: Path,
    out_path: Path,
    seq: int,
    interval: float | None,
    reason: str,
    container_id: str | None = None,
) -> int:
    """Record the disappearance in the JSONL, explain it, and return exit 3."""
    try:
        _append(out_path, _gone_record(cgroup, seq, interval, reason, container_id))
    except OSError as exc:  # pragma: no cover - unwritable output is fatal anyway
        print(f"mem_peak_watch: could not record cgroup loss: {exc}", file=sys.stderr)
    print(
        f"mem_peak_watch: cgroup {cgroup} disappeared while watching ({reason}); "
        "the event was recorded in "
        f"{out_path} and the recorder is stopping (a restart recreates the scope)",
        file=sys.stderr,
    )
    return EXIT_CGROUP_GONE


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return value


def _non_negative_float(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only memory.peak recorder for a docker container's cgroup: "
            "one JSONL line per new peak, naming every process in the cgroup "
            "with its anonymous RSS at that moment."
        )
    )
    parser.add_argument(
        "--container-id",
        default=None,
        help=(
            "full or short docker container id; default resolves the "
            f"{DEFAULT_CONTAINER!r} container by name via 'docker inspect'"
        ),
    )
    parser.add_argument(
        "--cgroup",
        default=None,
        help=(
            "explicit cgroup directory to watch (no docker call at all when "
            "this is given)"
        ),
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"output JSONL path (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--interval",
        type=_positive_float,
        default=1.0,
        help="sample interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--duration",
        type=_non_negative_float,
        default=0.0,
        help="seconds to watch; 0 means forever (default: 0)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="emit the current state exactly once and exit 0",
    )
    parser.add_argument(
        "--proc-root",
        default=str(PROC_ROOT),
        help=(
            "where to read per-pid comm/cmdline/status from; default /proc. "
            "Exposed so a test can drive the recorder deterministically."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_path = Path(args.out).expanduser()
    try:
        cgroup, container_id = resolve_cgroup(args.container_id, args.cgroup)
    except ResolveError as exc:
        print(f"mem_peak_watch: {exc}", file=sys.stderr)
        return EXIT_RESOLVE
    if container_id and not args.cgroup:
        print(f"mem_peak_watch: container {container_id} -> {cgroup}", file=sys.stderr)
    try:
        code, samples, records = watch(
            cgroup,
            out_path,
            interval=args.interval,
            duration=args.duration,
            once=args.once,
            proc_root=Path(args.proc_root),
            container_id=container_id,
        )
    except ResolveError as exc:
        print(f"mem_peak_watch: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        print("mem_peak_watch: interrupted", file=sys.stderr)
        return 130
    if code == EXIT_OK:
        print(
            f"mem_peak_watch: {samples} samples, {records} peak records in {out_path}",
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
