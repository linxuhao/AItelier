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
import math
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
OBSERVATION_SCHEMA_VERSION = 1
OBSERVATION_FIELDS = frozenset({
    "schema_version", "observed_at", "projects", "runs", "sidecar_owners",
    "godot_render_owners", "external_owners", "registered_external_owners",
    "blockers", "errors", "quiescent", "digest",
})
SIDECAR_DESIRED = frozenset({"ready", "released"})
SIDECAR_OUTCOMES = frozenset({"pending", "ready", "released", "error"})
EXTERNAL_OWNER_STATUSES = frozenset({"active", "paused", "unknown", "settled"})
EXTERNAL_OWNER_KINDS = frozenset({"docker", "process"})
GODOT_OWNER_STATUSES = frozenset({"active", "owner_lost", "reconciled", "released"})
UNKNOWN_PROCESS_ERROR_PREFIX = (
    "unregistered external measurement process has unknown ownership: ")
BLOCKER_IDENTITY_FIELDS = {
    "active_runs": ("run_id",),
    "active_operations": ("run_id",),
    "checkout_leases": ("run_id", "owner", "canonical_checkout"),
    "sidecar_owners": ("run_id",),
    "external_active": ("attempt_id", "run_id", "operation_id", "owner_id",
                        "external_id", "id", "command", "name"),
    "registered_external_owners": ("attempt_id",),
    "godot_render_owners": ("owner_id", "operation_id", "run_id"),
    "measurement_failure": ("reason",),
}
AUTHORITATIVE_IDENTITY_FIELDS = frozenset({
    "attempt_id", "run_id", "operation_id", "owner_id", "project_id",
    "external_id", "id", "owner", "node_key", "harness",
})
OWNER_INVENTORY_BLOCKERS = {
    "runs": ("active_runs", "active_operations"),
    "sidecar_owners": ("sidecar_owners",),
    "external_owners": ("external_active",),
    "registered_external_owners": ("registered_external_owners",),
    "godot_render_owners": ("godot_render_owners",),
}


class DeploymentBlocked(RuntimeError):
    """A deployment action was refused because its measured boundary is unsafe."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _observation_digest(observation: dict) -> str:
    """Use the producer's canonical JSON encoding over the frozen inventory."""
    return _digest({key: value for key, value in observation.items()
                    if key != "digest"})


def _plain_json_snapshot(value: Any) -> Any:
    """Deep-copy plain JSON while rejecting executable/custom containers."""
    ancestors: set[int] = set()

    def clone(item: Any, path: str) -> Any:
        if type(item) is dict:
            identity = id(item)
            if identity in ancestors:
                raise ValueError(f"{path} contains a cyclic value")
            ancestors.add(identity)
            try:
                result = {}
                for key, child in item.items():
                    if type(key) is not str:
                        raise ValueError(f"{path} has a non-string object key")
                    result[key] = clone(child, f"{path}.{key}")
                return result
            finally:
                ancestors.remove(identity)
        if type(item) is list:
            identity = id(item)
            if identity in ancestors:
                raise ValueError(f"{path} contains a cyclic value")
            ancestors.add(identity)
            try:
                return [clone(child, f"{path}[{index}]")
                        for index, child in enumerate(item)]
            finally:
                ancestors.remove(identity)
        if type(item) in {str, int, bool, type(None)}:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        raise ValueError(f"{path} contains a non-JSON or non-finite value")

    try:
        return clone(value, "observation")
    except RecursionError as exc:
        raise ValueError("observation exceeds the supported JSON nesting depth") from exc
    except RuntimeError as exc:
        raise ValueError("observation changed while it was being snapshotted") from exc


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _nonnegative_number(value: Any) -> bool:
    if type(value) is int:
        return value >= 0
    return type(value) is float and math.isfinite(value) and value >= 0


def _nonempty_string(value: Any) -> bool:
    return type(value) is str and bool(value.strip())


