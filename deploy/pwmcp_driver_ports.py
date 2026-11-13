#!/usr/bin/env python3
"""Per-driver PWMCP browser ports: allocate one, render the drop-in, read the host.

WHY THIS EXISTS
---------------
One Playwright MCP instance holds ONE client session: two drivers pointed at the
same URL do not share a browser, the second `initialize` evicts the first, and
the evicted driver's next call answers "-32001 Session not found" while the
service log stays silent. The instances already run one per driver on their own
port; what was missing is which port belongs to which driver id. That mapping is
`AITELIER_DSH_MCP_PWMCP_DRIVER_PORTS` (read by `core/dsh_driver.py`), and this
module is the operator tool for it: it derives the ports, writes the systemd
drop-in that carries them, and reports what the host is actually running.

THE MAPPING VALUE
-----------------
`<driver_id>=<port>` pairs joined by a COMMA - that is the separator
`core.dsh_driver.pwmcp_driver_ports()` splits on, and it also happens to be the
separator an unquoted systemd `Environment=` would split on by itself:

    AITELIER_DSH_MCP_PWMCP_DRIVER_PORTS=dsh-1a2b=9101,dsh-3c4d=9102

A ';'-joined value is NOT emitted here: that parser treats "9101;dsh-3c4d=9102"
as one non-numeric port and refuses the whole mapping. Empty segments and
whitespace around an entry are tolerated by the reader (and by this tool), so a
hand-edited value keeps working. Everything this tool writes is re-parsed by
that same reader before it is written or printed, so the tool cannot emit a
value the driver would reject.

Driver ids match `[a-z0-9][a-z0-9-]*` (the ids the runtime mints look like
`dsh-<hex>`); anything else is refused rather than guessed at.

TWO PORT BOUNDS
---------------
There are two. Each is defined once in this module and printed by `--help`; the
numbers are deliberately not repeated in this prose, because a second copy is a
second place to go stale.

  * `ACCEPTED` - what an entry of an EXISTING mapping may hold. It is the bound
    `core.dsh_driver.pwmcp_driver_ports()` enforces, so this tool accepts exactly
    what the driver accepts and refuses exactly what the driver refuses. A host
    may already run a driver on a port below `ALLOCATABLE`; such an entry is
    read, rendered and reported like any other.
  * `ALLOCATABLE` - where `allocate` may INVENT a port for a driver that has
    none. It lies above the legacy 89xx block already in service. This tool
    cannot see your host, so check with `status` before starting an instance on
    a port that was already taken.

Only a port `allocate` invents is constrained to `ALLOCATABLE`. An id already in
the mapping keeps its own port verbatim, and `allocate` says on stderr when that
port lies outside `ALLOCATABLE`.

`allocate` derives a port from the driver id, never from the clock or a counter,
so the same id with the same mapping always prints the same port:

  1. id already in the mapping  -> print its port, unchanged (the mapping wins);
  2. otherwise fold sha256(id)'s first 8 bytes into ALLOCATABLE - that is the
     candidate;
  3. while the candidate is already assigned to a DIFFERENT id, step to the next
     port in ALLOCATABLE, wrapping once - linear probing, determinism preserved
     because the walk order is fixed.

Two ids can therefore land on one port only where the mapping does not record
both of them: against an EMPTY mapping, two ids that fold to the same slot are
indistinguishable, and no allocator can tell them apart - which is exactly why
`render` takes the whole mapping at once. Once both drivers are in one mapping,
probing walks past a port another driver already holds, and two entries naming
one port are refused outright.

SUBCOMMANDS
-----------
    python -m deploy.pwmcp_driver_ports allocate <driver_id> [--mapping <value>]
        Print exactly one port on stdout. `--mapping` defaults to the current
        value of the environment variable, the same source the reader uses; an
        empty/absent mapping means "nothing assigned yet". An id already in the
        mapping gets its own port back verbatim, even one outside ALLOCATABLE
        (a driver that runs below the range today); that case also prints one
        informational line on stderr. Only a port invented here is in ALLOCATABLE.

    python -m deploy.pwmcp_driver_ports render <mapping> [--dropin <dir>] [--unit <name>]
        Validates every entry against ACCEPTED, so it renders whatever the host
        already runs, including a legacy port below ALLOCATABLE. Writes ONE
        drop-in file - the default `10-pwmcp-driver-ports.conf` in
        --dropin - containing exactly one `Environment="..."` assignment (entries
        sorted by driver id) plus the `[Service]` header the unit needs. The
        value is quoted because an unquoted systemd `Environment=` splits on the
        commas the reader needs kept. The file is written atomically and only
        when its bytes would change, so rendering the same mapping twice leaves
        it byte-identical, and unrelated drop-ins in the same directory are
        never read, rewritten or removed. It then prints the unit name and the
        exact `systemctl --user daemon-reload && systemctl --user restart <unit>`
        line it deliberately does NOT run: applying it is the operator's call.

    python -m deploy.pwmcp_driver_ports status [--unit <name>] [--dropin <dir>]
                                              [--profiles <dir>]
        Read-only report; entries are validated against ACCEPTED, as in render.
        For every driver named in the host unit's environment
        and in the per-driver profile files it prints the port, the URL the
        generated profile holds, and whether something listens on that port. It
        also flags a profile URL whose port disagrees with the mapping - the
        symptom of a profile generated before the port moved. It never writes,
        reloads, starts, stops or re-addresses anything, and it never runs
        docker.

DEFAULTS (all documented parameters, all overridable)
-----------------------------------------------------
    --unit      aitelier-dsh-host.service
    --dropin    $XDG_CONFIG_HOME (else ~/.config)/systemd/user/<unit>.d
    --profiles  $AITELIER_HOME (else ~/.AItelier)/dsh-drivers, where each
                <driver_id>/home/profiles/driver/cordis.patch.yml holds the URL
                the driver was generated with

WHAT THE TOOL DOES NOT RUN
--------------------------
`allocate` and `render` run nothing. `status` runs at most ONE read-only
`systemctl --user show --property=MainPID --value` to find the running unit's
pid and then reads that pid's `/proc/<pid>/environ`; only the one mapping
variable is extracted, never the rest of the environment. Daemon-reload,
restart, enable, start and stop are printed for a human, never executed; docker
is neither used nor needed.

REFUSALS
--------
Exit status 1, with the offending entry quoted verbatim on stderr, for:
    * a driver id that does not match [a-z0-9][a-z0-9-]*;
    * a port outside ACCEPTED (the bound the reader enforces);
    * the same driver id twice;
    * two different driver ids on the same port;
    * a mapping the reader (`core.dsh_driver.pwmcp_driver_ports`) rejects.
Argument errors exit 2 (argparse). Nothing is written when a refusal fires.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from core.datadir import aitelier_home
from core.dsh_driver import PWMCP_DRIVER_PORTS_ENV, pwmcp_driver_ports

# The two port bounds, each defined once. `--help`, the refusals and the code all
# read them from here, so changing one is a one-line edit that cannot leave the
# documentation describing a range the code no longer uses.
#
# ACCEPTED: what an entry of an EXISTING mapping may hold. Must equal the bound
# `core.dsh_driver.pwmcp_driver_ports()` enforces, so the tool never accepts what
# the driver refuses and never refuses what the driver accepts.
ACCEPTED = range(1024, 65536)
#
# ALLOCATABLE: where `allocate` may INVENT a port for a driver that has none. It
# lies above the legacy 89xx block already in service. It does NOT avoid 99xx by
# construction (9900..9999 is inside it), so `status` is the check against the
# live host before starting an instance on an allocated port.
ALLOCATABLE = range(9100, 10000)

DROPIN_FILENAME = "10-pwmcp-driver-ports.conf"

DEFAULT_UNIT = "aitelier-dsh-host.service"

DRIVER_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]*")

# Where a generated driver profile keeps its MCP entries, relative to the
# per-driver state directory.
PROFILE_RELPATH = Path("home") / "profiles" / "driver" / "cordis.patch.yml"

# The kernel's per-process view. A constant so the one /proc read can be pointed
# at a fixture instead of a live process.
PROC_ROOT = Path("/proc")


def range_text(bounds=ALLOCATABLE):
    """A port range as the operator sees and types it (`<start>..<stop-1>`)."""
    return f"{bounds.start}..{bounds.stop - 1}"


# ── the mapping value ────────────────────────────────────────────────────────


def parse_mapping(text):
    """Parse and validate a mapping value into `{driver_id: port}`.

    Deliberately stricter than `core.dsh_driver.pwmcp_driver_ports()`, which has
    to stay lenient enough for a hand-edited environment: this is the tool that
    WRITES the value, so a duplicate id or a shared port must stop here, with the
    entry quoted. (The reader cannot report either - it keeps the last duplicate
    and cannot see that two ids named one port.) Refusals quote the entry
    verbatim so the operator can find it in a long drop-in.
    """
    mapping, owner = {}, {}
    for segment in (text or "").split(","):
        entry = segment.strip()
        if not entry:
            continue
        driver_id, separator, port_text = entry.partition("=")
        driver_id, port_text = driver_id.strip(), port_text.strip()
        # The same digit test and conversion as the reader, so a port the reader
        # refuses for its syntax is refused here too, with the entry quoted.
        if not separator or not driver_id or not port_text.isdigit():
            raise ValueError(
                f"entry {entry!r} is not a <driver_id>=<port> pair")
        try:
            port = int(port_text)
        except ValueError:
            raise ValueError(
                f"entry {entry!r} is not a <driver_id>=<port> pair") from None
        if not DRIVER_ID_PATTERN.fullmatch(driver_id):
            raise ValueError(
                f"entry {entry!r} has driver id {driver_id!r}, which does not match "
                "[a-z0-9][a-z0-9-]*")
        if port not in ACCEPTED:
            raise ValueError(
                f"entry {entry!r} has port {port}, outside the accepted range "
                f"{range_text(ACCEPTED)}")
        if driver_id in mapping:
            raise ValueError(
                f"entry {entry!r} names driver id {driver_id!r} a second time "
                f"(already {mapping[driver_id]})")
        if port in owner:
            raise ValueError(
                f"entry {entry!r} gives port {port} to {driver_id!r}, but "
                f"{owner[port]!r} already has it")
        mapping[driver_id] = port
        owner[port] = driver_id
    return mapping


def format_mapping(mapping):
    """Render `{driver_id: port}` as the value the reader parses back unchanged.

    Sorted by driver id: the file's bytes must depend on the mapping alone, not
    on the order the operator happened to type the entries in.
    """
    return ",".join(f"{driver_id}={mapping[driver_id]}" for driver_id in sorted(mapping))


def checked_value(mapping):
    """The value to write or print, re-parsed by the real reader first.

    The round-trip is asserted here rather than trusted: this tool's only
    contract with the driver is that `core.dsh_driver.pwmcp_driver_ports()`
    returns exactly the mapping the operator asked for.
    """
    value = format_mapping(mapping)
    read_back = pwmcp_driver_ports(value)
    if read_back != mapping:
        raise ValueError(
            f"rendered mapping {value!r} reads back as {read_back!r}, not {mapping!r}; "
            f"{PWMCP_DRIVER_PORTS_ENV} would not describe these drivers")
    return value


# ── allocate ─────────────────────────────────────────────────────────────────


def allocate(driver_id, mapping):
    """The port for `driver_id`: its existing one, else a stable derived one.

    An id already in the mapping keeps its port verbatim, even one outside
    ALLOCATABLE: that is the port the driver already runs on. Only a port
    invented here is constrained to ALLOCATABLE.

    Stable because nothing but the id and the mapping's contents feed it - not
    the clock, not a counter, not the order of the other entries. A collision
    with a port already given to a DIFFERENT id walks ALLOCATABLE instead of
    sharing, which is the whole point: one driver per Playwright instance.
    """
    if not DRIVER_ID_PATTERN.fullmatch(driver_id or ""):
        raise ValueError(
            f"driver id {driver_id!r} does not match [a-z0-9][a-z0-9-]*")
    if driver_id in mapping:
        return mapping[driver_id]
    taken = set(mapping.values())
    span = len(ALLOCATABLE)
    digest = hashlib.sha256(driver_id.encode("utf-8")).digest()
    start = int.from_bytes(digest[:8], "big") % span
    for step in range(span):
        port = ALLOCATABLE[(start + step) % span]
        if port not in taken:
            return port
    raise ValueError(
        f"no free port in {range_text(ALLOCATABLE)} for {driver_id!r}: all {span} "
        f"are already assigned")


# ── render ───────────────────────────────────────────────────────────────────


def atomic_text(path, content, mode=0o644):
    """Write `content` to `path` only if its bytes would change.

    Same shape as the host installer's helper: an unchanged file is left
    untouched (no truncate, no new inode, so a second render keeps both its
    bytes and its timestamp), a changed one is replaced atomically so nothing
    ever reads half a drop-in, and the temporary is dot-prefixed so a crash
    cannot leave behind something the unit would read as a drop-in.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return False
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def dropin_text(value):
    """The drop-in file's exact content for a mapping value.

    Two things matter to systemd: the `[Service]` section (the drop-in is a
    fragment of the unit, not a unit) and the quoting. An unquoted
    `Environment=` value is split on commas by systemd itself, which would turn
    `a=9100,b=9101` into two assignments and leave the driver with a mapping
    holding one entry.
    """
    return (
        "# Per-driver PWMCP browser ports for the DSH host unit.\n"
        "# Generated by deploy/pwmcp_driver_ports.py; edit the mapping and re-render\n"
        "# rather than editing this file, so a second render stays byte-identical.\n"
        "[Service]\n"
        f'Environment="{PWMCP_DRIVER_PORTS_ENV}={value}"\n'
    )


