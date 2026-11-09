"""The sidecar must serve the CHECKOUT's harness, not only the one baked into its image.

Dockerfile.godot COPYs docker/godot/*.py to /srv at build time and the container's
CMD is `python3 /srv/godot_harness.py --serve` (docker-compose.yml `godot-builder`
service, container aitelier-godot). Before commit 1726456def the compose file declared
no /srv mount for `godot-builder`, so those COPY lines were the only source of /srv,
and:

  * merging a change to docker/godot/godot_harness.py into main changed nothing the
    sidecar EXECUTED until somebody rebuilt the image or hand-copied the file into
    the running container, and
  * any ordinary recreate (`docker compose up -d`, or the host rebooting and the
    container coming back) silently returned to whatever bytes the image baked.

The image aitelier-godot (created 2026-11-08T16:03:58Z) bakes the
docker/godot/godot_harness.py of commit db8c97855724 (committed 2026-11-08T15:51:41Z;
sha256 157aff350699ccee681ef179ba17eb0a6d22716b6a2423c0019f4f764276749b). Commit
20c31f4fa516 (committed 2026-11-08T16:31:46Z, after that image) is not what the image
holds, so the image cannot have been built from it. The fix is a read-only bind of the
checkout's docker/godot directory at /srv, in the compose file, so the declaration
travels with the repository and a plain `up -d` cannot drop it.

This file reads the repository's REAL docker-compose.yml (resolved from this test
file's own location, never a copy of the file) and REFUSES — never skips:

  * a compose file that does not parse, declares no `godot-builder` service, or
    whose `volumes` is not the list compose expects;
  * a service with no volume mapping THIS checkout's docker/godot directory onto
    /srv — a copy kept somewhere else, or a mount at a different target, does not
    count: the container runs /srv/godot_harness.py directly, and the sibling probes
    game_time_probe.py and clock_sensitivity.py load that same path by file
    (importlib, GODOT_HARNESS_PY defaulting to /srv/godot_harness.py), so mounting
    the whole directory keeps every file in /srv from one checkout; a per-file mount
    or a relocated directory would mix a checkout's file with the image's;
  * that mount being writable instead of read-only — the sidecar only renders into
    its own tmp dirs and writes its owner ledger under /var/lib/aitelier-godot, so
    nothing legitimately writes to /srv, and a writable mount would let an
    LLM-driven play-test rewrite the code the gate is measured against;
  * that service mounting ANYTHING ELSE at or under /srv — a per-file shadow
    (`/tmp/shadow/godot_harness.py:/srv/godot_harness.py`) or a sibling
    (`…:/srv/clock_sensitivity.py:ro`). Both shapes passed on 2026-11-09 before
    this refusal existed: the directory mount above was present and read-only
    exactly as intended, and one file inside /srv was still served from somewhere
    else. The container runs /srv/godot_harness.py directly, and the two probe scripts
    beside it load that same path by file, so a shadow at either destination makes the
    executed harness a mixture of this checkout and bytes no repository holds — the
    mount declaration is then true about the directory and false about the files the
    gate is measured against.

Declaring the mount is also not the same as the serving user being able to READ it.
`user: "${AITELIER_UID:-1000}:${AITELIER_GID:-1000}"` runs the sidecar as uid/gid
1000, and measured on this host 2026-11-09: `docker/godot/godot_harness.py` is mode
0600 owned by uid 1000 gid 1000 (same for `game_time_probe.py` and
`clock_sensitivity.py`), and `docker/godot/` itself is a 0700 directory 1000:1000 —
so uid 1000 may read them through the owner bits. A 0600 file owned by ROOT at the
same path would be mounted just as successfully (compose cannot see modes) and the
sidecar would then die at import, having served nothing. So this module MEASURES,
from the mounted tree's own bytes, that the uid/gid the service runs as can read
every file (and traverse every directory) under docker/godot, and refuses
otherwise. The uid/gid are resolved the way compose resolves them: `${VAR:-default}`
from the process environment, else the declared default, and a `user:` that does not
spell out both numbers is refused rather than assumed. Not measured, and not claimed:
a bind mount's access-control layers beyond POSIX mode bits (SELinux labels, ACLs).

The baked image copy is deliberately still allowed to exist: it is the fallback for
a container started without this compose file (a bare `docker run`). What this test
refuses is it being the ONLY copy the sidecar can ever run.

It reads the compose file as data and the mounted tree's modes from disk, never
starts Docker: a disposable test container has no docker binary, and the properties
under test are what the file DECLARES about /srv plus whether the uid it names can
actually open what it mounts.
"""
from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
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


