"""The bridge image's puppet is a fork; this checks the fork offline.

integrations/wechat-bridge/neo-fork/ holds everything the fork is made of: the upstream
commit, the patch against it, and the upstream files that patch touches. The bridge
Dockerfile fetches that commit, verifies those files against pin.json, applies the patch
and rebuilds the addon and dist/ from the result.

These checks need no network, no cargo and no docker: they compare the committed bytes
with pin.json, apply the patch to the vendored upstream snapshot, and read the Dockerfile
stages that do the same thing for real in the image build.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BRIDGE = REPO / "integrations" / "wechat-bridge"
FORK = BRIDGE / "neo-fork"
SNAPSHOT = FORK / "upstream"
PIN_PATH = FORK / "pin.json"
DOCKERFILE = BRIDGE / "Dockerfile"

DIFF_HEADER = re.compile(r"^diff --git a/(?P<left>\S+) b/(?P<right>\S+)$")
HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)
NATIVE_ADDON = "node_modules/wechaty-puppet-neo/native/linux-x64/neo.node"
NATIVE_DIST = "node_modules/wechaty-puppet-neo/dist/"


def sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


@pytest.fixture(scope="module")
def pin() -> dict:
    parsed = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    assert set(parsed) == {"upstream", "files", "patch"}, "pin.json carries more than it should"
    return parsed


def pinned_paths(pin: dict) -> set[str]:
    return {entry["path"] for entry in pin["files"]}


def docker_stages(dockerfile: str) -> list[tuple[str, str]]:
    """Each stage's name (or a synthetic one) and its body."""
    stages: list[tuple[str, list[str]]] = []
    for line in dockerfile.splitlines():
        stage = re.match(r"FROM\s+(?P<image>\S+)(?:\s+AS\s+(?P<name>\S+))?\s*$", line, re.IGNORECASE)
        if stage:
            stages.append((stage["name"] or f"stage{len(stages)}", []))
        elif stages:
            stages[-1][1].append(line)
    return [(name, "\n".join(body)) for name, body in stages]


def patch_hunks(patch: str) -> dict[str, list[tuple[int, int, list[str]]]]:
    """The files a git patch touches, and each file's hunks in patch order."""
    lines = patch.splitlines(keepends=True)
    files: dict[str, list[tuple[int, int, list[str]]]] = {}
    path: str | None = None
    index = 0
    while index < len(lines):
        diff = DIFF_HEADER.match(lines[index].rstrip("\n"))
        if diff:
            assert diff["left"] == diff["right"], f"two paths in one file header: {lines[index]!r}"
            path = diff["left"]
            files.setdefault(path, [])
            index += 1
            continue
        hunk = HUNK_HEADER.match(lines[index].rstrip("\n"))
        if hunk:
            assert path is not None, "a hunk comes before any file header"
            old_count = int(hunk["old_count"] if hunk["old_count"] is not None else 1)
            new_count = int(hunk["new_count"] if hunk["new_count"] is not None else 1)
            body: list[str] = []
            index += 1
            seen_old = seen_new = 0
            while seen_old < old_count or seen_new < new_count:
                assert index < len(lines), f"{path}: the patch ends inside a hunk"
                entry = lines[index]
                index += 1
                if entry.startswith("\\"):  # "\ No newline at end of file"
                    continue
                body.append(entry)
                if entry.startswith("+"):
                    seen_new += 1
                elif entry.startswith("-"):
                    seen_old += 1
                else:
                    assert entry.startswith(" "), f"{path}: not a hunk line: {entry!r}"
                    seen_old += 1
                    seen_new += 1
            files[path].append((int(hunk["old_start"]), old_count, body))
            continue
        index += 1
    return files


def apply_patch(patch: str, root: Path) -> None:
    """Apply a unified diff to the files under root, or fail."""
    for path, hunks in patch_hunks(patch).items():
        target = root / path
        assert target.is_file(), f"the patch touches {path}, which the pin does not vendor"
        original = target.read_text(encoding="utf-8").splitlines(keepends=True)
        patched: list[str] = []
        cursor = 0
        for old_start, old_count, body in hunks:
            start = old_start - 1 if old_count else old_start
            assert start >= cursor, f"{path}: hunks are out of order at line {old_start}"
            patched.extend(original[cursor:start])
            cursor = start
            for entry in body:
                tag, text = entry[0], entry[1:]
                if tag == "+":
                    patched.append(text)
                    continue
                assert cursor < len(original), f"{path}: line {cursor + 1} is past the end"
                assert original[cursor] == text, (
                    f"{path}: line {cursor + 1} is {original[cursor]!r}, the patch expects {text!r}"
                )
                if tag == "-":
                    cursor += 1
                else:
                    patched.append(original[cursor])
                    cursor += 1
        patched.extend(original[cursor:])
        target.write_text("".join(patched), encoding="utf-8")


