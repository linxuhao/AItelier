"""The zvec-grep semantic worker is chosen by docker-compose.yml, and only there.

The measured defect, 2026-11-10. `docker-compose.yml` `services.zvec-grep`
declares:

    image: sha256:4efa1423eb59b0544d8d45828bf93e61189cbbadbe4fdb3ba32d3a2f19837874
    pull_policy: never
    command: ["/bin/sh", "/app/docker/zvec-grep-entrypoint.sh"]

with deliberately NO `build:` stanza, and mounts the checkout read-only at
`.:/app:ro`. That block is the whole deployment split: the npm/zg layers come
from the accepted pinned image, while the entrypoint AND the worker module come
from the mounted checkout. Nothing in the repository read that block. The
existing `tests/unit/test_zvec_worker_source_root.py` EXECUTES
`docker/zvec-grep-entrypoint.sh` and proves the script resolves its worker from
`AITELIER_SOURCE_ROOT`; it never opens `docker-compose.yml`, so it stays green
when compose stops asking that script to run.

What a dropped `command:` line does, measured on this host from the pinned image
itself (`docker image inspect` / `docker run --rm --entrypoint cat`, 2026-11-10):

  * the image `sha256:4efa1423…` was created 2026-10-19T00:35:53Z with
    `Cmd=["/usr/local/bin/zvec-grep-entrypoint.sh"]`, a copy of the entrypoint
    from BEFORE the source-root fix: its worker line is
    `python3 /usr/local/lib/semantic_index_control.py`, with no
    `AITELIER_SOURCE_ROOT` and no `--path-only`;
  * that baked worker's sha256 is
    b3ff158dbd4069d05557d405a0790083277d52a5f7081638a03a12ad1632de46 (32712
    bytes, dated with the image), while the mounted
    `/app/core/semantic_index_control.py` is 35777 bytes and a different file;
  * the running sidecar's PID 1 is
    `docker-entrypoint.sh /bin/sh /app/docker/zvec-grep-entrypoint.sh` — i.e. the
    compose `command:` is exactly what makes the mounted entrypoint (and so the
    mounted worker) run.

So without `command:` the container falls back to the image's own CMD and
executes the BAKED worker copy, silently, on the next plain recreate — and every
existing test still passes. Same for the other three properties: an image
reference without a digest can be re-tagged, `pull_policy` that is merely
defaulted may pull a different build, and a `build:` stanza lets `up --build`
substitute a canonical-source build whose worker differs from the accepted one.

This file reads the repository's REAL `docker-compose.yml` (resolved from this
test file's own location, never a copy of the file, never the caller's cwd) and
refuses a `zvec-grep` service that:

  * does not declare `command` as exactly the two-element list
    `["/bin/sh", "/app/docker/zvec-grep-entrypoint.sh"]`. Only the list form is
    accepted: the string form is NOT equivalent — compose hands a string to the
    image's shell as `sh -c '<string>'`, so the argv split, quoting and any typo
    become shell semantics that the entrypoint's own `set -u` never sees, and a
    mistyped path fails inside the image's shell instead of resolving the
    mounted script (or, worse, the image's `docker-entrypoint.sh` "exec if it
    looks like a command" path runs something else). A list is exec'd verbatim,
    so what the file says is what runs;
  * does not pin `image` to one `sha256:` digest — `^sha256:[0-9a-f]{64}$`. A tag
    such as `aitelier-zvec-grep:latest` fails, and so does a digest WITH a tag
    (`sha256:<64 hex>:latest`), because compose/registry resolution then has a
    second, mutable name for the same reference;
  * does not DECLARE `pull_policy: never`. Absence is a failure, not "the
    default": a missing key means compose may pull, i.e. a missing local image
    can be satisfied by a different build of the same name instead of failing
    closed;
  * declares a `build:` stanza at all, which re-opens `up --build` as a way to
    swap the worker.

The four checks live in ONE helper (`assert_split`) that takes the parsed
service mapping, and the self-check arms feed it deliberately mutated copies of
the real service dict (described arm by arm below), so the assertions cannot be
satisfied by a helper that ignores its argument — and a director who mutates the
real compose file and re-runs this module gets the same message naming the
offending property.

It reads the compose file as data: no network, no Docker calls, no dependence on
the caller's cwd (a disposable test container has no docker binary).
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
SERVICE = "zvec-grep"

# The mounted entrypoint, exec'd as a two-element argv. See the module docstring
# for why the string spelling is not the same declaration.
ENTRYPOINT_COMMAND = ["/bin/sh", "/app/docker/zvec-grep-entrypoint.sh"]
# One digest, nothing else: no tag, no digest-plus-tag, no name.
IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _compose() -> dict:
    """The repository's real docker-compose.yml, parsed, or a refusal."""
    assert COMPOSE_PATH.is_file(), (
        f"{COMPOSE_PATH} is missing, so the `{SERVICE}` deployment split "
        f"(digest-pinned image, pull_policy never, mounted entrypoint, no build "
        f"stanza) cannot be checked")
    try:
        data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AssertionError(
            f"{COMPOSE_PATH} could not be parsed as YAML, so nothing can assert "
            f"how service `{SERVICE}` selects its semantic worker: {exc}") from exc
    assert isinstance(data, dict), (
        f"{COMPOSE_PATH} did not parse to a mapping (got {type(data).__name__}), "
        f"so service `{SERVICE}` cannot be found")
    return data


