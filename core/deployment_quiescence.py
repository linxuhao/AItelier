"""Read-only deployment quiescence measurement and fail-closed authorization.

Restarting one service can affect every project: SkillFlow runs share the
engine, the AItelier database owns checkout admissions, and the semantic
sidecar owns indexes for several run worktrees.  This module measures those
boundaries together before a rebuild, redeploy, or restart.  It deliberately
does not stop workers or replay work.

An unsuccessful gate is durable evidence with ``status=aborted`` and
``usable=false``.  Reconciliation only records that a later fresh measurement
is quiet; it never replays the interrupted deployment action.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core import datadir

TERMINAL_STATUSES = frozenset({"completed", "failed"})
BLOCKING_STATUSES = frozenset({"pending", "running", "paused", "draining"})
DEPLOY_ACTIONS = frozenset({"rebuild", "redeploy", "restart"})
UNKNOWN_PROCESS_ERROR_PREFIX = (
    "unregistered external measurement process has unknown ownership: ")


class DeploymentBlocked(RuntimeError):
    """A deployment action was refused because its measured boundary is unsafe."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evidence_path() -> Path:
    """Persistent journal outside project workspaces and State."""
    return datadir.aitelier_home() / "deployment-quiescence" / "journal.json"


def admission_fence_path() -> Path:
    return datadir.godot_control_dir() / "deployment-admission.lock"


def _open_fence():
    path = admission_fence_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stream = path.open("a+b")
    os.chmod(path, 0o600)
    return stream


def acquire_cutover_fence():
    """Exclude new main/sidecar operation admissions until cutover settles."""
    stream = _open_fence()
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
    except BaseException:
        stream.close()
        raise
    return stream


def release_cutover_fence(stream) -> None:
    if stream is None:
        return
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


