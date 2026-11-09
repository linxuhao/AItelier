"""The sidecar must serve the CHECKOUT's harness, not only the one baked into its image.

Dockerfile.godot COPYs docker/godot/*.py to /srv at build time and the container's
CMD is `python3 /srv/godot_harness.py --serve` (docker-compose.yml `godot-builder`
service, container aitelier-godot). Until now that was the only source of /srv, so:

  * merging a change to docker/godot/godot_harness.py into main changed nothing the
    sidecar EXECUTED until somebody rebuilt the image or hand-copied the file into
    the running container, and
  * any ordinary recreate (`docker compose up -d`, or the host rebooting and the
    container coming back) silently returned to whatever bytes the image baked.

Both happened. The 2026-11-08 recreate baked commit 20c31f4fa516's harness, and the
render-wait fix merged as e0367fc4aa only took effect because the director copied
the file into the container by hand at 18:40Z. The fix is a read-only bind of the
checkout's docker/godot directory at /srv, in the compose file, so the declaration
travels with the repository and a plain `up -d` cannot drop it.

This file reads the repository's REAL docker-compose.yml (resolved from this test
file's own location, never a copy of the file) and REFUSES — never skips:

  * a compose file that does not parse, declares no `godot-builder` service, or
    whose `volumes` is not the list compose expects;
  * a service with no volume mapping THIS checkout's docker/godot directory onto
    /srv — a copy kept somewhere else, or a mount at a different target, does not
    count: the container runs /srv/godot_harness.py and that harness imports its
    /srv siblings (game_time_probe.py, clock_sensitivity.py), so a per-file mount
    or a relocated directory would mix a checkout's file with the image's;
  * that mount being writable instead of read-only — the sidecar only renders into
    its own tmp dirs and writes its owner ledger under /var/lib/aitelier-godot, so
    nothing legitimately writes to /srv, and a writable mount would let an
    LLM-driven play-test rewrite the code the gate is measured against.

The baked image copy is deliberately still allowed to exist: it is the fallback for
a container started without this compose file (a bare `docker run`). What this test
refuses is it being the ONLY copy the sidecar can ever run.

It reads the compose file as data, never starts Docker: a disposable test container
has no docker binary, and the property under test is what the file DECLARES.
"""
from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
# Compose resolves a relative bind source against the compose file's own directory.
COMPOSE_DIR = COMPOSE_PATH.parent

SERVICE = "godot-builder"
TARGET = "/srv"
CHECKOUT_SOURCE = "docker/godot"

# The same directory, symlinks resolved, so "./docker/godot" and an absolute path
# naming this checkout are the same mount and a copy elsewhere never is.
CHECKOUT_GODOT = Path(os.path.realpath(REPO_ROOT / CHECKOUT_SOURCE))


@dataclass(frozen=True)
class Mount:
    """One `volumes:` entry, in the shape both compose spellings share."""

    source: str | None
    target: str | None
    read_only: bool
    raw: Any

    def describe(self) -> str:
        return (f"{self.source or '<anonymous>'} -> {self.target or '<no target>'}"
                f"{':ro' if self.read_only else ' (writable)'}")


def _compose() -> dict:
    assert COMPOSE_PATH.is_file(), (
        f"{COMPOSE_PATH} is missing, so the `{SERVICE}` sidecar's {TARGET} mount "
        f"cannot be checked")
    try:
        data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AssertionError(
            f"{COMPOSE_PATH} could not be parsed as YAML, so nothing can assert "
            f"that service `{SERVICE}` maps this checkout's {CHECKOUT_SOURCE} "
            f"directory onto {TARGET}: {exc}") from exc
    assert isinstance(data, dict), (
        f"{COMPOSE_PATH} did not parse to a mapping (got {type(data).__name__}), "
        f"so service `{SERVICE}` and its {TARGET} mount cannot be checked")
    return data