def _service(data: dict) -> dict:
    """The `zvec-grep` service mapping, or an assertion naming it."""
    services = data.get("services") or {}
    assert isinstance(services, dict), (
        f"{COMPOSE_PATH} declares `services` as {type(services).__name__}, not a "
        f"mapping — service `{SERVICE}` cannot be found")
    assert SERVICE in services, (
        f"{COMPOSE_PATH} declares no `{SERVICE}` service — the sidecar that runs "
        f"the semantic worker is gone, so the deployment split that decides which "
        f"worker copy it executes has no answer")
    service = services[SERVICE] or {}
    assert isinstance(service, dict), (
        f"`services.{SERVICE}` in {COMPOSE_PATH} is {type(service).__name__}, "
        f"not a mapping")
    return service


def assert_split(service: dict) -> None:
    """Refuse a `zvec-grep` service that does not declare the verified split.

    Takes the parsed service mapping, so the same four checks judge the real
    file and every deliberately mutated copy of it. Each refusal names the
    property that is wrong (`command`, `image`, `pull_policy`, `build`) and says
    what the file must declare instead.
    """
    assert isinstance(service, dict), (
        f"service `{SERVICE}` is {type(service).__name__}, not a mapping, so its "
        f"`command`, `image`, `pull_policy` and `build` cannot be read")

    command = service.get("command")
    assert command == ENTRYPOINT_COMMAND, (
        f"service `{SERVICE}` in docker-compose.yml declares `command` as "
        f"{command!r}, not the list {ENTRYPOINT_COMMAND!r}. Without it the "
        f"container falls back to the pinned image's own CMD "
        f"(`/usr/local/bin/zvec-grep-entrypoint.sh`), a pre-source-root copy that "
        f"executes the BAKED worker `python3 /usr/local/lib/"
        f"semantic_index_control.py` instead of the one delivered by the "
        f"read-only `.:/app:ro` mount — a silent reversion no other test sees. "
        f"Declare the list form only: a string would be run by `sh -c`, so argv "
        f"splitting, quoting and any typo become shell semantics and a mistyped "
        f"path silently does not execute the mounted entrypoint")

    image = service.get("image")
    assert isinstance(image, str) and IMAGE_DIGEST.match(image), (
        f"service `{SERVICE}` in docker-compose.yml declares `image` as "
        f"{image!r}, which is not one `sha256:<64 lowercase hex>` digest pin. A "
        f"tag such as `aitelier-zvec-grep:latest` is mutable, so a recreate can "
        f"run a different build than the accepted one; a digest WITH a tag keeps "
        f"a second mutable name for the same reference. Declare only "
        f"`image: sha256:<64 hex>`")

    pull_policy = service.get("pull_policy")
    assert pull_policy == "never", (
        f"service `{SERVICE}` in docker-compose.yml declares `pull_policy` as "
        f"{pull_policy!r}, not 'never'. Absence is a failure here, not a "
        f"default: without the key compose MAY pull, so a missing local image is "
        f"satisfied by whatever that name resolves to instead of failing closed. "
        f"Declare `pull_policy: never`")

    assert "build" not in service, (
        f"service `{SERVICE}` in docker-compose.yml declares a `build` stanza "
        f"(`build: {service.get('build')!r}`). With one, `docker compose up --build` can "
        f"substitute a canonical-source build whose worker differs from the "
        f"accepted digest-pinned one. Remove it and keep the image digest")


def _real_service() -> dict:
    """The real `zvec-grep` service as this repository declares it."""
    return _service(_compose())