def _normalised_target(target: str) -> str:
    """A container path with `//`, `.` and `..` folded away, still absolute."""
    normalised = posixpath.normpath(target)
    # POSIX normpath keeps a leading `//` (an implementation-defined root); compose
    # passes it to the runtime, which resolves it to `/`, so fold it here too.
    return "/" + normalised.lstrip("/") if normalised.startswith("/") else normalised


def _is_srv_target(mount: Mount) -> bool:
    return mount.target is not None and _normalised_target(mount.target) == TARGET


def _shadowed_srv_path(mount: Mount) -> str | None:
    """The path this mount puts UNDER /srv, or None when it is not under it.

    `/srv` itself is the directory mount the tests above own: a mount at exactly
    /srv is judged by `_missing_mount_message` / `_writable_mount_messages`, not
    here. Anything deeper — /srv/godot_harness.py, /srv/clock_sensitivity.py —
    lands inside that directory and replaces one entry of it.
    """
    if mount.target is None:
        return None
    target = _normalised_target(mount.target)
    if target.startswith(TARGET + "/"):
        return target
    return None


def _missing_mount_message(service: dict) -> str | None:
    """None when a volume maps this checkout's docker/godot onto /srv, else why not."""
    mounts = _mounts(service)
    if any(_is_srv_target(mount) and _is_checkout_godot(mount.source)
           for mount in mounts):
        return None
    return (f"service `{SERVICE}` in docker-compose.yml declares no volume mapping "
            f"this checkout's {CHECKOUT_SOURCE} directory onto {TARGET}: it mounts "
            f"{[mount.describe() for mount in mounts] or 'nothing'}. The sidecar runs "
            f"{TARGET}/godot_harness.py directly, and its probe scripts load that "
            f"same path by file, so "
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


# ── Nothing else may live under /srv ────────────────────────────────────────

def _srv_shadow_messages(service: dict) -> list[str]:
    """One refusal per volume whose destination is inside /srv, below /srv itself.

    A per-file mount (`…/godot_harness.py:/srv/godot_harness.py`, or the same
    read-only) does not remove the directory mount and is invisible to the checks
    above: they ask whether the intended mount EXISTS, and it does. But docker
    applies the more specific destination last, so that one file is served from
    outside this checkout while its siblings still come from it — the sidecar then
    runs a harness this repository does not contain, and the gate measures the
    bytes nobody reviewed.
    """
    return [
        f"service `{SERVICE}` in docker-compose.yml also mounts "
        f"{mount.describe()}: {_shadowed_srv_path(mount)} is inside {TARGET}, so it "
        f"shadows an entry of the checkout's {CHECKOUT_SOURCE} directory that the "
        f"sidecar must serve. Docker resolves the more specific destination last, so "
        f"that path alone would come from {mount.source or '<anonymous>'} while its "
        f"{TARGET} siblings still come from this checkout — the container would run "
        f"a mixture of the repository and bytes no repository holds, and the gate "
        f"would be measured against code a recreate can silently change. Mount only "
        f"./{CHECKOUT_SOURCE}:{TARGET}:ro, and nothing else at or under {TARGET}"
        for mount in _mounts(service)
        if _shadowed_srv_path(mount) is not None
    ]


# ── The serving uid must be able to READ what it mounts ─────────────────────
#
# Declaring the mount says which directory the sidecar sees, not that the process
# can open the files: a root-owned 0600 file at docker/godot/godot_harness.py is
# mounted just as successfully and then fails at import, with the sidecar serving
# nothing. These read the modes from the mounted tree itself, and the uid/gid from
# the service's own `user:` line.

_COMPOSE_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: str) -> str | None:
    """Compose's `${VAR}` / `${VAR:-default}` against this process's environment.

    None when the value needs a variable that is neither set nor defaulted: the
    number then depends on the shell that runs `docker compose up`, which is not a
    property this test can read.
    """
    unresolved = False

    def substitute(match: re.Match[str]) -> str:
        nonlocal unresolved
        name, default = match.group(1), match.group(2)
        from_env = os.environ.get(name)
        if from_env:
            return from_env
        if default is not None:
            return default
        unresolved = True
        return ""

    expanded = _COMPOSE_VAR.sub(substitute, value).strip()
    return None if unresolved else expanded


