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
  * does not pin `image` to exactly one `sha256:` digest, matched with
    `fullmatch` against `sha256:[0-9a-f]{64}` (nothing before or after it). A tag
    such as `aitelier-zvec-grep:latest` fails, and so does a digest WITH a tag
    (`sha256:<64 hex>:latest`), because compose/registry resolution then has a
    second, mutable name for the same reference;
  * does not DECLARE `pull_policy: never`. Absence is a failure, not "the
    default": a missing key means compose may pull, i.e. a missing local image
    can be satisfied by a different build of the same name instead of failing
    closed;
  * declares a `build:` stanza at all, which re-opens `up --build` as a way to
    swap the worker;
  * declares an `extends` key at all. Compose merges the extended service into
    this one BEFORE anything starts, and a YAML-only test cannot resolve that
    merge, so the raw stanza can read pinned while the effective service inherits
    a `build:` stanza — measured 2026-11-10: `extends: godot-builder` inherited
    `build: {context: ., dockerfile: Dockerfile.godot}` (verified with `docker
    compose config`), so `up --build` would build the worker from a
    canonical-source build instead of running the accepted digest-pinned image.
    Refusing the key is the honest check: this test reads text, not compose's
    merge result.

The five checks live in ONE helper (`assert_split`) that takes the parsed
service mapping, and the self-check arms feed it deliberately mutated copies of
the real service dict (described arm by arm below), so the assertions cannot be
satisfied by a helper that ignores its argument — and a director who mutates the
real compose file and re-runs this module gets the same message naming the
offending property.

Two of the properties are decided by how the FILE is read, not by `assert_split`,
and both used to be vacuous (measured 2026-11-10, on the real file):

  * `image` was matched with `^…$` + `re.match`. `$` also matches BEFORE a final
    `\\n`, so `"sha256:<64 hex>\\n"` satisfied a check whose own message claims the
    value is one digest pin. That byte is not decoration: `docker compose config`
    keeps it and renders the image as a literal block scalar (`image: |`,
    measured 2026-11-10, exit 0), i.e. the file declares a reference string
    distinct from the accepted digest and nothing said so. The check is now
    `fullmatch`.
  * the file was read with `yaml.safe_load`, which keeps the LAST of two equal
    mapping keys and silently drops the rest. The real `zvec-grep` service
    carried `pull_policy: always` and then `pull_policy: never` and this module
    stayed green, while `docker compose config` REFUSES that file (`mapping key
    "pull_policy" already defined`). The file is now parsed with
    `aitelier.strict_yaml.load_yaml_strict` — the repository's one
    duplicate-key-refusing reader (`tests/unit/test_strict_yaml.py` pins it) —
    so a repeated key is a refusal here too, and the merge key (`<<`) keeps its
    legal semantics: an anchor that supplies `build:` is still surfaced, and
    still refused, exactly as before.

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

from aitelier.strict_yaml import DuplicateKeyError, load_yaml_strict

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
SERVICE = "zvec-grep"

# The mounted entrypoint, exec'd as a two-element argv. See the module docstring
# for why the string spelling is not the same declaration.
ENTRYPOINT_COMMAND = ["/bin/sh", "/app/docker/zvec-grep-entrypoint.sh"]
# One digest, nothing else: no tag, no digest-plus-tag, no name, no trailing
# newline. Deliberately NOT anchored with `^…$` — matched with `fullmatch`,
# because `$` also matches before a final newline and a newline-tailed digest
# then passed this pin check.
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def _parse_compose(text: str, source: Any) -> dict:
    """Parse compose text with the repository's duplicate-refusing reader.

    `aitelier.strict_yaml.load_yaml_strict` is the one place the repo refuses a
    repeated mapping key (its own tests/unit/test_strict_yaml.py pins anchors,
    `<<` overrides and stock-load equivalence). Using it here rather than a
    loader of our own is deliberate: it judges the mapping AS AUTHORED, captured
    while the document is composed, because `SafeConstructor.flatten_mapping`
    rewrites merged nodes IN PLACE — a construction-time scan of `node.value`
    rejects legal YAML (that regression is recorded in test_strict_yaml.py).
    Nothing else about parsing changes.
    """
    try:
        return load_yaml_strict(text, source=str(source))
    except DuplicateKeyError as exc:
        raise AssertionError(
            f"{source} declares a mapping key more than once: {exc} "
            f"`docker compose config` REFUSES such a file (`mapping key "
            f"\"<key>\" already defined`), while PyYAML keeps only the LAST "
            f"value — so a `yaml.safe_load`-based test reads a file the "
            f"deployment cannot even start from, and the duplicate is invisible "
            f"here. Remove one of them and keep the single declaration the split "
            f"requires") from exc
    except yaml.YAMLError as exc:
        raise AssertionError(
            f"{source} could not be parsed as YAML, so nothing can assert "
            f"how service `{SERVICE}` selects its semantic worker: {exc}") from exc


