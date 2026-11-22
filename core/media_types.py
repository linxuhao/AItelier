"""The one media-types table, read by the backend (step 24.2, design-steward 7.8.12a piece 4).

integrations/dsh-director/media-types.json is the single source: the steward host's media
library (integrations/dsh-director/lib/media.mjs) and this module both read it, so a file's
type is decided by its first bytes the same way on both sides. Here it serves the gateway's
/fetch (api/steward_gateway.py) and the attachment metadata the channel relays accept.

An attachment's METADATA is all the backend ever keeps of a chat message's files
(`attachments_json` on the source row): kind, mime, size, durationMs and the library ref - or
the refusal of the closed set. No byte, no file name.
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
MIME = re.compile(r"[a-z]+/[a-z0-9.+-]{1,80}")
ATTACHMENTS_MAX = 10
_FIELDS = {"kind", "ref", "refused", "mime", "size", "durationMs"}


@lru_cache(maxsize=1)
def table() -> dict:
    return json.loads(TABLE_PATH.read_text(encoding="utf-8"))


def limits() -> dict:
    return dict(table()["limits"])


def sniff(head: bytes) -> dict | None:
    """The type the first bytes name (the table's order), or None."""
    for entry in table()["types"]:
        for signature in entry["signatures"]:
            if all(len(head) >= part["offset"] + len(bytes.fromhex(part["hex"]))
                   and head[part["offset"]:part["offset"] + len(bytes.fromhex(part["hex"]))] == bytes.fromhex(part["hex"])
                   for part in signature):
                return entry
    return None


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