def _service_uid_gid(service: dict) -> tuple[int, int]:
    """The uid/gid the sidecar's process runs as, from its own `user:`.

    Refuses rather than assumes: an absent `user:` means the IMAGE's default, and a
    lone uid leaves the gid to the image's /etc/passwd — in both cases the two
    numbers the readability check needs are not in this file.
    """
    raw = service.get("user")
    assert raw is not None, (
        f"service `{SERVICE}` in docker-compose.yml declares no `user:`, so the "
        f"uid/gid the sidecar runs as (the image's default) cannot be read from the "
        f"compose file, and whether that user can read the {TARGET} files it mounts "
        f"cannot be measured. Declare `user: \"<uid>:<gid>\"`")
    expanded = _expand(str(raw))
    assert expanded is not None, (
        f"`services.{SERVICE}.user` in docker-compose.yml is {raw!r}, which needs a "
        f"variable neither set here nor defaulted, so the serving uid/gid cannot be "
        f"resolved. Give the numbers, or a `:-<default>`")
    uid_text, separator, gid_text = expanded.partition(":")
    assert separator and uid_text.isdigit() and gid_text.isdigit(), (
        f"`services.{SERVICE}.user` in docker-compose.yml resolves to {expanded!r}, "
        f"not `<uid>:<gid>` with both numbers — the readability of the {TARGET} "
        f"files cannot be measured against a uid or gid taken from the image")
    return int(uid_text), int(gid_text)


def _permission_class(mode: int, file_uid: int, file_gid: int,
                      uid: int, gid: int) -> int:
    """The rwx bits POSIX applies for this uid/gid — exactly one class.

    Owner bits when the uids match, else group bits when the gids match, else the
    other bits; the classes never combine, so a 0600 file owned by root grants a
    uid-1000 process nothing (its group and other triplets are both empty).
    """
    if file_uid == uid:
        return (mode >> 6) & 0o7
    if file_gid == gid:
        return (mode >> 3) & 0o7
    return mode & 0o7


def _unreadable_harness_messages(service: dict,
                                 directory: Path = CHECKOUT_GODOT) -> list[str]:
    """One refusal per entry under docker/godot the serving uid/gid cannot use.

    `directory` defaults to this checkout's real docker/godot; the self-check
    points it at a throwaway tree so the mode/ownership arithmetic is exercised on
    real stat() output rather than only on made-up numbers.
    """
    uid, gid = _service_uid_gid(service)
    assert directory.is_dir(), (
        f"{directory} is not a directory, so the {TARGET} mount of "
        f"{CHECKOUT_SOURCE} has nothing to serve")
    problems: list[str] = []
    for entry in (directory, *sorted(directory.rglob("*"))):
        try:
            shown = str(entry.relative_to(REPO_ROOT))
        except ValueError:
            shown = str(entry)
        if entry.is_symlink():
            resolved = Path(os.path.realpath(entry))
            if resolved != directory and directory not in resolved.parents:
                problems.append(
                    f"{shown} is a symlink to {resolved}, "
                    f"which is outside the directory mounted at {TARGET}: inside the "
                    f"sidecar that link resolves against the container's own "
                    f"filesystem, not this checkout. Replace it with the file")
                continue
        stated = entry.stat()
        klass = _permission_class(stated.st_mode & 0o777, stated.st_uid,
                                  stated.st_gid, uid, gid)
        needed = 0o5 if entry.is_dir() else 0o4          # r-x for dirs, r for files
        which = ("owner" if stated.st_uid == uid
                 else "group" if stated.st_gid == gid else "other")
        if klass & needed != needed:
            problems.append(
                f"{shown} is mode {stated.st_mode & 0o777:04o} owned by uid "
                f"{stated.st_uid} gid {stated.st_gid}, and service `{SERVICE}` in "
                f"docker-compose.yml runs as uid {uid} gid {gid}: that uid gets the "
                f"{which} bits {klass:o}, so it cannot "
                f"{'traverse (r-x)' if entry.is_dir() else 'read'} it. Docker mounts "
                f"it anyway and the sidecar fails when it opens the path inside "
                f"{TARGET} — the mount would be declared correct and serve nothing. "
                f"Make it readable by uid {uid} (chmod o+r, or o+rx on a directory)")
    return problems