def environment_value(text):
    """The mapping value a rendered drop-in carries, or None if it carries none.

    Used by `status` on the on-disk drop-in, and by the tests to prove the
    round-trip: extract, then hand it to the real reader.
    """
    for line in text.splitlines():
        match = re.fullmatch(
            r'\s*Environment="' + re.escape(PWMCP_DRIVER_PORTS_ENV) + r'=(.*)"\s*', line)
        if match:
            return match.group(1)
    return None


# ── read-only host probes ────────────────────────────────────────────────────
# Each probe is one function so `status` can be tested - and trusted - without a
# live unit: a test monkeypatches these, and the tests never run systemctl.


def run_command(argv):
    """The only place this module starts a process, ever.

    `status` calls it for one read-only query. No code path here passes it
    daemon-reload, restart, enable, start or stop; those are printed instead.
    """
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def probe_unit_main_pid(unit):
    """MainPID of the running user unit, or 0 when it is not running."""
    try:
        completed = run_command(
            ["systemctl", "--user", "show", "--property=MainPID", "--value", unit])
    except OSError as error:  # no systemctl on this host: report, do not fail
        print(f"note: cannot query {unit}: {error}", file=sys.stderr)
        return 0
    text = (completed.stdout or "").strip()
    return int(text) if text.isdigit() else 0