def _normalized_owner_blockers(*, runs: list[dict], sidecar_owners: list[dict],
                               external_owners: list[dict],
                               registered_external_owners: list[dict],
                               godot_render_owners: list[dict]) -> dict[str, list[dict]]:
    """Project measured owner inventories through the producer's blocker rules."""
    return {
        "active_runs": [row for row in runs
                        if row.get("status") in BLOCKING_STATUSES],
        "active_operations": [row for row in runs
                              if isinstance(row.get("active_operations"), int)
                              and row["active_operations"] > 0],
        "sidecar_owners": [row for row in sidecar_owners
                           if row.get("desired") != "released"
                           or row.get("outcome") != "released"
                           or row.get("done_revision") != row.get("revision")],
        "external_active": [row for row in external_owners
                            if row.get("active") is True],
        "registered_external_owners": list(registered_external_owners),
        "godot_render_owners": [row for row in godot_render_owners
                                if row.get("status") in {"active", "owner_lost"}],
    }


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
            audit = skillflow.audit_operation_owners(run_id)
            if audit is None:
                audit = {}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"operation audit failed for {run_id}: {type(exc).__name__}: {exc}")
            audit = {"error": str(exc)}
        if type(audit) is not dict:
            errors.append(f"operation audit for {run_id} was malformed")
            audit = {"error": "malformed audit"}
        lost = audit.get("lost")
        unknown = audit.get("unknown")
        alive = audit.get("alive")
        audit_errors = []
        if type(lost) is not list or any(type(item) is not str for item in lost):
            audit_errors.append("lost must be a list of strings")
        if type(unknown) is not list or any(type(item) is not str for item in unknown):
            audit_errors.append("unknown must be a list of strings")
        if not _nonnegative_int(alive):
            audit_errors.append("alive must be a nonnegative integer")
        if audit_errors:
            audit_reason = "; ".join(audit_errors)
            errors.append(f"operation audit for {run_id} was malformed: {audit_reason}")
            audit = {"lost": [], "unknown": [], "alive": 0,
                     "error": audit_reason}
            lost, unknown, alive = [], [], 0
        row["audit"] = audit
        row["active_operations"] = len(lost) + len(unknown) + alive
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

    write_blockers = leases + admissions
    blockers = _normalized_owner_blockers(
        runs=runs,
        sidecar_owners=sidecar_rows,
        external_owners=external,
        registered_external_owners=registered_external,
        godot_render_owners=godot_rows,
    )
    blockers["checkout_leases"] = write_blockers
    if not godot_rows and not godot_errors:
        blockers.pop("godot_render_owners")
    observation = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
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
    inventory_error = _validate_owner_inventories(observation)
    if inventory_error is not None:
        errors.append(f"owner inventory measurement was malformed: {inventory_error}")
    observation["quiescent"] = not errors and not any(blockers.values())
    observation["digest"] = _observation_digest(observation)
    return observation


def failed_observation(reason: str) -> dict:
    """Represent an unavailable runtime measurement as unusable evidence."""
    observation = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "observed_at": _now(), "projects": [], "runs": [],
        "sidecar_owners": [], "godot_render_owners": [],
        "external_owners": [], "registered_external_owners": [],
        "blockers": {"measurement_failure": [{"reason": str(reason)[:500]}]},
        "errors": [str(reason)[:500]], "quiescent": False,
    }
    observation["digest"] = _observation_digest(observation)
    return observation


def _unknown_process_shape(row: Any) -> bool:
    return (isinstance(row, dict)
            and row.get("kind") == "process"
            and row.get("active") is True
            and row.get("resource") == "external_measurement"
            and row.get("ownership") == "unregistered")


def _unknown_process_noise(row: Any) -> bool:
    return (_unknown_process_shape(row)
            and isinstance(row.get("command"), str)
            and bool(row["command"].strip())
            and not any(field in row for field in AUTHORITATIVE_IDENTITY_FIELDS))


def _has_identity(row: dict, fields: tuple[str, ...]) -> bool:
    return any(isinstance(row.get(field), str) and row[field].strip()
               for field in fields)


