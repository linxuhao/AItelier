"""Host-observed review coverage, not a model's assertion of comprehension.

Only text actually present in a successful outbound model request is counted.
Every window is compared to the frozen material, and unions retain holes. The
host owns certificates; models can read materials and write judgments, not proof.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import os
import tempfile
import uuid

from .storage import BenchError, decode, encode, identifier, immutable, read_file, require, sha

PROTOCOL = 1
PHASES = {"literary_review": "literary", "ledger_audit": "ledger"}


@dataclass(frozen=True)
class Material:
    path: str
    source: str
    text: str

    @property
    def digest(self) -> str:
        return sha(self.text.encode())

    def descriptor(self) -> dict:
        return {"path": self.path, "source": self.source, "sha256": self.digest,
                "characters": len(self.text), "lines": len(self.text.splitlines())}


def frame(key: str, name: str, body: str) -> str:
    """A source identity in displayed text; it grants no coverage by itself."""
    header = f"<!-- WB-MATERIAL {key} {name} {sha(body.encode())} -->\n"
    return header + body


def union(ranges: list[list[int]]) -> list[list[int]]:
    merged = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def holes(size: int, ranges: list[list[int]]) -> list[list[int]]:
    cursor, out = 0, []
    for start, end in union(ranges):
        if start > cursor:
            out.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < size:
        out.append([cursor, size])
    return out


def material_identity(phase: str, key: str, targets: list[dict], materials: list[Material]) -> dict:
    return {"protocol": PROTOCOL, "phase": phase, "review_key": key,
            "targets": targets, "materials": [m.descriptor() for m in materials]}


def validate_targets(report: dict, key: str, targets: list[dict]) -> None:
    require(isinstance(report, dict) and report.get("review_key") == key,
            "review input fingerprint mismatch")
    require(report.get("reviewed_chapters") == targets,
            "review target mismatch: reviewed_chapters must exactly match the current chapter/title/prose_sha256 list")


def validate_certificate(cert: dict, identity: dict, report: dict) -> None:
    require(isinstance(cert, dict) and cert.get("identity") == identity,
            "host-observed reading certificate missing or bound to different materials")
    require(cert.get("report_sha256") == sha(encode(report)), "reading certificate is for a different verdict")
    coverage = cert.get("coverage")
    require(isinstance(coverage, dict) and set(coverage) == {m["path"] for m in identity["materials"]},
            "reading certificate material inventory mismatch")
    for m in identity["materials"]:
        spans = coverage[m["path"]]
        require(isinstance(spans, list) and all(isinstance(x, list) and len(x) == 2
                    and all(type(n) is int for n in x) and 0 <= x[0] < x[1] <= m["characters"] for x in spans),
                "invalid observed read range")
        require(not holes(m["characters"], spans), "incomplete observed material: " + m["path"])
    require(cert.get("complete") is True and isinstance(cert.get("claim"), dict),
            "host reading certificate incomplete")


class Coverage:
    """Coverage for one frozen review. Call observe AFTER a successful model call."""
    def __init__(self, phase: str, key: str, targets: list[dict], materials: list[Material]):
        self.materials = {m.path: m for m in materials}
        require(len(self.materials) == len(materials), "duplicate review material")
        self.identity = material_identity(phase, key, targets, materials)
        self.ranges: dict[str, list[list[int]]] = {p: [] for p in self.materials}
        self.calls: dict[str, str] = {}

    def _credit(self, name: str, start: int, end: int) -> None:
        self.ranges[name] = union(self.ranges[name] + [[start, end]])

    def _inline(self, content: str) -> None:
        for name, m in self.materials.items():
            pos = content.find(m.text.splitlines(keepends=True)[0])
            if pos < 0:
                continue
            visible = content[pos:]
            # Only the exact text or its complete-line prefix, never a summary.
            if visible.startswith(m.text):
                self._credit(name, 0, len(m.text))
                continue
            offset = 0
            for line in m.text.splitlines(keepends=True):
                if not visible[offset:].startswith(line):
                    break
                offset += len(line)
            if offset:
                self._credit(name, 0, offset)

    def _page(self, value: dict) -> None:
        if value.get("error"):
            return
        name = value.get("path")
        m = self.materials.get(name)
        if m is None or value.get("source") != m.source:
            return
        # Bounded custom reader: byte-identity + exact character slice.
        if value.get("material_sha256") == m.digest:
            start, end, text = value.get("start"), value.get("end"), value.get("text")
            if (type(start) is int and type(end) is int and 0 <= start < end <= len(m.text)
                    and text == m.text[start:end]):
                self._credit(name, start, end)
            return
        # Native source read: validate what returned, not the requested end_line.
        start, count = value.get("start_line"), value.get("returned_lines")
        lines = m.text.splitlines(keepends=True)
        if not (type(start) is int and type(count) is int and 0 <= start < len(lines)
                and 0 < count <= len(lines) - start and value.get("total_lines") == len(lines)):
            return
        selected = lines[start:start + count]
        numbered = "\n".join(f"{start + i + 1}\t{line.rstrip(chr(10)).rstrip(chr(13))}" for i, line in enumerate(selected))
        raw = "".join(selected)
        if value.get("content") not in (numbered, raw):
            return
        offset = sum(len(line) for line in lines[:start])
        self._credit(name, offset, offset + len(raw))

    def observe(self, messages: list[dict]) -> None:
        """Accept only host/user input and read/recall TOOL messages, never claims."""
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    if isinstance(call, dict):
                        self.calls[call.get("id", "")] = (call.get("function") or {}).get("name", "")
                continue
            content = message.get("content")
            if isinstance(content, list):
                content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict) and x.get("type") == "text")
            if not isinstance(content, str):
                continue
            if message.get("role") == "user":
                self._inline(content)
                continue
            tool = self.calls.get(message.get("tool_call_id"))
            if message.get("role") != "tool" or tool not in ("read", "novel_bench_read", "recall_observation"):
                continue
            try:
                value = decode(content.encode())
            except (ValueError, UnicodeError):
                continue  # Includes projected marker and clipped JSON: no proof.
            if not isinstance(value, dict):
                continue
            if tool == "recall_observation":
                # Outer complete means the observation was recalled, NOT that its
                # source file was read in full. Partial/grep JSON never grants EOF.
                try:
                    value = decode(value.get("content", "").encode())
                except (ValueError, UnicodeError, AttributeError):
                    continue
                if not isinstance(value, dict):
                    continue
            self._page(value)

    def missing(self) -> list[dict]:
        return [{"path": name, "source": m.source, "start": start, "end": end,
                 "read": {"tool": "novel_bench_read", "path": "review/" + name,
                          "start": start, "length": min(8000, end - start)}}
                for name, m in self.materials.items() for start, end in holes(len(m.text), self.ranges[name])]

    def certificate(self, report: dict, claim: dict) -> dict:
        validate_targets(report, self.identity["review_key"], self.identity["targets"])
        missing = self.missing()
        require(not missing, "required review text has not been presented; read missing ranges: " + json.dumps(missing, ensure_ascii=False))
        return {"identity": self.identity, "report_sha256": sha(encode(report)),
                "claim": claim, "coverage": self.ranges, "complete": True}


def bounded_page(material: Material, start: int, length: int) -> dict:
    require(type(start) is int and 0 <= start < len(material.text), "invalid review read offset")
    require(type(length) is int and length > 0, "positive review read length required")
    # The entire JSON observation, including escaping, stays below the native
    # projection's 16KiB character limit. No budget expansion or hidden tail.
    end = min(len(material.text), start + min(length, 8000))
    while True:
        result = {"path": material.path, "source": material.source, "material_sha256": material.digest,
                  "start": start, "end": end, "text": material.text[start:end],
                  "complete": end == len(material.text), "next_start": end if end < len(material.text) else None}
        if len(encode(result).decode()) <= 12000:
            return result
        end = start + max(1, (end - start) // 2)


def replace_owned(path: Path, value: dict) -> None:
    """Atomic host-only session pointer; immutable certificates live beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), "symlink reading pointer refused")
    fd, tmp = tempfile.mkstemp(prefix=".reading-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encode(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ReviewSession:
    """Trusted host owner; never exposed as an agent tool or a verdict field."""
    def __init__(self, coverage: Coverage, directory: Path, claim: dict):
        self.coverage, self.directory, self.claim = coverage, directory, claim
        self.session = uuid.uuid4().hex
        self.pointer = directory / (coverage.identity["phase"] + "-session.json")
        replace_owned(self.pointer, {"session": self.session, "claim": claim, "certificate": None})

    def observe(self, messages: list[dict]) -> None:
        self.coverage.observe(messages)

    def guard(self, tool: str, params: dict) -> dict | None:
        if tool in ("edit_verdict", "append_verdict"):
            return {"error": "Write the complete corrected review with create_verdict/write_verdict; its reading certificate binds the entire JSON."}
        if tool not in ("create_verdict", "write_verdict"):
            return None
        try:
            raw = params if "review_key" in params else params.get("content", params.get("initialContent"))
            report = decode(raw.encode()) if isinstance(raw, str) else raw
            require(isinstance(report, dict), "complete JSON review required")
            # Honest rejection is always allowed; it cannot reach positive gates.
            if report.get("passed") is False and report.get("read_complete") is False:
                return None
            cert = self.coverage.certificate(report, self.claim)
            current = decode(read_file(self.directory, self.pointer.name))
            require(current["session"] == self.session, "review executor was superseded")
            filename = sha(encode({"session": self.session, "report": report})) + ".json"
            immutable(self.directory / filename, encode(cert))
            replace_owned(self.pointer, {"session": self.session, "claim": self.claim,
                                        "certificate": filename, "sha256": sha(encode(cert))})
            return None
        except (ValueError, OSError, TypeError) as exc:
            return {"error": str(exc), "missing_review_material": self.coverage.missing()}


def load_certificate(directory: Path, phase: str) -> dict:
    try:
        pointer = decode(read_file(directory, phase + "-session.json"))
        filename = identifier(pointer.get("certificate"))
        raw = read_file(directory, filename)
        require(sha(raw) == pointer["sha256"], "host reading certificate changed")
        cert = decode(raw)
        require(cert["claim"] == pointer["claim"], "reading executor identity changed")
        return cert
    except (OSError, KeyError, TypeError) as exc:
        raise BenchError("host-observed complete reading certificate required") from exc
