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
    assert set(parsed) == {"upstream", "files", "patches"}, "pin.json carries more than it should"
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


def applied_snapshot(tmp_path: Path, pin: dict, name: str) -> Path:
    """The vendored upstream, copied into tmp_path and patched, as the image build does."""
    snapshot = tmp_path / name
    for entry in pin["files"]:
        copy = snapshot / entry["path"]
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes((SNAPSHOT / entry["path"]).read_bytes())
    for entry in pin["patches"]:
        apply_patch((REPO / entry["path"]).read_text(encoding="utf-8"), snapshot)
    return snapshot


RUST_EPRINTLN = "eprintln!("
DOWNLOAD_CALL = "cdn_client(account)?.download(&descriptor)"
DOWNLOAD_HANDLER = re.compile(r"Operation::DownloadMedia\s*\{(?P<fields>[^}]*)\}\s*=>\s*\{")
DOWNLOAD_FIELDS = {"file_id", "fallback_file_id", "aes_key", "quality", "expected_bytes"}
# The fields the fix's design note documents for the attempt's request line, and no others.
DIAGNOSTIC_FIELDS = {
    "variant", "quality", "filetype", "downpicformat", "size_class", "file_id_len", "has_slash", "has_query",
    "has_amp", "has_equals", "all_hex",
}


def balanced_slice(text: str, opening: int) -> tuple[int, int]:
    """The (start, end) of the (), {} or [] group whose opening character is at `opening`."""
    closer = {"(": ")", "{": "}", "[": "]"}[text[opening]]
    opener = text[opening]
    depth = 0
    index = opening
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            in_string = char != '"'
        elif char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return opening, index + 1
        index += 1
    raise AssertionError(f"unbalanced {opener!r} at offset {opening}")


def rust_string_literal(text: str, start: int = 0) -> tuple[str, int]:
    """The first "..." literal at or after start: its body and the index just past it."""
    index = text.index('"', start) + 1
    body: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "\\":
            body.append(text[index : index + 2])
            index += 2
            continue
        if char == '"':
            return "".join(body), index + 1
        body.append(char)
        index += 1
    raise AssertionError("a string literal runs past the end of the call")


