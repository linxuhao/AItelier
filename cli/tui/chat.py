# cli/tui/chat.py
# Chat zone — conversational interface with the Meta Agent.
# Handles user input, streams SSE responses, manages context.
# Includes /-command completion candidates display.

import json
import os
import time
import asyncio
from pathlib import Path

import httpx
from textual.containers import Container, VerticalScroll
from textual.widgets import Static, Input
from textual.screen import ModalScreen
from textual.message import Message
from textual import work, events
from textual.binding import Binding

_META_DIR = Path.home() / ".AItelier" / "meta"

# Slash commands: (command, description, takes_arg)
_SLASH_COMMANDS = [
    ("/help", "Show all commands", False),
    ("/quit", "Exit AItelier", False),
    ("/clear", "Clear chat history", False),
    ("/projects", "Back to project list dashboard", False),
    ("/project", "Enter project by # or ID (e.g. /project 1, /project my-app)", True),
    ("/status", "Show current project tasks", False),
    ("/url", "Show/change backend URL", True),
    ("/frequency", "Scheduler poll interval", True),
    ("/cron", "Scheduler cron schedule", True),
    ("/restart", "Restart backend server", False),
    ("/checkpoint", "View pending checkpoint for review", False),
    ("/approve", "Approve checkpoint to continue pipeline", False),
    ("/reject", "Reject checkpoint with feedback", True),
    ("/delete", "Delete a project (e.g. /delete hello-world)", True),
    ("/logs", "Show task logs (e.g. /logs 1)", True),
]


def _format_tool_result(name: str, result: dict) -> str:
    """Format a tool result dict into a compact one-line summary."""
    if not isinstance(result, dict):
        s = str(result)
        return f"Result: {s[:120]}"

    if "error" in result:
        msg = str(result["error"])[:100]
        return f"Error: {msg}"

    status = result.get("status", "")
    pid = result.get("project_id", "")
    tid = result.get("task_id", "")

    if name == "list_projects":
        n = len(result.get("projects", []))
        return f"{n} project(s)" if n else "no projects"
    elif name == "get_project":
        p = result.get("project", {})
        pname = p.get("name", pid) if isinstance(p, dict) else ""
        pstatus = p.get("status", "?") if isinstance(p, dict) else "?"
        return f"Project: {pname} ({pstatus})"
    elif name == "create_project":
        return f"Created project \"{pid}\""
    elif name == "save_draft_brief":
        return f"Draft brief saved for \"{pid}\""
    elif name == "edit_draft_brief":
        return f"Draft brief updated for \"{pid}\""
    elif name == "list_tasks":
        n = len(result.get("tasks", []))
        return f"{n} task(s)" if n else "no tasks"
    elif name == "save_draft_task":
        return f"Draft task saved for \"{pid}\""
    elif name == "suggest_submit_task":
        if status == "pending_confirm":
            return f"Task ready for review in \"{pid}\""
        return f"Task: {status}"
    elif name == "list_workspace_tree":
        n = len(result.get("tree", []))
        return f"Workspace: {n} file(s)"
    elif name == "read_workspace_file":
        path = result.get("path", "?")
        content = result.get("content", "")
        size = len(content) if content else 0
        return f"Read {path} ({size} chars)"
    elif name == "retry_task":
        return f"Task #{tid} retried" if status == "retried" else f"Retry failed"
    elif name == "retry_project":
        return f"Project \"{pid}\" retried" if status == "retried" else f"Retry failed"
    elif name == "refresh_planning":
        return f"Planning refreshed for \"{pid}\""
    elif name == "get_step_output":
        n = len(result.get("files", {}))
        return f"Step output: {n} file(s)"
    elif name == "retrieve_previous_context":
        which = result.get("which", "?")
        return f"Context #{which} retrieved"
    elif status and isinstance(status, str) and status.strip():
        return f"{name}: {status}"
    else:
        s = json.dumps(result, ensure_ascii=False)
        return f"Result: {s[:120]}"



