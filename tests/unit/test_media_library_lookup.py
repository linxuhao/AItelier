"""Step 24.2b (design-steward 7.8.12a "显示 / 文件怎么找", "消息带附件"): the backend's read-only
lookup in the media library, the staging directory's place, and the one message-attachment
shape (core.media_library, core.media_types).

The library is laid out exactly as the host's writer lays it out (integrations/dsh-director/
lib/media.mjs): `<root>/<store>/index.jsonl`, one JSON row per line, each row's `file` relative
to the ROOT. A `file` that would leave `<store>/` is never followed."""
from __future__ import annotations

import json
import os

import pytest

from core import media_library, media_types
from core.state_graph import StateGraphError

PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452") + b"\0" * 32
REF = "m_0123456789abcdef"
OTHER = "m_fedcba9876543210"


def row(ref=REF, file="owner/2026-11/aa.png", mime="image/png", **extra):
    return {"ref": ref, "sha256": "aa", "mime": mime, "size": len(PNG), "audience": "owner",
            "source": "in", "channel": "dashboard", "file": file, "at": "2026-11-23T00:00:00.000Z", **extra}


def library(tmp_path, lines, store="owner"):
    root = tmp_path / "media"
    (root / store / "2026-11").mkdir(parents=True)
    (root / store / "2026-11" / "aa.png").write_bytes(PNG)
    with open(root / store / "index.jsonl", "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
    return root


def test_finds_a_refs_row_and_its_file(tmp_path):
    root = library(tmp_path, [row(OTHER, file="owner/2026-11/aa.png"), row()])
    hit = media_library.lookup("owner", REF, root=root)
    assert hit.status == media_library.FOUND
    assert hit.row["ref"] == REF and hit.row["mime"] == "image/png"
    assert hit.path == (root / "owner" / "2026-11" / "aa.png").resolve()
    assert hit.path.read_bytes() == PNG
    assert hit.kind == "image"


def test_the_root_defaults_to_the_hosts_media_root(tmp_path, monkeypatch):
    root = library(tmp_path, [row()])
    monkeypatch.setenv("AITELIER_MEDIA_ROOT", str(root))
    assert media_library.lookup("owner", REF).status == media_library.FOUND


def test_malformed_index_lines_are_skipped(tmp_path):
    root = library(tmp_path, [
        "{not json", "[1, 2]", '"a string"', "",
        {"ref": REF, "mime": "image/png"},                      # no file
        {"ref": REF, "file": 7, "mime": "image/png"},           # file not a string
        row(mime="text/html"),                                  # a mime the shared table never decides
        row(ref="m_short", file="owner/2026-11/aa.png"),
        {"refused": "too_large", "channel": "wechat", "messageRef": "x", "index": 0, "at": "t"},
        row(),
    ])
    hit = media_library.lookup("owner", REF, root=root)
    assert hit.status == media_library.FOUND and hit.row["mime"] == "image/png"


def test_a_missing_file_is_gone_not_unknown(tmp_path):
    root = library(tmp_path, [row(file="owner/2026-10/cleaned.png")])
    hit = media_library.lookup("owner", REF, root=root)
    assert hit.status == media_library.GONE
    assert hit.row["ref"] == REF and hit.kind == "image" and hit.path is None


def test_an_unknown_ref_or_store_is_unknown(tmp_path):
    root = library(tmp_path, [row()])
    assert media_library.lookup("owner", OTHER, root=root).status == media_library.UNKNOWN
    assert media_library.lookup("d-other", REF, root=root).status == media_library.UNKNOWN
    for store in ("..", ".", ".hidden", "owner/..", "a/b", "", None):
        assert media_library.lookup(store, REF, root=root).status == media_library.UNKNOWN, store
    for ref in ("m_0123", "u_0123456789abcdef", None, REF + "0"):
        assert media_library.lookup("owner", ref, root=root).status == media_library.UNKNOWN, ref


@pytest.mark.parametrize("file", [
    "owner/../../outside/aa.png",                   # leaves the root
    "owner/../d-other/2026-11/aa.png",              # another writer's subdirectory
    "owner/2026-11/../../owner/2026-11/aa.png",     # resolves inside, still a `..`
    "d-other/2026-11/aa.png",                       # inside the root, not under <store>/
])
def test_a_file_with_dot_dot_or_outside_the_store_is_not_found(tmp_path, file):
    root = library(tmp_path, [row(file=file)])
    (root / "d-other" / "2026-11").mkdir(parents=True)
    (root / "d-other" / "2026-11" / "aa.png").write_bytes(PNG)
    (root.parent / "outside").mkdir()
    (root.parent / "outside" / "aa.png").write_bytes(PNG)
    assert media_library.lookup("owner", REF, root=root).status == media_library.UNKNOWN


def test_an_absolute_file_is_not_found(tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    root = library(tmp_path, [row(file=str(secret))])
    assert media_library.lookup("owner", REF, root=root).status == media_library.UNKNOWN


def test_a_symlink_pointing_outside_the_store_is_not_found(tmp_path):
    root = library(tmp_path, [row(file="owner/2026-11/link.png"), row(OTHER, file="owner/2026-11/sib.png"),
                              row("m_00000000000000aa", file="owner/2026-11/dangling.png")])
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    os.symlink(secret, root / "owner" / "2026-11" / "link.png")
    (root / "d-other").mkdir()
    (root / "d-other" / "x.png").write_bytes(PNG)
    os.symlink(root / "d-other" / "x.png", root / "owner" / "2026-11" / "sib.png")
    os.symlink(tmp_path / "nowhere.png", root / "owner" / "2026-11" / "dangling.png")
    assert media_library.lookup("owner", REF, root=root).status == media_library.UNKNOWN
    assert media_library.lookup("owner", OTHER, root=root).status == media_library.UNKNOWN
    assert media_library.lookup("owner", "m_00000000000000aa", root=root).status == media_library.UNKNOWN


def test_a_symlinked_store_directory_is_not_followed(tmp_path):
    elsewhere = library(tmp_path / "elsewhere", [row(file="d-1/2026-11/aa.png")], store="d-1")
    root = tmp_path / "media"
    root.mkdir()
    os.symlink(elsewhere / "d-1", root / "d-1")
    assert media_library.lookup("d-1", REF, root=root).status == media_library.UNKNOWN


def test_a_symlink_staying_inside_the_store_is_followed(tmp_path):
    root = library(tmp_path, [row(file="owner/2026-12/again.png")])
    (root / "owner" / "2026-12").mkdir()
    os.symlink(root / "owner" / "2026-11" / "aa.png", root / "owner" / "2026-12" / "again.png")
    hit = media_library.lookup("owner", REF, root=root)
    assert hit.status == media_library.FOUND and hit.path.read_bytes() == PNG


def test_a_directory_is_not_a_file(tmp_path):
    root = library(tmp_path, [row(file="owner/2026-11")])
    assert media_library.lookup("owner", REF, root=root).status == media_library.UNKNOWN


def test_the_staging_directory_is_under_the_users_local_share(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert media_library.staging_dir() == tmp_path / ".local" / "share" / "aitelier" / "media-staging"


# ---------------------------------------------------------------- the message-attachment shape
def test_a_message_attachment_has_the_one_shape():
    upload = media_types.message_attachment("u_0123456789abcdef", "image", "image/png", 12, "cat.png")
    assert list(upload) == ["id", "kind", "mime", "size", "name"]
    assert media_types.message_attachment(REF, "voice", "audio/ogg", 0, "v.ogg", refused="too_large") == {
        "id": REF, "kind": "voice", "mime": "audio/ogg", "size": 0, "name": "v.ogg", "refused": "too_large"}


@pytest.mark.parametrize("args", [
    ("x_0123456789abcdef", "image", "image/png", 1, "a"),
    ("u_0123", "image", "image/png", 1, "a"),
    ("u_0123456789abcdef", "video", "image/png", 1, "a"),
    ("u_0123456789abcdef", "image", "Image/PNG", 1, "a"),
    ("u_0123456789abcdef", "image", "image/png", -1, "a"),
    ("u_0123456789abcdef", "image", "image/png", True, "a"),
    ("u_0123456789abcdef", "image", "image/png", 1, ""),
    ("u_0123456789abcdef", "image", "image/png", 1, "a\nb"),
    ("u_0123456789abcdef", "image", "image/png", 1, "x" * 256),
])
def test_a_malformed_message_attachment_is_refused(args):
    with pytest.raises(StateGraphError, match="invalid_attachment"):
        media_types.message_attachment(*args)
    with pytest.raises(StateGraphError, match="invalid_attachment"):
        media_types.message_attachment("u_0123456789abcdef", "image", "image/png", 1, "a", refused="nope")


def test_source_row_attachments_show_in_the_message_shape():
    stored = media_types.normalise_attachments([
        {"kind": "image", "ref": REF, "mime": "image/jpeg", "size": 5},
        {"kind": "voice", "ref": OTHER, "durationMs": 3000},
        {"kind": "file", "refused": "too_large", "size": 9},
    ])
    assert media_types.from_source_attachments(stored) == [
        {"id": REF, "kind": "image", "mime": "image/jpeg", "size": 5, "name": "image-01234567.jpg"},
        {"id": OTHER, "kind": "voice", "name": "voice-fedcba98.bin"},
        {"kind": "file", "size": 9, "refused": "too_large"},
    ]
    assert media_types.from_source_attachments(None) == []
