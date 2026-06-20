# web_api/scheduler_manager.py
# Per-user scheduler lifecycle: create on first request, drain work, stop when idle.

from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.scheduler import start_scheduler
from core.db_manager import DBManager
from api.dependencies import get_db_manager

# Default idle timeout before stopping a user's scheduler (seconds).
DEFAULT_IDLE_TIMEOUT = 1800  # 30 minutes


class UserSchedulerManager:
    """Manages per-user scheduler instances for normal (multi-tenant) mode."""

    def __init__(self, idle_timeout: int = DEFAULT_IDLE_TIMEOUT):
        self._schedulers: dict[str, AsyncIOScheduler] = {}
        self._last_seen: dict[str, datetime] = {}
        self._idle_timeout = idle_timeout

    def touch(self, email: str):
        """Update last activity timestamp for a user."""
        self._last_seen[email] = datetime.now(timezone.utc)

    def get(self, email: str) -> AsyncIOScheduler | None:
        """Return existing scheduler for user, or None."""
        return self._schedulers.get(email)

    def get_or_create(self, email: str) -> AsyncIOScheduler:
        """Get existing scheduler or create one for this user."""
        if email not in self._schedulers:
            scheduler = start_scheduler(owner_email=email)
            self._schedulers[email] = scheduler
        self.touch(email)
        return self._schedulers[email]

    def remove(self, email: str):
        """Shutdown and remove a user's scheduler."""
        scheduler = self._schedulers.pop(email, None)
        if scheduler:
            scheduler.shutdown(wait=False)
        self._last_seen.pop(email, None)
        # Also clean up from scheduler module's user map
        from core.scheduler import _user_scheduler_map
        _user_scheduler_map.pop(email, None)

    def reap_inactive(self):
        """
        Stop schedulers for users that are both:
        1. Inactive for > idle_timeout seconds
        2. Have NO running/pending tasks (drain before stop)

        Also starts schedulers for active users who have tasks but no scheduler yet.
        """
        now = datetime.now(timezone.utc)
        db: DBManager = get_db_manager()

        for email in list(self._last_seen.keys()):
            last = self._last_seen[email]
            idle_seconds = (now - last).total_seconds()
            # "Work" = incomplete DPE tasks OR any active run (covers task-less
            # configs whose runs have no tasks rows).
            has_work = (db.has_incomplete_tasks_for_owner(email)
                        or db.has_active_runs_for_owner(email))
            has_scheduler = email in self._schedulers

            # Start scheduler for active user with work but no scheduler
            if has_work and not has_scheduler:
                self._schedulers[email] = start_scheduler(owner_email=email)

            # Stop idle scheduler only when no work remains
            if has_scheduler and idle_seconds >= self._idle_timeout and not has_work:
                self.remove(email)

    @property
    def active_count(self) -> int:
        return len(self._schedulers)

    def wake(self, email: str):
        """Wake the scheduler for a specific user (e.g., after submit_task)."""
        from core.scheduler import wake_scheduler
        wake_scheduler(owner_email=email)

    def shutdown_all(self):
        """Stop all user schedulers (for graceful server shutdown)."""
        for email in list(self._schedulers.keys()):
            self.remove(email)