def format_labels(template: str) -> list[str]:
    """The `name=` labels a Rust format string writes, in order."""
    return re.findall(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=", template)


def format_placeholders(template: str) -> tuple[set[str], int]:
    """The named fields a format string captures, and how many anonymous `{}` it has."""
    names: set[str] = set()
    anonymous = 0
    index = 0
    while index < len(template):
        if template[index] != "{":
            index += 1
            continue
        if template[index + 1 : index + 2] == "{":
            index += 2
            continue
        closing = template.index("}", index)
        field = template[index + 1 : closing].split(":", 1)[0]
        if field:
            names.add(field)
        else:
            anonymous += 1
        index = closing + 1
    return names, anonymous


def split_arguments(text: str) -> list[str]:
    """A Rust call's top-level comma-separated arguments."""
    arguments: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "({[":
            depth += 1
        elif char in ")}]":
            depth -= 1
        if char == "," and depth == 0:
            arguments.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    arguments.append("".join(current).strip())
    return [argument for argument in arguments if argument]


def eprintln_calls(rust: str) -> list[str]:
    """Every eprintln! call in the applied native source, in source order."""
    calls: list[str] = []
    start = 0
    while (at := rust.find(RUST_EPRINTLN, start)) != -1:
        _, end = balanced_slice(rust, at + len(RUST_EPRINTLN) - 1)
        calls.append(rust[at:end])
        start = end
    return calls


def the_diagnostic_calls(rust: str) -> tuple[str, str]:
    """The two eprintln! calls the applied source emits for one CDN attempt: the request
    line, emitted before the download, and the line naming the variant that answered."""
    calls = eprintln_calls(rust)
    assert len(calls) == 2, (
        f"the applied native source has {len(calls)} eprintln! calls, the fix documents the "
        "attempt's request line and the line naming the variant that answered"
    )
    request, served = calls
    assert "downpicformat=1" in request, "the first diagnostic is not the attempt's request line"
    return request, served


def the_diagnostic_call(rust: str) -> str:
    """The one request line: one per attempt, emitted before the download."""
    return the_diagnostic_calls(rust)[0]


def download_media_handler(rust: str) -> str:
    """The body of the `Operation::DownloadMedia` match arm that fetches the media."""
    handlers = [
        match
        for match in DOWNLOAD_HANDLER.finditer(rust)
        if {field.strip() for field in match["fields"].split(",") if field.strip()} == DOWNLOAD_FIELDS
    ]
    assert len(handlers) == 1, (
        f"the applied native source has {len(handlers)} handlers destructuring an image download"
    )
    _, end = balanced_slice(rust, handlers[0].end() - 1)
    return rust[handlers[0].end() : end]


CANDIDATE_PUSH = re.compile(
    r"candidates\.push\(\((?P<id>[^,]+?),\s*(?P<profile>MediaQuality::[A-Za-z]+|quality),"
    r'\s*"(?P<variant>[a-z-]+)"\)\);'
)


def candidate_arms(handler: str) -> tuple[str, str]:
    """The candidate list's two arms: an image's ladder, and the one attempt every other
    profile gets."""
    at = handler.index("let mut candidates = Vec::new();")
    image_open = handler.index("{", handler.index("if is_image", at))
    _, image_end = balanced_slice(handler, image_open)
    else_open = handler.index("{", handler.index("else", image_end))
    _, else_end = balanced_slice(handler, else_open)
    return handler[image_open + 1 : image_end - 1], handler[else_open + 1 : else_end - 1]


def candidate_pushes(arm: str) -> list[tuple[str, str, str]]:
    """The attempts one arm queues, in the order it queues them: id, profile, log label."""
    return [
        (push["id"].strip(), push["profile"], push["variant"])
        for push in CANDIDATE_PUSH.finditer(arm)
    ]


def distinct_second_id_guard(image_arm: str) -> str:
    """The body of the `other != file_id` test a payload's second id has to pass."""
    at = image_arm.index("if let Some(other) = fallback_file_id.clone() {")
    outer_open = image_arm.index("{", at)
    _, outer_end = balanced_slice(image_arm, outer_open)
    outer = image_arm[outer_open + 1 : outer_end - 1]
    inner_open = outer.index("{", outer.index("if other != file_id {"))
    _, inner_end = balanced_slice(outer, inner_open)
    return outer[inner_open + 1 : inner_end - 1]


def download_loop(handler: str) -> str:
    """The body of the loop that walks the candidates, one download per turn."""
    loop_open = handler.index("{", handler.index("for (this_id, this_quality, variant) in candidates {"))
    _, loop_end = balanced_slice(handler, loop_open)
    return handler[loop_open + 1 : loop_end - 1]


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


def test_the_patches_are_the_pinned_patches(pin: dict) -> None:
    for entry in pin["patches"]:
        patch_path = REPO / entry["path"]
        assert patch_path.is_file(), f"{entry['path']} is missing"
        assert patch_path.parent == FORK, "the pinned patch lives outside the fork directory"
        assert sha256(patch_path.read_bytes()) == entry["sha256"], "the patch is not the pinned patch"


def test_the_patches_touch_only_pinned_files(pin: dict) -> None:
    for entry in pin["patches"]:
        patch = (REPO / entry["path"]).read_text(encoding="utf-8")
        touched = patch_hunks(patch)
        pinned = pinned_paths(pin)
        assert set(touched) <= pinned, (
            f"the patch touches {sorted(set(touched) - pinned)}, which pin.json does not pin"
        )


def test_the_patch_applies_to_the_pinned_bytes(tmp_path: Path, pin: dict) -> None:
    snapshot = applied_snapshot(tmp_path, pin, "pin")

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

    patch_path = pin["patches"][0]["path"]
    context_path = patch_path.split("integrations/wechat-bridge/", 1)[1]
    assert patch_path.startswith("integrations/wechat-bridge/")
    assert context_path in native, f"the native stage does not apply {context_path}"
    assert "pin.json" in native, "the native stage does not read pin.json"
    assert "sha256" in native, "the native stage does not verify the pinned blobs"
    assert "cargo build --release --locked" in native
    assert "git apply" in native
    assert re.search(r"npm-cli\.js test", native), "the native stage does not test what it built"
    assert re.search(r"cargo test --release --locked --lib", native), (
        "the native stage builds the fork but never runs its own tests"
    )


def test_the_native_stage_checks_the_patch_digest_and_applies_that_one_patch(pin: dict) -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    native = dict(docker_stages(dockerfile))["native"]
    name = Path(pin["patches"][0]["path"]).name

    # Exactly one patch is carried into the build: the pinned one, by its own name.
    copied = [line for line in native.splitlines() if re.match(r"^COPY\s", line) and ".patch" in line]
    assert len(copied) == 1, f"the native stage copies {len(copied)} patch lines: {copied}"
    sources = [token for token in copied[0].split() if token.endswith(".patch")]
    assert sources == ["neo-fork/0001-incoming-message-types.patch", "neo-fork/0002-saved-login.patch"], f"the build copies {sources}"

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


def test_image_download_carries_both_ids_with_one_retry_and_redacted_diagnostic(tmp_path: Path, pin: dict) -> None:
    snapshot = applied_snapshot(tmp_path, pin, "pin2")

    puppet = (snapshot / "src/puppet-neo.ts").read_text(encoding="utf-8")
    native_ts = (snapshot / "src/native.ts").read_text(encoding="utf-8")
    rust = (snapshot / "native/src/lib.rs").read_text(encoding="utf-8")

    # Ordered pair: cdnbigimgurl first, cdnmidimgurl second when present and different.
    assert "const primaryId = attribute(image, 'cdnbigimgurl')" in puppet
    assert "const secondaryId = attribute(image, 'cdnmidimgurl')" in puppet
    assert "fallbackFileId: secondaryId" in puppet or "fallbackFileId: secondaryId }" in puppet

    # Second id carried across the napi boundary.
    assert "fallbackFileId?: string" in native_ts
    assert "fallback_file_id: Option<String>" in rust

    # And carried by the two calls that use it: the image path hands the second id to
    # downloadMedia, the file path still takes four arguments, and the napi wrapper forwards
    # it to the addon. Without these the types above can stay while the argument is dropped,
    # the last-resort attempt the handler implements never fires, and this file stays green.
    image_call = (
        "session.downloadMedia(media.fileId, media.aesKey, media.quality, media.size, "
        "media.fallbackFileId)"
    )
    file_call = "session.downloadMedia(media.fileId, media.aesKey, media.quality, media.size)"
    assert puppet.count(image_call) == 1, (
        f"the image path calls downloadMedia with the second id {puppet.count(image_call)} times, "
        "the fix documents one attempt that hands it over"
    )
    assert puppet.count(file_call) == 1, (
        f"the file path's four-argument downloadMedia call appears {puppet.count(file_call)} times, "
        "so the five-argument call above is not pinned to the image path"
    )
    addon_call = "addon.downloadMedia(fileId, aesKey, quality, expectedBytes, fallbackFileId)"
    assert native_ts.count(addon_call) == 1, (
        f"the napi wrapper forwards the second id {native_ts.count(addon_call)} times, "
        "so the id never reaches the addon"
    )

    handler = download_media_handler(rust)
    image_arm, _ = candidate_arms(handler)

    # The second id is the image ladder's last resort, and only a payload carrying a
    # different one queues it: the push sits behind the `other != file_id` test, so a
    # payload with no second id never reaches a `variant=mid` line.
    guard = distinct_second_id_guard(image_arm)
    assert guard.strip() == 'candidates.push((other, MediaQuality::Standard, "mid"));', (
        f"the second id's attempt is not the one the distinct-id test guards: {guard!r}"
    )
    assert handler.count("fallback_file_id") == 1, (
        "the second id is read somewhere the distinct-id test does not see"
    )
    mid_labels = handler.count('"mid"')
    assert mid_labels == 1, (
        f"the handler names the mid variant {mid_labels} times, so a payload carrying no "
        "second id could still produce its line"
    )
    pushes = candidate_pushes(image_arm)
    assert pushes[-1] == ("other", "MediaQuality::Standard", "mid"), (
        f"the second id is not the last candidate the download walks: {pushes}"
    )
    assert pushes.count(("other", "MediaQuality::Standard", "mid")) == 1, (
        f"the second id is tried more than once: {pushes}"
    )

    # One attempt per turn, and the line before it: these two lines are the instrument the
    # measurement came from, and the answered line sits where the bytes are.
    assert handler.count(DOWNLOAD_CALL) == 1, (
        f"the handler downloads {handler.count(DOWNLOAD_CALL)} times, the ladder is not one attempt per turn"
    )
    assert handler.index(RUST_EPRINTLN) < handler.index(DOWNLOAD_CALL), "the diagnostic follows the download"
    assert handler.index(DOWNLOAD_CALL) < handler.index("neo cdn download served:") < handler.index("break;"), (
        "the answered line is not emitted between the download and the attempt that ends the ladder"
    )

    # The attempt's line names the request fields and the id's shape, never an id, an AES
    # key or a message byte; the second line (the answered variant) has its own test.
    diagnostic = the_diagnostic_call(rust)
    template, template_end = rust_string_literal(diagnostic)
    labels = format_labels(template)
    assert len(labels) == len(set(labels)) == len(DIAGNOSTIC_FIELDS) and set(labels) == DIAGNOSTIC_FIELDS, (
        f"the diagnostic writes {labels}, the fix documents {sorted(DIAGNOSTIC_FIELDS)} once each"
    )
    assert dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=\{([^{}]*)\}", template)) == {
        "variant": "variant",
        "quality": "this_quality:?",
        "filetype": "filetype",
        "size_class": "size_class",
        "file_id_len": "",
        "has_slash": "shape_slash",
        "has_query": "shape_query",
        "has_amp": "shape_amp",
        "has_equals": "shape_equals",
        "all_hex": "shape_hex",
    }, "the diagnostic does not format the documented values"
    assert "downpicformat=1" in template, "the diagnostic does not pin the download format it asks for"
    assert "file_id_len={}" in template, "the diagnostic does not measure the id's length"
    placeholders, anonymous = format_placeholders(template)
    assert anonymous == 1 and placeholders == {
        "variant", "this_quality", "filetype", "size_class",
        "shape_slash", "shape_query", "shape_amp", "shape_equals", "shape_hex",
    }, (
        f"the diagnostic formats {sorted(placeholders)} and {anonymous} anonymous values"
    )
    assert split_arguments(diagnostic[template_end : diagnostic.rindex(")")]) == ["this_id.len()"], (
        "the diagnostic formats more than the id's length"
    )
    for forbidden in ("{this_id}", "{file_id}", "{aes_key}", "{body}", "{bytes}", "aes_key", "body", "bytes"):
        assert forbidden not in diagnostic, f"the diagnostic line carries {forbidden!r}"
    # ...and the shape it reports is a set of predicates over the id, not the id.
    loop_body = download_loop(handler)
    for predicate in ("contains('/')", "contains('?')", "contains('&')", "contains('=')"):
        assert f"this_id.{predicate}" in loop_body, f"the diagnostic does not report {predicate}"
    assert "this_id.bytes().all(|b| b.is_ascii_hexdigit());" in loop_body, (
        "the diagnostic does not report whether the id is plain hexadecimal"
    )


