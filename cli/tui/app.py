# cli/tui/app.py
# AItelier TUI — 4-zone Textual app (dashboard, chat + notifications, flash bar).

import os
import httpx
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static
from textual import work

from cli.tui.dashboard import DashboardZone
from cli.tui.chat import ChatZone, CheckpointModal
from cli.tui.notifications import NotificationZone
from cli.tui.flash import FlashBar

_DEFAULT_URL = f"http://localhost:{os.environ.get('AITELIER_PORT', '4444')}"


class AItelierApp(App):
    """AItelier TUI: Dashboard + Chat/Notifications + Flash bar."""

    CSS_PATH = "styles.tcss"
    TITLE = "AItelier"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=True),
    ]

    def __init__(self, server_url: str = None, first_prompt: str = None, **kwargs):
        super().__init__(**kwargs)
        self.server_url = server_url or _DEFAULT_URL
        self._first_prompt = first_prompt
        # Shared async HTTP client — all components reuse this
        self.http = httpx.AsyncClient(base_url=self.server_url.rstrip("/"), timeout=30.0)

    def compose(self) -> ComposeResult:
        yield DashboardZone(self.server_url, id="dashboard-zone")
        with Horizontal(id="main-area"):
            yield ChatZone(self.server_url)
            yield NotificationZone(self.server_url, id="notif-zone")
        yield FlashBar(self.server_url, id="flash-bar")

    def on_mount(self):
        # Round-2 fix: periodic safety net. The checkpoint modal is normally
        # popped by the SSE 'checkpoint_reached' handler, but that can be missed
        # (event lost while the loop is busy, or skipped by its verify-race),
        # leaving a project paused with NO modal and no way to approve. This
        # reconciler pops the modal within a few seconds whenever a project is
        # paused at a checkpoint and no modal is currently open.
        self.set_interval(6.0, self._reconcile_checkpoints)
        if self._first_prompt:
            # User explicitly provided a brief — they want a new project,
            # not to resume a paused one. Skip TUI-4 auto-attach.
            chat = self.query_one(ChatZone)
            inp = chat.query_one("#chat-input")
            inp.value = self._first_prompt
            chat.on_input_submitted(
                type("Event", (), {"value": self._first_prompt, "input": inp})()
            )
            return
        # TUI-4 fix: scan for paused projects and offer to resume.
        self._attach_to_paused_projects()

    @work(exclusive=True, group="reconcile")
    async def _reconcile_checkpoints(self):
        """Pop a CheckpointModal for any paused-at-checkpoint project that has
        no modal currently shown (recovers a missed SSE pop)."""
        # Don't stack a second modal over an existing one.
        for screen in self.screen_stack:
            if isinstance(screen, CheckpointModal):
                return
        try:
            resp = await self.http.get("/api/projects", timeout=5.0)
            resp.raise_for_status()
            projects = resp.json()
        except Exception:
            return
        for p in projects:
            st = p.get("status")
            pid = p.get("project_id", "")
            # The list endpoint enriches status from the skillflow run, which is
            # 'paused' at a checkpoint (the 'checkpoint:<label>' form only lives in
            # the cached DB column). Accept both, then confirm via the meta API.
            if not pid or not (isinstance(st, str)
                               and (st == "paused" or st.startswith("checkpoint:"))):
                continue
            try:
                cr = await self.http.get(f"/api/meta/{pid}/checkpoint", timeout=3.0)
                if cr.status_code != 200:
                    continue
                data = cr.json()
                if not data.get("checkpoint"):
                    continue
            except Exception:
                continue
            self.push_screen(CheckpointModal(
                self.server_url, pid,
                data.get("label", "Checkpoint"), data.get("step", "?")))
            return  # one at a time

    @work(exclusive=True)
    async def _attach_to_paused_projects(self):
        """TUI-4: at startup, query /api/projects for paused projects and
        push a CheckpointModal (or a picker) so the user can resume.

        Mirrors the SSE-driven path in cli/tui/flash.py:handle_checkpoint_event
        (which only fires AFTER a checkpoint is reached while the TUI is
        already running). On a fresh TUI start with a paused project, the
        SSE stream is empty — the user would otherwise see an empty prompt
        and the paused project would be invisible until they navigated to
        the dashboard manually.
        """
        try:
            resp = await self.http.get("/api/projects", timeout=10.0)
            resp.raise_for_status()
            projects = resp.json()
        except Exception:
            return  # best-effort; dashboard will still show them

        paused = [
            p for p in projects
            if isinstance(p.get("status"), str)
            and (p["status"] == "paused" or p["status"].startswith("checkpoint:"))
        ]
        if not paused:
            return

        # Verify each candidate still has a pending checkpoint. The
        # scheduler can leave status="checkpoint:..." set even after the
        # checkpoint has been resolved, until the next scheduler tick —
        # flash.py does the same sanity check before pushing its modal.
        attachable: list[dict] = []
        for p in paused:
            pid = p.get("project_id", "")
            if not pid:
                continue
            try:
                cr = await self.http.get(
                    f"/api/meta/{pid}/checkpoint", timeout=3.0
                )
                if cr.status_code != 200:
                    continue
                data = cr.json()
                if not data.get("checkpoint"):
                    continue  # already resolved
                attachable.append({
                    "project_id": pid,
                    "label": data.get("label", "Checkpoint"),
                    "step": data.get("step", "?"),
                })
            except Exception:
                continue

        if not attachable:
            return

        if len(attachable) == 1:
            info = attachable[0]
            self.push_screen(
                CheckpointModal(
                    self.server_url, info["project_id"],
                    info["label"], info["step"],
                )
            )
        else:
            # Multiple paused — let the user pick which to resume.
            def _on_chosen(info: dict | None) -> None:
                if not info:
                    return
                self.push_screen(
                    CheckpointModal(
                        self.server_url, info["project_id"],
                        info["label"], info["step"],
                    )
                )

            self.push_screen(
                ResumeProjectModal(self.server_url, attachable),
                callback=_on_chosen,
            )

    async def on_unmount(self):
        await self.http.aclose()

    # A4 fix: Textual calls _handle_exception, not _on_exception. Renamed
    # to actually intercept worker exceptions. The Textual default
    # _handle_exception calls self.panic() which exits the process;
    # we override to log + flash instead, keeping the TUI alive across
    # any worker error.
    def _handle_exception(self, error: Exception) -> None:
        """Log unhandled exceptions instead of crashing the process."""
        import logging
        import traceback
        logger = logging.getLogger("aitelier.tui")
        logger.error(f"TUI unhandled exception: {type(error).__name__}: {error}")
        logger.error(traceback.format_exc())
        try:
            flash = self.query_one("#flash-bar")
            flash.flash_immediate(f"Error: {error}", duration=8.0)
        except Exception:
            pass

    # Backward-compat alias so any older caller that still references
    # _on_exception doesn't crash with AttributeError.
    _on_exception = _handle_exception


