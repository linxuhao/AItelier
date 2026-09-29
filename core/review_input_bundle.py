"""Portable, explicitly declared text inputs for a State review attempt.

Bytes travel in the frozen descriptor, never by producer workspace paths. The
normal immutable seed publisher owns their run-local lifetime.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import stat
import re

from core.state_graph import StateConflict

MAX_ITEMS = 16
MAX_ITEM_BYTES = 16384
MAX_TOTAL_BYTES = 32768  # fits SkillFlow's 65536-character frozen-check bound
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
PREFIX = "review_input_"


def _shape(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"producer", "items"}:
        raise ValueError("review_input_bundle requires exactly producer and items")
    producer = arguments["producer"]
    if (not isinstance(producer, dict) or set(producer) != {"project_id", "attempt_id", "artifact"}
            or any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in producer.values())):
        raise ValueError("bundle producer requires project_id, attempt_id and artifact identities")
    items = arguments["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
        raise ValueError(f"bundle must declare 1-{MAX_ITEMS} inputs")
    names = set()
    for item in items:
        if (not isinstance(item, dict)
                or set(item) != {"name", "reference", "sha256", "size", "content_base64"}):
            raise ValueError("bundle item has an unknown shape")
        name, reference = item["name"], item["reference"]
        if (not isinstance(name, str) or not _NAME.fullmatch(name) or name in names
                or not isinstance(reference, str) or not 1 <= len(reference) <= 512
                or reference.startswith(("/", "file:"))):
            raise ValueError("bundle names must be unique basenames and references must be opaque identities")
        if not isinstance(item["sha256"], str) or not _SHA.fullmatch(item["sha256"]):
            raise ValueError("bundle item requires an exact sha256")
        if type(item["size"]) is not int or not 0 < item["size"] <= MAX_ITEM_BYTES:
            raise ValueError("bundle item exceeds the declared size bound")
        content = item["content_base64"]
        if content is not None and (not isinstance(content, str)
                                    or len(content) > 4 * ((MAX_ITEM_BYTES + 2) // 3)):
            raise ValueError("bundle encoded input exceeds size bound")
        names.add(name)
    if sum(item["size"] for item in items) > MAX_TOTAL_BYTES:
        raise ValueError("bundle exceeds total size bound")


def manifest(arguments):
    """The expected identity to copy into the frozen check's expected field."""
    _shape(arguments)
    return {"producer": arguments["producer"], "items": [
        {k: item[k] for k in ("name", "reference", "sha256", "size")}
        for item in arguments["items"]]}


def decode(arguments):
    """Observe every declared item, retaining expected/actual failures as data."""
    expected = manifest(arguments)
    actual = {"producer": expected["producer"], "items": []}
    files = {}
    total = 0
    for item in arguments["items"]:
        observed = {"name": item["name"], "reference": item["reference"],
                    "sha256": None, "size": None}
        try:
            if item["content_base64"] is None:
                raise ValueError("required bundle input is missing")
            raw = base64.b64decode(item["content_base64"], validate=True)
            total += len(raw)
            if not 0 < len(raw) <= MAX_ITEM_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError("decoded input exceeds size bound")
            observed.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
            text = raw.decode("utf-8")
            # The existing seed loader reads UTF-8 text with universal newlines.
            # Refuse CR rather than silently changing bytes at the reader.
            if "\r" in text or "\x00" in text:
                raise ValueError("review inputs require UTF-8 text without CR or NUL")
            files[PREFIX + item["name"]] = text
        except (ValueError, UnicodeError, binascii.Error) as exc:
            observed["error"] = str(exc)
        actual["items"].append(observed)
    return actual, files


def require_identity(actual, arguments):
    if actual != manifest(arguments):
        raise ValueError("required review input bytes differ or are unavailable")
    return actual


def digest(identity):
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def load_for_agent(step, sf):
    """Read only this run's declared seed basenames, before agent/tool setup."""
    identity = step.run_context.get("_review_input_bundle")
    if identity is None:
        return {}
    # Identity originated in preflight; reconstruct its strict shape before
    # using any name to address a file, including on resumed persisted runs.
    args = {"producer": identity.get("producer"), "items": [
        dict(item, content_base64=None) for item in identity.get("items", [])]}
    manifest(args)
    project_id = step.run_context["project_id"]
    config_name = step.run_context["_review_input_config"]
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", v)
           or v in {".", ".."} for v in (project_id, config_name)):
        raise StateConflict("review input location has an invalid project or config identity")
    actual = {"producer": identity["producer"], "items": []}
    context = {}
    for item in identity["items"]:
        observed = {"name": item["name"], "reference": item["reference"],
                    "sha256": None, "size": None}
        try:
            raw = _read_input(sf._workspace.base_path, project_id, config_name, PREFIX + item["name"])
            observed.update(sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
            context[f"[review input {item['name']}] reference={item['reference']} sha256={item['sha256']}"] = raw.decode("utf-8")
        except (OSError, ValueError, StateConflict) as exc:
            observed["error"] = str(exc)
        actual["items"].append(observed)
    report = {"required": identity, "actual": actual, "passed": actual == identity,
              "manifest_sha256": digest(identity)}
    sf.trace(step.token.run_id, "step", "review_inputs_admitted" if report["passed"] else "review_inputs_refused",
             report, step_id=step.step_id, project_id=step.run_context["project_id"])
    if not report["passed"]:
        exc = StateConflict("required review input refused before agent execution")
        exc.report = report
        raise exc
    return context


def _read_input(root, project_id, config_name, name):
    """No-follow traversal under the controlled workspace root, bounded read."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fds = []
    try:
        fds.append(os.open(root, flags | os.O_DIRECTORY))
        for part in (project_id, config_name, "_seed"):
            fds.append(os.open(part, flags | os.O_DIRECTORY, dir_fd=fds[-1]))
        fd = os.open(name, flags | os.O_NONBLOCK, dir_fd=fds[-1])
        fds.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_mode & 0o222:
            raise StateConflict("review input must be a read-only regular file")
        if not 0 < before.st_size <= MAX_ITEM_BYTES:
            raise StateConflict("review input exceeds size bound")
        raw = bytearray()
        while chunk := os.read(fd, MAX_ITEM_BYTES + 1 - len(raw)):
            raw.extend(chunk)
            if len(raw) > MAX_ITEM_BYTES:
                raise StateConflict("review input exceeds size bound")
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise StateConflict("review input changed during admission")
        return bytes(raw)
    finally:
        for fd in reversed(fds):
            os.close(fd)