def _validate_owner_inventories(observation: dict) -> str | None:
    inventories: dict[str, list[dict]] = {}
    for name in OWNER_INVENTORY_BLOCKERS:
        rows = observation.get(name)
        if type(rows) is not list:
            return f"{name} must be a list"
        if any(type(row) is not dict for row in rows):
            return f"{name} rows must be plain objects"
        inventories[name] = rows

    for index, row in enumerate(inventories["runs"]):
        prefix = f"runs row {index}"
        if not _nonempty_string(row.get("run_id")):
            return f"{prefix} needs a non-empty run_id"
        if not _nonempty_string(row.get("project_id")):
            return f"{prefix} needs a non-empty project_id"
        if row.get("status") not in TERMINAL_STATUSES | BLOCKING_STATUSES:
            return f"{prefix} has unknown status {row.get('status')!r}"
        if not _nonnegative_int(row.get("active_operations")):
            return f"{prefix} active_operations must be a nonnegative integer"
        audit = row.get("audit")
        if type(audit) is not dict:
            return f"{prefix} audit must be a plain object"
        lost = audit.get("lost")
        unknown = audit.get("unknown")
        alive = audit.get("alive")
        if type(lost) is not list or any(type(item) is not str for item in lost):
            return f"{prefix} audit.lost must be a list of strings"
        if type(unknown) is not list or any(type(item) is not str for item in unknown):
            return f"{prefix} audit.unknown must be a list of strings"
        if not _nonnegative_int(alive):
            return f"{prefix} audit.alive must be a nonnegative integer"
        if row["active_operations"] != len(lost) + len(unknown) + alive:
            return f"{prefix} active_operations contradicts its audit"

    for index, row in enumerate(inventories["sidecar_owners"]):
        prefix = f"sidecar_owners row {index}"
        for field in ("run_id", "root", "source"):
            if not _nonempty_string(row.get(field)):
                return f"{prefix} needs a non-empty {field}"
        if row.get("desired") not in SIDECAR_DESIRED:
            return f"{prefix} has unknown desired state {row.get('desired')!r}"
        if row.get("outcome") not in SIDECAR_OUTCOMES:
            return f"{prefix} has unknown outcome {row.get('outcome')!r}"
        for field in ("revision", "done_revision"):
            if not _nonnegative_int(row.get(field)):
                return f"{prefix} {field} must be a nonnegative integer"
        if not _nonnegative_number(row.get("activity_at")):
            return f"{prefix} activity_at must be a nonnegative finite number"
        if type(row.get("error")) is not str:
            return f"{prefix} error must be a string"

    for index, row in enumerate(inventories["external_owners"]):
        prefix = f"external_owners row {index}"
        if row.get("kind") not in EXTERNAL_OWNER_KINDS:
            return f"{prefix} has unknown kind {row.get('kind')!r}"
        if type(row.get("active")) is not bool:
            return f"{prefix} active must be a boolean"
        identity_field = "id" if row["kind"] == "docker" else "command"
        if not _nonempty_string(row.get(identity_field)):
            return f"{prefix} needs a non-empty {identity_field}"
        if "ownership" in row and row["ownership"] not in {"registered", "unregistered"}:
            return f"{prefix} has unknown ownership {row.get('ownership')!r}"

    for index, row in enumerate(inventories["registered_external_owners"]):
        prefix = f"registered_external_owners row {index}"
        if not _nonempty_string(row.get("attempt_id")):
            return f"{prefix} needs a non-empty attempt_id"
        if row.get("status") not in EXTERNAL_OWNER_STATUSES:
            return f"{prefix} has unknown status {row.get('status')!r}"

    for index, row in enumerate(inventories["godot_render_owners"]):
        prefix = f"godot_render_owners row {index}"
        if not _nonempty_string(row.get("owner_id")):
            return f"{prefix} needs a non-empty owner_id"
        if row.get("status") not in GODOT_OWNER_STATUSES:
            return f"{prefix} has unknown status {row.get('status')!r}"
        if "generation" in row and not _nonnegative_int(row["generation"]):
            return f"{prefix} generation must be a nonnegative integer"
        for field in ("started_at", "heartbeat_at", "ended_at"):
            if field in row and row[field] is not None \
                    and not _nonnegative_number(row[field]):
                return f"{prefix} {field} must be a nonnegative finite number"
    return None