def probe_unit_mapping(unit):
    """The mapping the RUNNING unit holds, or None when it is not running.

    `/proc/<pid>/environ` is the only place the effective value exists - a
    drop-in on disk is what the next start would apply. Only the one variable is
    extracted: the rest of the environment is never returned, printed or kept
    (it holds tokens).
    """
    pid = probe_unit_main_pid(unit)
    if not pid:
        return None
    try:
        raw = (PROC_ROOT / str(pid) / "environ").read_bytes()
    except OSError as error:
        print(f"note: cannot read the environment of pid {pid}: {error}", file=sys.stderr)
        return None
    prefix = PWMCP_DRIVER_PORTS_ENV.encode() + b"="
    for entry in raw.split(b"\0"):
        if entry.startswith(prefix):
            return entry[len(prefix):].decode("utf-8", "replace")
    return ""


class Profile:
    """One generated driver profile: the driver, its file, and its pwmcp URL."""

    def __init__(self, driver_id, path, url):
        self.driver_id, self.path, self.url = driver_id, path, url


def profile_url(text):
    """The `url` of the `pwmcp` MCP entry in a generated profile.

    The profile is YAML, but only this one entry is of interest and the writer
    (`core/dsh_driver.py`) emits it with stable keys, so a line scan is enough
    and this tool needs no YAML parser. `serverName` precedes `url` in the same
    entry; a new list item ends the entry being read.
    """
    inside = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            inside = False
            line = line[2:].strip()
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key, value = key.strip(), value.strip().strip("'\"")
        if key == "serverName":
            inside = value == "pwmcp"
        elif key == "url" and inside:
            return value
    return None


