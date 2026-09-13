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
MAX_CONTEXT_CHARS = 12_000
MAX_INPUT_CHARS = 65_536
REQUEST_TIMEOUT_SECONDS = 4.0
PENDING_DIR_NAME = "project-handoff-pending"
GUIDE_HEADINGS = (
    "# State DAG director protocol",
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


def _read_marker(path: Path) -> dict[str, str] | None:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            marker = json.loads(handle.read(4_097))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict) or marker.get("version") != 2:
        return None
    if marker.get("status") not in {"pending", "delivered"}:
        return None
    if not isinstance(marker.get("generation"), str):
        return None
    return marker


def _write_marker(path: Path, marker: dict[str, Any]) -> bool:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _mark_pending(hook_input: dict[str, Any]) -> bool:
    path, lock_fd = _acquire_marker(hook_input)
    if path is None:
        return False
    try:
        generation = str(hook_input.get("turn_id", ""))
        if not generation:
            return False
        current = _read_marker(path)
        if current == {"version": 2, "generation": generation, "status": "delivered"}:
            return True
        return _write_marker(path, {
            "version": 2,
            "generation": generation,
            "status": "pending",
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
    selected = "\n\n".join(chosen)
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
    token = os.environ.get("AITELIER_ADMIN_TOKEN")
    if token and not any(k.lower() in {"authorization", "x-aitelier-admin-token"} for k in headers):
        headers["X-AItelier-Admin-Token"] = token
    return url.rstrip("/") + "/", headers


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


def _read_sources() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
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
    return note, overview, guide


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
        if readiness == "in_progress" and isinstance(latest, dict):
            run_id = latest.get("run_id") or "none"
            active.append(
                f"- {node.get('node_key', node.get('key', '?'))}: "
                f"attempt_id={latest.get('attempt_id', 'unknown')} "
                f"run_id={run_id} "
                f"attempt_status={latest.get('status', 'unknown')}"
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
    return "\n".join([
        f"event_seq={overview.get('event_seq', 'unknown')}",
        "node_status_counts=" + json.dumps(counts, sort_keys=True, separators=(",", ":")),
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
1. state_graph_read(action=\"get_driver_note\", arguments={{\"project_id\":\"{PROJECT_ID}\"}})
2. state_graph_read(action=\"project_overview\", arguments={{\"project_id\":\"{PROJECT_ID}\"}})
3. state_graph_help()
Then reconcile referenced nodes/attempts using their exact IDs and current revisions.""", MAX_CONTEXT_CHARS)


def build_context() -> str:
    try:
        note, overview, help_payload = _read_sources()
    except Exception:
        return _recovery_context()
    permanent = str(note.get("permanent", ""))
    temporary = str(note.get("temporary", ""))
    guide = str(help_payload.get("driver_guide", ""))
    context = f"""# AItelier director bootstrap after compaction
project_id={PROJECT_ID} (fixed project isolation)
driver_note_revision={note.get('revision', 'unknown')} updated_at={note.get('updated_at', 'unknown')}
driver_note_permanent_sha256={_sha256(permanent)} chars={len(permanent)}
driver_note_temporary_sha256={_sha256(temporary)} chars={len(temporary)}
state_event_cursor={overview.get('event_seq', 'unknown')}
state_driver_guide_sha256={_sha256(guide)} chars={len(guide)} source={help_payload.get('driver_resource', 'state_graph_help')}

## Current permanent director note (bounded)
{_bounded_section(permanent, 3_000)}

## Current temporary director note (bounded)
{_bounded_section(temporary, 3_000)}

## Current State snapshot (bounded)
{_frontier_summary(overview)}

## Stable State driver guidance selected from the live MCP response
{_guide_sections(guide)}

This is a bounded resume aid, not the full DAG, note history, trace, or evidence. Reconcile exact State records before dispatch, acceptance, push, or deployment."""
    return _bounded_section(context, MAX_CONTEXT_CHARS)


def _write_output(output: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _deliver_once(hook_input: dict[str, Any], event: str, *, standalone: bool) -> bool:
    """Persist delivery before emitting context, so failures fail closed."""
    path, lock_fd = _acquire_marker(hook_input)
    if path is None:
        return False
    try:
        marker = _read_marker(path)
        if marker is None:
            if not standalone:
                return False
            marker = {"version": 2, "generation": "session-start", "status": "pending"}
        if marker["status"] != "pending":
            return False
        context = build_context()
        if not _write_marker(path, {**marker, "status": "delivered"}):
            return False
        _write_output({
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": context,
            }
        })
        return True
    finally:
        _release_marker(lock_fd)


def main() -> int:
    raw_input = sys.stdin.read(MAX_INPUT_CHARS)
    try:
        hook_input = json.loads(raw_input)
    except json.JSONDecodeError:
        hook_input = {}
    event = hook_input.get("hook_event_name")
    # Codex 0.153/0.154 exposes only the universal PostCompact output. Its
    # systemMessage is an attributable warning, while model context is supported
    # by SessionStart and UserPromptSubmit. Queue the latter as the guaranteed
    # next user-input fallback; SessionStart consumes the same marker when a
    # client does emit source=compact, so the model receives the context once.
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
    elif event == "SessionStart" and hook_input.get("source") == "compact":
        if _deliver_once(hook_input, "SessionStart", standalone=True):
            return 0
        output = {"continue": True}
    elif event == "UserPromptSubmit":
        if _deliver_once(hook_input, "UserPromptSubmit", standalone=False):
            return 0
        output = {"continue": True}
    else:
        output = {"continue": True}
    _write_output(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