# ── The real compose file ───────────────────────────────────────────────────

def test_the_compose_file_declares_the_godot_builder_service():
    """A missing service is a refusal, not "nothing to check"."""
    service = _godot_builder(_compose())
    assert isinstance(service.get("volumes"), list), (
        f"service `{SERVICE}` in docker-compose.yml declares no `volumes:` list — "
        f"the {TARGET} mount of this checkout's {CHECKOUT_SOURCE} directory is "
        f"gone with it")


def test_the_godot_builder_mounts_the_checkout_docker_godot_directory_at_srv():
    service = _godot_builder(_compose())
    message = _missing_mount_message(service)
    assert message is None, message
    # A file mounted INSIDE /srv leaves this mount present, so the mount existing
    # is not enough: what is at /srv must be this checkout's directory alone.
    shadows = _srv_shadow_messages(service)
    assert not shadows, "\n".join(shadows)


def test_no_other_volume_puts_anything_at_or_under_srv():
    problems = _srv_shadow_messages(_godot_builder(_compose()))
    assert not problems, "\n".join(problems)


def test_the_sidecar_can_read_every_file_it_mounts_at_srv():
    """The mounted tree's own modes, against the uid/gid the service runs as."""
    problems = _unreadable_harness_messages(_godot_builder(_compose()))
    assert not problems, "\n".join(problems)


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


def test_the_checks_refuse_a_shadow_at_or_under_srv():
    """Self-check: a per-file mount INSIDE a correct /srv mount is refused.

    This is the shape that passed on 2026-11-09: the directory mount below is
    present and read-only, and one file inside /srv is served from somewhere else.
    Each refusal must name the service and the shadowed path, so a mutation of the
    real compose file says which path was taken over."""
    intended = f"./{CHECKOUT_SOURCE}:{TARGET}:ro"

    # The intended declaration, mounts elsewhere, and the same directory named
    # absolutely or in long form are not shadows.
    for volumes in ((intended,),
                    (intended, "/tmp/state:/var/lib/aitelier-godot"),
                    (intended,
                     "${HOME}/.AItelier/godot-control:/var/lib/aitelier-godot"),
                    (intended, "./docker:/app:ro"),
                    (f"{CHECKOUT_GODOT}:{TARGET}:ro",),
                    ({"type": "bind", "source": f"./{CHECKOUT_SOURCE}",
                      "target": TARGET, "read_only": True},)):
        assert _srv_shadow_messages(_service_with(*volumes)) == [], volumes

    # A destination at or under /srv, other than /srv itself, is a shadow — read
    # only or writable, short or long form, and however the path is spelled.
    for volumes, shadowed in (
            ((intended, "/tmp/shadow/godot_harness.py:/srv/godot_harness.py"),
             "/srv/godot_harness.py"),
            ((intended, "/tmp/shadow/x.py:/srv/clock_sensitivity.py:ro"),
             "/srv/clock_sensitivity.py"),
            ((intended, {"type": "bind", "source": "/tmp/shadow/x.py",
                         "target": "/srv/game_time_probe.py", "read_only": True}),
             "/srv/game_time_probe.py"),
            ((intended, "/tmp/shadow/x.py:/srv//game_time_probe.py:ro"),
             "/srv/game_time_probe.py"),
            ((intended, "/tmp/shadow/x.py:/srv/./clock_sensitivity.py:ro"),
             "/srv/clock_sensitivity.py"),
            ((intended, "/tmp/shadow/x.py:/srv/../srv/godot_harness.py:ro"),
             "/srv/godot_harness.py"),
            ((intended, "godot_harness:/srv/godot_harness.py"),
             "/srv/godot_harness.py")):
        service = _service_with(*volumes)
        # The directory mount is still exactly right — which is why the shadow
        # needs its own check rather than riding on the ones above.
        assert _missing_mount_message(service) is None, volumes
        assert _writable_mount_messages(service) == [], volumes
        problems = _srv_shadow_messages(service)
        assert len(problems) == 1, (volumes, problems)
        assert SERVICE in problems[0] and shadowed in problems[0], problems[0]