def _compose() -> dict:
    """The repository's real docker-compose.yml, parsed, or a refusal."""
    assert COMPOSE_PATH.is_file(), (
        f"{COMPOSE_PATH} is missing, so the `{SERVICE}` deployment split "
        f"(digest-pinned image, pull_policy never, mounted entrypoint, no build "
        f"stanza) cannot be checked")
    data = _parse_compose(COMPOSE_PATH.read_text(encoding="utf-8"), COMPOSE_PATH)
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

    Takes the parsed service mapping, so the same five checks judge the real
    file and every deliberately mutated copy of it. Each refusal names the
    property that is wrong (`command`, `image`, `pull_policy`, `build`,
    `extends`) and says what the file must declare instead.

    `extends` is refused FIRST and on its own: it can contribute any of the four
    properties below to the EFFECTIVE service, which this function never sees, so
    the raw mapping cannot be trusted to have said what runs.
    """
    assert isinstance(service, dict), (
        f"service `{SERVICE}` is {type(service).__name__}, not a mapping, so its "
        f"`command`, `image`, `pull_policy`, `build` and extends key cannot be "
        f"read")

    assert "extends" not in service, (
        f"service `{SERVICE}` in docker-compose.yml declares `extends` as "
        f"{service.get('extends')!r}. Compose merges the extended service into "
        f"this one before anything starts, and a YAML-only test cannot resolve "
        f"that merge: measured 2026-11-10, an extends of godot-builder left this "
        f"stanza reading pinned while the effective service inherited a build "
        f"stanza (context `.`, dockerfile Dockerfile.godot), so an up --build "
        f"would build the worker from a canonical-source build instead of running "
        f"the accepted digest-pinned image. Spell the service out in full and "
        f"remove the extends key")

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
    assert isinstance(image, str) and IMAGE_DIGEST.fullmatch(image), (
        f"service `{SERVICE}` in docker-compose.yml declares `image` as "
        f"{image!r}, which is not one `sha256:<64 lowercase hex>` digest pin. A "
        f"tag such as `aitelier-zvec-grep:latest` is mutable, so a recreate can "
        f"run a different build than the accepted one; a digest WITH a tag keeps "
        f"a second mutable name for the same reference; and anything AROUND the "
        f"digest (a trailing newline included) is a different reference string. "
        f"Declare only `image: sha256:<64 hex>`")

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
    """The real docker-compose.yml passes all five checks (the green arm)."""
    service = _real_service()
    # State what the arms below are about, from the real mapping, so a reader of
    # a failure knows which half of the split moved. A witness, not the check:
    # `assert_split` owns the refusal and says what to declare — but this line
    # fires FIRST when the director deletes `command:` from the real file, so it
    # names the property instead of reporting a bare `None`.
    assert service.get("command") == ENTRYPOINT_COMMAND, (
        f"`command` on `{SERVICE}` in docker-compose.yml is "
        f"{service.get('command')!r}, not {ENTRYPOINT_COMMAND!r} — the mounted "
        f"entrypoint would not run")
    assert_split(service)


# ── How the file is READ (the two text-level properties) ────────────────────

# A document whose service merges an anchored `build:` in through `<<`. Compose
# honours that merge; flattening it is `SafeLoader`'s job, not this module's, so
# the arm below only proves the merge is still VISIBLE after the stricter parse.
_MERGE_KEY_COMPOSE = """\
x-baked: &bb
  build:
    context: .
    dockerfile: Dockerfile.godot
services:
  zvec-grep:
    image: sha256:%s
    pull_policy: never
    command: ["/bin/sh", "/app/docker/zvec-grep-entrypoint.sh"]
    <<: *bb
