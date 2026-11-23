"""Step 24.2d: a zip built from one of media-types.json's `container_samples` - the same bytes
integrations/dsh-director/test/fixtures/zip-samples.mjs builds for the host's side: local headers
with `packed` filler bytes each, a central directory declaring each entry's sizes and flags, and
an End of Central Directory (or none, a zip64 locator before it, a directory said to start past it)."""
from __future__ import annotations

import struct

from core import media_types


def samples() -> list[dict]:
    return media_types.table()["container_samples"]


def zip_from(sample: dict) -> bytes:
    entries = [tuple(e) for e in sample["entries"]] + [(f"f/{i}.xml", 10, 10) for i in range(sample.get("filler", 0))]
    locals_, central, offset = [], [], 0
    for entry in entries:
        name, plain, packed = entry[0], entry[1], entry[2]
        flags = entry[3] if len(entry) > 3 else 0
        raw = name.encode("utf-8")
        locals_.append(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flags, 8, 0, 0, 0, packed, plain, len(raw), 0) + raw + b"\0" * packed)
        central.append(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20, flags, 8, 0, 0, 0, packed, plain, len(raw), 0, 0, 0, 0, 0, offset) + raw)
        offset += 30 + len(raw) + packed
    directory = b"".join(central)
    start = offset + 100 if sample.get("eocd") == "past_end" else offset
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(directory), start, 0)
    tail = {"missing": b"", "zip64": bytes.fromhex("504b0607") + b"\0" * 16 + end}.get(sample.get("eocd"), end)
    return b"".join(locals_) + directory + tail


def ooxml(ext: str) -> bytes:
    about = {"docx": "a docx", "xlsx": "an xlsx", "pptx": "a pptx"}[ext]
    return zip_from(next(s for s in samples() if s["about"] == about))