class CheckpointModal(ModalScreen[bool]):
    """Interactive checkpoint review — Approve or Request Changes.

    Content is scrollable. Arrow keys navigate content first; when the scroll
    hits the boundary (top/bottom), the next press moves the Approve/Reject
    selection cursor. Enter to select, Esc to dismiss.
    On "Request Changes": an Input appears for rejection feedback.

    AT-13: constrained to 70% width x 80% height, centered, so the
    chat history remains partially visible behind the modal.
    """

    # AT-13: constrain modal size so chat is still visible behind
    DEFAULT_CSS = """
    CheckpointModal {
        align: center middle;
        width: 70%;
        height: 80%;
        max-width: 100;
        max-height: 42;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    CheckpointModal > VerticalScroll {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_option", "Confirm", show=False),
        Binding("ctrl+j", "select_option", "", show=False),
        Binding("escape", "dismiss_modal", "Cancel", show=True),
    ]

    _OPTIONS = [
        ("approve", "Approve — accept and continue pipeline"),
        ("reject", "Request Changes — provide feedback to redo"),
    ]

    def __init__(self, server_url: str, project_id: str,
                 checkpoint_label: str = "Checkpoint",
                 checkpoint_step: str = "?") -> None:
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.project_id = project_id
        self.checkpoint_label = checkpoint_label
        self.checkpoint_step = checkpoint_step
        self._cursor = 0        # 0 = approve, 1 = reject
        self._mode = "select"   # "select" | "feedback"

    def compose(self):
        yield Static(
            f"Checkpoint: {self.checkpoint_label} (step {self.checkpoint_step})",
            id="cp-header"
        )
        yield VerticalScroll(id="cp-content")
        yield Static("", id="cp-prompt")
        yield Static("", id="cp-options-display")
        yield Static("[↑↓] navigate  [Enter] confirm  [Esc] dismiss", id="cp-hint")
        yield Input(placeholder="Describe what needs to change...", id="cp-feedback-input", disabled=True)

    def on_mount(self):
        inp = self.query_one("#cp-feedback-input")
        inp.display = False
        inp.can_focus = False
        self._refresh_options()
        self._fetch_and_display()
        # P0-2: the backend can resolve a checkpoint out from under us (scheduler
        # kick, retry, another client) without an SSE event reaching this modal.
        # Poll every 5s; if the checkpoint is gone, self-dismiss to the dashboard
        # instead of stranding the user on a dead modal.
        self._recheck_timer = self.set_interval(5.0, self._recheck_checkpoint)

    def _dismiss_resolved(self, reason: str = "Checkpoint already resolved"):
        """Pop this modal and flash a message; safe to call more than once."""
        if getattr(self, "_dismissed", False):
            return
        self._dismissed = True
        try:
            self.app.query_one("#flash-bar").flash_immediate(reason)
        except Exception:
            pass
        try:
            dashboard = self.app.query_one("#dashboard-zone")
            dashboard._fetch_projects()
        except Exception:
            pass
        self.dismiss(False)

    @work(exclusive=True, group="recheck")
    async def _recheck_checkpoint(self):
        # Allow recheck during 'select' (idle) and '' (approving) modes.
        # The '' mode is set during _do_approve to block double-clicks;
        # if the HTTP call hangs, the recheck should still self-recover.
        if self._mode not in ("select", "") or getattr(self, "_dismissed", False):
            return  # already resolved or in feedback/other input mode
        url = f"/api/meta/{self.project_id}/checkpoint"
        try:
            resp = await self.app.http.get(url, timeout=3.0)
        except Exception:
            return  # transient network issue — keep the modal, try again next tick
        if resp.status_code == 404:
            self._dismiss_resolved()
            return
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                return
            if not data.get("checkpoint"):
                self._dismiss_resolved()

    def _refresh_options(self):
        lines = ["What would you like to do?", ""]
        for i, (_, label) in enumerate(self._OPTIONS):
            cursor = "●" if i == self._cursor else "○"
            lines.append(f"  {cursor} {label}")
        self.query_one("#cp-options-display").update("\n".join(lines))

    def action_cursor_up(self):
        if self._mode != "select":
            return
        # UX-2: if content is scrollable and not at the top, scroll it first.
        # Once the user reaches the top boundary, the next Up moves the
        # Approve/Reject selection cursor. This prevents the scrollable
        # checkpoint content from hijacking the arrow keys permanently.
        try:
            content = self.query_one("#cp-content")
            if content.scroll_offset.y > 0:
                content.scroll_up()
                return
        except Exception:
            pass
        self._cursor = (self._cursor - 1) % len(self._OPTIONS)
        self._refresh_options()

    def action_cursor_down(self):
        if self._mode != "select":
            return
        # UX-2: scroll first; at the bottom boundary, move selection cursor.
        try:
            content = self.query_one("#cp-content")
            max_y = content.virtual_size.height - content.container_size.height
            if content.scroll_offset.y < max_y:
                content.scroll_down()
                return
        except Exception:
            pass
        self._cursor = (self._cursor + 1) % len(self._OPTIONS)
        self._refresh_options()

    def action_select_option(self):
        if self._mode == "select":
            key = self._OPTIONS[self._cursor][0]
            if key == "approve":
                # Disable options immediately so user sees feedback
                self._mode = ""  # block double-clicks
                self.query_one("#cp-options-display").update("\n  ... Approving ...")
                self.query_one("#cp-hint").update("")
                self._do_approve()
            else:
                self._show_feedback()
        elif self._mode == "feedback":
            inp = self.query_one("#cp-feedback-input")
            feedback = inp.value.strip()
            if feedback:
                self._do_reject(feedback)

    @work(exclusive=True, group="fetch")
    async def _fetch_and_display(self):
        content_area = self.query_one("#cp-content")
        url = f"/api/meta/{self.project_id}/checkpoint"

        try:
            resp = await self.app.http.get(url, timeout=10.0)
            # P0-2: a 404 means the checkpoint doesn't exist (e.g. orphaned
            # project, or resolved before this modal opened). Don't strand the
            # user on a dead "Failed to load" modal — dismiss to the dashboard.
            if resp.status_code == 404:
                self._dismiss_resolved()
                return
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
                content_area.mount(Static(f"Failed to load checkpoint content: {e}"))
                return
        if not data.get("checkpoint"):
            self._dismiss_resolved()
            return

        step_output = data.get("step_output", {}) or {}
        files = step_output.get("files", {}) if isinstance(step_output, dict) else {}
        rejection_history = step_output.get("rejection_history") if isinstance(step_output, dict) else None

        # Show rejection summary if available (before or above file previews)
        if rejection_history:
            latest = rejection_history[-1]
            last_feedback = latest.get("user_feedback") or latest.get("reason") or "N/A"
            content_area.mount(Static(
                f"This step was revised {len(rejection_history)} time(s). "
                f"Last feedback: {str(last_feedback)[:200]}"
            ))

        if not files:
            if not rejection_history:
                content_area.mount(Static("(No file output to review)"))
            return

        # AT-19: for PM step (3), render a human-readable task summary first
        if self.checkpoint_step == "3":
            task_summary = self._build_pm_summary(files)
            if task_summary:
                content_area.mount(Static(task_summary))

        # UX-1: render full file content without truncation. Previously capped
        # at 30 lines which made the SOTA report unreadable in the checkpoint
        # review modal. VerticalScroll handles overflow natively.
        for fname, content in files.items():
            # AT-19: filter internal files from checkpoint display
            if fname.startswith(".") or fname == ".tasks_synced_hash":
                continue
            content_str = str(content)
            content_area.mount(Static(f" [{fname}]\n{content_str}"))

    @staticmethod
    def _build_pm_summary(files: dict) -> str:
        """AT-19: build a human-readable task summary from PM output files."""
        import json as _json
        # Parse tasks manifest for execution order
        manifest_str = files.get("tasks_manifest.json", "")
        tasks: list[dict] = []
        if manifest_str:
            try:
                manifest = _json.loads(manifest_str)
                exec_order = manifest.get("execution_order", [])
            except Exception:
                exec_order = []
        else:
            exec_order = []

        # Parse individual task cards
        for fname, content in sorted(files.items()):
            if fname.startswith("tasks/") and fname.endswith(".json"):
                try:
                    task = _json.loads(content)
                    tasks.append(task)
                except Exception:
                    pass

        if not tasks:
            return ""

        lines = [
            f"[bold]📋 Task Breakdown ({len(tasks)} tasks)[/bold]",
            "",
        ]
        for t in tasks:
            tid = t.get("id", "?")
            desc = t.get("description", "")[:100]
            deps = t.get("dependencies", [])
            dep_str = f" (depends: {', '.join(deps)})" if deps else ""
            lines.append(f"  [bold cyan]{tid}[/bold cyan]: {desc}{dep_str}")

        if exec_order:
            flat = []
            for group in exec_order:
                if isinstance(group, list):
                    flat.extend(group)
                else:
                    flat.append(group)
            lines.append("")
            lines.append(f"[dim]Execution order: {' → '.join(flat)}[/dim]")

        return "\n".join(lines)

    def _show_feedback(self):
        self._mode = "feedback"
        self.query_one("#cp-prompt").update(
            "Suggest changes — you are reviewing as Red Agent:"
        )
        self.query_one("#cp-options-display").display = False
        self.query_one("#cp-hint").update("[Enter] submit feedback  [Esc] cancel")
        inp = self.query_one("#cp-feedback-input")
        inp.placeholder = "Describe what needs to change in the output..."
        inp.display = True
        inp.can_focus = True
        inp.disabled = False
        inp.focus()

    @work(exclusive=True, group="approve")
    async def _do_approve(self):
        success = False
        error_msg = None
        url = f"/api/meta/{self.project_id}/checkpoint/approve"
        body = {"project_id": self.project_id, "checkpoint": "", "feedback": ""}

        try:
            resp = await self.app.http.post(url, json=body, timeout=45.0)
            resp.raise_for_status()
            success = True
        except Exception as e:
            if isinstance(e, httpx.ReadTimeout):
                # TUI-3: post timed out but backend may have accepted. Poll GET
                # /api/meta/{pid}/checkpoint once; if no checkpoint is pending
                # there, the approve went through and the backend has moved on.
                try:
                    ck = await self.app.http.get(
                        f"/api/meta/{self.project_id}/checkpoint", timeout=10.0
                    )
                    if ck.status_code == 200 and (ck.json().get("checkpoint") or "") == "":
                        # backend moved on (no checkpoint waiting). Approve went through.
                        success = True
                        error_msg = None
                    else:
                        error_msg = f"{type(e).__name__}: {e}"[:150]
                except Exception:
                    error_msg = f"{type(e).__name__}: {e}"[:150]
            else:
                error_msg = f"{type(e).__name__}: {e}"[:150]
        if success:
            self.query_one("#cp-options-display").update("\n  ✓ Approved")
            self.query_one("#cp-hint").update("")
            # Refresh the dashboard immediately so project table + sidebar
            # reflect the pipeline state change without waiting for the next
            # auto-refresh cycle.
            try:
                self.app.query_one("#dashboard-zone").force_refresh()
                # Also refresh the pipeline events panel
                notif = self.app.query_one("#notif-zone")
                notif.refresh_from_api()
            except Exception:
                pass
            self.dismiss(True)
        else:
            # Show error in modal so user knows to retry
            self._mode = "select"
            self._cursor = 0
            self._refresh_options()
            self.query_one("#cp-hint").update(
                f"[red]Approve failed[/]: {error_msg or 'unknown'} — try again"
            )

    @work(exclusive=True, group="approve")
    async def _do_reject(self, feedback: str):
        success = False
        url = f"/api/meta/{self.project_id}/checkpoint/reject"
        body = {"project_id": self.project_id, "checkpoint": "", "feedback": feedback}

        try:
            resp = await self.app.http.post(url, json=body, timeout=45.0)
            resp.raise_for_status()
            success = True
        except Exception:
            pass
        if success:
            try:
                self.app.query_one("#flash-bar").flash_immediate(
                    f"{self.project_id} rejected — redoing with feedback"
                )
                dashboard = self.app.query_one("#dashboard-zone")
                dashboard.force_refresh()
            except Exception:
                pass
            self.dismiss(False)
        # If HTTP failed, do NOT dismiss — user can retry

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle Enter in feedback mode — the Input consumes Enter before bindings."""
        if self._mode != "feedback":
            return
        feedback = event.value.strip()
        if feedback:
            self._do_reject(feedback)

    def action_dismiss_modal(self):
        self.dismiss(False)