def _validate_observation(observation: Any) -> str | None:
    """Validate the complete gate observation before any authorization path."""
    if type(observation) is not dict:
        return "observation must be a plain object"
    if type(observation.get("schema_version")) is not int \
            or observation["schema_version"] != OBSERVATION_SCHEMA_VERSION:
        return (f"unsupported observation schema_version "
                f"{observation.get('schema_version')!r}")
    unknown_fields = sorted(set(observation) - OBSERVATION_FIELDS)
    if unknown_fields:
        return "unknown observation fields: " + ", ".join(unknown_fields)
    observed_at = observation.get("observed_at")
    try:
        observed = datetime.fromisoformat(observed_at)
    except (TypeError, ValueError):
        return "observed_at must be an ISO-8601 timestamp"
    if observed.tzinfo is None:
        return "observed_at must include a timezone"
    projects = observation.get("projects")
    if (type(projects) is not list
            or any(not _nonempty_string(project) for project in projects)):
        return "projects must be a list of non-empty strings"
    if projects != sorted(set(projects)):
        return "projects must be sorted and unique"
    inventory_error = _validate_owner_inventories(observation)
    if inventory_error is not None:
        return inventory_error
    blockers = observation.get("blockers")
    if type(blockers) is not dict:
        return "blockers must be an object (plain dict required)"
    for name, rows in blockers.items():
        if name not in BLOCKER_IDENTITY_FIELDS:
            return f"unknown blocker category {name!r}"
        if type(rows) is not list:
            return f"blockers.{name} must be a list"
        for index, row in enumerate(rows):
            if type(row) is not dict:
                return (f"blockers.{name} rows must be objects "
                        f"(plain dict required, row {index})")
            if name == "external_active" and _unknown_process_shape(row):
                command = row.get("command")
                if not isinstance(command, str) or not command.strip():
                    return f"blockers.{name} noise row {index} needs a non-empty command"
                identities = sorted(AUTHORITATIVE_IDENTITY_FIELDS.intersection(row))
                if identities:
                    return (f"blockers.{name} noise row {index} carries authoritative "
                            f"identity fields: {', '.join(identities)}")
            elif not _has_identity(row, BLOCKER_IDENTITY_FIELDS[name]):
                return f"blockers.{name} row {index} lacks ownership identity"
    errors = observation.get("errors")
    if not isinstance(errors, list) or any(not isinstance(error, str) for error in errors):
        return "errors must be a list of strings"
    digest = observation.get("digest")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return "digest must be a lowercase hexadecimal SHA-256"
    try:
        expected_digest = _observation_digest(observation)
    except (TypeError, ValueError) as exc:
        return f"observation cannot be canonically digested: {exc}"
    if digest != expected_digest:
        return "digest does not match the canonical frozen inventory"
    inventories = {name: observation[name] for name in OWNER_INVENTORY_BLOCKERS}
    normalized = _normalized_owner_blockers(
        runs=inventories["runs"],
        sidecar_owners=inventories["sidecar_owners"],
        external_owners=inventories["external_owners"],
        registered_external_owners=inventories["registered_external_owners"],
        godot_render_owners=inventories["godot_render_owners"],
    )
    for inventory_name, blocker_names in OWNER_INVENTORY_BLOCKERS.items():
        for blocker_name in blocker_names:
            if blockers.get(blocker_name, []) != normalized[blocker_name]:
                return (f"blockers.{blocker_name} does not match normalized "
                        f"{inventory_name} inventory")
    declared_quiescent = observation.get("quiescent")
    if not isinstance(declared_quiescent, bool):
        return "quiescent must be a boolean"
    computed_quiescent = not errors and not any(blockers.values())
    if declared_quiescent != computed_quiescent:
        return ("quiescent contradicts validated blockers/errors: "
                f"declared={declared_quiescent} computed={computed_quiescent}")
    return None


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
    try:
        observation_value = _plain_json_snapshot(observation)
        observation_snapshot_error = None
    except (TypeError, ValueError) as exc:
        observation_value = {}
        observation_snapshot_error = str(exc)
    try:
        override_value = (_plain_json_snapshot(override)
                          if override is not None else None)
    except (TypeError, ValueError) as exc:
        override_value = {"_invalid_override": str(exc)}
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
        validation_error = (observation_snapshot_error
                            or _validate_observation(observation_value))
        if validation_error is not None:
            reason = f"observation is malformed: {validation_error}"
            valid = False
        elif observation_value["quiescent"] is True:
            event = _append_to(state, {"action": action, "status": "authorized",
                                       "pending": True, "usable": False,
                                       "inventory_digest": observation_value["digest"],
                                       "blockers": observation_value["blockers"],
                                       "errors": observation_value["errors"]})
            _atomic_write(path, state)
            return {"allowed": True, "replayed": False, "event": event}
        else:
            valid, reason = _valid_override(action, observation_value, override_value)
        if valid:
            authoritative, unknown_processes = _classify_blockers(observation_value)
            unknown_scope = override_value.get("acknowledge_unknown") is True
            event = _append_to(state, {"action": action, "status": "overridden",
                                       "pending": True, "usable": False,
                                       "audit": {"actor": override_value["actor"],
                                                 "reason": override_value["reason"],
                                                 "ticket": override_value["ticket"],
                                                 "override_scope": (
                                                     "unknown_process_noise" if unknown_scope
                                                     else "authoritative_blockers"),
                                                 "affected_ownership": (
                                                     unknown_processes if unknown_scope
                                                     else authoritative)},
                                       "inventory_digest": observation_value["digest"],
                                       "blockers": observation_value["blockers"],
                                       "errors": observation_value["errors"]})
            _atomic_write(path, state)
            return {"allowed": True, "replayed": False, "event": event}
        event = _append_to(state, {"action": action, "status": "aborted",
                                   "usable": False, "reason": reason,
                                   "inventory_digest": observation_value.get("digest"),
                                   "blockers": observation_value.get("blockers"),
                                   "errors": observation_value.get("errors")})
        _atomic_write(path, state)
    raise DeploymentBlocked(
        f"refusing {action}: cross-project deployment is not quiescent; "
        f"evidence {event['event_id']} is aborted/unusable ({reason})")


