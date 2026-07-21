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
_HISTORY_FILE = Path.home() / ".AItelier" / "cmd_history.json"

# Slash commands: (command, description, takes_arg, needs_project)
# needs_project=True means the command is only shown/completed when inside a project.
_SLASH_COMMANDS = [
    # Global commands (always available)
    ("/help", "Show all commands", False, False),
    ("/quit", "Exit AItelier", False, False),
    ("/clear", "Clear chat history", False, False),
    ("/projects", "Back to project list dashboard", False, False),
    ("/project", "Enter project by # or ID (e.g. /project 1)", True, False),
    ("/new", "Create a new project via meta conversation", False, False),
    ("/url", "Show/change backend URL", True, False),
    ("/frequency", "Scheduler poll interval", True, False),
    ("/cron", "Scheduler cron schedule", True, False),
    ("/restart", "Restart backend server", False, False),
    ("/mode", "Show/switch butler↔coding mode (/mode coding)", True, False),
    ("/delete", "Delete a project (e.g. /delete hello-world)", True, False),
    # Project-scoped commands (only when inside a project)
    ("/status", "Show current project tasks", False, True),
    ("/output", "View step output files (/output <task_id> [step])", True, True),
    ("/logs", "Show task logs (e.g. /logs 1)", True, True),
    ("/trace", "View execution traces (API, prompt/response pairs)", True, True),
    ("/runs", "List pipeline runs for current project", False, True),
    ("/errors", "View last pipeline error", False, True),
    ("/tree", "Browse workspace directory tree", True, True),
    ("/cat", "Read a workspace file (/cat <path>)", True, True),
    ("/edit", "Edit project (name, brief, priority, status)", True, True),
    ("/pause", "Pause the current project", False, True),
    ("/resume", "Resume the paused project", False, True),
    ("/refresh", "Re-run Researcher + Architect planning steps", False, True),
    ("/retry", "Retry a failed task or project", True, True),
    ("/rollback", "Rollback task to a git commit (/rollback <id> <hash>)", True, True),
    ("/cancel-task", "Cancel a running or pending task", True, True),
    ("/checkpoint", "View pending checkpoint for review", False, True),
    ("/approve", "Approve checkpoint to continue pipeline", False, True),
    ("/reject", "Reject checkpoint with feedback", True, True),
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
    elif name == "start_project_conversation":
        st = status or "started"
        return f"Started project conversation for \"{pid}\" ({st})"
    elif name == "answer_project_conversation":
        return f"Answer relayed ({status or 'ok'})"
    elif name == "approve_project_brief":
        return (f"Brief approved — pipeline starting for \"{pid}\""
                if status == "submitted" else f"Approve brief: {status or '?'}")
    elif name == "list_tasks":
        n = len(result.get("tasks", []))
        return f"{n} task(s)" if n else "no tasks"
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
        await self._submit_project()

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
        # Butler vs coding mode — user-toggled (never model-toggled, so prompt
        # injection can't escalate). Sent on every /api/agent/chat request and
        # persisted server-side to sessions.mode. Coding mode unlocks the
        # interactive coding agent (edit_file/bash/generate_pipeline/…).
        self._agent_mode: str = "butler"
        self._agent_streaming = False
        self._completion_matches: list[tuple] = []
        self._completion_index: int = 0
        self._skip_next_submit: bool = False
        self._pipeline_active = False
        self._pipeline_paused = False
        # Command history (max 100)
        self._cmd_history: list[str] = []
        self._history_index: int = -1  # -1 = no history navigation active
        self._history_search: str = ""  # current ctrl-r search term
        self._history_search_mode: bool = False
        self._saved_input: str = ""  # input saved before history navigation
        self._completion_just_accepted: bool = False  # set on Enter, cleared on next Submit

    def compose(self):
        yield VerticalScroll(id="chat-log")
        yield Input(
            placeholder="Message the agent... (/ to see commands)",
            id="chat-input",
        )
        yield Static("", id="completion-candidates", classes="completion-box")

    def on_mount(self):
        self.can_focus = False
        self._load_history()
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
        # Don't clear skip when completion is active — the value change
        # came from our own Enter/Tab handler, not from user typing.
        if not (self.query_one("#completion-candidates").display and self._completion_matches):
            self._skip_next_submit = False
        text = event.value
        box = self.query_one("#completion-candidates")

        # ── History search mode: filter by input text ──
        if self._history_search_mode:
            self._filter_history_matches(text)
            return

        # ── Slash command completion ──
        if not text.startswith("/"):
            box.display = False
            self._completion_matches = []
            return

        parts = text.split()
        if len(parts) > 1:
            box.display = False
            self._completion_matches = []
            return

        partial = text.split()[0].lower()
        in_project = bool(self.current_project)
        self._completion_matches = [
            (cmd, desc, has_arg)
            for cmd, desc, has_arg, needs_proj in _SLASH_COMMANDS
            if cmd.startswith(partial) and (in_project or not needs_proj)
        ]

        if not self._completion_matches or (
            len(self._completion_matches) == 1
            and self._completion_matches[0][0] == partial
        ):
            box.display = False
            return

        self._completion_index = 0
        self._render_completion()

    def _filter_history_matches(self, text: str):
        """Filter command history by text and show in completion box."""
        box = self.query_one("#completion-candidates")
        term = text.lower()

        if not self._cmd_history:
            box.display = False
            return

        if not term:
            # Show all history (most recent first)
            matches = list(enumerate(self._cmd_history))
            matches.reverse()  # newest first
            self._completion_matches = [
                (cmd, f"history #{i}", False)
                for i, cmd in matches
            ]
        else:
            # Filter by search term (newest first)
            self._completion_matches = [
                (cmd, f"history #{i}", False)
                for i, cmd in enumerate(self._cmd_history)
                if term in cmd.lower()
            ]
            self._completion_matches.reverse()  # newest first

        if not self._completion_matches:
            box.update("  no matching history")
            box.display = True
            return

        self._completion_index = 0
        self._render_completion()

    def _render_completion(self):
        box = self.query_one("#completion-candidates")
        matches = self._completion_matches
        if not matches:
            box.display = False
            return

        idx = self._completion_index
        total = len(matches)
        max_height = 10

        if total <= max_height:
            # All items fit — no indicators needed
            start, end = 0, total
            has_above = has_below = False
        else:
            # Reserve 2 lines for indicators, cursor centered in remaining space
            max_items = max_height - 2
            half = max_items // 2
            start = idx - half
            if start < 0:
                start = 0
            elif start + max_items > total:
                start = total - max_items
            end = start + max_items
            has_above = start > 0
            has_below = end < total

        lines = []
        if has_above:
            lines.append(f"  ... {start} more above")
        for i in range(start, end):
            cmd, desc, has_arg = matches[i]
            cursor = "→" if i == idx else " "
            arg_hint = " <arg>" if has_arg else ""
            lines.append(f" {cursor} {cmd}{arg_hint}  — {desc}")
        if has_below:
            lines.append(f"  ... {total - end} more below")

        box.update("\n".join(lines))
        box.display = True

    def on_key(self, event: events.Key) -> None:
        """Handle completion navigation, history search, and plain history."""
        box = self.query_one("#completion-candidates")
        inp = self.query_one("#chat-input", Input)

        # ── Ctrl+R: toggle history search mode ──
        if event.key == "ctrl+r":
            if self._history_search_mode:
                # Already searching — cancel
                self._history_search_mode = False
                self._completion_matches = []
                box.display = False
            elif self._cmd_history:
                # Start history search with current input as search term
                self._history_search_mode = True
                self._saved_input = inp.value
                self._filter_history_matches(inp.value)
            # else: no history — silently ignore
            event.prevent_default()
            return

        # ── Escape: cancel any completion/search mode ──
        if event.key == "escape":
            if self._history_search_mode:
                self._history_search_mode = False
                self._completion_matches = []
                inp.value = self._saved_input
                inp.cursor_position = len(inp.value)
                box.display = False
                event.prevent_default()
                return
            if box.display and self._completion_matches:
                self._completion_matches = []
                box.display = False
                event.prevent_default()
                return

        # ── Completion box is visible: navigate/select matches ──
        if box.display and self._completion_matches:
            if event.key == "up":
                self._completion_index = (self._completion_index - 1) % len(self._completion_matches)
                self._render_completion()
                event.prevent_default()
                return

            if event.key == "down":
                self._completion_index = (self._completion_index + 1) % len(self._completion_matches)
                self._render_completion()
                event.prevent_default()
                return

            if event.key == "enter":
                cmd = self._completion_matches[self._completion_index][0]
                # Set flag so on_input_submitted skips this Submit
                self._completion_just_accepted = True
                self._completion_matches = []
                box.display = False
                if self._history_search_mode:
                    self._history_search_mode = False
                    inp.value = cmd
                    inp.cursor_position = len(inp.value)
                else:
                    inp.value = cmd + " "
                    inp.cursor_position = len(inp.value)
                event.prevent_default()
                return

            if event.key == "tab":
                matches = [cmd for cmd, _, _ in self._completion_matches]
                if matches and not self._history_search_mode:
                    # Tab-complete slash command prefix
                    partial = inp.value.split()[0].lower()
                    common = os.path.commonprefix(matches)
                    if len(common) > len(partial):
                        inp.value = common
                        inp.cursor_position = len(inp.value)
                        self._skip_next_submit = True
                self._completion_matches = []
                box.display = False
                event.prevent_default()
                return

            # Any other key in search mode: let it through to Input,
            # on_input_changed will re-filter
            if self._history_search_mode:
                return

            return

        # ── Normal mode (no completion box): Up/Down = history ──
        if event.key == "up":
            self._navigate_history(-1)
            event.prevent_default()
            return

        if event.key == "down":
            self._navigate_history(1)
            event.prevent_default()
            return

    # ── History persistence ────────────────────────────────────────

    def _load_history(self):
        """Load command history from disk."""
        try:
            if _HISTORY_FILE.exists():
                data = json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._cmd_history = [str(x) for x in data[-100:]]
        except Exception:
            pass

    def _save_history(self):
        """Persist command history to disk (max 100)."""
        try:
            _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            _HISTORY_FILE.write_text(
                json.dumps(self._cmd_history[-100:], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ── History navigation ──────────────────────────────────────────

    def _navigate_history(self, direction: int):
        """Navigate command history (direction: -1=older, +1=newer)."""
        inp = self.query_one("#chat-input", Input)
        if not self._cmd_history:
            return

        if self._history_index == -1:
            self._saved_input = inp.value
            if direction < 0:
                self._history_index = len(self._cmd_history) - 1
            else:
                self._history_index = 0
        else:
            self._history_index += direction
            if self._history_index < 0 or self._history_index >= len(self._cmd_history):
                self._history_index = -1
                inp.value = self._saved_input
                inp.cursor_position = len(inp.value)
                return

        inp.value = self._cmd_history[self._history_index]
        inp.cursor_position = len(inp.value)

    def on_input_submitted(self, event: Input.Submitted):
        # Skip if completion just filled the input (Enter selected a candidate)
        if self._completion_just_accepted:
            self._completion_just_accepted = False
            return
        if self._skip_next_submit:
            self._skip_next_submit = False
            return
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""

        # Save to command history (deduplicate consecutive, max 100)
        if not self._cmd_history or self._cmd_history[-1] != text:
            self._cmd_history.append(text)
            if len(self._cmd_history) > 100:
                self._cmd_history.pop(0)
            self._save_history()
        self._history_index = -1
        self._history_search_mode = False

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
            in_project = bool(self.current_project)
            lines = []
            for c, desc, has_arg, needs_proj in _SLASH_COMMANDS:
                if needs_proj and not in_project:
                    continue
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
        elif cmd == "/new":
            self._add_system("To create a new project, just describe what you want to build. "
                             "For example: 'build me a todo app with FastAPI'")
            return True
        elif cmd == "/runs":
            self._show_runs()
            return True
        elif cmd == "/trace":
            self._show_trace(rest.strip())
            return True
        elif cmd == "/tree":
            self._show_tree(rest.strip())
            return True
        elif cmd == "/cat":
            self._handle_cat_cmd(rest.strip())
            return True
        elif cmd == "/output":
            self._handle_output_cmd(rest.strip())
            return True
        elif cmd == "/errors":
            self._show_errors()
            return True
        elif cmd == "/pause":
            self._handle_pause_cmd()
            return True
        elif cmd == "/resume":
            self._handle_resume_cmd()
            return True
        elif cmd == "/refresh":
            self._handle_refresh_cmd()
            return True
        elif cmd == "/retry":
            self._handle_retry_cmd(rest.strip())
            return True
        elif cmd == "/rollback":
            self._handle_rollback_cmd(rest.strip())
            return True
        elif cmd == "/cancel-task":
            self._handle_cancel_task_cmd(rest.strip())
            return True
        elif cmd == "/edit":
            self._add_system("Use /edit <field> <value> — fields: name, brief, priority, status")
            return True
        elif cmd == "/url":
            self._handle_url_cmd(rest.strip())
            return True
        elif cmd == "/frequency":
            self._handle_frequency_cmd(rest.strip())
            return True
        elif cmd == "/cron":
            self._handle_cron_cmd(rest.strip())
            return True
        elif cmd == "/restart":
            self._handle_restart_cmd()
            return True
        elif cmd == "/mode":
            self._handle_mode_cmd(rest.strip())
            return True
        return False

    def _handle_mode_cmd(self, arg: str):
        """Show or switch the butler↔coding agent mode.

        Coding mode unlocks the interactive coding agent (edit_file / bash /
        generate_pipeline / drive_pipeline / …). Mirrors the web SPA's mode
        toggle; the request field is the ONLY way to set it (never a model
        tool), so this stays a user-driven control."""
        arg = (arg or "").lower()
        if not arg:
            self._add_system(
                f"Agent mode: {self._agent_mode}. "
                "Use /mode coding or /mode butler to switch.")
            return
        if arg not in ("butler", "coding"):
            self._add_error("Usage: /mode <butler|coding>")
            return
        self._agent_mode = arg
        self._add_system(
            f"Switched to {arg} mode."
            + (" Coding agent unlocked (edit_file/bash/generate_pipeline/…)."
               if arg == "coding" else ""))

    def _handle_project_cmd(self, arg: str):
        """Handle /project <number|id>."""
        if not arg:
            if self.current_project:
                self._add_system(f"Current project: {self.current_project}")
            else:
                self._add_system("No project selected. Use /project <# or id> to enter one.")
            return

        # Try as index number (1-based, matching dashboard display)
        try:
            idx = int(arg) - 1
            import httpx
            resp = httpx.get(f"{self.server_url}/api/projects", timeout=5.0)
            resp.raise_for_status()
            projects = resp.json()
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
        # AT-1: For meta conversation (gather step), tell user to use chat
        if step == "gather":
            self._add_system(
                "This is a requirements conversation checkpoint. "
                "Answer the question or approve/reject the brief in chat — "
                "the butler agent handles it."
            )
            return
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
            import httpx
            resp = httpx.get(f"{self.server_url}/api/projects", timeout=5.0)
            resp.raise_for_status()
            projects = resp.json()
            if 0 <= idx < len(projects):
                target = projects[idx]["project_id"]
            else:
                self._add_error(f"Index {target} out of range (1-{len(projects)})")
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

    # ── New slash command handlers ────────────────────────────────

    @work(exclusive=True)
    async def _show_runs(self):
        """Handle /runs — list pipeline runs for current project."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        try:
            resp = await self.app.http.get(
                f"/api/projects/{self.current_project}/runs", timeout=5.0
            )
            resp.raise_for_status()
            data = resp.json()
            runs = data.get("runs", [])
            if not runs:
                self._add_system("No pipeline runs found for this project.")
                return
            lines = [f"--- Runs for {self.current_project} ---"]
            for r in runs:
                status = r.get("status", "?")
                steps = f"{r.get('completed_steps', 0)}/{r.get('step_count', 0)}"
                lines.append(
                    f"  {r.get('id', '?')[:22]} [{status}] steps={steps} "
                    f"graph={r.get('graph_name', '?')}"
                )
            lines.append("Use /trace <run_id> to view execution traces.")
            self._add_system("\n".join(lines))
        except Exception as e:
            self._add_error(f"Failed to fetch runs: {e}")

    @work(exclusive=True)
    async def _show_trace(self, arg: str):
        """Handle /trace [run_id] [category] — show execution traces."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        parts = arg.split()
        category = parts[1] if len(parts) > 1 else None

        # Resolve run_id
        if parts:
            run_id = parts[0]
        else:
            try:
                resp = await self.app.http.get(
                    f"/api/projects/{self.current_project}/runs", timeout=5.0
                )
                resp.raise_for_status()
                runs = resp.json().get("runs", [])
                if not runs:
                    self._add_system("No runs found.")
                    return
                run_id = runs[0]["id"]
            except Exception as e:
                self._add_error(f"Failed to resolve run: {e}")
                return

        try:
            params = {"limit": 50}
            if category:
                params["category"] = category
            resp = await self.app.http.get(
                f"/api/runs/{run_id}/trace", params=params, timeout=10.0
            )
            resp.raise_for_status()
            data = resp.json()
            traces = data.get("traces", [])
            if not traces:
                self._add_system(f"No trace entries for run {run_id}.")
                return
            lines = [f"--- Trace for run {run_id} ({len(traces)} entries) ---"]
            for t in traces[-30:]:
                cat = t.get("category", "")
                event = t.get("event", "")
                step = t.get("step_id", "-")
                payload = t.get("payload", {})
                if cat == "prompt":
                    text = str(payload.get("user", ""))[:120].replace("\n", " ")
                    lines.append(f"  → [{step}] {event}: {text}")
                elif cat == "response":
                    text = str(payload.get("text", ""))[:120].replace("\n", " ")
                    lines.append(f"  ← [{step}] {event}: {text}")
                elif cat == "tool_call":
                    params_str = str(payload.get("params", {}))[:100]
                    lines.append(f"  🔧 [{step}] {event}: {params_str}")
                elif cat == "error":
                    lines.append(f"  ❌ [{step}] {event}: {str(payload)[:150]}")
            self._add_system("\n".join(lines))
        except Exception as e:
            self._add_error(f"Failed to fetch trace: {e}")

    @work(exclusive=True)
    async def _show_tree(self, arg: str):
        """Handle /tree [subdir] — browse workspace directory tree."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        params = {}
        if arg:
            params["subdir"] = arg
        try:
            resp = await self.app.http.get(
                f"/api/projects/{self.current_project}/workspace/tree",
                params=params, timeout=5.0,
            )
            resp.raise_for_status()
            data = resp.json()
            tree = data.get("tree", [])
            if not tree:
                self._add_system(f"Workspace is empty{' in ' + arg if arg else ''}.")
                return
            lines = [f"--- Workspace: {self.current_project}{'/' + arg if arg else ''} ---"]
            for path in tree[:40]:
                lines.append(f"  {path}")
            if len(tree) > 40:
                lines.append(f"  ... and {len(tree) - 40} more files")
            self._add_system("\n".join(lines))
        except Exception as e:
            self._add_error(f"Failed to fetch tree: {e}")

    @work(exclusive=True)
    async def _handle_cat_cmd(self, arg: str):
        """Handle /cat <path> — read a workspace file."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        if not arg:
            self._add_error("Usage: /cat <path>  (use /tree to browse paths)")
            return
        try:
            resp = await self.app.http.get(
                f"/api/projects/{self.current_project}/workspace/file",
                params={"path": arg}, timeout=5.0,
            )
            resp.raise_for_status()
            content = resp.json().get("content", "")
            self._add_system(f"--- {arg} ---\n{content[:3000]}")
        except Exception as e:
            self._add_error(f"Failed to read file: {e}")

    @work(exclusive=True)
    async def _handle_output_cmd(self, arg: str):
        """Handle /output <task_id> [step_id] — view step output."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        parts = arg.split()
        if not parts:
            self._add_error("Usage: /output <task_id> [step_id]")
            return
        try:
            task_id = int(parts[0])
        except ValueError:
            self._add_error(f"Invalid task ID: {parts[0]}")
            return
        step_id = parts[1] if len(parts) > 1 else None
        try:
            if step_id:
                resp = await self.app.http.get(
                    f"/api/tasks/{task_id}/steps/{step_id}/output", timeout=5.0
                )
                resp.raise_for_status()
                data = resp.json()
                files = data.get("files", {})
                if not files:
                    self._add_system(f"No output files for task #{task_id} step {step_id}.")
                    return
                lines = [f"--- Output for task #{task_id} step {step_id} ---"]
                for fname, content in files.items():
                    lines.append(f"\n[{fname}]\n{str(content)[:2000]}")
                self._add_system("\n".join(lines))
            else:
                # List available steps
                resp = await self.app.http.get(
                    f"/api/tasks/{task_id}", timeout=5.0
                )
                resp.raise_for_status()
                task = resp.json()
                completed = task.get("completed_steps", [])
                current = task.get("current_step", "")
                self._add_system(
                    f"Task #{task_id}: current_step={current}, "
                    f"completed_steps={completed}\n"
                    f"Use /output {task_id} <step_id> to view a specific step."
                )
        except Exception as e:
            self._add_error(f"Failed to fetch output: {e}")

    @work(exclusive=True)
    async def _show_errors(self):
        """Handle /errors — show last pipeline error."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        try:
            resp = await self.app.http.get(
                f"/api/projects/{self.current_project}", timeout=5.0
            )
            resp.raise_for_status()
            project = resp.json()
            meta_state = project.get("meta_state")
            if meta_state:
                import json as _json
                try:
                    state = _json.loads(meta_state)
                    error = state.get("error", "")
                    step = state.get("step", "?")
                    tb = state.get("traceback", "")
                    self._add_system(
                        f"--- Last Error ({self.current_project}) ---\n"
                        f"Step: {step}\nError: {error}\n\n{tb[:1500]}"
                    )
                    return
                except Exception:
                    pass
            status = project.get("status", "")
            self._add_system(f"No error details found. Status: {status}")
        except Exception as e:
            self._add_error(f"Failed to fetch project: {e}")

    @work(exclusive=True)
    async def _handle_pause_cmd(self):
        """Handle /pause — pause the current project."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        try:
            resp = await self.app.http.patch(
                f"/api/projects/{self.current_project}",
                params={"status": "paused"}, timeout=5.0,
            )
            resp.raise_for_status()
            self._add_system(f"Project '{self.current_project}' paused.")
            self.app.query_one("#dashboard-zone")._fetch_projects()
        except Exception as e:
            self._add_error(f"Failed to pause: {e}")

    @work(exclusive=True)
    async def _handle_resume_cmd(self):
        """Handle /resume — resume a paused project."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        try:
            resp = await self.app.http.patch(
                f"/api/projects/{self.current_project}",
                params={"status": "executing"}, timeout=5.0,
            )
            resp.raise_for_status()
            self._add_system(f"Project '{self.current_project}' resumed.")
            self.app.query_one("#dashboard-zone")._fetch_projects()
        except Exception as e:
            self._add_error(f"Failed to resume: {e}")

    @work(exclusive=True)
    async def _handle_refresh_cmd(self):
        """Handle /refresh — re-run planning steps."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        try:
            resp = await self.app.http.post(
                f"/api/projects/{self.current_project}/refresh-planning",
                timeout=5.0,
            )
            resp.raise_for_status()
            self._add_system(f"Planning refresh triggered for '{self.current_project}'.")
            self.app.query_one("#dashboard-zone")._fetch_projects()
        except Exception as e:
            self._add_error(f"Failed to refresh planning: {e}")

    @work(exclusive=True)
    async def _handle_retry_cmd(self, arg: str):
        """Handle /retry [task_id] — retry a failed task or project."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        try:
            if arg:
                task_id = int(arg)
                resp = await self.app.http.post(
                    f"/api/tasks/{task_id}/retry", timeout=5.0,
                )
                resp.raise_for_status()
                self._add_system(f"Task #{task_id} retry triggered.")
            else:
                resp = await self.app.http.post(
                    f"/api/projects/{self.current_project}/retry", timeout=5.0,
                )
                resp.raise_for_status()
                self._add_system(f"Project '{self.current_project}' retry triggered.")
            self.app.query_one("#dashboard-zone")._fetch_projects()
        except Exception as e:
            self._add_error(f"Failed to retry: {e}")

    @work(exclusive=True)
    async def _handle_rollback_cmd(self, arg: str):
        """Handle /rollback <task_id> <commit_hash>."""
        parts = arg.split()
        if len(parts) < 2:
            self._add_error("Usage: /rollback <task_id> <commit_hash>")
            return
        try:
            task_id = int(parts[0])
        except ValueError:
            self._add_error(f"Invalid task ID: {parts[0]}")
            return
        commit_hash = parts[1]
        try:
            resp = await self.app.http.post(
                f"/api/tasks/{task_id}/rollback",
                json={"commit_hash": commit_hash}, timeout=10.0,
            )
            resp.raise_for_status()
            self._add_system(f"Task #{task_id} rolled back to {commit_hash}.")
        except Exception as e:
            self._add_error(f"Rollback failed: {e}")

    @work(exclusive=True)
    async def _handle_cancel_task_cmd(self, arg: str):
        """Handle /cancel-task [task_id]."""
        if not self.current_project:
            self._add_error("No project selected.")
            return
        if not arg:
            # List cancellable tasks
            try:
                resp = await self.app.http.get(
                    f"/api/projects/{self.current_project}/tasks", timeout=5.0
                )
                resp.raise_for_status()
                tasks = resp.json()
                cancellable = [t for t in tasks if t.get("status") in ("pending", "running")]
                if not cancellable:
                    self._add_system("No pending or running tasks to cancel.")
                    return
                lines = ["Cancellable tasks:"]
                for t in cancellable:
                    lines.append(f"  #{t['id']} ({t.get('status')}) — {t.get('prompt', '')[:60]}")
                lines.append("Use /cancel-task <id> to cancel one.")
                self._add_system("\n".join(lines))
                return
            except Exception as e:
                self._add_error(f"Failed to list tasks: {e}")
                return
        try:
            task_id = int(arg)
            resp = await self.app.http.patch(
                f"/api/tasks/{task_id}", params={"status": "failed"}, timeout=5.0,
            )
            resp.raise_for_status()
            self._add_system(f"Task #{task_id} cancelled.")
        except Exception as e:
            self._add_error(f"Failed to cancel task: {e}")

    @work(exclusive=True)
    async def _handle_url_cmd(self, arg: str):
        """Handle /url [new_url]."""
        if arg:
            # Update server URL
            from cli import server as _srv
            self.app.server_url = arg.rstrip("/")
            # Recreate http client with new base URL
            import httpx
            from cli.client import _auth_headers
            self.app.http = httpx.AsyncClient(
                base_url=self.app.server_url, timeout=30.0,
                headers=_auth_headers())
            self.server_url = self.app.server_url
            self._add_system(f"Server URL set to: {self.app.server_url}")
        else:
            self._add_system(f"Server URL: {self.server_url}")

    @work(exclusive=True)
    async def _handle_frequency_cmd(self, arg: str):
        """Handle /frequency [slow|medium|high|Xs|Xm]."""
        freq_map = {"slow": 30, "medium": 10, "high": 5}
        try:
            if not arg:
                resp = await self.app.http.get("/api/settings/scheduler", timeout=5.0)
                resp.raise_for_status()
                s = resp.json()
                self._add_system(
                    f"Scheduler: {s.get('scheduler_type', '?')} — "
                    f"interval={s.get('scheduler_interval', '?')}s, "
                    f"cron={s.get('scheduler_cron', '-')}"
                )
                return
            if arg in freq_map:
                interval = freq_map[arg]
                body = {"scheduler_type": "interval", "scheduler_interval": interval}
            elif arg.endswith("s") and arg[:-1].isdigit():
                interval = int(arg[:-1])
                body = {"scheduler_type": "interval", "scheduler_interval": interval}
            elif arg.endswith("m") and arg[:-1].isdigit():
                interval = int(arg[:-1]) * 60
                body = {"scheduler_type": "interval", "scheduler_interval": interval}
            else:
                self._add_error("Usage: /frequency slow|medium|high|Xs|Xm")
                return
            resp = await self.app.http.post(
                "/api/settings/scheduler", json=body, timeout=5.0
            )
            resp.raise_for_status()
            self._add_system(f"Scheduler frequency set to {body['scheduler_interval']}s.")
        except Exception as e:
            self._add_error(f"Failed to update frequency: {e}")

    @work(exclusive=True)
    async def _handle_cron_cmd(self, arg: str):
        """Handle /cron [5-field expression]."""
        try:
            if not arg:
                resp = await self.app.http.get("/api/settings/scheduler", timeout=5.0)
                resp.raise_for_status()
                s = resp.json()
                self._add_system(
                    f"Scheduler: {s.get('scheduler_type', '?')} — "
                    f"cron={s.get('scheduler_cron', '-')}"
                )
                return
            body = {"scheduler_type": "cron", "scheduler_cron": arg}
            resp = await self.app.http.post(
                "/api/settings/scheduler", json=body, timeout=5.0
            )
            resp.raise_for_status()
            self._add_system(f"Cron schedule set to: {arg}")
        except Exception as e:
            self._add_error(f"Failed to set cron: {e}")

    @work(exclusive=True)
    async def _handle_restart_cmd(self):
        """Handle /restart — restart backend server."""
        try:
            from cli.server import restart_server
            restart_server()
            self._add_system("Backend server restart triggered.")
        except Exception as e:
            self._add_error(f"Failed to restart server: {e}")

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

        try:
            async with self.app.http.stream(
                "POST",
                "/api/agent/chat",
                json={
                    "message": message,
                    "history": self.history,
                    "current_project": self.current_project,
                    "session_id": self.session_id,
                    "mode": self._agent_mode,
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

                        if etype == "session":
                            # Server-minted session id (our request carried
                            # none) — adopt it so history persists and the
                            # butler can resume this conversation's runs.
                            sid = event.get("session_id")
                            if sid:
                                self.session_id = sid
                            continue

                        if etype == "text_delta":
                            if agent_widget is None:
                                agent_widget = self._add_message("agent", "")
                            full_agent_text += event.get("content", "")
                            agent_widget.update(full_agent_text)
                            self._scroll_to_bottom()

                        elif etype == "tool_call":
                            name = event.get("name", "?")
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

                            # Intercept pending_confirm: show project review modal
                            if result.get("status") == "pending_confirm":
                                r_pid = result.get("project_id", "")
                                content = result.get("brief_markdown") or ""
                                brief_data = result.get("brief")
                                approved = await self._show_brief_review_modal(
                                    "project", r_pid, content, brief_data
                                )
                                if approved:
                                    self._add_system(
                                        "Project submitted — pipeline starting."
                                    )
                                else:
                                    # Append rejection feedback to history so meta-agent can retry
                                    self.history.append({
                                        "role": "user",
                                        "content": (
                                            "The project was not approved. "
                                            "Please ask what changes the user wants and revise."
                                        ),
                                    })
                                    self._add_system(
                                        "Project review cancelled. Describe what to change."
                                    )
                                # Refresh dashboard on either outcome
                                self.app.query_one("#dashboard-zone")._fetch_projects()

                        elif etype == "done":
                            msg = event.get("message", {})
                            content = msg.get("content", "")
                            if agent_widget is None:
                                agent_widget = self._add_message("agent", content)
                            else:
                                agent_widget.update(content)
                            self.history.append({"role": "assistant", "content": content})
                            self._scroll_to_bottom()
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