def _godot_builder(data: dict) -> dict:
    """The `godot-builder` service, or an assertion naming it."""
    services = data.get("services") or {}
    assert isinstance(services, dict), (
        f"{COMPOSE_PATH} declares `services` as {type(services).__name__}, not a "
        f"mapping — service `{SERVICE}` cannot be found")
    assert SERVICE in services, (
        f"{COMPOSE_PATH} declares no `{SERVICE}` service — the sidecar that runs "
        f"/srv/godot_harness.py is gone, so the question of whether it serves this "
        f"checkout's {CHECKOUT_SOURCE} directory at {TARGET} has no answer")
    service = services[SERVICE] or {}
    assert isinstance(service, dict), (
        f"`services.{SERVICE}` in {COMPOSE_PATH} is "
        f"{type(service).__name__}, not a mapping")
    return service


def _short_mount(entry: str) -> Mount:
    """`source:target[:options]` — compose's short form."""
    parts = entry.split(":")
    if len(parts) < 2:
        return Mount(source=parts[0] or None, target=None, read_only=False, raw=entry)
    options = parts[2].split(",") if len(parts) > 2 else []
    return Mount(source=parts[0] or None, target=parts[1] or None,
                 read_only="ro" in [option.strip() for option in options], raw=entry)


def _long_mount(entry: dict) -> Mount:
    """`{type: bind, source: …, target: …, read_only: …}` — compose's long form."""
    read_only = entry.get("read_only")
    if isinstance(read_only, str):                     # "true"/"false" spellings
        read_only = read_only.strip().lower() == "true"
    return Mount(source=entry.get("source"), target=entry.get("target"),
                 read_only=bool(read_only), raw=entry)


def _mounts(service: dict) -> list[Mount]:
    entries = service.get("volumes") or []
    assert isinstance(entries, list), (
        f"service `{SERVICE}` in {COMPOSE_PATH} declares `volumes` as "
        f"{type(entries).__name__}, not the list compose expects, so its {TARGET} "
        f"mount cannot be read")
    mounts: list[Mount] = []
    for entry in entries:
        if isinstance(entry, str):
            mounts.append(_short_mount(entry))
        elif isinstance(entry, dict):
            mounts.append(_long_mount(entry))
        else:
            mounts.append(Mount(source=None, target=None, read_only=False, raw=entry))
    return mounts


def _is_checkout_godot(source: str | None) -> bool:
    """Does this bind source resolve to THIS checkout's docker/godot directory?

    A path that would need compose's variable expansion, a named volume, or a copy
    kept anywhere else is not this checkout's directory and answers False.
    """
    if not source or "${" in source or ":" in source:
        return False
    path = Path(source)
    if not path.is_absolute():
        path = COMPOSE_DIR / path
    return Path(os.path.realpath(path)) == CHECKOUT_GODOT


def _is_srv_target(mount: Mount) -> bool:
    return mount.target is not None and posixpath.normpath(mount.target) == TARGET


def _missing_mount_message(service: dict) -> str | None:
    """None when a volume maps this checkout's docker/godot onto /srv, else why not."""
    mounts = _mounts(service)
    if any(_is_srv_target(mount) and _is_checkout_godot(mount.source)
           for mount in mounts):
        return None
    return (f"service `{SERVICE}` in docker-compose.yml declares no volume mapping "
            f"this checkout's {CHECKOUT_SOURCE} directory onto {TARGET}: it mounts "
            f"{[mount.describe() for mount in mounts] or 'nothing'}. The sidecar runs "
            f"{TARGET}/godot_harness.py (and imports its {TARGET} siblings), so "
            f"without that mount a merged harness change reaches the container only "
            f"through an image rebuild or a hand copy, and any recreate silently "
            f"returns to the baked image's bytes. Add `- ./{CHECKOUT_SOURCE}:"
            f"{TARGET}:ro` to its `volumes:`")


