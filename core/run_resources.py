"""Host lifecycle policy for run-owned semantic resources."""
from __future__ import annotations

import math
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from core import datadir
from core.semantic_index_control import IndexControl, validate_checkout


def enabled() -> bool:
    return os.environ.get("AITELIER_ZVEC_LIFECYCLE", "0") == "1"


def control() -> IndexControl:
    return IndexControl(datadir.semantic_index_control_dir(), datadir.worktrees_dir())


def prepare(rec: dict) -> None:
    """Publish demand; index outages degrade search, not run isolation/start."""
    if not enabled() or rec.get("mode") not in ("worktree", "read_snapshot"):
        return
    try:
        from api.dependencies import get_skillflow
        run = get_skillflow().get_run(rec["run_id"])
        if not isinstance(run, dict):
            raise TypeError("run observation is unavailable")
        if run.get("status") not in ("pending", "running", "paused"):
            return  # terminal runs are not reopened by inspecting isolation
        control().request(rec, "ready", touch=True)
    except Exception as exc:  # noqa: BLE001 -- retain/degrade on unknown provider or I/O failures
        import logging
        logging.getLogger("aitelier.run_resources").warning(
            "semantic_index_prepare_degraded run=%s reason=%s: %s",
            rec.get("run_id"), type(exc).__name__, str(exc)[:500])


def terminal_quiet(sf, run_id: str) -> tuple[dict | None, str]:
    """Require a known terminal run and a complete, positive operation audit."""
    try:
        row = sf.get_run(run_id)
        if not isinstance(row, dict):
            return None, "run is unknown to the engine"
        if row.get("status") not in ("completed", "failed"):
            return None, f"run is {row.get('status')!r}, not terminal"
        audit = sf.audit_operation_owners(run_id)
        if (not isinstance(audit, dict) or not isinstance(audit.get("lost"), list)
                or not isinstance(audit.get("unknown"), list)
                or type(audit.get("alive")) is not int or audit["alive"] < 0):
            return None, "operation audit is incomplete or malformed"
        count = len(audit["lost"]) + len(audit["unknown"]) + audit["alive"]
        if count:
            return None, f"{count} admitted operation(s) have not retired"
        return row, "terminal and quiet"
    except Exception as exc:  # noqa: BLE001 -- retain/degrade on unknown provider or I/O failures
        return None, f"engine observation failed: {type(exc).__name__}: {exc}"


def reconcile(db, sf, run_id: str | None = None) -> dict:
    """Reconcile recorded run roots, including worktrees that hold no lease."""
    from core import run_isolation as ri
    records = [ri.record(db, run_id)] if run_id else ri.retained(db)
    results = {"requested": [], "retained": []}
    for rec in records:
        if not rec or rec.get("mode") not in (ri.MODE_WORKTREE, ri.MODE_READ_SNAPSHOT):
            continue
        if (rec.get("disposition") or "").startswith("reaped_"):
            continue
        rid = rec["run_id"]
        try:
            row = sf.get_run(rid)
            if not isinstance(row, dict):
                raise TypeError("run is unknown to the engine")
            if row.get("status") in ("pending", "running", "paused"):
                if enabled():
                    control().request(rec, "ready")
                continue
            row, reason = terminal_quiet(sf, rid)
            if row is None:
                raise ValueError(reason)
            validate_checkout(rec, datadir.worktrees_dir())
            if enabled():
                control().request(rec, "released")
            # Terminal ownership is not an assertion of discard or integration.
            with db.get_connection() as conn:
                conn.execute("""UPDATE run_isolation SET
                    released_at=COALESCE(released_at,datetime('now')),
                    disposition=COALESCE(disposition,'auto_released_terminal_quiet')
                    WHERE run_id=?""", (rid,))
                conn.commit()
            results["requested"].append(rid)
        except Exception as exc:  # noqa: BLE001 -- retain/degrade on unknown provider or I/O failures
            results["retained"].append({"run_id": rid, "reason": str(exc)[:500]})
    return results