def probe_profiles(root):
    """Every generated driver profile under `root`, sorted by driver id."""
    profiles = []
    root = Path(root)
    if not root.is_dir():
        return profiles
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        path = directory / PROFILE_RELPATH
        try:
            text = path.read_text()
        except OSError:
            continue
        profiles.append(Profile(directory.name, path, profile_url(text)))
    return profiles


def probe_listening(paths=("/proc/net/tcp", "/proc/net/tcp6")):
    """Ports with a LISTEN socket, read from the kernel's tables.

    Deliberately not a connect(): a report must not touch - let alone start a
    session on - a browser someone else is driving, and a `connect` would look
    like a client to a Playwright instance that serves exactly one.
    """
    listening = set()
    for path in paths:
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":  # 0A = LISTEN
                continue
            _, _, port_hex = fields[1].rpartition(":")
            if port_hex:
                listening.add(int(port_hex, 16))
    return listening


# ── output ───────────────────────────────────────────────────────────────────


def table(headers, rows):
    """A plain fixed-width table; `rows` may be empty."""
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    lines = ["  ".join(header.ljust(widths[index])
                       for index, header in enumerate(headers)).rstrip()]
    for row in rows:
        lines.append("  ".join(str(cell).ljust(widths[index])
                               for index, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def port_of_url(url):
    """The port an MCP URL names, or None."""
    match = re.search(r":(\d+)/", url or "")
    return int(match.group(1)) if match else None


# ── subcommands ──────────────────────────────────────────────────────────────


def cmd_allocate(args):
    mapping = parse_mapping(
        os.environ.get(PWMCP_DRIVER_PORTS_ENV, "") if args.mapping is None else args.mapping)
    port = allocate(args.driver_id, mapping)
    print(port)
    if args.driver_id in mapping and port not in ALLOCATABLE:
        print(f"note: {args.driver_id} already runs on port {port}, the driver's existing "
              f"port, outside the allocation range {range_text(ALLOCATABLE)}",
              file=sys.stderr)
    return 0


def cmd_render(args):
    mapping = parse_mapping(args.mapping)
    value = checked_value(mapping)
    dropin = Path(args.dropin or default_dropin_dir(args.unit))
    path = dropin / DROPIN_FILENAME
    changed = atomic_text(path, dropin_text(value))
    print(f"drop-in: {path} ({'written' if changed else 'unchanged'})")
    print(f"mapping: {value or '(empty - no per-driver mapping)'}")
    print(f"unit: {args.unit}")
    print("not run (apply deliberately): "
          f"systemctl --user daemon-reload && systemctl --user restart {args.unit}")
    return 0


def cmd_status(args):
    dropin = Path(args.dropin or default_dropin_dir(args.unit))
    live = probe_unit_mapping(args.unit)
    try:
        text = (dropin / DROPIN_FILENAME).read_text()
    except OSError:
        on_disk, disk_label = None, "(no drop-in)"
    else:
        on_disk = environment_value(text)
        disk_label = "(no Environment assignment)" if on_disk is None else on_disk or "(empty)"
    # A malformed value is reported as a refusal rather than papered over: the
    # driver would refuse it too, and the operator needs the entry quoted. Both
    # values are parsed before anything is printed, so a refusal prints no table.
    live_mapping = parse_mapping(live) if live is not None else {}
    disk_mapping = parse_mapping(on_disk) if on_disk is not None else {}
    profiles = {profile.driver_id: profile for profile in probe_profiles(args.profiles)}
    listening = probe_listening()

    print(f"unit: {args.unit}")
    print(f"running-unit mapping: {'(unit not running)' if live is None else live or '(empty)'}")
    print(f"drop-in ({dropin / DROPIN_FILENAME}): {disk_label}")

    drivers = sorted(set(live_mapping) | set(disk_mapping) | set(profiles))
    rows, notes = [], []
    for driver_id in drivers:
        profile = profiles.get(driver_id)
        url = profile.url if profile else None
        port = live_mapping.get(driver_id, disk_mapping.get(driver_id))
        if port is None:
            port = port_of_url(url)
        if port is not None and port_of_url(url) not in (None, port):
            notes.append(
                f"note: {driver_id}: the profile at {profile.path} names port "
                f"{port_of_url(url)}, the mapping says {port}")
        rows.append((driver_id,
                     "-" if port is None else port,
                     url or "(none)",
                     "-" if port is None else ("yes" if port in listening else "no")))
    if rows:
        print(table(("driver", "port", "profile url", "listening"), rows))
    else:
        print("no per-driver mapping and no generated profiles found")
    for note in notes:
        print(note)
    return 0


# ── defaults and the command line ────────────────────────────────────────────


def default_dropin_dir(unit):
    """Where systemd reads drop-ins for a user unit."""
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / (unit + ".d")


def default_profile_root():
    """Where the host keeps one state directory per driver."""
    return aitelier_home() / "dsh-drivers"


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m deploy.pwmcp_driver_ports",
        description=__doc__.split("\n\nTHE MAPPING VALUE")[0].strip(),
        epilog=f"port bounds: accepted for an existing mapping entry {range_text(ACCEPTED)} "
               f"(ACCEPTED, the reader's bound); allocated for a new driver "
               f"{range_text(ALLOCATABLE)} (ALLOCATABLE). "
               f"the mapping variable is {PWMCP_DRIVER_PORTS_ENV}, entries joined by ','.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    allocate = commands.add_parser(
        "allocate", help="print the port for one driver id and nothing else",
        description="Print exactly one port on stdout. The mapping's own entry wins, even "
                    f"one outside {range_text(ALLOCATABLE)} (then one stderr note says so); "
                    f"otherwise a deterministic sha256 fold into {range_text(ALLOCATABLE)} "
                    "with linear probing around ports already given to other drivers.")
    allocate.add_argument("driver_id", help="driver id, [a-z0-9][a-z0-9-]*")
    allocate.add_argument("--mapping", default=None,
                          help=f"mapping value to allocate against "
                               f"(default: ${PWMCP_DRIVER_PORTS_ENV}, else empty)")
    allocate.set_defaults(handler=cmd_allocate)

    render = commands.add_parser(
        "render", help="write the systemd drop-in carrying a mapping",
        description="Write one drop-in holding one Environment= assignment; the same "
                    "mapping rendered twice is byte-identical, and unrelated drop-ins "
                    "in the directory are untouched. Prints the reload/restart it "
                    "does not run.")
    render.add_argument("mapping", help="mapping value: '<driver_id>=<port>,...'")
    render.add_argument("--dropin", default=None,
                        help="drop-in DIRECTORY (default: "
                             "$XDG_CONFIG_HOME/systemd/user/<unit>.d)")
    render.add_argument("--unit", default=DEFAULT_UNIT,
                        help=f"host unit name (default: {DEFAULT_UNIT})")
    render.set_defaults(handler=cmd_render)

    status = commands.add_parser(
        "status", help="read-only report of ports, profile URLs and listeners",
        description="Report per driver: port, the URL its generated profile holds, "
                    "and whether something listens. Writes, reloads, starts and stops "
                    "nothing.")
    status.add_argument("--unit", default=DEFAULT_UNIT,
                        help=f"host unit name (default: {DEFAULT_UNIT})")
    status.add_argument("--dropin", default=None,
                        help="drop-in DIRECTORY (default: "
                             "$XDG_CONFIG_HOME/systemd/user/<unit>.d)")
    status.add_argument("--profiles", default=None,
                        help="per-driver state root (default: "
                             "$AITELIER_HOME/dsh-drivers)")
    status.set_defaults(handler=cmd_status)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "status" and args.profiles is None:
        args.profiles = default_profile_root()
    try:
        return args.handler(args)
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