class BriefReviewModal(ModalScreen[bool]):
    """Interactive brief/task review before submission.

    Navigate with Up/Down, Enter to select, Esc to dismiss.
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Up", show=False),
        Binding("down", "cursor_down", "Down", show=False),
        Binding("enter", "select_option", "Confirm", show=False),
        Binding("escape", "dismiss_modal", "Cancel", show=True),
    ]

    _OPTIONS = [
        ("approve", "Approve — submit and start the pipeline"),
        ("revise", "Request Changes — go back and describe what to change"),
    ]

    def __init__(self, server_url: str, submit_type: str,
                 project_id: str, review_content: str,
                 brief_data: dict | None = None) -> None:
        super().__init__()
        self.server_url = server_url.rstrip("/")
        self.submit_type = submit_type  # "project" or "task"
        self.project_id = project_id
        self.review_content = review_content
        self.brief_data = brief_data or {}
        self._cursor = 0

    def compose(self):
        if self.submit_type == "project":
            header = f"[bold]Review Project Brief — {self.project_id}[/bold]"
        else:
            header = f"[bold]Review Task — {self.project_id}[/bold]"
        yield Static(header)
        yield Static("")
        yield Static(self.review_content[:3000])
        yield Static("")
        yield Static(self._build_options(), id="brief-options")
        yield Static("[↑↓] navigate  [Enter] confirm  [Esc] cancel")

    def _build_options(self):
        lines = ["What would you like to do?", ""]
        for i, (_, label) in enumerate(self._OPTIONS):
            cursor = "●" if i == self._cursor else "○"
            lines.append(f"  {cursor} {label}")
        return "\n".join(lines)

    def _refresh_options(self):
        self.query_one("#brief-options", Static).update(self._build_options())

    def action_cursor_up(self):
        self._cursor = (self._cursor - 1) % len(self._OPTIONS)
        self._refresh_options()

    def action_cursor_down(self):
        self._cursor = (self._cursor + 1) % len(self._OPTIONS)
        self._refresh_options()

    def action_select_option(self):
        key = self._OPTIONS[self._cursor][0]
        if key == "approve":
            self._do_approve()
        else:
            self.dismiss(False)

    def action_dismiss_modal(self):
        self.dismiss(False)

    @work(exclusive=True)
    async def _do_approve(self):
        if self.submit_type == "project":
            await self._submit_project()
        else:
            await self._submit_task()

    async def _run_async_post(self, path: str, body: dict) -> tuple[bool, str]:
        """Execute HTTP POST using the app's shared async client."""
        try:
            resp = await self.app.http.post(path, json=body, timeout=30.0)
            resp.raise_for_status()
            return True, ""
        except Exception as e:
            return False, str(e)

    async def _submit_project(self):
        path = "/api/projects/submit"
        body = {
            "project_id": self.project_id,
            "brief": self.brief_data,
            "name": self.brief_data.get("project_name", self.project_id),
        }
        # A4 fix: always dismiss the modal, even on exception, so the
        # user is never trapped behind a stuck screen.
        try:
            ok, err = await self._run_async_post(path, body)
            if ok:
                self._flash(f"Project '{self.project_id}' submitted — pipeline starting")
            else:
                self._flash(f"Submit failed: {err}", error=True)
        except Exception as e:
            self._flash(f"Submit exception: {e}", error=True)
        finally:
            self.dismiss(True)

    async def _submit_task(self):
        path = "/api/tasks"
        body = {"project_id": self.project_id, "prompt": self.review_content}
        # A4 fix: same defensive pattern as _submit_project.
        try:
            ok, err = await self._run_async_post(path, body)
            if ok:
                self._flash(f"Task submitted to '{self.project_id}'")
            else:
                self._flash(f"Task submit failed: {err}", error=True)
        except Exception as e:
            self._flash(f"Submit exception: {e}", error=True)
        finally:
            self.dismiss(True)

    def _flash(self, text: str, error: bool = False):
        try:
            bar = self.app.query_one("#flash-bar")
            bar.flash_immediate(text)
        except Exception:
            pass


