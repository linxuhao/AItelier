#!/usr/bin/env python3
"""Emit bounded, fresh AItelier State context for the Codex PostCompact hook."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ID = "aitelier"
# 20_000, not 12_000, since 2026-09-21: the durable assertions this hook must
# re-inject live in the note's ENTRY INDEX, and since 2026-10-06 the index is the
# whole notebook (the free-text sections are closed and get_driver_note no longer
# returns them). The guide field shrank by far more than that in the same move --
# state_graph_help serves STATE_DRIVER_GUIDE_INDEX (3.7k), not the 27.8k guide --
# so the ceiling rises while the payload does not.
MAX_CONTEXT_CHARS = 20_000
MAX_ENTRY_INDEX_CHARS = 10_000  # 7_000 dropped a line on first run; the budget must
# not be the reason an assertion goes missing. It reports omissions rather than
# hiding them, and the outer MAX_CONTEXT_CHARS still bounds the whole payload.
# The index arrives oldest-first, so cutting its end would drop exactly the newest
# lines (every supersede successor); over the cap the OLDEST line goes first.
ENTRY_INDEX_LABEL = "current entries"
MAX_STANDING_CONTEXT_CHARS = 3_000
MAX_INPUT_CHARS = 65_536
REQUEST_TIMEOUT_SECONDS = 4.0
PENDING_DIR_NAME = "project-handoff-pending"
MARKER_VERSION = 3
MARKER_PENDING = "pending"
MARKER_ATTEMPTED = "delivery_attempted"
MARKER_NO_ACK = "none"
DELIVERY_ATTEMPTED = "attempted"
DELIVERY_NOOP = "noop"
DELIVERY_FAILED = "failed"
SESSION_BOOTSTRAP_SOURCES = frozenset({"startup", "resume", "compact"})
GUIDE_HEADINGS = (
    "# State DAG director protocol",
    "## Driver loop: whoami, claim, dispatch, heartbeat",
    "## Resume safely",
    "## Dispatch through either executor",
    "## Wait instead of repeatedly querying",
    "## Director notebook: context, not a second State database",
)


class SourceUnavailable(RuntimeError):
    pass


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _pending_path(hook_input: dict[str, Any]) -> Path | None:
    """Return a checkout- and session-scoped handoff marker path."""
    session_id = hook_input.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    checkout = str(Path(__file__).resolve().parents[2])
    key = _sha256(f"{PROJECT_ID}\0{checkout}\0{session_id}")
    return codex_home / PENDING_DIR_NAME / f"{key}.json"


def _acquire_marker(hook_input: dict[str, Any]) -> tuple[Path | None, int | None]:
    path = _pending_path(hook_input)
    if path is None:
        return None, None
    lock_fd: int | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_stat = path.parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid():
            return None, None
        os.chmod(path.parent, 0o700)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(path.with_suffix(".lock"), flags, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return path, lock_fd
    except OSError:
        if lock_fd is not None:
            os.close(lock_fd)
        return None, None


def _release_marker(lock_fd: int | None) -> None:
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _valid_generation(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_marker(path: Path) -> tuple[str, dict[str, str] | None]:
    try:
        marker_stat = path.lstat()
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "invalid", None
    if not stat.S_ISREG(marker_stat.st_mode) or marker_stat.st_uid != os.getuid():
        return "invalid", None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            marker = json.loads(handle.read(4_097))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", None
    if not isinstance(marker, dict) or not _valid_generation(marker.get("generation")):
        return "invalid", None
    if marker.get("version") == 2:
        if marker.get("status") == "pending":
            return "valid", {
                "version": str(MARKER_VERSION),
                "generation": marker["generation"],
                "status": MARKER_PENDING,
            }
        if marker.get("status") == "delivered":
            return "valid", {
                "version": str(MARKER_VERSION),
                "generation": marker["generation"],
                "status": MARKER_ATTEMPTED,
                "acknowledgement": MARKER_NO_ACK,
            }
        return "invalid", None
    if marker.get("version") != MARKER_VERSION:
        return "invalid", None
    if marker.get("status") == MARKER_PENDING:
        return "valid", {
            "version": str(MARKER_VERSION),
            "generation": marker["generation"],
            "status": MARKER_PENDING,
        }
    if (
        marker.get("status") == MARKER_ATTEMPTED
        and marker.get("acknowledgement") == MARKER_NO_ACK
    ):
        return "valid", {
            "version": str(MARKER_VERSION),
            "generation": marker["generation"],
            "status": MARKER_ATTEMPTED,
            "acknowledgement": MARKER_NO_ACK,
        }
    return "invalid", None


def _write_marker(path: Path, marker: dict[str, Any]) -> bool:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    replaced = False
    parent_fd: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        replaced = True
        parent_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        parent_fd = os.open(path.parent, parent_flags)
        os.fsync(parent_fd)
        return True
    except OSError:
        if not replaced:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _mark_pending(hook_input: dict[str, Any]) -> bool:
    path, lock_fd = _acquire_marker(hook_input)
    if path is None:
        return False
    try:
        generation = hook_input.get("turn_id")
        if not _valid_generation(generation):
            return False
        state, current = _read_marker(path)
        if state == "invalid":
            return False
        if current and current["generation"] == generation and current["status"] == MARKER_ATTEMPTED:
            return True
        # Some clients can report the compact SessionStart before the matching
        # PostCompact callback. That activation already delivered the handoff;
        # bind its provisional marker to the real turn without reopening it.
        if (
            current
            and current["generation"] == "compact-session-start"
            and current["status"] == MARKER_ATTEMPTED
        ):
            return _write_marker(path, {
                "version": MARKER_VERSION,
                "generation": generation,
                "status": MARKER_ATTEMPTED,
                "acknowledgement": MARKER_NO_ACK,
            })
        return _write_marker(path, {
            "version": MARKER_VERSION,
            "generation": generation,
            "status": MARKER_PENDING,
        })
    finally:
        _release_marker(lock_fd)


def _redact(text: str) -> str:
    # Remove whole PEM/OpenSSH private-key blocks before handling inline
    # assignments.  Values in tests are synthetic; never put a real credential
    # in a test merely to exercise this boundary.
    text = re.sub(
        r"(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
    )
    patterns = (
        (r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)(authorization\s*[:=]\s*basic\s+)[^\s,;]+", r"\1[REDACTED]"),
        (r"(?i)(://[^\s/:@]+:)[^\s/@]+(@)", r"\1[REDACTED]\2"),
        (
            r"(?i)((?:password|passwd|pwd|passphrase|private[_ -]?key|credential(?:s)?|"
            r"x-aitelier-admin-token|api[_-]?key|access[_-]?token|refresh[_-]?token|"
            r"auth[_-]?token|client[_-]?secret|secret)\s*[\"']?\s*[:=]\s*)"
            r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\]}]+)",
            r"\1[REDACTED]",
        ),
        (r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_\-]{16,}\b", "[REDACTED]"),
        (r"\bAKIA[A-Z0-9]{16}\b", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def _bounded_section(text: str, limit: int) -> str:
    text = _redact(text.strip())
    if len(text) <= limit:
        return text
    marker = f"\n... [truncated by hook; full_chars={len(text)} sha256={_sha256(text)}] ...\n"
    room = max(0, limit - len(marker))
    head = room * 2 // 3
    return text[:head] + marker + text[-(room - head) :]


def _centered_excerpt(text: str, needle: str, limit: int = 400) -> str:
    """Return a bounded live-source excerpt that always retains ``needle``."""
    match_at = text.lower().find(needle.lower())
    if match_at < 0:
        return ""
    start = max(0, match_at - (limit - len(needle)) // 2)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    excerpt = text[start:end].strip()
    if start:
        excerpt = "... " + excerpt
    if end < len(text):
        excerpt += " ..."
    return _redact(excerpt)


def _guide_sections(guide: str, limit: int = 4_000) -> str:
    lines = guide.splitlines()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        if line.startswith("#"):
            current = line.strip()
            sections.setdefault(current, [])
        if current is not None:
            sections[current].append(line)
    chosen = ["\n".join(sections[h]).strip() for h in GUIDE_HEADINGS if h in sections]
    # The live guide is served in index mode and no longer carries these exact
    # headings. An empty selection would silently drop the rules this hook exists
    # to re-inject, so fall back to the whole (already bounded) guide.
    selected = "\n\n".join(chosen) if chosen else guide.strip()
    search_excerpt = _centered_excerpt(guide, "search_driver_note_history")
    warning_excerpt = next(
        (
            excerpt
            for marker in (
                "Do not load the full driver_note_history",
                "Do not load driver_note_history in full",
                "Do not load the full history",
            )
            if (excerpt := _centered_excerpt(guide, marker))
        ),
        "",
    )
    if not search_excerpt:
        return _bounded_section(selected, limit)
    excerpts = [search_excerpt]
    if warning_excerpt and warning_excerpt != search_excerpt:
        excerpts.append(warning_excerpt)
    suffix = "\n\n## Selected live driver-note history search guidance\n" + "\n".join(excerpts)
    return _bounded_section(selected, max(0, limit - len(suffix))) + suffix


def _entry_index(note: dict[str, Any], limit: int = MAX_ENTRY_INDEX_CHARS) -> str:
    """Render the driver note's ENTRY INDEX: one assertion per line, with its status.

    Since 2026-10-06 the index is the whole notebook and every entry is a rule:
    the free-text sections are closed, informational entries are refused, and
    in-flight state lives in the State DAG. get_driver_note lists only current,
    listed entries by default; a superseded line that still arrives is skipped.
    Bodies stay behind get_driver_note_entry and are never injected.

    Over the cap the OLDEST line is dropped first, and the header states how many
    lines were omitted. The header comes first and every line carries its position
    [i] of 0..total-1, so a reader of a truncated copy can see what it is missing.
    """
    index = note.get("index")
    if not isinstance(index, list) or not index:
        return "- none listed (driver_note_index returned no entries)"
    current = [item for item in index if isinstance(item, dict)
               and item.get("lifecycle") != "superseded"
               and str(item.get("index_line", "")).strip()]
    if not current:
        return "- none listed (entries present but carried no current index_line)"
    total = len(current)
    lines: list[str] = []
    for i, item in enumerate(current):
        entry_id = str(item.get("address", "")).rsplit("/", 1)[-1] or "?"
        line = str(item.get("index_line", "")).replace("\n", " ").strip()
        lines.append(f"- [{i}] {entry_id} {line}")
    omitted = 0
    while True:
        rendered = _redact("\n".join([
            f"### {ENTRY_INDEX_LABEL}: total={total} shown={len(lines)} "
            f"omitted_entry_index_lines={omitted}"
            + (f" (oldest [0]..[{omitted - 1}] dropped)" if omitted else ""),
            *lines]))
        if len(rendered) <= limit or not lines:
            return rendered
        lines.pop(0)
        omitted += 1


def _codex_config() -> dict[str, Any]:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    path = codex_home / "config.toml"
    if not path.is_file():
        return {}
    # Codex ships on machines whose system python can predate tomllib.  Only
    # read the two string keys needed from this exact table; no general TOML
    # interpretation or shell evaluation is involved.
    result: dict[str, Any] = {}
    in_aitelier = False
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("[") and line.endswith("]"):
                in_aitelier = line == "[mcp_servers.aitelier]"
                continue
            if not in_aitelier or "=" not in line:
                continue
            key, raw_value = (part.strip() for part in line.split("=", 1))
            if key not in {"url", "http_headers_helper"}:
                continue
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                continue
            if isinstance(value, str):
                result[key] = value
    except OSError:
        return {}
    return result


def _connection() -> tuple[str, dict[str, str]]:
    config = _codex_config()
    url = os.environ.get("AITELIER_MCP_URL") or config.get("url") or "http://127.0.0.1:4444/mcp/"
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise SourceUnavailable("invalid MCP endpoint")
    headers: dict[str, str] = {}
    helper = config.get("http_headers_helper")
    if isinstance(helper, str) and helper:
        try:
            proc = subprocess.run(
                [helper], text=True, capture_output=True, timeout=2.0, check=False,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            if proc.returncode == 0:
                parsed = json.loads(proc.stdout)
                if isinstance(parsed, dict):
                    headers.update({str(k): str(v) for k, v in parsed.items() if isinstance(v, str)})
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
    # Per-driver token first (design/multi-driver-coop.md §3; same order as
    # core/driver_credentials.py, inlined because hooks stay dependency-free),
    # then the legacy admin token exactly as before.
    credential_keys = {"authorization", "x-aitelier-admin-token", "x-aitelier-driver-token"}
    if not any(k.lower() in credential_keys for k in headers):
        driver_token = _driver_token()
        token = os.environ.get("AITELIER_ADMIN_TOKEN")
        if driver_token:
            headers["X-AItelier-Driver-Token"] = driver_token
        elif token:
            headers["X-AItelier-Admin-Token"] = token
    return url.rstrip("/") + "/", headers


def _driver_token() -> str | None:
    """AITELIER_DRIVER_TOKEN, AITELIER_DRIVER_TOKEN_FILE, or ~/.aitelier-drivers/$AITELIER_DRIVER_ID.token.

    A file readable by group/other is ignored (fail closed to the legacy path).
    """
    token = (os.environ.get("AITELIER_DRIVER_TOKEN") or "").strip()
    if token:
        return token
    path = (os.environ.get("AITELIER_DRIVER_TOKEN_FILE") or "").strip()
    driver_id = (os.environ.get("AITELIER_DRIVER_ID") or "").strip()
    if not path and driver_id and "/" not in driver_id and not driver_id.startswith("."):
        path = os.path.join(os.path.expanduser("~"), ".aitelier-drivers", driver_id + ".token")
    if not path:
        return None
    try:
        if os.stat(path).st_mode & 0o077:
            return None
        with open(path, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value if value and "\n" not in value else None


def _mcp_call(url: str, headers: dict[str, str], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        **headers,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read(2_000_000))
    except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        raise SourceUnavailable(f"{name} unavailable") from exc
    envelope = payload.get("result", {})
    if envelope.get("isError"):
        raise SourceUnavailable(f"{name} returned an error")
    try:
        text = next(item["text"] for item in envelope["content"] if item.get("type") == "text")
        decoded = json.loads(text)
    except (KeyError, StopIteration, TypeError, json.JSONDecodeError) as exc:
        raise SourceUnavailable(f"{name} returned an invalid envelope") from exc
    if decoded.get("isError"):
        raise SourceUnavailable(f"{name} returned an error")
    return decoded.get("result", decoded)


def _read_sources() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    url, headers = _connection()
    note = _mcp_call(url, headers, "state_graph_read", {
        "action": "get_driver_note", "arguments": {"project_id": PROJECT_ID},
    })
    if note.get("project_id") != PROJECT_ID:
        raise SourceUnavailable("driver note project mismatch")
    overview = _mcp_call(url, headers, "state_graph_read", {
        "action": "project_overview", "arguments": {"project_id": PROJECT_ID},
    })
    guide = _mcp_call(url, headers, "state_graph_help", {})
    standing = _mcp_call(url, headers, "state_graph_read", {
        "action": "list_director_messages",
        "arguments": {
            "project_id": PROJECT_ID,
            "after": 0,
            "limit": 8,
            "delivery_mode": "standing",
            "statuses": ["unread", "acknowledged"],
        },
    })
    private_guidance = standing.get("schema") == "aitelier.director-messaging.v3"
    if "driver_postcompact_guidance" in guide.get("operations", {}):
        identity = _mcp_call(url, headers, "driver_whoami", {})
        private_guidance = bool(identity.get("driver_id") or str(identity.get("actor","")).startswith("owner:"))
    if private_guidance:
        standing = _mcp_call(url, headers, "state_graph_read", {
            "action": "driver_postcompact_guidance", "arguments": {"project_id": PROJECT_ID},
        })
    return note, overview, guide, standing


def _active_standing_projection(envelope: dict[str, Any]) -> str:
    """Render whole, redacted v2 inbox lines inside the contract's hard cap."""
    if envelope.get("schema") == "aitelier.driver-guidance.v1":
        projection = envelope.get("projection")
        if (envelope.get("project_id") != PROJECT_ID or not isinstance(projection,str)
                or len(projection)>3000 or type(envelope.get("included")) is not int
                or not 0<=envelope["included"]<=8):
            raise SourceUnavailable("private guidance bounds mismatch")
        return projection
    if envelope.get("schema") != "aitelier.director-messaging.v2":
        raise SourceUnavailable("director inbox schema mismatch")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise SourceUnavailable("director inbox result missing")
    if result.get("project_id") != PROJECT_ID:
        raise SourceUnavailable("director inbox project mismatch")
    items = result.get("items")
    matched_total = result.get("matched_total")
    if (not isinstance(items, list) or len(items) > 8
            or type(matched_total) is not int or matched_total < len(items)):
        raise SourceUnavailable("director inbox result invalid")
    lines: list[str] = []
    previous_seq = 0
    for item in items:
        try:
            message = item["message"]
            delivery = item["delivery"]
            if message["delivery_mode"] != "standing" or delivery["status"] not in {
                    "unread", "acknowledged"}:
                raise SourceUnavailable("director inbox filter mismatch")
            if type(delivery["delivery_seq"]) is not int or delivery["delivery_seq"] <= previous_seq:
                raise SourceUnavailable("director inbox order mismatch")
            previous_seq = delivery["delivery_seq"]
            payload = {
                "message_id": message["message_id"],
                "thread_id": message["thread_id"],
                "delivery_id": delivery["delivery_id"],
                "sender_project_id": message["sender_project_id"],
                "delivery_seq": delivery["delivery_seq"],
                "status": delivery["status"],
                "version": delivery["version"],
                "subject": _redact(message["subject"]),
                "body_excerpt": _redact(message["body"])[:320],
            }
        except (KeyError, TypeError) as exc:
            raise SourceUnavailable("director inbox item invalid") from exc
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")))
    included = len(lines)
    while True:
        omitted = matched_total - included
        parts = lines[:included]
        if omitted:
            parts.append(f"[omitted_active_standing={omitted}]")
        projection = "\n".join(parts)
        if len(projection) <= MAX_STANDING_CONTEXT_CHARS:
            return projection
        included -= 1