"""


def test_the_loader_refuses_a_duplicate_mapping_key():
    """A repeated key is refused, not silently resolved to the last value.

    Measured 2026-11-10 on the REAL file: `services.zvec-grep` declared
    `pull_policy: always` and then `pull_policy: never`. `yaml.safe_load` keeps
    the last one, so every check below read `never` and this module was green —
    while `docker compose config` refuses that file outright
    (`mapping key "pull_policy" already defined`), i.e. the deployment it
    describes cannot start at all. The first assertion pins the vacuous shape;
    the loader must refuse it by name.
    """
    text = (
        "services:\n"
        "  zvec-grep:\n"
        f"    image: sha256:{'0' * 64}\n"
        "    pull_policy: always\n"
        "    pull_policy: never\n"
    )
    # What `yaml.safe_load` (the old reading) does with it: the duplicate
    # disappears and the surviving value is the permissive-looking `never`.
    assert yaml.safe_load(text)["services"]["zvec-grep"]["pull_policy"] == "never"

    with pytest.raises(AssertionError) as refusal:
        _parse_compose(text, "a compose document with a duplicated key")
    message = str(refusal.value)
    # The shared reader names the key and BOTH lines; the wrapper says why a
    # duplicate matters even though compose's own error is the authority.
    assert "pull_policy" in message, message
    assert "line 5" in message and "line 4" in message, message
    assert "already defined" in message, message

    # The real file must still parse: the reader is a strictness ADDITION, and
    # the green arm above re-reads it on every run.
    assert _real_service().get("pull_policy") == "never", (
        "the real docker-compose.yml must still parse with the strict reader")


def test_the_loader_reads_the_real_file_exactly_as_safe_load_does():
    """Stricter about duplicates, identical otherwise.

    Anchors, aliases, `<<` merges and block scalars must mean what they always
    meant — otherwise the duplicate check would have changed what the assertions
    judge. One comparison over the REAL file says so.
    """
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    assert _parse_compose(text, COMPOSE_PATH) == yaml.safe_load(text)


def test_a_merge_key_build_is_still_surfaced_and_refused():
    """`<<: *bb` supplying `build:` stays RED for the right reason.

    This is the arm that says the duplicate check did not cost merge-key
    semantics: the anchor's build stanza is flattened in, so `assert_split` sees
    `build` and refuses it. A loader that skipped or dropped the merge would
    leave the service looking pinned.
    """
    text = _MERGE_KEY_COMPOSE % ("0" * 64)
    service = _parse_compose(text, "a compose document merging build: via <<")
    service = service["services"][SERVICE]
    assert service["build"] == {"context": ".", "dockerfile": "Dockerfile.godot"}, service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    message = str(refusal.value)
    assert "`build`" in message, message
    assert "Dockerfile.godot" in message, message


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


def test_a_digest_with_a_trailing_newline_is_refused():
    """Mutation 2c: the real digest with a trailing newline appended.

    Measured 2026-11-10: `image` was checked with `^…$` + `re.match`, and `$`
    matches BEFORE a final newline — so `"sha256:<64 hex>\\n"` satisfied a check
    whose message claims the value is one digest pin. The shape is not exotic: a
    YAML block scalar, a copy/paste or a templating edit produces exactly it, and
    `docker compose config` then renders the image as a literal block scalar
    carrying that newline (measured), i.e. a declared reference distinct from
    the accepted digest. The check is now `fullmatch`; the refusal must name
    `image`.
    """
    service = _mutated(image=f"{_real_service()['image']}\n")
    assert service["image"].endswith("\n"), service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    message = str(refusal.value)
    assert "`image`" in message, message
    assert "sha256:" in message, message


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


def test_an_extends_key_is_refused_because_it_hides_an_inherited_build():
    """Mutation 5: `extends: godot-builder` added to the real service.

    Measured 2026-11-10: the raw stanza kept passing every check while the
    EFFECTIVE service (per `docker compose config`) inherited
    `build: {context: ., dockerfile: Dockerfile.godot}` from `godot-builder`, so
    `up --build` would build the worker from a canonical-source build. A
    pure-Python test cannot resolve `extends`, so the key itself is refused. The
    first assertion records why nothing below would have caught it.
    """
    service = _mutated(extends="godot-builder")
    assert "build" not in service, service
    assert service["image"] == _real_service()["image"], service
    with pytest.raises(AssertionError) as refusal:
        assert_split(service)
    message = str(refusal.value)
    assert "`extends`" in message, message
    assert "godot-builder" in message, message


def test_each_mutation_is_refused_by_the_check_that_owns_it():
    """The five single-property mutations, one table, each message exclusive.

    A refusal that named every property would pass the arms above while being
    unable to say what is wrong, and a helper that ignored its argument could
    not refuse all five at once. This arm asserts the partition: each mutation's
    message names its OWN property and none of the other four check-owned
    tokens."""
    mutations = {
        "command": ({"command": None}, "`pull_policy`", "`image`", "`build`",
                    "`extends`"),
        "image": ({"image": "aitelier-zvec-grep:latest"},
                  "`command`", "`pull_policy`", "`build`", "`extends`"),
        "pull_policy": ({"pull_policy": None}, "`command`", "`image`", "`build`",
                        "`extends`"),
        "build": ({"build": {}}, "`command`", "`image`", "`pull_policy`",
                  "`extends`"),
        "extends": ({"extends": "godot-builder"}, "`command`", "`image`",
                    "`pull_policy`", "`build`"),
    }
    for property_name, (changes, *others) in mutations.items():
        with pytest.raises(AssertionError) as refusal:
            assert_split(_mutated(**changes))
        message = str(refusal.value)
        assert f"`{property_name}`" in message, (property_name, message)
        for other in others:
            assert other not in message, (property_name, other, message)
