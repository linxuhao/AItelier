"""The backend's read-only view of the media library (step 24.2b, design-steward 7.8.12a
"一个媒体库" and "显示 / 文件怎么找").

The library is written by the hosts only (integrations/dsh-director/lib/media.mjs): one writer
per subdirectory `<root>/<store>/` - the steward's `<userId>/`, each director's
`d-<driver_id>/` - and each writer keeps its own `<store>/index.jsonl`, one JSON row per line.
A row names its file by `file`, a path relative to the media ROOT (24.2 piece 2: deduplicated
by sha256, a file stays in the month directory it was first written to, so the path cannot be
derived from `at`). The backend never writes here; the root is mounted into it read-only.

`lookup(store, ref)` answers one of three things:
- `found`: the index row, the file's resolved path and its kind;
- `gone`: the row is there but its file is not (cleaned, or cleared);
- `unknown`: no row names the ref - or the row's `file` would leave `<store>/` (a `..`
  component, an absolute path, a symlink pointing outside): a path that escapes is treated as
  if the row did not exist, never followed.

A malformed index line (not JSON, not an object, no `m_` ref, no string `file`, a mime outside
the shared media-types table) is skipped, as the host's own reader skips it; the first row
naming a ref wins, as in the host's `lookup`. The mime served is therefore always one the
shared table decided from the file's first bytes.

The dashboard's upload staging directory (`~/.local/share/aitelier/media-staging`, 7.8.12a
"暂存") is named here too: it is mounted read-write into the backend only, outside
`~/.AItelier`, so `dev_mcp` never reads it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core import media_types

STAGING_DEFAULT = "~/.local/share/aitelier/media-staging"
# A writer's subdirectory name, as the host's library accepts it (createMediaLibrary's userId).
STORE = re.compile(r"[A-Za-z0-9_.-]+")
INDEX = "index.jsonl"

FOUND, GONE, UNKNOWN = "found", "gone", "unknown"


def staging_dir() -> Path:
    """The dashboard uploads' staging directory (7.8.12a "暂存")."""
    return Path(STAGING_DEFAULT).expanduser()


@dataclass(frozen=True)
class Lookup:
    status: str                 # found | gone | unknown
    row: dict | None = None     # the index row (found, gone)
    path: Path | None = None    # the resolved file (found)
    kind: str | None = None     # image | voice | file, from the row's mime (found, gone)


def _valid_store(store) -> bool:
    return isinstance(store, str) and bool(STORE.fullmatch(store)) and not store.startswith(".")


def _mimes() -> dict:
    return {entry["mime"]: entry["kind"] for entry in media_types.table()["types"]}


def index_row(root, store: str, ref: str) -> dict | None:
    """The first well-formed row of `<root>/<store>/index.jsonl` naming `ref`, or None."""
    if not _valid_store(store) or not isinstance(ref, str) or not media_types.REF.fullmatch(ref):
        return None
    mimes = _mimes()
    try:
        handle = open(os.path.join(os.fspath(root), store, INDEX), encoding="utf-8", errors="replace")
    except OSError:
        return None
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if (isinstance(row, dict) and row.get("ref") == ref and isinstance(row.get("file"), str)
                    and row.get("mime") in mimes):
                return row
    return None


def _inside(root, store: str, file: str) -> str | None:
    """`<root>/<file>` resolved, when it stays strictly under `<root>/<store>/`; else None."""
    if not file or "\x00" in file or file.startswith("/") or ".." in PurePosixPath(file).parts:
        return None
    base = os.path.join(os.path.realpath(os.fspath(root)), store)
    resolved = os.path.realpath(os.path.join(os.fspath(root), file))
    return resolved if resolved.startswith(base + os.sep) else None


def lookup(store: str, ref: str, root=None) -> Lookup:
    """Where the library keeps `ref` of writer `store` (see the module docstring)."""
    if root is None:
        from core.dsh_driver import media_root
        root = media_root()
    row = index_row(root, store, ref)
    if row is None:
        return Lookup(UNKNOWN)
    path = _inside(root, store, row["file"])
    if path is None:
        return Lookup(UNKNOWN)
    kind = _mimes()[row["mime"]]
    if not os.path.lexists(path):
        return Lookup(GONE, row=row, kind=kind)
    if not os.path.isfile(path):
        return Lookup(UNKNOWN)
    return Lookup(FOUND, row=row, path=Path(path), kind=kind)