def _frontier_summary(overview: dict[str, Any]) -> str:
    nodes = overview.get("nodes") if isinstance(overview.get("nodes"), list) else []
    counts: dict[str, int] = {}
    selected: list[str] = []
    active: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        status = str(node.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
        readiness = str(node.get("readiness", "unknown"))
        next_action = node.get("next_action")
        latest = node.get("latest_attempt")
        # in_progress_lease_expired (multi-driver P1) is still active ownership:
        # the attempt holds its slot; only its owner stopped renewing the lease.
        if readiness in {"in_progress", "in_progress_lease_expired"} and isinstance(latest, dict):
            run_id = latest.get("run_id") or "none"
            owner = latest.get("owner") or latest.get("reporting_actor") or "unknown"
            checkpoint = latest.get("checkpoint") or "none"
            checkpoint_id = latest.get("checkpoint_id") or "none"
            lease = ""
            if latest.get("lease_state") not in {None, "legacy_unleased"}:
                lease = (f" owner_driver_id={latest.get('owner_driver_id') or 'none'}"
                         f" lease_state={latest.get('lease_state')}")
            active.append(
                f"- {node.get('node_key', node.get('key', '?'))}: "
                f"attempt_id={latest.get('attempt_id', 'unknown')} "
                f"run_id={run_id} "
                f"attempt_status={latest.get('status', 'unknown')} "
                f"owner={owner} checkpoint={checkpoint} checkpoint_id={checkpoint_id}{lease}"
            )
        if readiness == "ready" and next_action in {"new_attempt", "candidate_review"}:
            candidate = ""
            if next_action == "candidate_review" and isinstance(latest, dict):
                candidate = (
                    f" attempt_id={latest.get('attempt_id', 'unknown')}"
                    f" artifact_ref={latest.get('artifact_ref', 'unknown')}"
                )
            selected.append(
                f"- {node.get('node_key', node.get('key', '?'))}: node={status} "
                f"readiness=ready next_action={next_action}{candidate}"
            )
    selected = selected[:8]
    active = active[:8]
    policy = overview.get("policy") if isinstance(overview.get("policy"), dict) else {}
    return "\n".join([
        f"event_seq={overview.get('event_seq', 'unknown')}",
        "node_status_counts=" + json.dumps(counts, sort_keys=True, separators=(",", ":")),
        # Multi-driver: policy (multi_driver is always on), the two switches and the advisory review counter (P4).
        f"policy multi_driver={policy.get('multi_driver', 'unknown')} "
        f"claim_enforcement={policy.get('claim_enforcement', 'unknown')} "
        f"review_independence={policy.get('review_independence', 'unknown')} "
        f"self_reviewed_receipts={overview.get('self_reviewed_receipts', 'unknown')}",
        "bounded active ownership (readiness=in_progress, max 8; retain identities):",
        *(active or ["- none listed"]),
        "bounded actionable frontier (readiness=ready, max 8; reconcile exact records before acting):",
        *(selected or ["- none listed"]),
    ])


def _recovery_context(failed_sources: str = "driver note, State overview, or guide") -> str:
    return _bounded_section(f"""# AItelier director bootstrap (recovery required)
project_id={PROJECT_ID}
The current {failed_sources} could not be loaded within the bounded PostCompact hook request. Do not use ~/.AItelier/DRIVER_STATE.md and do not infer current ownership or acceptance from this message.
Before continuing director work, reconnect the configured AItelier MCP and call:
0. driver_whoami() - it must answer driver:<your id> before any write
1. state_graph_read(action=\"get_driver_note\", arguments={{\"project_id\":\"{PROJECT_ID}\"}})
2. state_graph_read(action=\"project_overview\", arguments={{\"project_id\":\"{PROJECT_ID}\"}})
3. state_graph_help()
Then reconcile referenced nodes/attempts using their exact IDs and current revisions.""", MAX_CONTEXT_CHARS)


def build_context() -> str:
    try:
        note, overview, help_payload, standing_envelope = _read_sources()
        standing = _active_standing_projection(standing_envelope)
    except Exception:
        return _recovery_context()
    guide = str(help_payload.get("driver_guide", ""))
    context = f"""# AItelier director bootstrap after compaction
project_id={PROJECT_ID} (fixed project isolation)
driver_note_revision={note.get('revision', 'unknown')}
state_event_cursor={overview.get('event_seq', 'unknown')}
state_driver_guide_sha256={_sha256(guide)} chars={len(guide)} source={help_payload.get('driver_resource', 'state_graph_help')}

## Current driver note ENTRY INDEX (current rules; bodies fetched by address)
Each line is an assertion WITH its status. A line that contradicts what you are about
to do wins until you have re-measured it. In-flight state is not here: read it from
the State DAG (attempts, node holds, issues, priorities). Fetch a body with
state_graph_read(action="get_driver_note_entry", arguments={{"project_id":"{PROJECT_ID}","entry_id":"<12-hex>"}}).
entry_count={note.get('entry_count', 'unknown')} listed={note.get('listed_count', 'unknown')} delisted={note.get('delisted_count', 'unknown')} superseded={note.get('superseded_count', 'unknown')}
{_entry_index(note)}

## Current State snapshot (bounded)
{_frontier_summary(overview)}

## Multi-driver loop (bounded reminder; guide://driver-loop-whoami-claim-dispatch-heartbeat)
whoami (driver_whoami must answer driver:<your id>) -> claim_node(implement|review) on a ready node -> start_*attempt with claim_id+fence (register_subagent records your workers) -> heartbeat(claims, attempts, subagents) every 20-30 min from the client wait loop while you supervise -> record_evidence, verify_node, release_claim or offer_handoff. Lapsed leases are reclaimable by any member; never renew unattended.
Two inboxes: project inbox = send_director_message (ack means "I take this"; ack_mode=at_least_n default, ack_mode=broadcast needs every member's ack; distinct from the cross-project broadcast flag). Driver inbox = send_driver_message / list_driver_messages / wait_for_driver_inbox (private; system notices land here). Your private notebook dnote://<your id> is fetched by address with driver_id, never injected here. The standing section below is driver_postcompact_guidance: project standing, your unacked broadcast transients, your standing driver notices.

## Stable State driver guidance selected from the live MCP response
{_guide_sections(guide)}

This is a bounded resume aid, not the full DAG, note history, trace, or evidence. Reconcile exact State records before dispatch, acceptance, push, or deployment."""
    standing_section = "\n\n## Active standing director guidance (bounded, read-only)\n" + (
        standing or "- none")
    return (_bounded_section(context, MAX_CONTEXT_CHARS - len(standing_section))
            + standing_section)


def _write_output(output: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _claim_context(
    hook_input: dict[str, Any], *, standalone: bool,
    missing_generation: str = "session-start",
) -> tuple[str, str | None]:
    """Persist one no-ACK delivery attempt and return its fresh context."""
    path, lock_fd = _acquire_marker(hook_input)
    if path is None:
        return (DELIVERY_FAILED if standalone else DELIVERY_NOOP), None
    try:
        state, marker = _read_marker(path)
        if state == "invalid":
            return DELIVERY_FAILED, None
        if state == "missing":
            if not standalone:
                return DELIVERY_NOOP, None
            marker = {
                "version": MARKER_VERSION,
                "generation": missing_generation,
                "status": MARKER_PENDING,
            }
        if marker["status"] != MARKER_PENDING:
            return DELIVERY_NOOP, None
        context = build_context()
        if not _write_marker(path, {
            "version": MARKER_VERSION,
            "generation": marker["generation"],
            "status": MARKER_ATTEMPTED,
            "acknowledgement": MARKER_NO_ACK,
        }):
            return DELIVERY_FAILED, None
        return DELIVERY_ATTEMPTED, context
    finally:
        _release_marker(lock_fd)


def _deliver_once(
    hook_input: dict[str, Any], event: str, *, standalone: bool,
    missing_generation: str = "session-start",
) -> str:
    """Persist one no-ACK delivery attempt before emitting additional context."""
    delivery, context = _claim_context(
        hook_input, standalone=standalone, missing_generation=missing_generation
    )
    if delivery == DELIVERY_ATTEMPTED:
        assert context is not None
        _write_output({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": context,
            }
        })
    return delivery


def _continue_once(hook_input: dict[str, Any]) -> str:
    """Use Stop's supported continuation prompt when compact activation was absent."""
    delivery, context = _claim_context(hook_input, standalone=False)
    if delivery == DELIVERY_ATTEMPTED:
        assert context is not None
        _write_output({"decision": "block", "reason": context})
    return delivery


def _delivery_recovery_context() -> str:
    return _bounded_section(f"""# AItelier compact handoff recovery required
project_id={PROJECT_ID}
The local handoff marker was malformed, unreadable, or its no-ACK delivery attempt could not be persisted. No model context was injected and no work was dispatched by this hook.
Reconnect the configured AItelier MCP and reconcile the exact State driver note, owner, run, checkpoint, attempt, and current generation before continuing. Do not infer successful delivery from this message.""", MAX_CONTEXT_CHARS)


def main() -> int:
    raw_input = sys.stdin.read(MAX_INPUT_CHARS)
    try:
        hook_input = json.loads(raw_input)
    except json.JSONDecodeError:
        hook_input = {}
    event = hook_input.get("hook_event_name")
    # PostCompact has no model-context output. Queue the next supported
    # SessionStart/UserPromptSubmit delivery, with Stop's supported continuation
    # prompt as the same-turn fallback. The marker records an at-most-once
    # attempt because Codex provides no acknowledgement that stdout reached the
    # model.
    if event == "PostCompact":
        queued = _mark_pending(hook_input)
        context = build_context()
        if not queued:
            context = _bounded_section(
                context + "\n\nAutomatic model-context follow-up could not be queued; "
                "use the recovery calls above before director work.",
                MAX_CONTEXT_CHARS,
            )
        output = {"continue": True, "systemMessage": context}
    elif event == "SessionStart" and hook_input.get("source") in SESSION_BOOTSTRAP_SOURCES:
        delivery = _deliver_once(
            hook_input,
            "SessionStart",
            standalone=True,
            missing_generation=(
                "compact-session-start"
                if hook_input.get("source") == "compact"
                else "session-start"
            ),
        )
        if delivery == DELIVERY_ATTEMPTED:
            return 0
        output = {"continue": True}
        if delivery == DELIVERY_FAILED:
            output["systemMessage"] = _delivery_recovery_context()
    elif event == "UserPromptSubmit":
        delivery = _deliver_once(hook_input, "UserPromptSubmit", standalone=False)
        if delivery == DELIVERY_ATTEMPTED:
            return 0
        output = {"continue": True}
        if delivery == DELIVERY_FAILED:
            output["systemMessage"] = _delivery_recovery_context()
    elif event == "Stop":
        delivery = _continue_once(hook_input)
        if delivery == DELIVERY_ATTEMPTED:
            return 0
        output = {"continue": True}
        if delivery == DELIVERY_FAILED:
            output["systemMessage"] = _delivery_recovery_context()
    else:
        output = {"continue": True}
    _write_output(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
