# cli/tui/dashboard.py
# Display-only dashboard zone. Two modes: pipelines→runs grouping / task list.
# No keyboard interaction — navigation via chat input (/project, /projects).
# Starting a run of a config is done via chat (the butler's start_config_run tool).
# Simple polling — no cache, no optimistic updates, no debounce races.

import asyncio

from rich.console import Group
from rich.table import Table
from textual.widgets import Static
from textual import work


# step_id → label. Seeded with DPE; the grouped view also falls back to the raw
# step id for configs whose steps aren't listed here (graceful for any config).
_STEP_LABELS = {
    "1": "Researcher", "1_review": "Researcher Review",
    "2": "Architect", "2_review": "Architect Review",
    "3": "PM", "3_review": "PM Review",
    "5": "Final Verifier", "5_review": "Final Review",
    "t_plan": "Task Planner", "t_plan_review": "Plan Review",
    "t_impl": "Implementer", "t_impl_review": "Impl Review",
    "t_verify": "Task Verifier", "t_verify_review": "Verify Review",
    "task_loop": "Task Loop", "git_sync_pre": "Sync Repo",
}


def _format_run_status(p: dict) -> str:
    status = p.get("status", "?")
    step = p.get("current_project_step", "")
    step_label = _STEP_LABELS.get(step, step)
    if status.startswith("checkpoint:"):
        return f"[yellow]⏸ {status.split(':', 1)[1]}[/yellow]"
    if status.startswith("running:"):
        s = status.split(":", 1)[1]
        return f"[green]▶ {_STEP_LABELS.get(s, s)}[/green]"
    if status.startswith("failed:"):
        return f"[red]✗ {status.split(':', 1)[1][:60]}[/red]"
    if status == "planning":
        return f"planning ({step_label})" if step_label else "planning"
    if status in ("waiting_user_approval", "paused"):
        return f"[yellow]⏸ {step_label or step or 'checkpoint'}[/yellow]"
    if status == "completed":
        return "[green]✓ completed[/green]"
    if status == "failed":
        return f"[red]failed at {step_label}[/red]" if step_label else "[red]failed[/red]"
    return status


def _format_tasks(p: dict) -> str:
    total = p.get("task_count", 0)
    if not total:
        return "-"
    done = p.get("completed_count", 0)
    running = p.get("running_count", 0)
    return f"{done}/{total} ▶{running}" if running else f"{done}/{total}"


def _format_updated(p: dict) -> str:
    updated = (p.get("last_update") or p.get("updated_at") or p.get("created_at") or "")[:16]
    if "T" in updated:
        updated = updated.split("T")[1][:5]
    return updated


class DashboardZone(Static):
    """Top zone: display-only pipelines→runs grouping or task table.
    Level 1 = installed pipelines, each with its runs nested.
    Level 2 = task list for a selected run.
    """

    def __init__(self, server_url: str, **kwargs):
        super().__init__(**kwargs)
        self.server_url = server_url.rstrip("/")
        self._level = "projects"  # "projects" (pipelines→runs) or "tasks"
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
        """Switch to task list view for the given run."""
        self._level = "tasks"
        self._current_project = project_id
        self._fetch_tasks()

    def exit_project(self):
        """Switch back to the pipelines→runs view."""
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
            cfg_resp, runs_resp = await asyncio.gather(
                self.app.http.get("/api/configs", timeout=10.0),
                self.app.http.get("/api/runs", timeout=10.0),
            )
            cfg_resp.raise_for_status()
            runs_resp.raise_for_status()
            configs = cfg_resp.json().get("configs", [])
            runs = runs_resp.json().get("runs", [])
        except Exception:
            self._flash_error("Failed to fetch pipelines — API unreachable")
            return
        if gen < self._fetch_gen:
            return
        self._render_dashboard(configs, runs)

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

    def _render_dashboard(self, configs: list[dict], runs: list[dict]):
        self.update(self._build_grouped(configs, runs))
        self.refresh()

    def _render_tasks(self, tasks: list[dict]):
        self.update(self._build_tasks_table(tasks))
        self.refresh()

    def _build_grouped(self, configs: list[dict], runs: list[dict]):
        """One table per installed pipeline (incl. zero-run ones), runs nested."""
        by_config: dict[str, list] = {}
        for r in runs:
            by_config.setdefault(r.get("config_name") or "dpe_default_v2", []).append(r)

        # Show every installed pipeline; fall back to runs' configs if /api/configs
        # was unavailable.
        pipelines = configs or [{"config_name": c, "label": c} for c in by_config]

        renderables = []
        for cfg in pipelines:
            cname = cfg.get("config_name")
            cruns = by_config.get(cname, [])
            label = cfg.get("label") or cname
            count = "1 run" if len(cruns) == 1 else f"{len(cruns)} runs"
            table = Table(title=f"▸ {label}  ({count})", show_lines=False,
                          expand=True, padding=0, title_justify="left")
            table.add_column("#", style="cyan", width=3)
            table.add_column("Run", min_width=16)
            table.add_column("Status", width=16)
            table.add_column("Tasks", width=10)
            table.add_column("Updated", width=8)
            if not cruns:
                table.add_row("", "[dim]No runs yet[/dim]", "", "", "")
            else:
                for i, p in enumerate(cruns):
                    if "_error" in p:
                        table.add_row("!", p["_error"], "", "", "")
                        continue
                    name = p.get("name", p.get("project_id", "?"))
                    table.add_row(str(i + 1), name, _format_run_status(p),
                                  _format_tasks(p), _format_updated(p))
            renderables.append(table)

        if not renderables:
            empty = Table(title="Pipelines", expand=True)
            empty.add_column("info")
            empty.add_row("[dim]No pipelines installed[/dim]")
            return empty
        return Group(*renderables)

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
