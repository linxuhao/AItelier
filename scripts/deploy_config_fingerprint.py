#!/usr/bin/env python3
"""Per-field fingerprint of a running container, and a diff of two fingerprints.

Usage::

    python scripts/deploy_config_fingerprint.py capture <container> <out.json>
    python scripts/deploy_config_fingerprint.py diff <a.json> <b.json>

``capture`` runs ``docker inspect <container>`` and keeps ONLY the whitelisted
fields listed in ``_WHITELIST`` (image id, start time, restart count, argv,
command, working dir, user, environment field NAMES + count, label keys, a
healthcheck digest, restart policy, network mode, bind targets with their rw/ro
mode, port-binding keys, CapAdd/CapDrop, SecurityOpt, ShmSize, extra-host count)
plus, when the container can be exec'd, the sha256 of ``/srv/web_dist/index.html``
and the installed skillflow version.  It writes two digests: ``structural_digest``
(over the projection WITHOUT start time and restart count) and ``full_digest``
(over everything), so a plain restart is distinguishable from a configuration
change instead of hiding behind one aggregate number.

An exec probe that cannot answer is recorded INSIDE the whitelisted field it
belongs to, from a closed vocabulary — ``unavailable:exec-exit-<n>``,
``unavailable:unexpected-output``, ``unavailable:timeout``.  It is never free
text and never a second field: container-side stdout/stderr is an unfiltered
channel (it can carry anything the image prints, a secret included) and no byte
of it is quoted here.  Encoding the failure in the value also keeps the
structural digest from churning on transient stderr wording.

``diff`` prints one line per differing field as ``path: A -> B``, reports fields
present on only one side as ``added``/``removed``, prints the two digests, and
exits non-zero if anything differs, zero if identical, two if a capture file
cannot be read.

Two boundaries, both load-bearing:

* Names only, never values.  Every ``Config.Env`` entry is split at the first
  ``=`` and only the part before it is kept, so no environment value can reach
  the output of ``capture`` or ``diff`` — success path or error path.  An entry
  with no ``=`` has no name/value split at all, so it is SKIPPED rather than
  kept whole (keeping it whole is exactly how a value would reach the output),
  and ``Config.Env.count`` still counts it: a count larger than
  ``len(Config.Env.names)`` means entries were skipped, never stringified.
  Bind sources are dropped for the same reason (only target and mode are kept),
  and label VALUES are dropped (only keys).
* This is a manual operator tool, not a gate.  Nothing in the gate imports it
  and no gate behaviour changes because of it: a human runs it when a container
  recreate needs explaining, and its exit status is for that human.

Docker is reached only through the injectable ``runner`` callable, so the whole
tool can be exercised without a docker binary, a daemon or a network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

# A runner is ``fn(argv) -> (returncode, stdout, stderr)``; the default shell is
# the only place a real process is started, which is what keeps capture/diff
# testable with no docker at all.
Runner = Callable[[Sequence[str]], "tuple[int, str, str]"]

DOCKER = "docker"
WEB_DIST_INDEX = "/srv/web_dist/index.html"
DIAGNOSTIC_LIMIT = 300

# The complete set of projected paths.  Nothing outside this tuple is emitted:
# no catch-all dump of the inspect document, ever.
_WHITELIST = (
    "Image",
    "State.StartedAt",
    "RestartCount",
    "Config.Entrypoint",
    "Config.Cmd",
    "Config.WorkingDir",
    "Config.User",
    "Config.Env.names",
    "Config.Env.count",
    "Config.Labels.keys",
    "Config.Healthcheck.sha256",
    "HostConfig.RestartPolicy.Name",
    "HostConfig.NetworkMode",
    "HostConfig.Binds",
    "HostConfig.PortBindings.keys",
    "HostConfig.CapAdd",
    "HostConfig.CapDrop",
    "HostConfig.SecurityOpt",
    "HostConfig.ShmSize",
    "HostConfig.ExtraHosts.count",
    "Exec.index_html.sha256",
    "Exec.skillflow_version",
)

# Fields that move on a plain restart; excluded from structural_digest.
RESTART_VOLATILE = ("State.StartedAt", "RestartCount")

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9][^\s]{0,63}$")

# Prints the installed skillflow distribution version, whichever of the two
# distribution names it was installed under, and exits non-zero when neither is
# importable — "never guess" means no fallback string here.
_VERSION_MARKER = "skillflow-version="
_VERSION_PROBE = (
    "import importlib.metadata as md\n"
    "for _dist in ('skillflow-py', 'skillflow'):\n"
    "    try:\n"
    "        print('" + _VERSION_MARKER + "' + md.version(_dist))\n"
    "        break\n"
    "    except md.PackageNotFoundError:\n"
    "        continue\n"
    "else:\n"
    "    raise SystemExit(3)\n"
)


class CaptureError(RuntimeError):
    """Capture could not produce an honest fingerprint."""


class DiffError(RuntimeError):
    """A diff input is not a capture document."""


# --------------------------------------------------------------------------- #
# docker shell (the single place a process is started)
# --------------------------------------------------------------------------- #
def subprocess_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run one argv without a shell, with a timeout, and never raise."""
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError:
        return 127, "", "docker binary not found on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after 120s: {' '.join(argv[:3])}"
    return completed.returncode, completed.stdout, completed.stderr


