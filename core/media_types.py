"""The one media-types table, read by the backend (step 24.2, design-steward 7.8.12a piece 4).

integrations/dsh-director/media-types.json is the single source: the steward host's media
library (integrations/dsh-director/lib/media.mjs) and this module both read it, so a file's
type is decided by its first bytes the same way on both sides. Here it serves the gateway's
/fetch (api/steward_gateway.py) and the attachment metadata the channel relays accept.

An attachment's METADATA is all the backend ever keeps of a chat message's files
(`attachments_json` on the source row): kind, mime, size, durationMs and the library ref - or
the refusal of the closed set. No byte, no file name.

Step 24.2b (7.8.12a "消息带附件", "显示"): a conversation MESSAGE row carries its attachments in
one shape, whoever wrote it - `{id, kind, mime, size, name}`, where `id` is a dashboard upload's
`u_` upload_id or a library `m_` ref, and `refused` is added for one the host refused. A channel
message's attachments live on its source row instead (above, no id and no name); the
conversation shows them in the message shape: `ref` becomes `id`, a name is made up from the kind
and the ref, and a refused one keeps its refusal and has no id.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from core.state_graph import StateGraphError

TABLE_PATH = Path(__file__).resolve().parents[1] / "integrations" / "dsh-director" / "media-types.json"
KINDS = ("image", "voice", "file")
REFUSALS = ("too_large", "type", "unsupported", "gone")
REF = re.compile(r"m_[0-9a-f]{16}")
UPLOAD_ID = re.compile(r"u_[0-9a-f]{16}")
NAME_MAX = 255
MIME = re.compile(r"[a-z]+/[a-z0-9.+-]{1,80}")
ATTACHMENTS_MAX = 10
_FIELDS = {"kind", "ref", "refused", "mime", "size", "durationMs"}


@lru_cache(maxsize=1)
def table() -> dict:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


def limits() -> dict:
    return dict(table()["limits"])


def sniff(head: bytes) -> dict | None:
    """The type the first bytes name (the table's order), or None. A container row (an OOXML
    file: its first bytes are any zip's) is never named by its first bytes: see identify."""
    for entry in table()["types"]:
        if entry.get("container"):
            continue
        for signature in entry["signatures"]:
            if all(len(head) >= part["offset"] + len(bytes.fromhex(part["hex"]))
                   and head[part["offset"]:part["offset"] + len(bytes.fromhex(part["hex"]))] == bytes.fromhex(part["hex"])
                   for part in signature):
                return entry
    return None


def is_zip(head: bytes) -> bool:
    """Whether the first bytes are a zip's local file header (PK\\x03\\x04)."""
    return head[:4] == b"PK\x03\x04"


_EOCD, _CENTRAL, _ZIP64_LOCATOR = 0x06054B50, 0x02014B50, 0x07064B50


def ooxml_type(data: bytes, bounds: dict | None = None) -> dict | None:
    """Step 24.2d (design-steward 7.8.12c "六"): the OOXML row (docx, xlsx, pptx) of a whole zip
    file, or None - the same judgement as lib/media.mjs ooxmlType, on the same container_samples.
    Only the End of Central Directory and the central directory are read; nothing is unzipped."""
    bounds = bounds or limits()
    try:
        if not is_zip(data) or len(data) < 22:
            return None
        eocd = -1
        for at in range(len(data) - 22, max(0, len(data) - 22 - 65535) - 1, -1):
            if (int.from_bytes(data[at:at + 4], "little") == _EOCD
                    and at + 22 + int.from_bytes(data[at + 20:at + 22], "little") == len(data)):
                eocd = at
                break
        if eocd < 0:
            return None
        if eocd >= 20 and int.from_bytes(data[eocd - 20:eocd - 16], "little") == _ZIP64_LOCATOR:
            return None

        def u16(at):
            return int.from_bytes(data[at:at + 2], "little")

        def u32(at):
            return int.from_bytes(data[at:at + 4], "little")

        disk, cd_disk, on_disk, count = u16(eocd + 4), u16(eocd + 6), u16(eocd + 8), u16(eocd + 10)
        size, offset = u32(eocd + 12), u32(eocd + 16)
        if disk or cd_disk or on_disk != count or count == 0xFFFF or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
            return None
        if count > bounds["office_zip_entries"] or offset + size > eocd:
            return None
        names, at, unzipped = set(), offset, 0
        for _ in range(count):
            if at + 46 > offset + size or u32(at) != _CENTRAL:
                return None
            flags, packed, plain = u16(at + 8), u32(at + 20), u32(at + 24)
            end = at + 46 + u16(at + 28) + u16(at + 30) + u16(at + 32)
            if end > offset + size:
                return None
            name = data[at + 46:at + 46 + u16(at + 28)].decode("utf-8", "replace")
            if flags & 1 or name.startswith("/") or ".." in name or "\\" in name:
                return None
            if name.rsplit("/", 1)[-1].lower() == "vbaproject.bin":
                return None
            unzipped += plain
            if unzipped > bounds["office_unzipped_bytes"]:
                return None
            if plain > bounds["office_zip_ratio_floor"] and plain > packed * bounds["office_zip_ratio"]:
                return None
            names.add(name)
            at = end
        if "[Content_Types].xml" not in names:
            return None
        found = [entry for entry in table()["types"] if entry.get("container") == "ooxml" and entry["main_part"] in names]
        return found[0] if len(found) == 1 else None
    except (IndexError, ValueError):
        return None


def identify(data: bytes) -> dict | None:
    """The type of a whole file: its first bytes' (sniff), or - for a zip - its OOXML type."""
    return sniff(data[:64]) or (ooxml_type(data) if is_zip(data) else None)


def normalise_attachments(value) -> list[dict] | None:
    """The attachments of one relayed message as stored, or None for none. Each one is a
    kind and EXACTLY one of a library ref (m_ + 16 hex) or a refusal of the closed set, with
    optional mime, size and durationMs; any other key (a file name among them) or shape is
    refused invalid_attachments."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > ATTACHMENTS_MAX:
        raise StateGraphError(f"invalid_attachments: at most {ATTACHMENTS_MAX} attachments")
    kept = []
    for item in value:
        if not isinstance(item, dict) or set(item) - _FIELDS or item.get("kind") not in KINDS:
            raise StateGraphError("invalid_attachments: each is {kind, ref | refused, mime?, size?, durationMs?}")
        if ("ref" in item) == ("refused" in item):
            raise StateGraphError("invalid_attachments: each carries exactly one of ref and refused")
        out = {"kind": item["kind"]}
        if "ref" in item:
            if not isinstance(item["ref"], str) or not REF.fullmatch(item["ref"]):
                raise StateGraphError("invalid_attachments: a ref is m_ and 16 hex characters")
            out["ref"] = item["ref"]
        elif item["refused"] not in REFUSALS:
            raise StateGraphError(f"invalid_attachments: refused is one of {', '.join(REFUSALS)}")
        else:
            out["refused"] = item["refused"]
        if "mime" in item:
            if not isinstance(item["mime"], str) or not MIME.fullmatch(item["mime"]):
                raise StateGraphError("invalid_attachments: mime")
            out["mime"] = item["mime"]
        for field, top in (("size", 2 ** 31), ("durationMs", 10 ** 7)):
            if field in item:
                if not isinstance(item[field], int) or isinstance(item[field], bool) or not 0 <= item[field] <= top:
                    raise StateGraphError(f"invalid_attachments: {field}")
                out[field] = item[field]
        kept.append(out)
    return kept or None


def ext_of(mime) -> str:
    """The table's extension for a mime ('bin' when the table has none, as the host says)."""
    return next((entry["ext"] for entry in table()["types"] if entry["mime"] == mime), "bin")


def made_up_name(kind: str, ref: str, mime=None) -> str:
    """The name shown for a library ref that never had one (the host's index keeps none): the
    same `<kind>-<ref8>.<ext>` send_media gives the file it sends (lib/media-tools.mjs)."""
    return f"{kind}-{ref[2:10]}.{ext_of(mime)}"


def message_attachment(id, kind, mime, size, name, refused=None) -> dict:
    """One attachment of a conversation message row, in the one shape (keys in this order);
    anything else is refused invalid_attachment."""
    if not isinstance(id, str) or not (UPLOAD_ID.fullmatch(id) or REF.fullmatch(id)):
        raise StateGraphError("invalid_attachment: id is a u_ upload_id or an m_ ref")
    if kind not in KINDS:
        raise StateGraphError("invalid_attachment: kind")
    if not isinstance(mime, str) or not MIME.fullmatch(mime):
        raise StateGraphError("invalid_attachment: mime")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= 2 ** 31:
        raise StateGraphError("invalid_attachment: size")
    if (not isinstance(name, str) or not 1 <= len(name) <= NAME_MAX
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise StateGraphError("invalid_attachment: name")
    if refused is not None and refused not in REFUSALS:
        raise StateGraphError(f"invalid_attachment: refused is one of {', '.join(REFUSALS)}")
    out = {"id": id, "kind": kind, "mime": mime, "size": size, "name": name}
    if refused is not None:
        out["refused"] = refused
    return out


def from_source_attachments(stored) -> list[dict]:
    """A channel message's source-row attachments (as normalise_attachments stored them) in the
    message shape: `ref` -> `id` with a made-up name; a refused one keeps `refused`, no id and no
    name; mime and size only when the source row had them; durationMs is not part of the shape."""
    shown = []
    for item in stored or ():
        out = {"id": item["ref"]} if "ref" in item else {}
        out["kind"] = item["kind"]
        for field in ("mime", "size"):
            if field in item:
                out[field] = item[field]
        if "ref" in item:
            out["name"] = made_up_name(item["kind"], item["ref"], item.get("mime"))
        else:
            out["refused"] = item["refused"]
        shown.append(out)
    return shown