def test_a_refused_image_retries_the_mid_id_as_the_crates_standard_profile(tmp_path: Path, pin: dict) -> None:
    """An image asks for the standard profile first, reaches the original one only after that
    attempt was refused, and a payload's distinct second id is the last resort, also standard."""
    snapshot = applied_snapshot(tmp_path, pin, "fallback")
    rust = (snapshot / "native/src/lib.rs").read_text(encoding="utf-8")
    handler = download_media_handler(rust)

    # The filetype is the crate's, read from the profile the attempt's descriptor carries.
    # No list of `MediaQuality` arms is written here: the local match this replaces made the
    # native stage's `cargo build` fail the moment upstream added a variant (E0004), and a
    # test that only reads text cannot see that, so what is pinned is the absence of a list.
    assert "let filetype = this_quality.filetype();" in handler, (
        "the filetype is not read from the crate's own MediaQuality mapping"
    )
    local_arms = re.findall(
        r"MediaQuality::[A-Za-z]+(?:\s*\|\s*MediaQuality::[A-Za-z]+)*\s*(?:if [^=]+)?=>", handler
    )
    assert local_arms == [], (
        f"the handler writes its own MediaQuality arms instead of asking the crate: {local_arms}"
    )

    # ...and it is read per attempt, inside the loop: a filetype fixed once per medium could
    # not tell the ladder's attempts apart.
    loop_body = download_loop(handler)
    assert "let filetype = this_quality.filetype();" in loop_body, (
        "the filetype is chosen once per medium, so every attempt would ask for the same profile"
    )
    assert (
        loop_body.index("let filetype = this_quality.filetype();")
        < loop_body.index(RUST_EPRINTLN)
        < loop_body.index(DOWNLOAD_CALL)
    ), "the request line does not report the profile the attempt is about to send"

    # The ladder is the measured order. The first request is the same id at `Standard`, which
    # the crate maps to `filetype` 2 (the profile that served in 5.97 s); the full-size
    # `Original` profile (filetype 1, refused after 2.81 s) is second; a payload's distinct
    # second id is the last resort, at the profile that answered. The never-measured
    # `Thumbnail` profile is not a candidate at all.
    image_arm, _ = candidate_arms(handler)
    pushes = candidate_pushes(image_arm)
    assert pushes == [
        ("file_id.clone()", "MediaQuality::Standard", "primary-standard"),
        ("file_id.clone()", "MediaQuality::Original", "primary-original"),
        ("other", "MediaQuality::Standard", "mid"),
    ], f"the image download walks {pushes}"
    first_id, first_profile, first_variant = pushes[0]
    assert (first_id, first_profile) == ("file_id.clone()", "MediaQuality::Standard"), (
        f"an image's first request is {first_profile} on {first_id}, not the standard profile"
    )
    assert "standard" in first_variant, (
        f"the first attempt's log label {first_variant!r} does not name the standard profile"
    )
    assert "MediaQuality::Thumbnail" not in handler, "the handler still asks for the thumbnail profile"
    assert "primary-thumbnail" not in handler, "the never-measured third profile is still a candidate"

    # A refusal moves to the next candidate: only a download that answered ends the ladder,
    # a refusal is recorded and the next turn runs. So the `Original` request can only be
    # sent after the `Standard` one was refused, not before it.
    download_at = loop_body.index(DOWNLOAD_CALL)
    ok_at = loop_body.index("Ok(ok) => {")
    ok_open = loop_body.index("{", ok_at)
    _, ok_end = balanced_slice(loop_body, ok_open)
    ok_arm = loop_body[ok_open + 1 : ok_end - 1]
    assert download_at < ok_at, "the success arm counts before the download it answers"
    assert "downloaded = Some(ok);" in ok_arm and "break;" in ok_arm, (
        "a download that answered does not end the candidates"
    )
    assert "Err(err) => last_refusal = Some(err)," in loop_body, (
        "a refused attempt does not record the refusal for the next candidate"
    )
    assert "continue" not in loop_body and "return" not in loop_body, (
        "the loop leaves through the refusal instead of walking to the next candidate"
    )

    # The descriptor carries the attempt's profile, so the first request really asks for
    # filetype 2 and the second for filetype 1 - not only the log line says so.
    descriptor_open = handler.index("{", handler.index("let descriptor = MediaDescriptor {"))
    _, descriptor_end = balanced_slice(handler, descriptor_open)
    descriptor = handler[descriptor_open:descriptor_end]
    assert re.search(r"^\s*quality: this_quality,$", descriptor, re.MULTILINE), (
        "the descriptor pins one profile, so a later attempt would repeat the first one's request"
    )
    for pinned in ("MediaQuality::Original", "MediaQuality::Standard", "MediaQuality::Thumbnail"):
        assert pinned not in descriptor, f"the descriptor pins {pinned}, not the attempt's profile"

    # And the answer names which variant produced the bytes: same redacted shape summary as
    # the request line, no id, no key, no byte.
    request, served = the_diagnostic_calls(rust)
    assert "variant={variant}" in request, "the request line does not name the variant it sends"
    assert "variant={variant}" in served and "filetype={filetype}" in served, (
        "the line for a download that worked does not name the variant the CDN served"
    )
    served_template, served_end = rust_string_literal(served)
    assert format_labels(served_template) == ["variant", "filetype", "file_id_len", "has_slash", "has_query"]
    assert format_placeholders(served_template) == ({"variant", "filetype", "shape_slash", "shape_query"}, 1)
    assert split_arguments(served[served_end : served.rindex(")")]) == ["this_id.len()"], (
        "the served line formats more than the id's length"
    )
    for forbidden in ("{this_id}", "{file_id}", "{aes_key}", "{body}", "{bytes}", "aes_key", "body", "bytes"):
        assert forbidden not in served, f"the served line carries {forbidden!r}"
    # Emitted where the bytes are: inside the Ok arm, after the download that produced them.
    assert handler.index(DOWNLOAD_CALL) < handler.index(served) < handler.index("break;")