def _one_line(text: Any, limit: int = DIAGNOSTIC_LIMIT) -> str:
    """Collapse a diagnostic to one bounded line (never used on env values)."""
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


# --------------------------------------------------------------------------- #
# projection helpers — the value-dropping ones sit here
# --------------------------------------------------------------------------- #
def env_names_and_count(entries: Any) -> tuple[list[str], int]:
    """(sorted field NAMES, entry COUNT).  The part after ``=`` is never kept.

    An entry with no ``=`` has no name to keep, and keeping it whole would put
    whatever it holds into the projection, so it is skipped — but it is still
    counted: ``count > len(names)`` is how a skipped entry stays visible.
    """
    if not isinstance(entries, (list, tuple)):
        return [], 0
    names: list[str] = []
    total = 0
    for entry in entries:
        total += 1
        # A non-string entry is skipped rather than stringified: stringifying
        # an unknown object is how a value would sneak into the output.
        if not isinstance(entry, str) or "=" not in entry:
            continue
        names.append(entry.split("=", 1)[0])
    return sorted(names), total


def _strings(value: Any) -> list[str]:
    """Keep string argv/capability members, drop everything else."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def bind_targets(binds: Any) -> list[str]:
    """``source:target[:opts]`` -> ``target:ro|rw``; the source is dropped."""
    projected: list[str] = []
    for entry in _strings(binds):
        parts = entry.split(":")
        if len(parts) == 1:
            target, options = parts[0], []
        elif len(parts) == 2:
            target, options = parts[1], []
        else:
            target, options = ":".join(parts[1:-1]), parts[-1].split(",")
        mode = "ro" if "ro" in options else "rw"
        projected.append(f"{target}:{mode}")
    return sorted(projected)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def build_projection(document: dict, exec_fields: dict) -> dict:
    """Project one ``docker inspect`` document onto the whitelist."""
    state = document.get("State") or {}
    config = document.get("Config") or {}
    host = document.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    ports = host.get("PortBindings") or {}
    names, env_count = env_names_and_count(config.get("Env"))

    projection = {
        "Image": document.get("Image"),
        "State.StartedAt": state.get("StartedAt"),
        "RestartCount": document.get("RestartCount"),
        "Config.Entrypoint": _strings(config.get("Entrypoint")),
        "Config.Cmd": _strings(config.get("Cmd")),
        "Config.WorkingDir": config.get("WorkingDir"),
        "Config.User": config.get("User"),
        "Config.Env.names": names,
        "Config.Env.count": env_count,
        "Config.Labels.keys": sorted(labels) if isinstance(labels, dict) else [],
        "Config.Healthcheck.sha256": sha256_canonical(config.get("Healthcheck") or {}),
        "HostConfig.RestartPolicy.Name": (host.get("RestartPolicy") or {}).get("Name"),
        "HostConfig.NetworkMode": host.get("NetworkMode"),
        "HostConfig.Binds": bind_targets(host.get("Binds")),
        "HostConfig.PortBindings.keys": sorted(ports) if isinstance(ports, dict) else [],
        "HostConfig.CapAdd": sorted(_strings(host.get("CapAdd"))),
        "HostConfig.CapDrop": sorted(_strings(host.get("CapDrop"))),
        "HostConfig.SecurityOpt": sorted(_strings(host.get("SecurityOpt"))),
        "HostConfig.ShmSize": host.get("ShmSize"),
        "HostConfig.ExtraHosts.count": len(_strings(host.get("ExtraHosts"))),
    }
    projection.update(exec_fields)
    return projection


# --------------------------------------------------------------------------- #
# exec probes
# --------------------------------------------------------------------------- #
def _exec_unavailable(code: int) -> str:
    """Closed vocabulary for a probe that could not answer.

    The container's own stdout/stderr is deliberately NOT part of this value:
    it is unfiltered text from the image and could carry a secret straight into
    a capture file.  Only the exit code is carried.
    """
    if code == 124:
        return "unavailable:timeout"
    return f"unavailable:exec-exit-{code}"


def _sha_probe(container: str, runner: Runner) -> str:
    code, out, _ = runner([DOCKER, "exec", container, "sha256sum", WEB_DIST_INDEX])
    tokens = out.split() if isinstance(out, str) else []
    if code == 0:
        digest = tokens[0].strip() if tokens else ""
        if _SHA256_RE.match(digest):
            return digest
        return "unavailable:unexpected-output"
    return _exec_unavailable(code)


def _version_probe(container: str, runner: Runner) -> str:
    code, out, _ = runner([DOCKER, "exec", container, "python", "-c", _VERSION_PROBE])
    if code != 0:
        return _exec_unavailable(code)
    if isinstance(out, str):
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith(_VERSION_MARKER):
                version = stripped[len(_VERSION_MARKER):].strip()
                if _VERSION_RE.match(version):
                    return version
                break
    return "unavailable:unexpected-output"


def probe_container(container: str, runner: Runner) -> dict:
    """In-container facts, or a closed ``unavailable:<why>`` — never a guess."""
    return {
        "Exec.index_html.sha256": _sha_probe(container, runner),
        "Exec.skillflow_version": _version_probe(container, runner),
    }


# --------------------------------------------------------------------------- #
# digests
# --------------------------------------------------------------------------- #
def structural_digest(projection: dict) -> str:
    """sha256 over everything EXCEPT the two fields a plain restart moves.

    An exec probe's failure lives in its own field's VALUE (a closed
    ``unavailable:`` token), so a transient probe failure is a structural change
    on purpose and cannot masquerade as one: no free text is folded in here.
    """
    return sha256_canonical(
        {path: value for path, value in projection.items() if path not in RESTART_VOLATILE}
    )


def full_digest(projection: dict) -> str:
    return sha256_canonical(projection)


def digests_for(projection: dict) -> dict:
    return {
        "structural_digest": structural_digest(projection),
        "full_digest": full_digest(projection),
    }


# --------------------------------------------------------------------------- #
# capture
# --------------------------------------------------------------------------- #
def capture(container: str, runner: Runner | None = None) -> dict:
    """Return ``{projection, digests}`` for one container."""
    runner = runner or subprocess_runner

    code, out, err = runner([DOCKER, "inspect", container])
    if code != 0:
        # Only the docker CLI's own diagnostic is quoted (a missing object, a
        # daemon error).  ``out`` is never echoed: on a partial read it can
        # still be the document, env values included.
        detail = _one_line(err) or "no diagnostic output"
        raise CaptureError(f"docker inspect {container} failed (exit {code}: {detail})")
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        # exc.msg carries a position, not the document text.
        raise CaptureError(
            f"docker inspect {container} returned unparsable JSON: {exc.msg}"
        ) from None

    if isinstance(parsed, list):
        if not parsed:
            raise CaptureError(f"docker inspect {container} returned no document")
        document = parsed[0]
    else:
        document = parsed
    if not isinstance(document, dict):
        raise CaptureError(
            f"docker inspect {container} returned {type(document).__name__}, "
            "not an inspect document"
        )

    projection = build_projection(document, probe_container(container, runner))
    return {"projection": projection, "digests": digests_for(projection)}


def write_capture(document: dict, out_path: Path) -> None:
    out_path.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# diff
# --------------------------------------------------------------------------- #
def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def load_capture(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DiffError(f"{path}: cannot read ({exc.strerror or exc})") from None
    except json.JSONDecodeError as exc:
        raise DiffError(f"{path}: not valid JSON ({exc.msg})") from None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("projection"), dict):
        raise DiffError(f"{path}: not a capture document (no projection object)")
    return loaded


def compare(a: dict, b: dict) -> list[str]:
    """One line per differing field; one-sided fields are added/removed."""
    left = a["projection"]
    right = b["projection"]
    lines: list[str] = []
    for path in sorted(set(left) | set(right)):
        if path in left and path in right:
            if left[path] != right[path]:
                lines.append(f"{path}: {_render(left[path])} -> {_render(right[path])}")
        elif path in left:
            lines.append(f"{path}: removed (only in A: {_render(left[path])})")
        else:
            lines.append(f"{path}: added (only in B: {_render(right[path])})")
    return lines


def diff_report(a: dict, b: dict) -> tuple[list[str], bool]:
    """Lines to print and whether anything differs."""
    differences = compare(a, b)
    left = digests_for(a["projection"])
    right = digests_for(b["projection"])
    report = list(differences)
    report.append(f"structural_digest A: {left['structural_digest']}")
    report.append(f"structural_digest B: {right['structural_digest']}")
    report.append(f"full_digest A: {left['full_digest']}")
    report.append(f"full_digest B: {right['full_digest']}")
    report.append(f"differences: {len(differences)}")
    return report, bool(differences)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None, runner: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deploy_config_fingerprint.py",
        description=(
            "Capture a whitelisted per-field container fingerprint, or diff two "
            "captures. Environment NAMES are kept; values never are."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture_parser = sub.add_parser(
        "capture", help="capture <container> <out.json>"
    )
    capture_parser.add_argument("container")
    capture_parser.add_argument("out", type=Path)

    diff_parser = sub.add_parser("diff", help="diff <a.json> <b.json>")
    diff_parser.add_argument("a", type=Path)
    diff_parser.add_argument("b", type=Path)

    args = parser.parse_args(argv)

    if args.command == "capture":
        try:
            document = capture(args.container, runner)
        except CaptureError as exc:
            print(f"capture failed: {exc}", file=sys.stderr)
            return 2
        try:
            write_capture(document, args.out)
        except OSError as exc:
            print(
                f"capture failed: cannot write {args.out}: {exc.strerror or exc}",
                file=sys.stderr,
            )
            return 2
        print(
            f"wrote {args.out} "
            f"structural_digest={document['digests']['structural_digest']} "
            f"full_digest={document['digests']['full_digest']}"
        )
        return 0

    try:
        left = load_capture(args.a)
        right = load_capture(args.b)
    except DiffError as exc:
        print(f"diff failed: {exc}", file=sys.stderr)
        return 2
    report, differs = diff_report(left, right)
    for line in report:
        print(line)
    return 1 if differs else 0


if __name__ == "__main__":
    raise SystemExit(main())