def idle_eligible(rec: dict, row: dict, ledger: dict | None = None, *,
                  now: float | None = None, minimum: float | None = None) -> tuple[bool, str]:
    """Use observed activity, not directory age or the time of a sweep."""
    try:
        limit = float(minimum if minimum is not None else os.environ.get(
            "AITELIER_WORKTREE_IDLE_SECONDS", "86400"))
        if not math.isfinite(limit) or limit < 0:
            raise ValueError("idle delay must be finite and nonnegative")
        if limit == 0:
            return True, "idle delay explicitly disabled"
        now = time.time() if now is None else now
        if not math.isfinite(now):
            raise ValueError("invalid clock")
        stamps = []
        if not (row.get("updated_at") or row.get("completed_at")):
            raise ValueError("engine last-activity timestamp unavailable")
        for value in (rec.get("created_at"), rec.get("released_at"),
                      row.get("updated_at"), row.get("completed_at")):
            if value is None:
                continue
            dt = datetime.fromisoformat(str(value))
            stamps.append(dt.replace(tzinfo=UTC).timestamp()
                          if dt.tzinfo is None else dt.timestamp())
        if ledger:
            stamps.append(float(ledger["activity_at"]))
        from core.semantic_index_control import git
        for name in ("HEAD", "index"):
            result = git(rec["worktree_path"], "rev-parse", "--path-format=absolute", "--git-path", name)
            if result.returncode or not result.stdout.strip():
                raise ValueError("Git activity observation failed")
            p = Path(result.stdout.strip())
            if p.exists():
                stamps.append(p.stat().st_mtime)
        if not stamps or any(not math.isfinite(t) or t > now for t in stamps):
            raise ValueError("activity is missing, malformed or in the future")
        idle = now - max(stamps)
        return idle >= limit, f"idle {int(idle)}s; minimum {int(limit)}s"
    except Exception as exc:  # noqa: BLE001 -- retain/degrade on unknown provider or I/O failures
        return False, f"cannot establish inactivity: {exc}"


@contextmanager
def search_lease(root: str):
    """Keep a read lease through search/fallback, with a bounded readiness wait."""
    path = Path(root)
    managed = datadir.worktrees_dir().absolute()
    resolved = path.resolve()
    if resolved.is_relative_to(managed.resolve()) and (
            not path.is_absolute() or path.absolute() != resolved):
        raise ValueError("managed search path is a symlink or alias")
    path = resolved
    root = str(path)
    if not enabled() or not path.is_relative_to(managed):
        yield None
        return
    from api.dependencies import get_db_manager, get_skillflow
    db, sf = get_db_manager(), get_skillflow()
    with db.get_connection() as conn:
        value = conn.execute("SELECT * FROM run_isolation WHERE worktree_path=?", (root,)).fetchone()
    if value is None:
        raise ValueError("managed search root has no isolation owner")
    rec = dict(value)
    row = sf.get_run(rec["run_id"])
    if not isinstance(row, dict) or row.get("status") not in ("pending", "running", "paused"):
        raise ValueError("managed search root has no active run")
    ctl = control()
    ctl.request(rec, "ready", touch=True)
    budget = float(os.environ.get("AITELIER_ZVEC_PREPARE_WAIT_SECONDS", "10"))
    if not math.isfinite(budget) or not 0 <= budget <= 60:
        raise ValueError("preparation wait must be between 0 and 60 seconds")
    deadline = time.monotonic() + budget
    while True:
        lease = ctl.lock(rec["run_id"], exclusive=False)
        try:
            lease.__enter__()
        except TimeoutError:
            if time.monotonic() >= deadline:
                raise TimeoutError("resource still busy; no unleased fallback") from None
        else:
            try:
                state = ctl.get(rec["run_id"])
                if not state or state["desired"] != "ready":
                    raise ValueError("run index was released while search was waiting")
                ready = ctl.settled(state, "ready")
                if ready or time.monotonic() >= deadline:
                    validate_checkout(rec, managed)
                    live = sf.get_run(rec["run_id"])
                    if not isinstance(live, dict) or live.get("status") not in ("pending", "running", "paused"):
                        raise ValueError("run ended while search was waiting")
                    yield None if ready else (state["error"] or "index preparation is pending or unavailable")
                    return
            finally:
                lease.__exit__(None, None, None)
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def invalidate(root: str) -> None:
    """Missing index schedules repair without asking an agent to retry."""
    if not enabled() or not Path(root).is_relative_to(datadir.worktrees_dir().absolute()):
        return
    ctl = control()
    row = ctl.for_root(root)
    if row and row["desired"] == "ready":
        ctl.request(row, "ready", force=True)
