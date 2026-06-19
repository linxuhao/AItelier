# cli/tui/dashboard.py
# Display-only dashboard zone. Two modes: project list / task list.
# No keyboard interaction — navigation via chat input (/project, /projects).
# Simple 3-second polling — no cache, no optimistic updates, no debounce races.

import httpx
from rich.table import Table
from textual.widgets import Static
from textual import work


class DashboardZone(Static):
    """Top zone: display-only project or task table (Static with Rich rendering).
    Level 1 = project list, Level 2 = task list for selected project.
    Polls /api/projects every 3 seconds — single source of truth, no cache races.
    """

    def __init__(self, server_url: str, **kwargs):
        super().__init__(**kwargs)
        self.server_url = server_url.rstrip("/")
        self._level = "projects"  # "projects" or "tasks"
        self._current_project: str | None = None

    def on_mount(self):
        self.can_focus = False
        self._fetch_projects()
        self.set_interval(3, self._auto_refresh)

    # ── Public API ───────────────────────────────────────────────

    @property
    def level(self) -> str:
        return self._level

    @property
    def current_project(self) -> str | None:
        return self._current_project

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
    _fetch_gen = 0

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
            return
        if gen < self._fetch_gen:
            return
        self._render_projects(data)

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
            return
        if gen < self._fetch_gen:
            return
        self._render_tasks(data)

    def _auto_refresh(self):
        if self._level == "projects":
            self._fetch_projects()
        else:
            self._fetch_tasks()

    def force_refresh(self):
        """Immediately re-fetch and re-render (called by notification consumers)."""
        self._auto_refresh()

    # ── Rendering ────────────────────────────────────────────────

    def _render_projects(self, projects: list[dict]):
        self.update(self._build_projects_table(projects))
        self.refresh()

    def _render_tasks(self, tasks: list[dict]):
        self.update(self._build_tasks_table(tasks))
        self.refresh()

    def _build_projects_table(self, projects: list[dict]) -> Table:
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

        for i, p in enumerate(projects):
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
                status_str = f"[yellow]⏸ {label}[/yellow]"
            elif status.startswith("running:"):
                label = status.split(":", 1)[1]
                sl = _STEP_LABELS.get(label, label)
                status_str = f"[green]▶ {sl}[/green]"
            elif status.startswith("failed:"):
                reason = status.split(":", 1)[1][:60]
                status_str = f"[red]✗ {reason}[/red]"
            elif status == "planning":
                status_str = f"planning ({step_label})" if step_label else "planning"
            elif status in ("waiting_user_approval", "paused"):
                label = step_label or step or "checkpoint"
                status_str = f"[yellow]⏸ {label}[/yellow]"
            elif status == "completed":
                status_str = "[green]✓ completed[/green]"
            elif status == "failed":
                status_str = f"[red]failed at {step_label}[/red]" if step_label else "[red]failed[/red]"
            else:
                status_str = status

            done = p.get("completed_count", 0)
            total = p.get("task_count", 0)
            running_cnt = p.get("running_count", 0)

            if total > 0:
                if running_cnt > 0:
                    tasks_str = f"{done}/{total} ▶{running_cnt}"
                else:
                    tasks_str = f"{done}/{total}"
            else:
                tasks_str = "-"

            updated = (p.get("updated_at") or p.get("created_at") or "")[:16]
            # Keep only the time part if it's ISO format
            if "T" in updated:
                updated = updated.split("T")[1][:5] if "T" in updated else updated

            table.add_row(str(i + 1), name, status_str, tasks_str, updated)

        if not projects:
            table.add_row("", "[dim]No projects yet[/dim]", "", "", "")
        return table

    def _build_tasks_table(self, tasks: list[dict]) -> Table:
        table = Table(title=f"Tasks — {self._current_project}", show_lines=False,
                      expand=True, padding=0)
        table.add_column("#", style="cyan", width=3)
        table.add_column("Task", min_width=24)
        table.add_column("Status", width=12)
        table.add_column("Step", width=14)
        table.add_column("Retries", width=6)

        for i, t in enumerate(tasks):
            tid = t.get("id", i + 1)
            desc = (t.get("prompt") or "").split("\n")[0][:60]
            status = t.get("status", "?")
            step = t.get("current_step", "")
            retries = str(t.get("retry_count", 0))

            if status == "completed":
                status_str = "[green]✓ done[/green]"
            elif status == "running":
                status_str = "[green]▶ running[/green]"
            elif status == "failed":
                status_str = "[red]✗ failed[/red]"
            elif status == "pending":
                status_str = "[dim]○ pending[/dim]"
            else:
                status_str = status

            table.add_row(str(tid), desc, status_str, step or "-", retries)

        if not tasks:
            table.add_row("", "[dim]No tasks assigned yet[/dim]", "", "", "")
        return table

    def _flash_error(self, text: str):
        try:
            self.app.query_one("#flash-bar").flash_immediate(text, duration=8.0)
        except Exception:
            pass