def test_the_checks_read_the_serving_uid_and_refuse_modes_it_cannot_read(tmp_path):
    """Self-check: modes and ownership decide the verdict, not a file existing.

    Measured on this host 2026-11-09: `docker/godot/*.py` are mode 0600 owned by uid
    1000 gid 1000 and `docker/godot` is a 0700 directory 1000:1000, while the
    godot-builder service runs `user: "${AITELIER_UID:-1000}:${AITELIER_GID:-1000}"`
    — so uid 1000 reads them through the OWNER bits. The mutation is the same 0600
    file owned by root: uid 1000 then matches no class that grants anything, and the
    refusal must name the file and both uid numbers."""
    # uid/gid are read from the service, resolved like compose resolves them.
    assert _service_uid_gid(
        {"user": "${AITELIER_TEST_ONLY_UID:-1000}:${AITELIER_TEST_ONLY_GID:-1000}"}
    ) == (1000, 1000)
    assert _service_uid_gid({"user": "1000:1000"}) == (1000, 1000)
    for unusable in ({}, {"user": "1000"}, {"user": "app"},
                     {"user": "${AITELIER_TEST_ONLY_UID}"}):
        with pytest.raises(AssertionError) as refusal:
            _service_uid_gid(unusable)
        assert SERVICE in str(refusal.value), (unusable, refusal.value)

    # POSIX applies exactly one class: owner, else group, else other.
    assert _permission_class(0o600, 1000, 1000, 1000, 1000) == 0o6
    assert _permission_class(0o600, 0, 0, 1000, 1000) == 0o0      # root-owned 0600
    assert _permission_class(0o640, 0, 1000, 1000, 1000) == 0o4   # group bits only
    assert _permission_class(0o604, 0, 0, 1000, 1000) == 0o4      # other bits only
    assert _permission_class(0o700, 1000, 1000, 1000, 1000) == 0o7
    assert _permission_class(0o644, 0, 0, 0, 0) == 0o6            # owner wins

    tree = tmp_path / "godot"
    tree.mkdir()
    # Pinned, not inherited from the umask: the yes/no answer below must come from
    # the mode this check reads, not from how the invoking shell was configured.
    tree.chmod(0o755)
    harness = tree / "godot_harness.py"
    harness.write_text("print('harness')\n", encoding="utf-8")
    harness.chmod(0o600)
    owner = harness.stat()
    serving_uid = 1000 if owner.st_uid != 1000 else 1001     # never the owner
    serving = {"user": f"{serving_uid}:{owner.st_gid}"}

    # The same bytes and mode, owned by the uid the sidecar runs as, are accepted.
    assert _unreadable_harness_messages({"user": f"{owner.st_uid}:{owner.st_gid}"},
                                       directory=tree) == []
    # Owned by somebody else with only owner bits, they are refused, and the
    # refusal names the file, its mode, its owner and the serving uid.
    problems = _unreadable_harness_messages(serving, directory=tree)
    assert len(problems) == 1, problems
    assert "godot_harness.py" in problems[0], problems[0]
    assert "0600" in problems[0] and str(owner.st_uid) in problems[0], problems[0]
    assert str(serving_uid) in problems[0], problems[0]

    # A directory the serving uid cannot traverse is refused too, even when every
    # file in it is world-readable: docker mounts it and the sidecar cannot reach
    # the file inside.
    tree.chmod(0o700)
    harness.chmod(0o644)
    problems = _unreadable_harness_messages(serving, directory=tree)
    assert len(problems) == 1 and "godot" in problems[0], problems
    assert "0700" in problems[0], problems[0]