def finalize(clearance: dict, *, success: bool, error: str | None = None,
             journal: Path | str | None = None) -> dict:
    """Commit or abort the deployment action represented by a pending gate."""
    try:
        clearance_value = _plain_json_snapshot(clearance)
    except (TypeError, ValueError) as exc:
        raise DeploymentBlocked(f"deployment clearance is malformed: {exc}") from exc
    if type(success) is not bool:
        raise DeploymentBlocked("deployment result success must be a boolean")
    path = Path(journal) if journal is not None else evidence_path()
    prior = clearance_value.get("event") if type(clearance_value) is dict else None
    if type(prior) is not dict:
        raise DeploymentBlocked("deployment clearance has no pending event")
    with _journal_lock(path):
        _ensure_journal(path)
        state = _load_journal(path)
        latest = state.get("latest") or {}
        if latest.get("event_id") != prior.get("event_id"):
            raise DeploymentBlocked("deployment clearance was superseded; refusing finalization")
        if latest.get("status") not in {"authorized", "overridden"} \
                or latest.get("pending") is not True:
            raise DeploymentBlocked(
                "latest deployment evidence is not a pending authorization")
        if prior.get("action") != latest.get("action"):
            raise DeploymentBlocked("deployment clearance action does not match the journal")
        if prior.get("inventory_digest") != latest.get("inventory_digest"):
            raise DeploymentBlocked(
                "deployment clearance inventory digest does not match the journal")
        if latest.get("action") not in DEPLOY_ACTIONS:
            raise DeploymentBlocked("persisted deployment action is malformed")
        if (not isinstance(latest.get("inventory_digest"), str)
                or re.fullmatch(r"[0-9a-f]{64}", latest["inventory_digest"]) is None):
            raise DeploymentBlocked("persisted deployment inventory digest is malformed")
        event = _append_to(state, {
            "action": latest["action"],
            "status": "completed" if success else "aborted",
            "pending": False,
            "usable": success,
            "replayed": False,
            "prior_event_id": latest["event_id"],
            "inventory_digest": latest["inventory_digest"],
            **({} if success else {"reason": str(error or "deployment action failed")[:500]}),
        })
        _atomic_write(path, state)
    return {"allowed": bool(success), "replayed": False, "event": event}


def reconcile(*, observation: dict, journal: Path | str | None = None) -> dict:
    """Reconcile an aborted gate without replaying its deployment action."""
    try:
        observation_value = _plain_json_snapshot(observation)
        observation_snapshot_error = None
    except (TypeError, ValueError) as exc:
        observation_value = {}
        observation_snapshot_error = str(exc)
    validation_error = (observation_snapshot_error
                        or _validate_observation(observation_value))
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
        if validation_error is not None:
            reason = f"reconciliation observation is malformed: {validation_error}"
        elif observation_value["quiescent"] is not True:
            reason = "reconciliation still not quiescent"
        else:
            reason = None
        if reason is not None:
            event = _append_to(state, {"action": latest.get("action"),
                                       "status": "aborted", "usable": False,
                                       "replayed": False,
                                       "reason": reason,
                                       "inventory_digest": observation_value.get("digest"),
                                       "blockers": observation_value.get("blockers"),
                                       "errors": observation_value.get("errors")})
            _atomic_write(path, state)
            return {"reconciled": False, "replayed": False, "event": event}
        event = _append_to(state, {"action": latest.get("action"),
                                   "status": "reconciled_quiescent", "usable": False,
                                   "replayed": False,
                                   "inventory_digest": observation_value["digest"],
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