class ChatZone(Container):
    """Middle zone: conversational chat with the Meta Agent."""

    class ContextSaved(Message):
        def __init__(self, project_id: str) -> None:
            self.project_id = project_id
            super().__init__()

    def __init__(self, server_url: str, **kwargs):
        super().__init__(**kwargs)
        self.server_url = server_url.rstrip("/")
        self.history: list[dict] = []
        self.current_project: str | None = None
        self.session_id: str | None = None
        self._agent_streaming = False
        self._completion_matches: list[tuple] = []
        self._completion_index: int = 0
        self._skip_next_submit: bool = False
        self._pipeline_active = False
        self._pipeline_paused = False

    def compose(self):
        yield VerticalScroll(id="chat-log")
        yield Static("", id="completion-candidates", classes="completion-box")
        yield Input(
            placeholder="Message the agent... (/ to see commands)",
            id="chat-input",
        )

    def on_mount(self):
        self.can_focus = False
        self._init_session()

    @work(exclusive=True)
    async def _init_session(self):
        """Create or load the chat session for this TUI instance."""
        try:
            resp = await self.app.http.post(
                f"{self.server_url}/api/agent/session/create", timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.session_id = data.get("session_id")
        except Exception:
            pass  # Non-critical; chat works without session
        self.query_one("#chat-log").can_focus = False
        self.query_one("#completion-candidates").can_focus = False
        self.query_one("#completion-candidates").display = False
        self.query_one("#chat-input").focus()
        # AT-17: periodically update input placeholder based on pipeline state
        self._placeholder_timer = self.set_interval(2.0, self._update_input_placeholder)

    def _update_input_placeholder(self):
        """AT-17: Show pipeline-aware placeholder in the chat input."""
        try:
            import cli.completer as _comp
            state = _comp.flash_state
        except Exception:
            state = None

        try:
            inp = self.query_one("#chat-input", Input)
        except Exception:
            return

        if state and state.get("step"):
            step_name = state.get("step_name", state.get("step"))
            pid = state.get("project", state.get("project_id", ""))
            # AT-29: show "done" vs "running" based on state to avoid
            # contradictory text like "Research Review done running on..."
            if "done" in step_name.lower() or "✓" in step_name:
                new_placeholder = f"[{step_name} on {pid} — advancing]"
            elif state.get("checkpoint"):
                # Only show "wait for checkpoint" for steps that actually
                # have a checkpoint (steps 1, 2, 3). Task-loop steps
                # (t_plan, t_impl, t_verify) run without pausing.
                new_placeholder = f"[{step_name} running on {pid} — wait for checkpoint]"
            else:
                new_placeholder = f"[{step_name} running on {pid}]"
        else:
            new_placeholder = "Message the agent... (/ to see commands)"

        # Only update if changed to avoid flicker
        if inp.placeholder != new_placeholder:
            inp.placeholder = new_placeholder

    def set_current_project(self, project_id: str | None):
        self.current_project = project_id
        dashboard = self.app.query_one("#dashboard-zone")
        if project_id:
            dashboard.enter_project(project_id)
            self._load_chat_history()
        else:
            dashboard.exit_project()

    # ── Input handling ───────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed):
        """Live completion candidates as user types."""
        # AT-7: clear completion guard so next Enter isn't swallowed
        # after user types additional characters post-autocomplete
        self._skip_next_submit = False
        text = event.value
        box = self.query_one("#completion-candidates")

        if not text.startswith("/"):
            box.display = False
            self._completion_matches = []
            return

        # Don't show completion if user already typed arguments (after space)
        parts = text.split()
        if len(parts) > 1:
            box.display = False
            self._completion_matches = []
            return

        partial = text.split()[0].lower()
        self._completion_matches = [
            (cmd, desc, has_arg)
            for cmd, desc, has_arg in _SLASH_COMMANDS
            if cmd.startswith(partial)
        ]

        if not self._completion_matches or (
            len(self._completion_matches) == 1
            and self._completion_matches[0][0] == partial
        ):
            box.display = False
            return

        self._completion_index = 0
        self._render_completion()

    def _render_completion(self):
        box = self.query_one("#completion-candidates")
        matches = self._completion_matches
        if not matches:
            box.display = False
            return
        lines = []
        for i, (cmd, desc, has_arg) in enumerate(matches):
            cursor = "→" if i == self._completion_index else " "
            arg_hint = " <arg>" if has_arg else ""
            lines.append(f" {cursor} {cmd}{arg_hint}  — {desc}")
        box.update("\n".join(lines))
        box.display = True

    def on_key(self, event: events.Key) -> None:
        """Handle Tab completion and Up/Down navigation for slash commands."""
        box = self.query_one("#completion-candidates")
        if not box.display or not self._completion_matches:
            return

        if event.key == "tab":
            inp = self.query_one("#chat-input", Input)
            text = inp.value
            partial = text.split()[0].lower()
            matches = [cmd for cmd, _, _ in self._completion_matches]
            if matches:
                if len(matches) == 1:
                    inp.value = matches[0] + " "
                else:
                    common = os.path.commonprefix(matches)
                    if len(common) > len(partial):
                        inp.value = common
                inp.cursor_position = len(inp.value)
                self._skip_next_submit = True  # prevent auto-submit
            box.display = False
            self._completion_matches = []
            event.prevent_default()

        elif event.key == "up":
            self._completion_index = (self._completion_index - 1) % len(self._completion_matches)
            self._render_completion()
            event.prevent_default()

        elif event.key == "down":
            self._completion_index = (self._completion_index + 1) % len(self._completion_matches)
            self._render_completion()
            event.prevent_default()

        elif event.key == "enter":
            if self._completion_matches:
                cmd = self._completion_matches[self._completion_index][0]
                inp = self.query_one("#chat-input", Input)
                inp.value = cmd + " "
                inp.cursor_position = len(inp.value)
                self._skip_next_submit = True  # prevent auto-submit
            box.display = False
            self._completion_matches = []
            event.prevent_default()

        elif event.key == "escape":
            box.display = False
            self._completion_matches = []
            event.prevent_default()

    def on_input_submitted(self, event: Input.Submitted):
        # Skip if completion just filled the input (Enter/Tab selected a candidate)
        if self._skip_next_submit:
            self._skip_next_submit = False
            return
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        # Hide completion box
        self.query_one("#completion-candidates").display = False

        # Slash commands — handled locally
        if text.startswith("/"):
            if self._handle_slash(text):
                return

        # Add user message to chat display
        self._add_message("user", text)
        self.history.append({"role": "user", "content": text})

        # Stream agent response
        self._stream_agent_response(text)

    # ── Slash commands ───────────────────────────────────────────

    def _handle_slash(self, text: str) -> bool:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower()

        if cmd in ("/quit", "/exit", "/q"):
            self.app.exit()
            return True
        elif cmd == "/help":
            lines = []
            for c, desc, has_arg in _SLASH_COMMANDS:
                arg_hint = " <arg>" if has_arg else ""
                lines.append(f"  {c}{arg_hint}  — {desc}")
            self._add_system("\n".join(lines))
            return True
        elif cmd == "/clear":
            self.history.clear()
            log = self.query_one("#chat-log")
            for child in list(log.children):
                child.remove()
            return True
        elif cmd == "/projects":
            self.set_current_project(None)
            self._add_system("Switched to project list.")
            return True
        elif cmd == "/project":
            self._handle_project_cmd(rest.strip())
            return True
        elif cmd == "/status":
            self._show_status()
            return True
        elif cmd == "/checkpoint":
            self._show_checkpoint()
            return True
        elif cmd == "/approve":
            self._approve_checkpoint()
            return True
        elif cmd == "/reject":
            if not rest.strip():
                self._add_error("Usage: /reject <feedback on what needs to change>")
                return True
            self._reject_checkpoint(rest.strip())
            return True
        elif cmd == "/delete":
            self._handle_delete(rest.strip())
            return True
        elif cmd == "/logs":
            self._handle_logs(rest.strip())
            return True
        return False

    def _handle_project_cmd(self, arg: str):
        """Handle /project <number|id>."""
        if not arg:
            if self.current_project:
                self._add_system(f"Current project: {self.current_project}")
            else:
                self._add_system("No project selected. Use /project <# or id> to enter one.")
            return

        dashboard = self.app.query_one("#dashboard-zone")

        # Try as index number (1-based, matching dashboard display)
        try:
            idx = int(arg) - 1
            projects = dashboard.projects_cache
            if 0 <= idx < len(projects):
                pid = projects[idx]["project_id"]
                self.set_current_project(pid)
                self._add_system(f"Entered project: {pid} (type /projects to go back)")
                return
            else:
                self._add_error(f"Index {arg} out of range (1-{len(projects)})")
                return
        except ValueError:
            pass

        # Treat as project_id
        self.set_current_project(arg)
        self._add_system(f"Entered project: {arg}")

    @work(exclusive=True)
    async def _show_status(self):
        if not self.current_project:
            self._add_system("No project selected. Use /project <# or id> first.")
            return
        try:
            resp = await self.app.http.get(
                f"/api/projects/{self.current_project}/tasks", timeout=5.0
            )
            resp.raise_for_status()
            tasks = resp.json()
            if not tasks:
                self._add_system("No tasks yet.")
                return
            lines = []
            for t in tasks:
                tid = t.get("id", "?")
                status = t.get("status", "?")
                step = t.get("current_step", "")
                prompt = t.get("prompt", "")[:60]
                lines.append(f"  #{tid} [{status}] step={step} — {prompt}")
            self._add_system("\n".join(lines))
        except Exception as e:
            self._add_error(f"Failed to fetch status: {e}")

    @work(exclusive=True)
    async def _show_checkpoint(self):
        if not self.current_project:
            self._add_system("No project selected. Use /project <# or id> first.")
            return
        # Fetch checkpoint info to get label + step, then push the interactive modal
        try:
            resp = await self.app.http.get(
                f"/api/meta/{self.current_project}/checkpoint", timeout=5.0
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            self._add_error(f"Failed to fetch checkpoint: {e}")
            return
        if not data.get("checkpoint"):
            self._add_system("No pending checkpoint for this project.")
            return
        label = data.get("label", "Checkpoint")
        step = data.get("step", "?")
        self.app.push_screen(
            CheckpointModal(self.server_url, self.current_project, label, step)
        )

    @work(exclusive=True)
    async def _approve_checkpoint(self):
        if not self.current_project:
            self._add_system("No project selected. Use /project <# or id> first.")
            return
        # If checkpoint modal is open, delegate to it to avoid race condition
        for screen in self.app.screen_stack:
            if isinstance(screen, CheckpointModal):
                await screen._do_approve()
                return
        try:
            resp = await self.app.http.post(
                f"/api/meta/{self.current_project}/checkpoint/approve",
                json={"project_id": self.current_project, "checkpoint": "", "feedback": ""},
                timeout=5.0,
            )
            resp.raise_for_status()
            pid = self.current_project
            self._add_system(f"Checkpoint approved — {pid} pipeline resuming. Please wait...")
            # Update FlashBar immediately
            flash_bar = self.app.query_one("#flash-bar")
            flash_bar.flash_immediate(f"{pid} checkpoint approved")
            # Refresh dashboard
            dashboard = self.app.query_one("#dashboard-zone")
            dashboard._fetch_projects()
        except Exception as e:
            self._add_error(f"Failed to approve checkpoint: {e}")

    @work(exclusive=True)
    async def _reject_checkpoint(self, feedback: str):
        if not self.current_project:
            self._add_system("No project selected. Use /project <# or id> first.")
            return
        # If checkpoint modal is open, delegate to it
        for screen in self.app.screen_stack:
            if isinstance(screen, CheckpointModal):
                await screen._do_reject(feedback)
                return
        try:
            resp = await self.app.http.post(
                f"/api/meta/{self.current_project}/checkpoint/reject",
                json={"project_id": self.current_project, "checkpoint": "", "feedback": feedback},
                timeout=5.0,
            )
            resp.raise_for_status()
            pid = self.current_project
            self._add_system(f"Changes requested — step will redo with your feedback.")
            flash_bar = self.app.query_one("#flash-bar")
            flash_bar.flash_immediate(f"{pid} checkpoint rejected — redoing")
            dashboard = self.app.query_one("#dashboard-zone")
            dashboard._fetch_projects()
        except Exception as e:
            self._add_error(f"Failed to reject checkpoint: {e}")

    # ── Delete / Logs handlers ───────────────────────────────────────

    @work(exclusive=True)
    async def _handle_delete(self, arg: str):
        target = arg.strip() or self.current_project
        if not target:
            self._add_error("Usage: /delete <project_id or #>")
            return

        # Resolve display index to actual project_id
        try:
            idx = int(target)
            dashboard = self.app.query_one("#dashboard-zone")
            projects = dashboard.projects_cache
            if 0 <= idx < len(projects):
                target = projects[idx]["project_id"]
            else:
                self._add_error(f"Index {arg} out of range (1-{len(projects)})")
                return
        except ValueError:
            pass  # not an index — treat as literal project_id

        self._add_system(f"Deleting project '{target}'...")
        try:
            resp = await self.app.http.delete(
                f"/api/projects/{target}?cascade=true", timeout=5.0
            )
            resp.raise_for_status()
            self._add_system(f"Project '{target}' deleted.")
            if self.current_project == target:
                self.set_current_project(None)
            dashboard = self.app.query_one("#dashboard-zone")
            dashboard._fetch_projects()
        except Exception as e:
            self._add_error(f"Failed to delete project: {e}")

    @work(exclusive=True)
    async def _handle_logs(self, arg: str):
        task_id = arg.strip()
        if not task_id:
            self._add_error("Usage: /logs <task_id>")
            return
        try:
            resp = await self.app.http.get(
                f"/api/tasks/{task_id}/logs", timeout=5.0
            )
            resp.raise_for_status()
            data = resp.json()
            logs = data.get("logs", data.get("entries", []))
            if isinstance(logs, list):
                if not logs:
                    self._add_system(f"No log entries for task #{task_id}.")
                    return
                lines = [f"--- Logs for task #{task_id} ---"]
                for entry in logs[-30:]:  # last 30 entries
                    lines.append(str(entry)[:300])
                self._add_system("\n".join(lines))
            else:
                self._add_system(f"Logs for task #{task_id}:\n{str(logs)[:3000]}")
        except Exception as e:
            self._add_error(f"Failed to fetch logs: {e}")

    # ── Agent streaming ──────────────────────────────────────────

    @work(exclusive=True)
    async def _stream_agent_response(self, message: str):
        """POST to /api/agent/chat and stream SSE events."""
        if self._agent_streaming:
            self._add_system("Agent is still responding, please wait...")
            return

        self._agent_streaming = True
        agent_widget = None
        full_agent_text = ""
        tool_names = []

        try:
            async with self.app.http.stream(
                "POST",
                "/api/agent/chat",
                json={
                    "message": message,
                    "history": self.history,
                    "current_project": self.current_project,
                    "session_id": self.session_id,
                },
                timeout=None,
            ) as resp:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue

                        etype = event.get("type")

                        if etype == "text_delta":
                            if agent_widget is None:
                                agent_widget = self._add_message("agent", "")
                            full_agent_text += event.get("content", "")
                            agent_widget.update(full_agent_text)
                            self._scroll_to_bottom()

                        elif etype == "tool_call":
                            name = event.get("name", "?")
                            tool_names.append(name)
                            args = event.get("args", {})
                            summary = ""
                            for key in ("project_id", "task_id", "prompt", "checkpoint", "name"):
                                if key in args:
                                    val = str(args[key])
                                    if len(val) > 40:
                                        val = val[:40] + "..."
                                    summary = f'{key}="{val}"'
                                    break
                            self._add_tool(
                                f"Calling {name}({summary})" if summary else f"Calling {name}()"
                            )
                            self._scroll_to_bottom()

                        elif etype == "tool_result":
                            name = event.get("name", "?")
                            result = event.get("result", {})
                            summary = _format_tool_result(name, result)
                            self._add_tool(summary)
                            self._scroll_to_bottom()

                            # Intercept pending_confirm: show review modal
                            if result.get("status") == "pending_confirm":
                                r_pid = result.get("project_id", "")
                                submit_type = (
                                    "project" if "project" in name else "task"
                                )
                                content = (
                                    result.get("brief_markdown")
                                    or result.get("task_summary")
                                    or ""
                                )
                                brief_data = result.get("brief") if submit_type == "project" else None
                                approved = await self._show_brief_review_modal(
                                    submit_type, r_pid, content, brief_data
                                )
                                if approved:
                                    self._add_system(
                                        f"{submit_type.title()} submitted — pipeline starting."
                                    )
                                else:
                                    # Append rejection feedback to history so meta-agent can retry
                                    self.history.append({
                                        "role": "user",
                                        "content": (
                                            f"The {submit_type} was not approved. "
                                            "Please ask what changes the user wants and revise."
                                        ),
                                    })
                                    self._add_system(
                                        f"{submit_type.title()} review cancelled. "
                                        "Describe what to change."
                                    )
                                # Refresh dashboard on either outcome
                                dashboard = self.app.query_one("#dashboard-zone")
                                if submit_type == "project":
                                    dashboard._fetch_projects()
                                else:
                                    dashboard._fetch_tasks()

                        elif etype == "done":
                            msg = event.get("message", {})
                            content = msg.get("content", "")
                            if agent_widget is None:
                                agent_widget = self._add_message("agent", content)
                            else:
                                agent_widget.update(content)
                            self.history.append({"role": "assistant", "content": content})
                            self._scroll_to_bottom()
                            submit_type = self._detect_submit(tool_names)
                            if submit_type:
                                # Project submit: clear history (fresh start).
                                # Task submit: keep history (user may add more tasks).
                                self._save_context(clear_history=(submit_type == "project"))
                            # Always refresh dashboard after agent finishes a turn
                            dashboard = self.app.query_one("#dashboard-zone")
                            if self.current_project:
                                dashboard._fetch_tasks()
                            else:
                                dashboard._fetch_projects()

                        elif etype == "error":
                            self._add_error(event.get("message", "Unknown error"))
                            self._scroll_to_bottom()

        except Exception as e:
            self._add_error(f"Connection error: {e}")
        finally:
            self._agent_streaming = False
            # Reload history to pick up any checkpoint messages injected by scheduler
            self._reload_session_history()

    # ── Context management ───────────────────────────────────────

    async def _show_brief_review_modal(
        self, submit_type: str, project_id: str,
        review_content: str, brief_data: dict | None = None,
    ) -> bool:
        """Push a BriefReviewModal and wait for user response."""
        future: asyncio.Future = asyncio.Future()
        modal = BriefReviewModal(
            self.server_url, submit_type, project_id,
            review_content, brief_data,
        )

        def on_dismiss(result: bool) -> None:
            future.set_result(result)

        self.app.push_screen(modal, callback=on_dismiss)
        return await future

    def _detect_submit(self, tool_names: list[str]) -> str | None:
        """Return 'task' if suggest_submit_task was used, else None."""
        if "suggest_submit_task" in tool_names:
            return "task"
        return None

    def _save_context(self, clear_history: bool = True):
        if not self.history or not self.current_project:
            return
        _META_DIR.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        pid = self.current_project or "unknown"
        path = _META_DIR / f"{pid}_context_{ts}.json"
        path.write_text(json.dumps(self.history, ensure_ascii=False, indent=2))
        if clear_history:
            self.history.clear()
        files = sorted(_META_DIR.glob(f"{pid}_context_*.json"))
        for f in files[:-3]:
            f.unlink()
        if clear_history:
            self._add_system("[Context saved and cleared. Agent starts fresh.]")
        else:
            self._add_system("[Context saved. You can continue adding tasks.]")

    # ── Chat rendering helpers ───────────────────────────────────

    def _add_message(self, role: str, text: str) -> Static:
        css_class = f"chat-{role}"
        prefix = "You: " if role == "user" else "Agent: "
        widget = Static(prefix + text, classes=css_class)
        log = self.query_one("#chat-log")
        log.mount(widget)
        self._persist_message(role, prefix + text)
        return widget

    def _add_tool(self, text: str):
        widget = Static(f"  >> {text}", classes="chat-tool")
        self.query_one("#chat-log").mount(widget)
        self._persist_message("tool", text)

    def _add_system(self, text: str):
        widget = Static(text, classes="chat-system")
        self.query_one("#chat-log").mount(widget)
        self._persist_message("system", text)

    def _add_error(self, text: str):
        widget = Static(f"Error: {text}", classes="chat-error")
        self.query_one("#chat-log").mount(widget)
        self._persist_message("error", text)

    def _persist_message(self, role: str, text: str):
        """Save chat message to DB for session persistence."""
        try:
            from core.db_manager import get_db_manager
            db = get_db_manager()
            if self.session_id and self.current_project:
                db.save_chat_message_with_session(
                    self.session_id, self.current_project, role, text)
            elif self.current_project:
                db.save_chat_message(self.current_project, role, text)
        except Exception:
            pass  # Best-effort; don't break UI on DB failure

    @work(exclusive=True)
    async def _reload_session_history(self):
        """Pull chat messages from DB (includes checkpoint injections)."""
        if not self.session_id:
            return
        try:
            from core.db_manager import get_db_manager
            db = get_db_manager()
            messages = db.get_chat_history_by_session(self.session_id, limit=100)
            # Find messages newer than what we have in self.history
            existing_keys = {
                (m.get("role"), m.get("content", "")[:100]) for m in self.history
            }
            for msg in messages:
                key = (msg["role"], msg.get("content", "")[:100])
                if key not in existing_keys:
                    role = msg["role"]
                    text = msg["content"]
                    if role == "assistant":
                        self._add_message("agent", text)
                    elif role == "user":
                        self._add_message("user", text)
                    elif role == "system":
                        self._add_system(text)
                    self.history.append({"role": role, "content": text})
                    existing_keys.add(key)
            self._scroll_to_bottom()
        except Exception:
            pass

    def _load_chat_history(self):
        """Restore chat messages from previous sessions."""
        try:
            from core.db_manager import get_db_manager
            db = get_db_manager()
            if not self.current_project:
                return
            # B4: clear any prior project's transcript before loading this
            # project's history, otherwise switching projects leaves the
            # previous project's scrollback stacked above the new one.
            log = self.query_one("#chat-log")
            for child in list(log.children):
                child.remove()
            self.history.clear()
            messages = db.get_chat_history(self.current_project, limit=50)
            for msg in messages:
                role = msg["role"]
                text = msg["content"]
                if role == "user":
                    widget = Static(text, classes="chat-user")
                elif role == "error":
                    widget = Static(text, classes="chat-error")
                elif role == "tool":
                    widget = Static(text, classes="chat-tool")
                else:
                    widget = Static(text, classes="chat-system")
                self.query_one("#chat-log").mount(widget)
        except Exception:
            pass

    def _scroll_to_bottom(self):
        log = self.query_one("#chat-log")
        log.scroll_end(animate=False)