def _mutated(**changes: Any) -> dict:
    """A deep copy of the real service with `changes` applied.

    Keyword `None` drops the key (`command=None` removes `command`); any other
    value replaces it. Starting from the REAL mapping is what makes each arm a
    statement about this file: only the named property differs, so a refusal can
    only come from the check that owns it.
    """
    service = copy.deepcopy(_real_service())
    for key, value in changes.items():
        if value is None:
            service.pop(key, None)
        else:
            service[key] = copy.deepcopy(value)
    return service


# ── The real file ───────────────────────────────────────────────────────────

def test_the_compose_file_pins_the_verified_zvec_grep_split():
    """The real docker-compose.yml passes all four checks (the green arm)."""
    service = _real_service()
    # State what the arms below are about, from the real mapping, so a reader of
    # a failure knows which half of the split moved.
    assert service.get("command") == ENTRYPOINT_COMMAND, service.get("command")
    assert_split(service)


# ── The checks must be able to fail, and must name what is wrong ────────────

def test_dropping_the_command_reverts_the_worker_to_the_baked_copy():
    """Mutation 1: `command` deleted from the real service.

    That is the silent reversion — compose then uses the pinned image's CMD and
    runs the baked worker. The refusal must name `command`."""
    service = _mutated(command=None)
    assert "command" not in service, service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    message = str(refusal.value)
    assert "`command`" in message, message
    assert "/app/docker/zvec-grep-entrypoint.sh" in message, message


def test_a_string_command_is_not_the_list_form():
    """Mutation 1b: `command` replaced by the equivalent-looking STRING.

    Compose runs a string through `sh -c`, so this spelling is not the same
    declaration even when it names the right script; the refusal must name
    `command` and say why the list form is required."""
    service = _mutated(command=" ".join(ENTRYPOINT_COMMAND))
    assert isinstance(service["command"], str), service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    message = str(refusal.value)
    assert "`command`" in message, message
    assert "sh -c" in message, message


def test_a_tagged_image_is_refused():
    """Mutation 2: `image` replaced by the tag `aitelier-zvec-grep:latest`.

    A tag is mutable, so a recreate may run a different build. The refusal must
    name `image`."""
    service = _mutated(image="aitelier-zvec-grep:latest")
    assert service["image"] == "aitelier-zvec-grep:latest", service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    message = str(refusal.value)
    assert "`image`" in message, message
    assert "sha256:" in message, message


def test_a_digest_with_a_tag_is_refused():
    """Mutation 2b: the real digest with `:latest` appended.

    A digest plus a tag keeps a second mutable name for the same reference, so
    the pin is not a pin. The refusal must name `image`."""
    service = _mutated(image=f"{_real_service()['image']}:latest")
    assert service["image"].endswith(":latest"), service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    assert "`image`" in str(refusal.value), str(refusal.value)


def test_a_missing_pull_policy_is_a_failure_not_a_default():
    """Mutation 3: `pull_policy` deleted from the real service.

    Absence must fail — treated as "may pull" — rather than being read as
    compose's default. The refusal must name `pull_policy`."""
    service = _mutated(pull_policy=None)
    assert "pull_policy" not in service, service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    message = str(refusal.value)
    assert "`pull_policy`" in message, message
    assert "never" in message, message


def test_a_build_stanza_is_refused():
    """Mutation 4: `build: {}` added to the real service.

    A build stanza re-opens `up --build` as a way to swap the worker for a
    canonical-source build. The refusal must name `build`."""
    service = _mutated(build={})
    assert "build" in service, service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    message = str(refusal.value)
    assert "`build`" in message, message
    assert "digest" in message, message


def test_each_mutation_is_refused_by_the_check_that_owns_it():
    """The four single-property mutations, one table, each message exclusive.

    A refusal that named every property would pass the arms above while being
    unable to say what is wrong, and a helper that ignored its argument could
    not refuse all four at once. This arm asserts the partition: each mutation's
    message names its OWN property and none of the other three check-owned
    tokens."""
    mutations = {
        "command": ({"command": None}, "`pull_policy`", "`image`", "`build`"),
        "image": ({"image": "aitelier-zvec-grep:latest"},
                  "`command`", "`pull_policy`", "`build`"),
        "pull_policy": ({"pull_policy": None}, "`command`", "`image`", "`build`"),
        "build": ({"build": {}}, "`command`", "`image`", "`pull_policy`"),
    }
    for property_name, (changes, *others) in mutations.items():
        with pytest.raises(AssertionError) as refusal:
            assert_split(_mutated(**changes))
        message = str(refusal.value)
        assert f"`{property_name}`" in message, (property_name, message)
        for other in others:
            assert other not in message, (property_name, other, message)