def test_the_file_branch_keeps_one_request_and_its_own_profile(tmp_path: Path, pin: dict) -> None:
    """A file is never the image's fallback: one request, and the profile it was asked for."""
    snapshot = applied_snapshot(tmp_path, pin, "file-arm")
    rust = (snapshot / "native/src/lib.rs").read_text(encoding="utf-8")
    handler = download_media_handler(rust)

    # The ladder is an image's, and the addon says so itself rather than relying on the
    # caller that happens to pass no second id for a file: a file handed one is still one
    # request.
    bound = re.search(r"let is_image = (?P<expression>[^;]+);", handler)
    assert bound, "the ladder is not gated on a named image flag"
    expression = bound["expression"].strip()
    assert expression in {"quality == MediaQuality::Original", "MediaQuality::Original == quality"}, (
        f"the ladder is gated on {expression!r}, so a file would be retried as a mid-size image"
    )

    # Every other profile gets exactly one attempt, carrying the profile the caller asked
    # for and the label the log has always printed; the second id is not reachable there.
    _, file_arm = candidate_arms(handler)
    assert candidate_pushes(file_arm) == [("file_id.clone()", "quality", "primary")], (
        f"a file's request walks {candidate_pushes(file_arm)}"
    )
    assert "fallback_file_id" not in file_arm, "a file's request can reach the image's second id"
    assert not re.search(r"MediaQuality::(?:Standard|Original|Thumbnail)", file_arm), (
        "something in the file branch asks for a size other than the one the caller named"
    )

    # A file's own filetype: the descriptor carries the profile the caller asked for, the
    # crate maps `File` to 5 (`LargeFile` 7 beyond 25 MiB), and no line here can turn a file
    # into another size - the profiles the handler names are inside the image's ladder.
    descriptor_open = handler.index("{", handler.index("let descriptor = MediaDescriptor {"))
    _, descriptor_end = balanced_slice(handler, descriptor_open)
    assert re.search(r"^\s*quality: this_quality,$", handler[descriptor_open:descriptor_end], re.MULTILINE), (
        "a file's request does not carry the profile it was asked for"
    )
    parsed = re.search(r"let quality = match quality\.as_str\(\) \{(?P<arms>[^}]*)\}", handler)
    assert parsed, "the handler no longer reads the profile the caller asked for"
    assert dict(re.findall(r'"(?P<name>\w+)"\s*=>\s*MediaQuality::(?P<variant>\w+)', parsed["arms"])) == {
        "original": "Original",
        "file": "File",
        "large_file": "LargeFile",
    }, "the profile a caller asks for is not the one the request carries"
    assert re.findall(r"^[ \t]*quality = MediaQuality::(\w+);[ \t]*$", handler, re.MULTILINE) == [], (
        "the handler reassigns the caller's profile, so a file can be asked for as another size"
    )

    # One request per attempt: the handler has one download call, inside the loop, and the
    # loop walks the candidates the arms above queue.
    loop_body = download_loop(handler)
    assert handler.count(DOWNLOAD_CALL) == 1, (
        f"the handler downloads {handler.count(DOWNLOAD_CALL)} times, so a file is asked for twice"
    )
    assert loop_body.count(DOWNLOAD_CALL) == 1, "the file request leaves the one attempt loop"

# placeholder