@contextmanager
def operation_admission_fence():
    """Share the cutover lock only while an operation becomes durable."""
    stream = _open_fence()
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        payload = (json.dumps(value, sort_keys=True, indent=2,
                              ensure_ascii=True) + "\n").encode("utf-8")
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _load_journal(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "events": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DeploymentBlocked(
            f"deployment quiescence journal is unreadable at {path}: {exc}; "
            "refusing to replay or start a deployment action") from exc
    if (not isinstance(value, dict) or value.get("version") != 1
            or not isinstance(value.get("events"), list)):
        raise DeploymentBlocked(
            f"deployment quiescence journal at {path} is malformed; "
            "refusing to replay or start a deployment action")
    return value


def _recover_corrupt_journal(path: Path, reason: str) -> dict:
    """Preserve malformed bytes and replace them with explicit failed evidence."""
    raw = b""
    try:
        raw = path.read_bytes()
    except OSError:
        pass
    digest = hashlib.sha256(raw).hexdigest()
    backup = path.with_name(f"{path.name}.corrupt.{digest[:16]}")
    if path.exists() and not backup.exists():
        try:
            backup.write_bytes(raw)
            os.chmod(backup, 0o600)
        except OSError:
            # The original is still retained if quarantine cannot be written;
            # the replacement below remains fail-closed evidence.
            backup = None
    event = {"event_id": uuid.uuid4().hex, "at": _now(),
             "action": "unknown", "status": "aborted", "usable": False,
             "reason": f"corrupt deployment journal: {reason}",
             "corrupt_sha256": digest}
    if backup is not None:
        event["preserved_bytes"] = str(backup)
    _atomic_write(path, {"version": 1, "events": [event], "latest": event})
    return event


def _ensure_journal(path: Path) -> None:
    try:
        _load_journal(path)
    except DeploymentBlocked as exc:
        event = _recover_corrupt_journal(path, str(exc))
        raise DeploymentBlocked(
            f"deployment quiescence journal was malformed and converted to "
            f"aborted/unusable "
            f"evidence {event['event_id']} at {path}") from exc


@contextmanager
def _journal_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_name(path.name + ".lock").open("a+b") as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _append_to(journal: dict, event: dict) -> dict:
    event = {"event_id": uuid.uuid4().hex, "at": _now(), **event}
    journal["events"].append(event)
    journal["latest"] = event
    return event


def _append_event(path: Path, event: dict) -> dict:
    with _journal_lock(path):
        journal = _load_journal(path)
        appended = _append_to(journal, event)
        _atomic_write(path, journal)
        return appended


def _run_command(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=10,
                          check=False)


def _command_has_identity(command: str, external_id: str) -> bool:
    """Match an owner token without allowing an ID suffix collision."""
    token = external_id.strip().lower()
    if not token:
        return False
    try:
        words = [word.lower() for word in shlex.split(command)]
    except ValueError:
        words = command.lower().split()
    if token in words:
        return True
    boundary = r"(?<![a-z0-9_.-])" + re.escape(token) + r"(?![a-z0-9_.-])"
    return re.search(boundary, command.lower()) is not None


def _resident_service_identity(command: str) -> bool:
    """Recognize only the executable identity of a resident service.

    Repository, input, and output arguments are data.  They must not turn a
    real evaluator into an ignored service merely because their path contains
    a service name.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    if len(words) < 3:
        return False
    executable = Path(words[2]).name.lower()
    if executable.startswith("zvec-grep"):
        return True
    if executable == "skillflow" or executable.startswith("skillflow-"):
        return True
    for index, word in enumerate(words[2:-1], start=2):
        if word == "-m" and words[index + 1].lower() in {"skillflow", "skillflow_mcp"}:
            return True
    return False


def external_owners(*, runner: Callable[[list[str]], subprocess.CompletedProcess]
                    = _run_command,
                    registered_external_owners: list[dict] | None = None
                    ) -> tuple[list[dict], list[str]]:
    """Measure resident external processes without treating them as work.

    Resident services are not blockers by themselves.  A Godot render/compile
    command is a real owner even when its harness container is merely ``Up``;
    that distinction keeps a live render from being hidden by a service row.
    """
    owners: list[dict] = []
    errors: list[str] = []
    registered_external_owners = registered_external_owners or []
    docker = runner(["docker", "ps", "--format",
                     "{{.ID}}\t{{.Names}}\t{{.Label \"com.docker.compose.service\"}}\t{{.Status}}"])
    if docker.returncode == 0:
        for line in (docker.stdout or "").splitlines():
            parts = line.split("\t", 3)
            if len(parts) >= 2:
                name = parts[1]
                service = parts[2] if len(parts) > 2 else ""
                render_service = ("godot" in name.lower()
                                  and service not in {"", "godot-builder"})
                owners.append({"kind": "docker", "id": parts[0],
                               "name": name, "service": service,
                               "status": parts[3] if len(parts) > 3 else "",
                               "active": render_service,
                               "resource": "render" if render_service else ""})
    else:
        errors.append(f"docker inventory failed: {(docker.stderr or '').strip()[:300]}")
    processes = runner(["ps", "-axo", "pid=,ppid=,command="])
    if processes.returncode == 0:
        needles = ("godot-builder", "zvec-grep", "skillflow", "aitelier",
                   "godot --", "godot --headless", "xvfb-run")
        for line in (processes.stdout or "").splitlines():
            lowered = line.lower()
            measurement_name = bool(re.search(
                r"(?:^|[^a-z0-9])(measurement|evaluation|evaluator|eval(?:[_-]?job)?|"
                r"benchmark|playtest|judge|grader|grading|scor(?:e|ing)|"
                r"assessment|assessor|rater|review|quality[_-]?check|"
                r"metrics?|"
                r"[a-z0-9]+[_-](?:worker|job)|[a-z0-9]+(?:worker|job))"
                r"(?:[^a-z0-9]|$)", lowered)) or "--long-gate" in lowered
            # These are already enumerated shared services, not an unknown
            # evaluator worker whose ownership needs State admission.
            unknown_measurement = measurement_name and not _resident_service_identity(line)
            if any(needle in lowered for needle in needles) or unknown_measurement:
                command = line.strip()
                active = any(token in lowered for token in (
                    "godot --", "godot --headless", "xvfb-run",
                    "playtest", "render", "x11_input_smoke", "run_script")) or unknown_measurement
                matched = next((row for row in registered_external_owners
                                if row.get("status") in {"active", "paused", "unknown"}
                                and isinstance(row.get("external_id"), str)
                                and _command_has_identity(command, row["external_id"])), None)
                ownership = "registered" if matched else "unregistered"
                owners.append({"kind": "process", "command": command,
                               "active": active,
                               "resource": ("external_measurement" if unknown_measurement
                                            else "render" if active else ""),
                               **({"ownership": ownership} if unknown_measurement else {}),
                               **({"attempt_id": matched["attempt_id"]}
                                  if matched and matched.get("attempt_id") else {})})
                if unknown_measurement:
                    if not matched:
                        errors.append(
                            "unregistered external measurement process has unknown ownership: "
                            + command[:500])
    else:
        errors.append(f"process inventory failed: {(processes.stderr or '').strip()[:300]}")
    return owners, errors


def _sidecar_rows(path: Path | None) -> tuple[list[dict], list[str]]:
    if path is None:
        return [], []
    if path.is_symlink():
        return [], [f"sidecar ledger is a symlink: {path}"]
    if not path.exists():
        return [], []
    try:
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(
                "SELECT run_id,root,source,desired,revision,done_revision,"
                "outcome,error,activity_at FROM indexes ORDER BY run_id")]
        return rows, []
    except (OSError, sqlite3.Error) as exc:
        return [], [f"sidecar ledger measurement failed: {type(exc).__name__}: {exc}"]


def _godot_rows(path: Path | None) -> tuple[list[dict], list[str]]:
    if path is None or not path.exists():
        return [], []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM render_owners ORDER BY generation, owner_id")]
        return rows, []
    except (OSError, sqlite3.Error) as exc:
        return [], [f"Godot owner ledger measurement failed: {type(exc).__name__}: {exc}"]


def _db_rows(db) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    if db is None:
        return [], [], [], []
    leases: list[dict] = []
    admissions: list[dict] = []
    external_registry: list[dict] = []
    errors: list[str] = []
    try:
        with db.get_connection() as conn:
            for table, target in (("checkout_leases", leases),
                                  ("checkout_write_admissions", admissions)):
                try:
                    target.extend(dict(row) for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"))
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc):
                        errors.append(f"{table} measurement failed: {exc}")
            try:
                external_registry.extend(dict(row) for row in conn.execute(
                    "SELECT attempt_id,project_id,node_key,harness,external_id,status,"
                    "admitted_at,updated_at,settled_at FROM state_external_owners "
                    "WHERE status != 'settled' ORDER BY project_id,node_key,attempt_id"))
                active_attempts = [dict(row) for row in conn.execute(
                    "SELECT attempt_id,project_id,node_key,harness,external_id,status "
                    "FROM state_attempts WHERE execution_kind='external' "
                    "AND status IN ('running','paused','unknown') "
                    "ORDER BY project_id,node_key,attempt_id")]
                registered = {row["attempt_id"]: row for row in external_registry}
                for attempt in active_attempts:
                    owner = registered.get(attempt["attempt_id"])
                    expected = {"running": "active", "paused": "paused", "unknown": "unknown"}[
                        attempt["status"]]
                    if owner is None:
                        errors.append(
                            "external owner registry is missing active attempt "
                            + attempt["attempt_id"])
                        external_registry.append({
                            **attempt, "status": "unknown", "ownership": "registry_missing",
                            "admitted_at": None, "updated_at": None, "settled_at": None,
                        })
                    elif owner["status"] != expected:
                        errors.append(
                            f"external owner registry status mismatch for {attempt['attempt_id']}: "
                            f"attempt={attempt['status']} owner={owner['status']}")
                    else:
                        mismatched = [field for field in (
                            "project_id", "node_key", "harness", "external_id")
                            if owner[field] != attempt[field]]
                        if mismatched:
                            errors.append(
                                f"external owner registry identity mismatch for {attempt['attempt_id']}: "
                                + ",".join(mismatched))
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    errors.append(f"external owner registry measurement failed: {exc}")
                else:
                    try:
                        unregistered = conn.execute(
                            "SELECT COUNT(*) FROM state_attempts WHERE execution_kind='external' "
                            "AND status IN ('running','paused','unknown')").fetchone()[0]
                    except sqlite3.OperationalError:
                        unregistered = 0
                    if unregistered:
                        errors.append(
                            "external owner registry is missing while external attempts are active")
    except Exception as exc:  # noqa: BLE001 -- unknown DB state blocks deployment
        errors.append(f"AItelier DB measurement failed: {type(exc).__name__}: {exc}")
    return leases, admissions, external_registry, errors


def measure(*, skillflow, db=None, sidecar_db: Path | str | None = None,
            external_probe: Callable[[], list[dict]] | None = None,
            command_runner: Callable[[list[str]], subprocess.CompletedProcess]
            = _run_command) -> dict:
    """Take one cross-project read-only observation from real runtime owners."""
    runs: list[dict] = []
    errors: list[str] = []
    try:
        raw_runs = skillflow.list_runs() or []
    except Exception as exc:  # noqa: BLE001
        raw_runs = []
        errors.append(f"SkillFlow run inventory failed: {type(exc).__name__}: {exc}")
    if not isinstance(raw_runs, list):
        errors.append("SkillFlow run inventory was not a list")
        raw_runs = []
    for raw in raw_runs:
        if not isinstance(raw, dict):
            errors.append("SkillFlow returned a malformed run row")
            continue
        run_id = raw.get("id") or raw.get("run_id")
        project_id = raw.get("project_id") or raw.get("project")
        status = raw.get("status")
        row = {"run_id": run_id, "project_id": project_id, "status": status}
        if not isinstance(run_id, str) or not run_id:
            errors.append("SkillFlow run has no usable id")
            row["error"] = "missing run id"
            runs.append(row)
            continue
        if not isinstance(project_id, str) or not project_id:
            errors.append(f"SkillFlow run {run_id} has no project identity")
        if not isinstance(status, str) or not status:
            errors.append(f"SkillFlow run {run_id} has no status")
        elif status not in TERMINAL_STATUSES and status not in BLOCKING_STATUSES:
            errors.append(f"SkillFlow run {run_id} has unknown status {status!r}")
        try:
            audit = skillflow.audit_operation_owners(run_id) or {}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"operation audit failed for {run_id}: {type(exc).__name__}: {exc}")
            audit = {"error": str(exc)}
        if not isinstance(audit, dict):
            errors.append(f"operation audit for {run_id} was malformed")
            audit = {"error": "malformed audit"}
        row["audit"] = audit
        row["active_operations"] = (
            len(audit.get("lost") or []) + len(audit.get("unknown") or [])
            + int(audit.get("alive") or 0)
            if isinstance(audit.get("alive", 0), int) else None)
        runs.append(row)

    leases, admissions, registered_external, db_errors = _db_rows(db)
    errors.extend(db_errors)
    sidecar_rows, sidecar_errors = _sidecar_rows(
        Path(sidecar_db) if sidecar_db is not None else None)
    errors.extend(sidecar_errors)
    if external_probe is None:
        external, external_errors = external_owners(
            runner=command_runner,
            registered_external_owners=registered_external)
        errors.extend(external_errors)
    else:
        try:
            external = external_probe()
            if not isinstance(external, list) or any(not isinstance(x, dict) for x in external):
                raise ValueError("external probe must return a list of objects")
        except Exception as exc:  # noqa: BLE001
            external = []
            errors.append(f"external owner measurement failed: {type(exc).__name__}: {exc}")

    godot_db = None
    if sidecar_db is not None:
        godot_db = Path(sidecar_db).parent.parent / "godot-control" / "owners.sqlite3"
    godot_rows, godot_errors = _godot_rows(godot_db)
    errors.extend(godot_errors)
    resident_godot = [r for r in external
                      if r.get("service") == "godot-builder"
                      or "aitelier-godot" in str(r.get("name", "")).lower()
                      or "godot-builder" in str(r.get("command", "")).lower()]
    if resident_godot and (godot_db is None or not godot_db.is_file()):
        errors.append(f"Godot owner ledger is missing while its shared sidecar is resident: {godot_db}")
    if sidecar_db is not None and not Path(sidecar_db).exists():
        shared = [r for r in external
                  if r.get("service") in {"zvec-grep", "godot-builder"}
                  or any(name in str(r.get("name", "")).lower()
                         for name in ("zvec-grep", "aitelier-godot"))
                  or any(name in str(r.get("command", "")).lower()
                         for name in ("zvec-grep", "godot-builder", "godot"))]
        if shared:
            errors.append(f"shared sidecar ledger is missing: {sidecar_db}")

    active_runs = [r for r in runs if r.get("status") in BLOCKING_STATUSES]
    active_ops = [r for r in runs if isinstance(r.get("active_operations"), int)
                  and r["active_operations"] > 0]
    sidecar_blockers = [r for r in sidecar_rows
                        if r.get("desired") != "released"
                        or r.get("outcome") != "released"
                        or r.get("done_revision") != r.get("revision")]
    write_blockers = leases + admissions
    external_blockers = [r for r in external if r.get("active") is True]
    godot_blockers = [r for r in godot_rows
                      if r.get("status") in {"active", "owner_lost"}]
    blockers = {
        "active_runs": active_runs,
        "active_operations": active_ops,
        "checkout_leases": write_blockers,
        "sidecar_owners": sidecar_blockers,
        "external_active": external_blockers,
        "registered_external_owners": registered_external,
    }
    if godot_rows or godot_errors:
        blockers["godot_render_owners"] = godot_blockers
    observation = {
        "observed_at": _now(),
        "projects": sorted(
            {r["project_id"] for r in runs if isinstance(r.get("project_id"), str)}
            | {r["project_id"] for r in registered_external
               if isinstance(r.get("project_id"), str)}),
        "runs": runs,
        "sidecar_owners": sidecar_rows,
        "godot_render_owners": godot_rows,
        "external_owners": external,
        "registered_external_owners": registered_external,
        "blockers": blockers,
        "errors": errors,
    }
    observation["quiescent"] = not errors and not any(blockers.values())
    observation["digest"] = _digest(observation)
    return observation


def failed_observation(reason: str) -> dict:
    """Represent an unavailable runtime measurement as unusable evidence."""
    observation = {
        "observed_at": _now(), "projects": [], "runs": [],
        "sidecar_owners": [], "external_owners": [],
        "blockers": {"measurement_failure": [{"reason": str(reason)[:500]}]},
        "errors": [str(reason)[:500]], "quiescent": False,
    }
    observation["digest"] = _digest(observation)
    return observation


def _unknown_process_noise(row: Any) -> bool:
    return (isinstance(row, dict)
            and row.get("kind") == "process"
            and row.get("active") is True
            and row.get("resource") == "external_measurement"
            and row.get("ownership") == "unregistered")


def _classify_blockers(observation: dict) -> tuple[dict, list[dict]]:
    """Separate authoritative owners from heuristic process-name matches."""
    blockers = observation.get("blockers")
    if not isinstance(blockers, dict):
        return {"measurement_failure": [{"reason": "blocker inventory is malformed"}]}, []

    authoritative: dict[str, Any] = {}
    unknown_processes: list[dict] = []
    for name, rows in blockers.items():
        if not rows:
            continue
        if name != "external_active" or not isinstance(rows, list):
            authoritative[name] = rows
            continue
        known_rows = []
        for row in rows:
            if _unknown_process_noise(row):
                unknown_processes.append(row)
            else:
                known_rows.append(row)
        if known_rows:
            authoritative[name] = known_rows

    expected_unknown_errors = {
        UNKNOWN_PROCESS_ERROR_PREFIX + str(row.get("command", ""))[:500]
        for row in unknown_processes
    }
    errors = observation.get("errors", [])
    if not isinstance(errors, list):
        authoritative["measurement_errors"] = ["measurement errors are malformed"]
    else:
        known_errors = [error for error in errors
                        if not isinstance(error, str)
                        or error not in expected_unknown_errors]
        if known_errors:
            authoritative["measurement_errors"] = known_errors
    return authoritative, unknown_processes


def _valid_override(action: str, observation: dict, override: dict | None) -> tuple[bool, str]:
    if not isinstance(override, dict):
        return False, "no audited override supplied"
    if override.get("_invalid_override"):
        return False, f"override is malformed: {override['_invalid_override']}"
    required = ("action", "actor", "reason", "ticket", "inventory_digest", "expires_at")
    if any(not isinstance(override.get(key), str) or not override[key].strip()
           for key in required):
        return False, "override needs action, actor, reason, ticket, inventory_digest and expires_at"
    if override["action"] != action:
        return False, f"override is for {override['action']!r}, not {action!r}"
    if override["inventory_digest"] != observation.get("digest"):
        return False, "override inventory digest does not match this fresh measurement"
    try:
        expires = datetime.fromisoformat(override["expires_at"])
    except (TypeError, ValueError):
        return False, "override expiry is not an ISO-8601 timestamp"
    if expires.tzinfo is None:
        return False, "override expiry must include a timezone"
    if expires <= datetime.now(UTC):
        return False, "override has expired"
    authoritative, unknown_processes = _classify_blockers(observation)
    if override.get("acknowledge_unknown") is True and authoritative:
        return False, ("unknown-process override cannot bypass authoritative active owners: "
                       + ", ".join(sorted(authoritative)))
    if (unknown_processes or observation.get("errors")) \
            and override.get("acknowledge_unknown") is not True:
        return False, "override must acknowledge unknown measurement errors"
    return True, "audited override accepted"


def authorize(action: str, observation: dict, *, journal: Path | str | None = None,
              override: dict | None = None) -> dict:
    """Authorize a deployment action, persisting every refusal or override."""
    if action not in DEPLOY_ACTIONS:
        raise ValueError(f"unknown deployment action {action!r}")
    path = Path(journal) if journal is not None else evidence_path()
    with _journal_lock(path):
        _ensure_journal(path)
        state = _load_journal(path)
        latest = state.get("latest") or {}
        if latest.get("pending") is True:
            _append_to(state, {"action": latest.get("action"),
                               "status": "aborted", "usable": False,
                               "replayed": False,
                               "reason": "deployment authorization was interrupted",
                               "prior_event_id": latest.get("event_id")})
        if observation.get("quiescent") is True:
            event = _append_to(state, {"action": action, "status": "authorized",
                                       "pending": True, "usable": False,
                                       "inventory_digest": observation.get("digest"),
                                       "blockers": observation.get("blockers", {}),
                                       "errors": observation.get("errors", [])})
            _atomic_write(path, state)
            return {"allowed": True, "replayed": False, "event": event}
        valid, reason = _valid_override(action, observation, override)
        if valid:
            authoritative, unknown_processes = _classify_blockers(observation)
            unknown_scope = override.get("acknowledge_unknown") is True
            event = _append_to(state, {"action": action, "status": "overridden",
                                       "pending": True, "usable": False,
                                       "audit": {"actor": override["actor"],
                                                 "reason": override["reason"],
                                                 "ticket": override["ticket"],
                                                 "override_scope": (
                                                     "unknown_process_noise" if unknown_scope
                                                     else "authoritative_blockers"),
                                                 "affected_ownership": (
                                                     unknown_processes if unknown_scope
                                                     else authoritative)},
                                       "inventory_digest": observation.get("digest"),
                                       "blockers": observation.get("blockers", {}),
                                       "errors": observation.get("errors", [])})
            _atomic_write(path, state)
            return {"allowed": True, "replayed": False, "event": event}
        event = _append_to(state, {"action": action, "status": "aborted",
                                   "usable": False, "reason": reason,
                                   "inventory_digest": observation.get("digest"),
                                   "blockers": observation.get("blockers", {}),
                                   "errors": observation.get("errors", [])})
        _atomic_write(path, state)
    raise DeploymentBlocked(
        f"refusing {action}: cross-project deployment is not quiescent; "
        f"evidence {event['event_id']} is aborted/unusable ({reason})")


def finalize(clearance: dict, *, success: bool, error: str | None = None,
             journal: Path | str | None = None) -> dict:
    """Commit or abort the deployment action represented by a pending gate."""
    path = Path(journal) if journal is not None else evidence_path()
    prior = (clearance or {}).get("event") or {}
    with _journal_lock(path):
        _ensure_journal(path)
        state = _load_journal(path)
        latest = state.get("latest") or {}
        if latest.get("event_id") != prior.get("event_id"):
            raise DeploymentBlocked("deployment clearance was superseded; refusing finalization")
        event = _append_to(state, {
            "action": prior.get("action"),
            "status": "completed" if success else "aborted",
            "usable": bool(success),
            "replayed": False,
            "prior_event_id": prior.get("event_id"),
            **({} if success else {"reason": str(error or "deployment action failed")[:500]}),
        })
        _atomic_write(path, state)
    return {"allowed": bool(success), "replayed": False, "event": event}


def reconcile(*, observation: dict, journal: Path | str | None = None) -> dict:
    """Reconcile an aborted gate without replaying its deployment action."""
    path = Path(journal) if journal is not None else evidence_path()
    with _journal_lock(path):
        _ensure_journal(path)
        state = _load_journal(path)
        latest = state.get("latest")
        if latest and latest.get("pending") is True:
            event = _append_to(state, {"action": latest.get("action"),
                                       "status": "aborted", "usable": False,
                                       "replayed": False,
                                       "reason": "deployment authorization was interrupted",
                                       "prior_event_id": latest.get("event_id")})
            _atomic_write(path, state)
            return {"reconciled": False, "replayed": False, "event": event}
        if not latest or latest.get("status") != "aborted":
            return {"reconciled": False, "replayed": False,
                    "reason": "no aborted deployment evidence"}
        if observation.get("quiescent") is not True:
            event = _append_to(state, {"action": latest.get("action"),
                                       "status": "aborted", "usable": False,
                                       "reason": "reconciliation still not quiescent",
                                       "inventory_digest": observation.get("digest"),
                                       "blockers": observation.get("blockers", {}),
                                       "errors": observation.get("errors", [])})
            _atomic_write(path, state)
            return {"reconciled": False, "replayed": False, "event": event}
        event = _append_to(state, {"action": latest.get("action"),
                                   "status": "reconciled_quiescent", "usable": False,
                                   "replayed": False,
                                   "inventory_digest": observation.get("digest"),
                                   "prior_event_id": latest.get("event_id")})
        _atomic_write(path, state)
        return {"reconciled": True, "replayed": False, "event": event}


def load_override(path: str | Path | None) -> dict | None:
    """Read an operator-supplied audited override; malformed data fails closed."""
    if not path:
        return None
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"_invalid_override": str(exc)}
    if not isinstance(value, dict):
        return {"_invalid_override": "override must be a JSON object"}
    return value
