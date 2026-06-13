# cli/tui/flash.py
# Flash bar — single-line status bar at the bottom.
# SSE events are consumed centrally by NotificationZone and dispatched here.

import asyncio
from textual.widgets import Static

from cli.tui.chat import CheckpointModal


class FlashBar(Static):
    """Bottom bar for one-shot messages and checkpoint modal triggers."""

    def __init__(self, server_url: str, **kwargs):
        super().__init__(**kwargs)
        self.server_url = server_url.rstrip("/")
        self._flash_text = ""
        self._clear_timer = None

    def on_mount(self):
        self.can_focus = False
        self.update("")

    def flash(self, text: str, duration: float = 5.0):
        """Show a flash message that auto-clears. Delegates to flash_immediate."""
        self.flash_immediate(text, duration)

    def flash_immediate(self, text: str, duration: float = 5.0):
        """Show a flash message immediately."""
        self._flash_text = text
        self.update(text)
        if self._clear_timer:
            self._clear_timer.stop()
        self._clear_timer = self.set_timer(duration, self._clear)

    def _clear(self):
        self._flash_text = ""
        self.update("")

    async def handle_checkpoint_event(self, event: dict):
        """Called by the centralized SSE consumer for checkpoint events."""
        etype = event.get("type", "")

        if etype in ("checkpoint_reached", "checkpoint_paused"):
            pid = event.get("project_id", "")
            label = event.get("label", "checkpoint")
            step = event.get("step") or event.get("step_id", "?")

            # Avoid pushing a duplicate modal over an existing one
            for screen in self.app.screen_stack:
                if isinstance(screen, CheckpointModal) and screen.project_id == pid:
                    return
            # Verify checkpoint is still pending via API
            try:
                http = getattr(self.app, "http", None)
                if http:
                    cr = await http.get(
                        f"/api/meta/{pid}/checkpoint", timeout=3.0
                    )
                    if cr.status_code == 200:
                        cp_data = cr.json()
                        if not cp_data.get("checkpoint"):
                            return  # checkpoint already resolved, skip
            except Exception:
                pass  # API unreachable, push anyway (best-effort)
            self.flash(
                f"Checkpoint: {label} — use /checkpoint to review ({pid})",
                duration=30.0,
            )
            self.app.push_screen(
                CheckpointModal(self.server_url, pid, label, step)
            )

        elif etype == "checkpoint_resolved":
            pid = event.get("project_id", "")
            # Pop any CheckpointModal for this project
            for screen in list(self.app.screen_stack):
                if isinstance(screen, CheckpointModal) and screen.project_id == pid:
                    self.app.pop_screen()
                    break
            # AT-3: clear immediately (previous 30s checkpoint message lingers
            # otherwise) then show a brief resolved confirmation that auto-clears.
            self._clear()
            action = event.get("action", "resolved")
            label = event.get("label", "checkpoint")
            self.flash_immediate(
                f"✓ {label} {action}",
                duration=3.0,
            )