def test_the_pin_names_a_commit_and_the_snapshot_says_which(tmp_path: Path, pin: dict) -> None:
    upstream = pin["upstream"]
    assert upstream["repository"] == "https://github.com/wechaty/puppet-neo"
    assert re.fullmatch(r"[0-9a-f]{40}", upstream["commit"]), "the pin is not a commit id"
    # The vendored snapshot is the only offline witness to where its bytes came from.
    notice = (SNAPSHOT / "NOTICE").read_text(encoding="utf-8")
    assert upstream["commit"] in notice, "the snapshot names another commit than pin.json"
    assert upstream["repository"] in notice
    # That commit is the npm release the bridge's package.json asks for.
    package = json.loads((SNAPSHOT / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == upstream["version"], (
        f"the pin is version {package['version']}, pin.json says {upstream['version']}"
    )
    assert package["name"] == "wechaty-puppet-neo"
    license_text = (SNAPSHOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text and "Version 2.0" in license_text


def test_every_pinned_file_is_vendored_unchanged(pin: dict) -> None:
    assert pin["files"], "pin.json pins no files"
    assert len(pin["files"]) == 7, "the pin no longer names the seven upstream blobs the fork carries"
    for entry in pin["files"]:
        vendored = SNAPSHOT / entry["path"]
        assert vendored.is_file(), f"{entry['path']} is pinned but not vendored"
        blob = vendored.read_bytes()
        assert len(blob) == entry["size"], f"{entry['path']} is {len(blob)} bytes, pinned {entry['size']}"
        assert sha256(blob) == entry["sha256"], f"{entry['path']} is not the pinned blob"
    snapshot = {path.relative_to(SNAPSHOT).as_posix() for path in SNAPSHOT.rglob("*") if path.is_file()}
    assert snapshot == pinned_paths(pin) | {"NOTICE"}, (
        f"the snapshot carries {sorted(snapshot - pinned_paths(pin) - {'NOTICE'})}, which pin.json does not pin"
    )


def test_the_patch_is_the_pinned_patch(pin: dict) -> None:
    patch_path = REPO / pin["patch"]["path"]
    assert patch_path.is_file(), f"{pin['patch']['path']} is missing"
    assert patch_path.parent == FORK, "the pinned patch lives outside the fork directory"
    assert sha256(patch_path.read_bytes()) == pin["patch"]["sha256"], "the patch is not the pinned patch"


def test_the_patch_touches_only_pinned_files(pin: dict) -> None:
    patch = (REPO / pin["patch"]["path"]).read_text(encoding="utf-8")
    touched = patch_hunks(patch)
    assert touched, "the patch changes nothing"
    pinned = pinned_paths(pin)
    assert set(touched) <= pinned, (
        f"the patch touches {sorted(set(touched) - pinned)}, which pin.json does not pin"
    )


def test_the_patch_applies_to_the_pinned_bytes(tmp_path: Path, pin: dict) -> None:
    snapshot = tmp_path / "pin"
    for entry in pin["files"]:
        copy = snapshot / entry["path"]
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes((SNAPSHOT / entry["path"]).read_bytes())

    apply_patch((REPO / pin["patch"]["path"]).read_text(encoding="utf-8"), snapshot)

    # The addon: outgoing is still the one thing dropped; every other type is carried
    # with the type and raw payload the wire gave it.
    native = (snapshot / "native/src/lib.rs").read_text(encoding="utf-8")
    assert "if message.message_type == 1 {" in native
    assert "if message.outgoing {" in native
    assert "message_type: message.message_type," in native
    assert "content: STANDARD.encode(&message.content)," in native
    assert "fn incoming_messages(" in native
    # The puppet's payload type and the mapping from that payload, not a constant.
    assert "messageType?: number" in (snapshot / "src/native.ts").read_text(encoding="utf-8")
    puppet = (snapshot / "src/puppet-neo.ts").read_text(encoding="utf-8")
    assert "function messageType({ messageType }: IncomingMessage): types.Message {" in puppet
    assert "default: return types.Message.Unknown" in puppet
    assert "type: types.Message.Text }" not in puppet, "the payload type is still a constant"
    # Which also answers the two payloads Wechaty asks for when a message arrives: the
    # room a group message was posted in, and a sender neo has not cached. Without the
    # room payload `Room.find` fails (`roomRawPayload is not a function`) and every
    # consumer's `message.room()` throws; without the contact one the sender has no
    # payload to read (`Neo has no payload for this contact`).
    assert "override async roomRawPayload(id: string): Promise<payloads.Room> {" in puppet
    assert "return { id, topic: '', memberIdList: [], adminIdList: [] }" in puppet, (
        "the room payload carries more than neo can supply (a topic, a member roster)"
    )
    assert "override async roomRawPayloadParser(payload: payloads.Room): Promise<payloads.Room> {" in puppet
    assert "Neo has no payload for this contact" not in puppet, "an uncached sender still throws"
    assert "if (contact) return contact" in puppet
    assert "type: types.Contact.Individual," in puppet, "an uncached contact is not nameable as itself"
    fork_tests = (snapshot / "test/ding-dong.test.ts").read_text(encoding="utf-8")
    assert "await puppet.roomPayload(room)" in fork_tests, "the fork does not test the room payload"
    assert "fixture-stranger" in fork_tests, "the fork does not test an uncached sender"


def test_the_native_stage_builds_the_pinned_commit_with_the_pinned_patch(pin: dict) -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    stages = dict(docker_stages(dockerfile))
    assert "native" in stages, "the bridge image has no native stage"
    native = stages["native"]

    toolchain = re.search(r'^channel\s*=\s*"([^"]+)"', (SNAPSHOT / "rust-toolchain.toml").read_text(encoding="utf-8"),
                          re.MULTILINE)
    assert toolchain, "the pin's rust-toolchain.toml names no channel"
    rust = re.search(r"^FROM rust:(\S+?)(?:-slim)?-(\S+)(?:\s+AS\s+native)?$", dockerfile, re.MULTILINE)
    assert rust, "the Dockerfile does not build with a rust image"
    assert rust.group(1) == toolchain.group(1), (
        f"the image builds with rust {rust.group(1)}, the pin's toolchain is {toolchain.group(1)}"
    )

    commits = re.findall(r"\b[0-9a-f]{40}\b", dockerfile)
    assert set(commits) == {pin["upstream"]["commit"]}, (
        f"the Dockerfile pins {sorted(set(commits))}, pin.json says {pin['upstream']['commit']}"
    )
    assert len(commits) >= 2, "the native stage fetches the commit but never checks what arrived"

    patch_path = pin["patch"]["path"]
    context_path = patch_path.split("integrations/wechat-bridge/", 1)[1]
    assert patch_path.startswith("integrations/wechat-bridge/")
    assert context_path in native, f"the native stage does not apply {context_path}"
    assert "pin.json" in native, "the native stage does not read pin.json"
    assert "sha256" in native, "the native stage does not verify the pinned blobs"
    assert "cargo build --release --locked" in native
    assert "git apply" in native
    assert re.search(r"npm-cli\.js test", native), "the native stage does not test what it built"


def test_the_native_stage_checks_the_patch_digest_and_applies_that_one_patch(pin: dict) -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    native = dict(docker_stages(dockerfile))["native"]
    name = Path(pin["patch"]["path"]).name

    # Exactly one patch is carried into the build: the pinned one, by its own name.
    copied = [line for line in native.splitlines() if re.match(r"^COPY\s", line) and ".patch" in line]
    assert len(copied) == 1, f"the native stage copies {len(copied)} patch lines: {copied}"
    sources = [token for token in copied[0].split() if token.endswith(".patch")]
    assert sources == [f"neo-fork/{name}"], f"the build copies {sources}, the pin names neo-fork/{name}"

    # Its digest is checked against pin.json's before anything is patched, and the check
    # precedes the apply: an unverified patch would make the pin a comment.
    read = re.search(rf"const\s+patch\s*=\s*readFileSync\('([^']*{re.escape(name)})'\)", native)
    assert read, f"the native stage never reads {name}"
    compare = re.search(r"if \(sha256\(patch\) !== pin\.patch\.sha256\) throw new Error\(", native)
    assert compare, "the native stage applies the patch without checking pin.json's sha256 for it"
    check_at = re.search(r"git apply --check (\S+)", native)
    apply_at = re.search(r"git apply (?!-)(\S+)", native)
    assert check_at and apply_at, "the native stage does not check the patch before applying it"
    assert apply_at.group(1) == check_at.group(1) == read.group(1), (
        f"the stage verifies {read.group(1)} and applies {apply_at.group(1)}"
    )
    assert native.index(compare.group(0)) < native.index(check_at.group(0)) < native.index(apply_at.group(0)), (
        "the digest is checked, or the patch tested, after it was already applied"
    )

    # One patch, applied once: a second apply would land on a tree the pin never described.
    applies = re.findall(r"git apply (?!-)(\S+)", native)
    assert applies == [read.group(1)], f"the native stage applies {applies}"


def test_the_image_overlays_exactly_the_fork_addon_and_its_dist(pin: dict) -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    stages = docker_stages(dockerfile)
    named = dict(stages)
    assert "native" in named and "build" in named
    assert re.search(r"^FROM rust:", dockerfile, re.MULTILINE), "nothing native is built"

    build = named["build"]
    overlays = re.findall(r"^COPY\s+(?:--\S+\s+)*--from=native\s+(\S+)\s+(\S+)\s*$", build, re.MULTILINE)
    assert [target for _, target in overlays] == [NATIVE_ADDON, NATIVE_DIST], (
        f"the image overlays {overlays} from the native stage"
    )
    assert "COPY --chmod=0444" in build, "the image does not pin the file modes of what it copies"

    final = stages[-1][1]
    assert "--from=build /opt/wechat-bridge" in final
    assert "useradd --uid 1000" in final, "the bridge no longer runs as uid 1000"
    assert "EXPOSE 18731" in final
    assert "npm" not in final, "the final image carries npm"
    assert "cargo" not in final and "rust" not in final, "the final image carries a compiler"
    assert pin["upstream"]["commit"] in dockerfile