def _writable_mount_messages(service: dict) -> list[str]:
    """One refusal per /srv mount of this checkout that is not read-only."""
    return [
        f"service `{SERVICE}` in docker-compose.yml mounts {mount.describe()}: "
        f"{TARGET} is writable inside the sidecar, but nothing there is the "
        f"container's to write — the harness only renders into container-local tmp "
        f"dirs and keeps its owner ledger under /var/lib/aitelier-godot, so a "
        f"writable {TARGET} would let a play-test rewrite the very harness (and the "
        f"probe scripts beside it) the gate is measured against. Append `:ro`"
        for mount in _mounts(service)
        if _is_srv_target(mount) and _is_checkout_godot(mount.source)
        and not mount.read_only
    ]


# ── The real compose file ───────────────────────────────────────────────────

def test_the_compose_file_declares_the_godot_builder_service():
    """A missing service is a refusal, not "nothing to check"."""
    service = _godot_builder(_compose())
    assert isinstance(service.get("volumes"), list), (
        f"service `{SERVICE}` in docker-compose.yml declares no `volumes:` list — "
        f"the {TARGET} mount of this checkout's {CHECKOUT_SOURCE} directory is "
        f"gone with it")


def test_the_godot_builder_mounts_the_checkout_docker_godot_directory_at_srv():
    message = _missing_mount_message(_godot_builder(_compose()))
    assert message is None, message


def test_that_mount_is_read_only():
    service = _godot_builder(_compose())
    # Presence first: the mode of a mount that is not there is not a property.
    missing = _missing_mount_message(service)
    assert missing is None, missing
    problems = _writable_mount_messages(service)
    assert not problems, "\n".join(problems)


# ── The checks must be able to fail, and must name what is wrong ────────────

def _service_with(*volumes: Any) -> dict:
    return {"volumes": list(volumes)}


def test_the_checks_refuse_every_wrong_shape_and_accept_both_explicit_forms():
    """Self-check: the assertions above are exercised on synthetic compose data.

    Each refusal must name the service and the /srv target, so a mutation of the
    real file produces a message that says what is missing instead of a bare
    `assert False`."""
    checkout = str(CHECKOUT_GODOT)

    # Both spellings of the intended declaration are accepted.
    for volumes in (("./docker/godot:/srv:ro",),
                    ("docker/godot:/srv:ro",),
                    (f"{checkout}:{TARGET}:ro",),
                    ({"type": "bind", "source": "./docker/godot", "target": TARGET,
                      "read_only": True},)):
        service = _service_with(*volumes)
        assert _missing_mount_message(service) is None, volumes
        assert _writable_mount_messages(service) == [], volumes

    # Writable, in either spelling, is refused and names the service and /srv.
    for volumes in (("./docker/godot:/srv",),
                    ("./docker/godot:/srv:rw",),
                    ({"type": "bind", "source": "./docker/godot", "target": TARGET},
                     )):
        service = _service_with(*volumes)
        assert _missing_mount_message(service) is None, volumes
        problems = _writable_mount_messages(service)
        assert len(problems) == 1, (volumes, problems)
        assert SERVICE in problems[0] and TARGET in problems[0], problems[0]

    # A copy kept somewhere else, a different target, or no volumes at all is
    # refused as MISSING, and the message names the service and /srv.
    for volumes in (("./elsewhere/docker/godot:/srv:ro",),
                    ("/opt/copied/docker/godot:/srv:ro",),
                    ("./docker/godot:/srv/godot:ro",),
                    ("./docker/godot:/opt/srv:ro",),
                    ("${HOME}/src/docker/godot:/srv:ro",),
                    ("godot_harness:/srv:ro",),
                    ("./docker:/srv:ro",),
                    ()):
        service = _service_with(*volumes)
        message = _missing_mount_message(service)
        assert message is not None, volumes
        assert SERVICE in message and TARGET in message, message
        assert _writable_mount_messages(service) == [], volumes
