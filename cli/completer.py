# cli/completer.py
# Context-aware auto-completion for the AItelier REPL.
# Different completion lists for dashboard, project, meta chat, and checkpoint contexts.

from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import FormattedText, HTML
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style


# ── Flash state (shared with app.py) ─────────────────────────────────

# Module-level state updated by the SSE consumer thread.
# Format: {"project": "name", "project_id": "id", "step": "t_impl",
#          "step_name": "Implementer", "task_id": 42} or None
flash_state: dict | None = None


def _get_bottom_toolbar():
    """Dynamic bottom toolbar showing current pipeline status."""
    if flash_state:
        parts = []
        project = flash_state.get("project") or flash_state.get("project_id", "?")
        parts.append(f"  <style fg=\"#00dddd\">{project}</style>")
        step = flash_state.get("step")
        step_name = flash_state.get("step_name")
        if step:
            label = step_name or step
            parts.append(f"<style fg=\"#ffffff\"> | </style><style fg=\"#ffaa00\">{label}</style>")
        task_id = flash_state.get("task_id")
        if task_id:
            parts.append(f"<style fg=\"#ffffff\"> | </style><style fg=\"#aaaaaa\">Task #{task_id}</style>")
        return HTML("".join(parts))
    return HTML("")

# ── Command registry per context ───────────────────────────────────

DASHBOARD_COMMANDS = [
    ("/help",               "Show available commands"),
    ("/new",                "Create a new project"),
    ("/delete",             "Delete a project"),
    ("/status",             "Show all tasks"),
    ("/restart",            "Restart the backend server"),
    ("/url",                "Set or show server URL"),
    ("/frequency",          "Set scheduler frequency (slow|medium|high|Xs|Xm)"),
    ("/cron",               "Set cron schedule (e.g. */5 * * * *)"),
    ("/resume",             "Resume unfinished assessment"),
    ("/resume-assessment",  "Resume unfinished assessment (explicit)"),
    ("/cancel",             "Cancel pending assessment"),
    ("/retry",              "Retry a failed project"),
    ("/logs",               "View pipeline execution logs"),
    ("/errors",             "View last pipeline error"),
    ("/quit",               "Exit the REPL"),
    ("/exit",               "Exit the REPL (alias)"),
    ("/q",                  "Exit the REPL (alias)"),
]

PROJECT_COMMANDS = [
    ("/help",        "Show available commands"),
    ("/projects",    "Return to project dashboard"),
    ("/tasks",       "Refresh task dashboard"),
    ("/edit",        "Edit project (name, brief, priority, status)"),
    ("/add-task",    "Add a new task via meta conversation"),
    ("/resume-task", "Resume interrupted task meta conversation"),
    ("/output",      "View step output files (/output <task_id> [step_id])"),
    ("/refresh",     "Re-run Researcher + Architect planning steps"),
    ("/retry",       "Retry a failed task"),
    ("/cancel-task", "Cancel a running or pending task"),
    ("/logs",        "View pipeline execution logs for a task"),
    ("/errors",      "View last pipeline error for this project"),
    ("/status",      "Show project tasks"),
    ("/project",     "Set or show project ID"),
    ("/pause",       "Pause the current project"),
    ("/resume",      "Resume the paused project"),
    ("/delete",      "Delete a project"),
    ("/new",         "Create a new project"),
    ("/url",         "Set or show server URL"),
    ("/frequency",   "Set scheduler frequency (slow|medium|high|Xs|Xm)"),
    ("/cron",        "Set cron schedule (e.g. */5 * * * *)"),
    ("/restart",     "Restart the backend server"),
    ("/quit",        "Exit the REPL"),
    ("/exit",        "Exit the REPL (alias)"),
    ("/q",           "Exit the REPL (alias)"),
]

META_CHAT_COMMANDS = [
    ("/help",   "Show available commands"),
    ("/skip",   "Skip meta conversation, use raw prompt"),
    ("/cancel", "Cancel current conversation"),
    ("/status", "Show all tasks"),
    ("/project", "Set or show project ID"),
    ("/url",    "Set or show server URL"),
    ("/quit",   "Exit the REPL"),
    ("/exit",   "Exit the REPL (alias)"),
    ("/q",      "Exit the REPL (alias)"),
]

CHECKPOINT_COMMANDS = [
    ("approve", "Accept this step"),
    ("reject",  "Request changes (reason required)"),
]

CLARIFY_COMMANDS = [
    ("/help",   "Show available commands"),
    ("/cancel", "Cancel assessment"),
    ("/quit",   "Exit the REPL"),
    ("/exit",   "Exit the REPL (alias)"),
    ("/q",      "Exit the REPL (alias)"),
]


class ContextAwareCompleter(Completer):
    """Context-aware completer that switches command sets based on REPL mode."""

    def __init__(self):
        self._context = "dashboard"

    def set_context(self, context: str):
        """Set the current completion context.
        Options: 'dashboard', 'project', 'meta_chat', 'checkpoint', 'clarify'
        """
        self._context = context

    @property
    def context(self) -> str:
        return self._context

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor

        # Checkpoint context: complete action words (no slash prefix)
        if self._context == "checkpoint":
            for cmd, desc in CHECKPOINT_COMMANDS:
                if cmd.startswith(text.lower()):
                    yield Completion(
                        cmd[len(text):],
                        start_position=0,
                        display_meta=desc,
                    )
            return

        # All other contexts: only trigger on slash
        if not text.startswith("/"):
            return

        commands = {
            "dashboard": DASHBOARD_COMMANDS,
            "project": PROJECT_COMMANDS,
            "meta_chat": META_CHAT_COMMANDS,
            "clarify": CLARIFY_COMMANDS,
        }.get(self._context, DASHBOARD_COMMANDS)

        for cmd, desc in commands:
            if cmd.startswith(text):
                yield Completion(
                    cmd[len(text):],
                    start_position=0,
                    display_meta=desc,
                )


# ── Shared session ────────────────────────────────────────────────

_repl_history = InMemoryHistory()

_PT_STYLE = Style.from_dict({
    "prompt":               "#00dddd bold",
    "completion-menu":      "bg:#333333 #ffffff",
    "completion-menu.completion.current": "bg:#00aaaa #000000 bold",
})

_completer = ContextAwareCompleter()

_session = PromptSession(
    history=_repl_history,
    style=_PT_STYLE,
    completer=_completer,
    complete_while_typing=True,
    enable_history_search=True,
    bottom_toolbar=_get_bottom_toolbar,
    refresh_interval=1.0,
)

_no_complete_session = PromptSession(
    history=_repl_history,
    style=_PT_STYLE,
    bottom_toolbar=_get_bottom_toolbar,
    refresh_interval=1.0,
)


# ── Public API ────────────────────────────────────────────────────

def set_completion_context(context: str):
    """Set the completion context for the shared session.
    Options: 'dashboard', 'project', 'meta_chat', 'checkpoint', 'clarify'
    """
    _completer.set_context(context)


def get_completion_context() -> str:
    """Get the current completion context."""
    return _completer.context


def repl_input(prompt_text: str = "> ", allow_commands: bool = True) -> str:
    """
    Read a line of input with context-aware auto-completion.

    Args:
        prompt_text: The prompt string shown to the user (e.g. "> ").
        allow_commands: If True, slash commands trigger the completion menu.

    Returns:
        The user's input string (stripped).
    """
    ft_prompt = FormattedText([
        ("class:prompt", prompt_text),
    ])

    s = _session if allow_commands else _no_complete_session
    result = s.prompt(ft_prompt)
    return result.strip()
