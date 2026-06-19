# cli/tui/notifications.py
# Notification zone — right-side panel showing pipeline events.
# Single SSE consumer for the entire TUI: dispatches to notification feed,
# flash bar (checkpoint modals), and completer.flash_state (status bar).

import json
import asyncio
from datetime import datetime, timezone
from textual.widgets import Static
from textual.containers import VerticalScroll
from textual import work

try:
    import cli.completer as _comp
except Exception:
    _comp = None

from aitelier.step_labels import STEP_NAMES, CHECKPOINT_STEPS


class NotificationZone(VerticalScroll):
    """Right-side notification panel + single SSE consumer for the TUI."""

    _MAX_LINES = 500  # UX-3: increased from 60 to hold full pipeline event history
                       # without losing events to ring-buffer eviction. Retries,
                       # agent messages, and lifecycle events can easily exceed
                       # 60 lines in a single run.

    def __init__(self, server_url: str, **kwargs):
        super().__init__(**kwargs)
        self.server_url = server_url.rstrip("/")
        self._lines: list[str] = []
        self._display = Static(
            "[dim]Pipeline events will appear here[/]",
            id="notif-display",
        )
        self._project_name_cache: dict[str, str] = {}

    def compose(self):
        yield self._display

    def on_mount(self):
        self.can_focus = False
        self._start_sse()
        # Polling fallback: if SSE misses events (e.g. during checkpoint modal),
        # periodic refresh keeps the panel in sync with backend state.
        self._poll_timer = self.set_interval(3.0, self._poll_refresh)  # AT-15

    def _poll_refresh(self):
        """Lightweight poll — only refreshes if dashboard has active projects.
        Bypasses the optimistic-update debounce so task counts stay current."""
        try:
            dashboard = self.app.query_one("#dashboard-zone")
            dashboard.force_refresh(bypass_debounce=True)
        except Exception:
            pass

    def _poll_repaint(self):
        """Full repaint refresh after status-changing events (AT-15)."""
        try:
            dashboard = self.app.query_one("#dashboard-zone")
            dashboard.force_refresh_repaint()
        except Exception:
            pass

    def refresh_from_api(self):
        """Called after checkpoint approve/reject to force immediate refresh."""
        self._poll_repaint()

    @work(exclusive=True)
    async def _start_sse(self):
        """Single SSE consumer — dispatches to notification feed, flash bar, status bar."""
        try:
            async with self.app.http.stream(
                "GET", "/api/events/stream", timeout=None
            ) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        raw = json.loads(line[6:])
                        log_str = raw.get("log", "")
                        if log_str == "__END__":
                            break
                        event = (
                            json.loads(log_str)
                            if isinstance(log_str, str) and log_str.startswith("{")
                            else {}
                        )
                    except json.JSONDecodeError:
                        continue

                    # 1. Feed the notification panel
                    self._handle_event(event)

                    etype = event.get("type", "")

                    # 2. Refresh dashboard on every event — no more stale status
                    # during long-running steps (AT-29).
                    self._poll_repaint()

                    # 3. Forward checkpoint events to FlashBar for modal management
                    if etype in ("checkpoint_reached", "checkpoint_paused",
                                 "checkpoint_resolved", "checkpoint_approved"):
                        try:
                            flash_bar = self.app.query_one("#flash-bar")
                            await flash_bar.handle_checkpoint_event(event)
                        except Exception:
                            pass

                    # 4. Update status bar (completer.flash_state)
                    self._update_flash_state(event)

                    # 5. AT-28: optimistic dashboard update (instant, before async fetch)
                    if etype in ("step_claimed", "step_completed",
                                 "checkpoint_paused", "run_completed", "run_failed"):
                        self._optimistic_dashboard(event)

        except Exception:
            self._add_line("[dim]SSE disconnected[/dim]")

    def _update_flash_state(self, event: dict):
        """Update cli.completer.flash_state for the bottom status bar."""
        if _comp is None:
            return
        etype = event.get("type", "")
        pid = event.get("project_id", "")

        if etype == "step_claimed":
            task_id = event.get("task_id") or event.get("step_instance_id")
            # Resolve project name
            name = self._project_name_cache.get(pid, pid)
            step = event.get("step_id", "?")
            is_checkpoint = step in CHECKPOINT_STEPS
            _comp.flash_state = {
                "project_id": pid,
                "project": name,
                "step": step,
                "step_name": STEP_NAMES.get(step, step),
                "task_id": task_id if task_id and task_id != 0 else None,
                "checkpoint": is_checkpoint,
            }
        elif etype == "step_completed":
            # AT-23: show completed state instead of clearing entirely.
            # Keep project context so the status bar shows useful info
            # during the gap before the next step_claimed event.
            if _comp.flash_state and _comp.flash_state.get("project_id") == pid:
                step = event.get("step_id", "?")
                step_name = STEP_NAMES.get(step, step)
                is_checkpoint = step in CHECKPOINT_STEPS
                _comp.flash_state = {
                    **(_comp.flash_state or {}),
                    "step": f"{step} ✓",
                    "step_name": f"{step_name} done",
                    "task_id": None,
                    "checkpoint": is_checkpoint,
                }
            elif not _comp.flash_state:
                # No prior state — set a minimal one
                step = event.get("step_id", "?")
                step_name = STEP_NAMES.get(step, step)
                is_checkpoint = step in CHECKPOINT_STEPS
                _comp.flash_state = {
                    "project_id": pid,
                    "project": self._project_name_cache.get(pid, pid),
                    "step": f"{step} ✓",
                    "step_name": f"{step_name} done",
                    "task_id": None,
                    "checkpoint": is_checkpoint,
                }
        elif etype in ("project_completed", "project_failed", "run_completed", "run_failed"):
            _comp.flash_state = None

    def _format_ctx(self, event: dict) -> str:
        """Format timestamp + project + step + task context for a notification line."""
        ts = event.get("_ts", 0)
        if ts:
            local_dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
            time_str = local_dt.strftime("%H:%M:%S")
        else:
            time_str = "--:--:--"

        # Project name
        pname = event.get("_project_name", "")
        pid = event.get("project_id", "")
        project = pname or pid[:16] if pid else ""

        # Step name
        step_id = (event.get("_step_id") or event.get("step_id") or
                    event.get("step") or "")
        step_name = STEP_NAMES.get(step_id, step_id) if step_id else ""

        # Task ID (for task-loop steps)
        task_id = event.get("_task_id", "")

        parts = [f"[dim]{time_str}[/]"]
        if project:
            parts.append(f"[bold]{project}[/]")
        if step_name:
            parts.append(f"[dim]{step_name}[/]")
        if task_id:
            parts.append(f"[dim italic]{task_id}[/]")
        return " ".join(parts)

    def _handle_event(self, event: dict):
        etype = event.get("type", "")
        pid = event.get("project_id", "")
        pid_short = pid[:24] if pid else ""
        ctx = self._format_ctx(event)

        if etype == "step_start":
            step = event.get("step", "?")
            self._add_line(f"{ctx} [bold yellow]●[/] {step}")
        elif etype == "step_end":
            step = event.get("step", "?")
            if event.get("success"):
                self._add_line(f"{ctx} [bold green]✓[/] {step} done")
            else:
                self._add_line(f"{ctx} [bold red]✗[/] {step} failed")
        elif etype == "step_timeout":
            step = event.get("step", "?")
            err = (event.get("error", "") or "timed out")[:80]
            self._add_line(f"{ctx} [bold red]⏰[/] {step} timed out")
        elif etype == "step_failed":
            step = event.get("step", "?")
            err = (event.get("error", "") or "")[:120]
            self._add_line(f"{ctx} [bold red]✗[/] {step}: {err}")
        elif etype in ("checkpoint_reached", "checkpoint_paused"):
            label = event.get("label", "checkpoint")
            self._add_line(f"{ctx} [bold cyan]⏳[/] {label}")
        elif etype in ("checkpoint_resolved", "checkpoint_approved"):
            label = event.get("label", "checkpoint")
            action = event.get("action", "approved")
            self._add_line(f"{ctx} [bold green]✓[/] {label} {action}")
        elif etype == "step_checkpoint_rejected":
            label = event.get("label", "checkpoint")
            self._add_line(f"{ctx} [bold yellow]↺[/] {label} rejected — redo")
        elif etype == "agent_message":
            content = (event.get("content", "") or "")[:160]
            level = event.get("level", "info")
            prefix = {"milestone": "!", "warning": "!!"}.get(level, "i")
            self._add_line(f"{ctx}  {prefix} {content}")
        elif etype == "project_completed":
            self._add_line(f"{ctx} [bold green]✓ Project done[/]")
        elif etype == "project_failed":
            reason = (event.get("reason", "") or "")[:80]
            self._add_line(f"{ctx} [bold red]✗ Project failed[/]: {reason}")
        elif etype == "run_failed":
            reason = (event.get("reason", "") or "")[:120]
            self._add_line(f"{ctx} [bold red]✗ Run failed[/]: {reason}")
        elif etype == "step_done":
            step = event.get("step_id", "?")
            name = STEP_NAMES.get(step, step)
            files = event.get("files", [])
            preview = ", ".join(files[:3]) if files else ""
            self._add_line(f"{ctx} [bold green]✓[/] {name} → {preview}")
        elif etype == "step_completed":
            step = event.get("step_id", "?")
            name = STEP_NAMES.get(step, step)
            self._add_line(f"{ctx} [bold green]✓[/] {name} completed")
        elif etype == "step_claimed":
            step = event.get("step_id", "?")
            name = STEP_NAMES.get(step, step)
            self._add_line(f"{ctx} [bold yellow]●[/] {name} started")
        elif etype == "run_started":
            self._add_line(f"{ctx} [bold blue]▶[/] Pipeline started")
        elif etype == "files_written":
            files = event.get("files", [])
            preview = ", ".join(files[:3]) if files else "?"
            self._add_line(f"{ctx}  ↳ wrote: {preview}")
        elif etype == "lifecycle_hook":
            hook = event.get("hook", "?")
            status = event.get("status", "")
            detail = event.get("detail", "")
            # Skip only empty completed hooks — show completed with detail
            # (e.g. "5 file(s)", "committed")
            if status == "completed" and not detail:
                return
            status_glyph = {"completed": "✓", "failed": "✗", "warned": "⚠",
                           "retry": "↺", "skipped": "→"}.get(status, "")
            if detail:
                self._add_line(f"{ctx} [dim]{status_glyph} {hook}: {detail}[/]")
            elif status:
                self._add_line(f"{ctx} [dim]{status_glyph} {hook} {status}[/]")

        # AT-28: do NOT call _refresh_dashboard here. The SSE event loop
        # calls _optimistic_dashboard + _poll_repaint right after _handle_event
        # returns. Calling _refresh_dashboard now would trigger an HTTP fetch
        # that races with the optimistic update and overwrites it with stale
        # DB data. The 3s polling fallback keeps the dashboard in sync.

    def _add_line(self, text: str):
        self._lines.append(text)
        if len(self._lines) > self._MAX_LINES:
            self._lines = self._lines[-self._MAX_LINES:]
        self._display.update("\n".join(self._lines))
        # Auto-scroll to bottom
        self.scroll_end(animate=False)

    def _refresh_dashboard(self):
        try:
            dashboard = self.app.query_one("#dashboard-zone")
            dashboard.force_refresh()
            dashboard.refresh(layout=True)  # AT-15: force Textual repaint
        except Exception:
            pass

    def _optimistic_dashboard(self, event: dict):
        """AT-28: update dashboard cache synchronously before async fetch.

        When step_claimed / step_completed / checkpoint events arrive via SSE,
        we update the cached project status immediately so the dashboard
        shows the correct step name without waiting for the next HTTP round-trip.
        """
        pid = event.get("project_id", "")
        etype = event.get("type", "")
        if not pid:
            return
        try:
            dashboard = self.app.query_one("#dashboard-zone")
        except Exception:
            return

        if etype == "step_claimed":
            step = event.get("step_id", "")
            step_name = STEP_NAMES.get(step, step)
            # Use fine-grained step_id in status to match enrich_project_status
            # format (e.g. "running:t_impl" not "running:3").
            dashboard.optimistic_update(pid, status=f"running:{step}",
                                        step=step, step_name=step_name)
        elif etype == "step_completed":
            step = event.get("step_id", "")
            step_name = STEP_NAMES.get(step, step)
            dashboard.optimistic_update(pid, step=step, step_name=step_name)
        elif etype in ("checkpoint_paused",):
            label = event.get("label", "checkpoint")
            dashboard.optimistic_update(pid, status=f"checkpoint:{label}",
                                        step=event.get("step_id", ""))
        elif etype in ("run_completed",):
            dashboard.optimistic_update(pid, status="completed")
            # AT-31: force-refresh after run completes so task counts are
            # fetched from the API instead of staying stale from the cache.
            dashboard.force_refresh()
        elif etype in ("run_failed",):
            dashboard.optimistic_update(pid, status="failed")
            dashboard.force_refresh()