class ResumeProjectModal(ModalScreen[dict | None]):
    """Picker shown when multiple paused projects exist at TUI startup.

    Up/Down to navigate, Enter to select (dismisses with the chosen
    project info dict), Esc to dismiss with None (no auto-attach).
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_option", "Confirm", show=False),
        Binding("escape", "dismiss_modal", "Cancel", show=True),
    ]

    def __init__(self, server_url: str, paused: list[dict]) -> None:
        super().__init__()
        self.server_url = server_url.rstrip("/")
        # Each item: {"project_id": str, "label": str, "step": str}
        self._paused = paused
        self._cursor = 0

    def compose(self):
        yield Static(
            "[bold]Multiple paused projects — choose one to resume:[/bold]",
            id="resume-header",
        )
        yield Static("", id="resume-list")
        yield Static(
            "[↑↓] navigate  [Enter] resume  [Esc] skip", id="resume-hint"
        )

    def on_mount(self):
        self._refresh_list()

    def _refresh_list(self):
        lines = []
        for i, p in enumerate(self._paused):
            cursor = "●" if i == self._cursor else "○"
            pid = p.get("project_id", "?")
            label = p.get("label", "Checkpoint")
            step = p.get("step", "?")
            lines.append(f"  {cursor} {pid} — {label} (step {step})")
        self.query_one("#resume-list", Static).update("\n".join(lines))

    def action_cursor_up(self):
        if not self._paused:
            return
        self._cursor = (self._cursor - 1) % len(self._paused)
        self._refresh_list()

    def action_cursor_down(self):
        if not self._paused:
            return
        self._cursor = (self._cursor + 1) % len(self._paused)
        self._refresh_list()

    def action_select_option(self):
        if not self._paused:
            self.dismiss(None)
            return
        self.dismiss(self._paused[self._cursor])

    def action_dismiss_modal(self):
        self.dismiss(None)
