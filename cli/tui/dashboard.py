# cli/tui/dashboard.py
# Display-only dashboard zone. Two modes: project list / task list.
# No keyboard interaction — navigation via chat input (/project, /projects).

import time
import httpx
from rich.table import Table
from rich.text import Text
from textual.widgets import Static
from textual import work


class DashboardZone(Static):
    """Top zone: display-only project or task table (Static with Rich rendering).
    Level 1 = project list, Level 2 = task list for selected project.
    """

    # AT-28: debounce window in seconds — skip HTTP fetch after optimistic update
    _OPTIMISTIC_DEBOUNCE = 2.0

    def __init__(self, server_url: str, **kwargs):
        super().__init__(**kwargs)
        self.server_url = server_url.rstrip("/")
        self._level = "projects"  # "projects" or "tasks"
        self._current_project: str | None = None
        self._projects_cache: list[dict] = []
        self._tasks_cache: list[dict] = []
        self._last_optimistic_ts: float = 0.0

    def on_mount(self):
        self.can_focus = False
        self._fetch_projects()
        self.set_interval(3, self._auto_refresh)  # AT-15: faster refresh for live status

    # ── Public API ───────────────────────────────────────────────

    @property
    def level(self) -> str:
        return self._level

    @property
    def current_project(self) -> str | None:
        return self._current_project

    @property
    def projects_cache(self) -> list[dict]:
        return self._projects_cache

    @property
    def tasks_cache(self) -> list[dict]:
        return self._tasks_cache

    def enter_project(self, project_id: str):
        """Switch to task list view for the given project."""
        self._level = "tasks"
        self._current_project = project_id
        self._fetch_tasks()

    def exit_project(self):
        """Switch back to project list view."""
        self._level = "projects"
        self._current_project = None
        self._fetch_projects()

    # ── Data fetching (async) ────────────────────────────────────
    _fetch_gen = 0  # Version counter to reject stale responses

    @work(exclusive=True)
    async def _fetch_projects(self):
        self._fetch_gen += 1
        gen = self._fetch_gen

        try:
            resp = await self.app.http.get("/api/projects", timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            self._flash_error("Failed to fetch projects — API unreachable")
            self.call_after_refresh(self._refresh_display)
            return
        if gen < self._fetch_gen:
            return
        self._projects_cache = data
        self.call_after_refresh(self._refresh_display)

    @work(exclusive=True)
    async def _fetch_tasks(self):
        if not self._current_project:
            return
        self._fetch_gen += 1
        gen = self._fetch_gen

        try:
            resp = await self.app.http.get(
                f"/api/projects/{self._current_project}/tasks", timeout=10.0
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            self._flash_error("Failed to fetch tasks — API unreachable")
            self.call_after_refresh(self._refresh_display)
            return
        if gen < self._fetch_gen:
            return
        self._tasks_cache = data
        self.call_after_refresh(self._refresh_display)

    def _auto_refresh(self):
        if self._level == "projects":
            self._fetch_projects()
        else:
            self._fetch_tasks()

    def _should_skip_fetch(self) -> bool:
        """AT-28: skip HTTP fetch if optimistic update was applied recently.

        The SSE stream delivers step_claimed/step_completed events faster than
        the DB sync. Without this debounce, an HTTP fetch immediately after an
        optimistic update would overwrite the fresh SSE-driven status with stale
        DB data, causing the dashboard to flicker between correct (optimistic)
        and stale (DB) values.
        """
        if self._last_optimistic_ts <= 0:
            return False
        return (time.monotonic() - self._last_optimistic_ts) < self._OPTIMISTIC_DEBOUNCE

    def force_refresh(self, bypass_debounce: bool = False):
        """Immediately re-fetch data and re-render. Used by SSE event handlers.

        Set bypass_debounce=True for periodic polling refreshes so that
        slow-changing fields (task counts, completed counts) are not
        permanently hidden behind the optimistic-update debounce."""
        if not bypass_debounce and self._should_skip_fetch():
            return
        self._auto_refresh()

    def force_refresh_repaint(self):
        """Force re-fetch + full repaint. Used after SSE events that change status."""
        if self._should_skip_fetch():
            # Still repaint even if we skip the fetch — the optimistic data
            # is already in the cache and just needs rendering.
            try:
                self.refresh(layout=True)
            except Exception:
                pass
            return
        self._auto_refresh()
        try:
            self.refresh(layout=True)
        except Exception:
            pass

    def optimistic_update(self, project_id: str, *, status: str = "",
                          step: str = "", step_name: str = "") -> None:
        """Synchronously update cached project status for instant TUI feedback.

        SSE event handlers call this BEFORE the async HTTP refresh so the
        dashboard shows the correct step immediately, without waiting for
        the next /api/projects round-trip (AT-28).
        """
        for p in self._projects_cache:
            if p.get("project_id") == project_id:
                if status:
                    p["status"] = status
                if step:
                    p["current_project_step"] = step
                # AT-28: set debounce timestamp so subsequent HTTP fetches
                # don't overwrite this optimistic update with stale DB data.
                self._last_optimistic_ts = time.monotonic()
                # Re-render immediately with the optimistic data
                self._refresh_display()
                return

    def _flash_error(self, text: str):
        """Show a flash-bar error message. Best-effort (no crash if bar missing)."""
        try:
            self.app.query_one("#flash-bar").flash_immediate(text, duration=8.0)
        except Exception:
            pass

    # ── Rendering ────────────────────────────────────────────────

    def _refresh_display(self):
        """Re-render by calling self.update() with a Rich renderable."""
        if self._level == "projects":
            self.update(self._build_projects_table())
        else:
            self.update(self._build_tasks_table())
        self.refresh()  # Ensure Textual repaints the widget

    def _build_projects_table(self) -> Table:
        table = Table(title="Projects", show_lines=False, expand=True, padding=0)
        table.add_column("#", style="cyan", width=3)
        table.add_column("Project", min_width=16)
        table.add_column("Status", width=12)
        table.add_column("Tasks", width=12)
        table.add_column("Updated", width=16)

        _STEP_LABELS = {
            "1": "Researcher",
            "1_review": "Researcher Review",
            "2": "Architect",
            "2_review": "Architect Review",
            "3": "PM",
            "3_review": "PM Review",
            "5": "Final Verifier",
            "5_review": "Final Review",
            "t_plan": "Task Planner",
            "t_plan_review": "Plan Review",
            "t_impl": "Implementer",
            "t_impl_review": "Impl Review",
            "t_verify": "Task Verifier",
            "t_verify_review": "Verify Review",
            "task_loop": "Task Loop",
        }

        for i, p in enumerate(self._projects_cache):
            if "_error" in p:
                table.add_row("!", p["_error"], "", "", "")
                continue
            pid = p.get("project_id", "?")
            name = p.get("name", pid)
            status = p.get("status", "?")
            step = p.get("current_project_step", "")
            step_label = _STEP_LABELS.get(step, step)
            if status.startswith("checkpoint:"):
                label = status.split(":", 1)[1]
                status = f"[yellow]⏸ {label}[/yellow]"
            elif status.startswith("running:"):
                label = status.split(":", 1)[1]
                sl = _STEP_LABELS.get(label, label)
                status = f"[green]▶ {sl}[/green]"
            elif status.startswith("failed:"):
                reason = status.split(":", 1)[1][:60]
                status = f"[red]✗ {reason}[/red]"
            elif status == "planning":
                status = f"planning ({step_label})" if step_label else "planning"
            elif status == "waiting_user_approval" or status == "paused":
                label = step_label or step or "checkpoint"
                status = f"[yellow]⏸ {label}[/yellow]"
            elif status == "executing":
                status = f"executing ({step_label})" if step_label else "executing"
            elif status == "verifying":
                status = "verifying"
            elif status == "completed":
                status = "[green]✓ completed[/green]"
            elif status == "failed":
                if step_label:
                    status = f"failed at {step_label}"
                else:
                    status = "failed"
            done = p.get("completed_count", 0)
            running = p.get("running_count", 0)
            failed = p.get("failed_count", 0)
            total = p.get("task_count", 0)
            # AT-16: show task progress clearly: "2/4 done" or "1 running" etc.
            if total:
                if done == total:
                    task_str = f"[green]{done}/{total} ✓[/green]"
                elif running:
                    task_str = f"{done}/{total} [yellow]▶{running}[/yellow]"
                elif failed:
                    task_str = f"{done}/{total} [red]✗{failed}[/red]"
                else:
                    task_str = f"{done}/{total}"
            else:
                task_str = "-"
            updated = p.get("last_update", p.get("updated_at", ""))[:16]
            table.add_row(str(i + 1), name, status, task_str, updated)
        return table

    def _build_tasks_table(self) -> Table:
        table = Table(
            title=f"Tasks — {self._current_project}", show_lines=False, expand=True, padding=0
        )
        table.add_column("#", style="cyan", width=3)
        table.add_column("Status", width=12)
        table.add_column("Step", width=10)
        table.add_column("Prompt", min_width=40)

        for i, t in enumerate(self._tasks_cache):
            status = t.get("status", "?")
            if status == "failed":
                err = t.get("last_error", "")
                if err:
                    # Show first 60 chars of error next to status
                    err_short = err[:60].replace("\n", " ")
                    status = f"failed: {err_short}"
            step = t.get("current_step", "-")
            prompt = t.get("prompt", "")[:80]
            table.add_row(str(i), status, step, prompt)
        return table
