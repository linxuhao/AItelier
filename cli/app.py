# cli/app.py
# AItelier CLI — Claude Code style interface.
#
# Usage:
#   aitelier                      → project dashboard
#   aitelier "hello"              → auto-create project, run pipeline
#   aitelier run "hello"          → same, with explicit options
#   aitelier status               → list tasks
#   aitelier status 42            → show task #42 detail
#   aitelier server               → start backend
#   aitelier rollback 42 abc123   → rollback task

import json
import os
import re
import sys
from typing import Optional

# Human-readable step status messages
# Helper to render interaction hints from API responses
def _show_interaction_hint(interaction: dict | None):
    """Display contextual interaction guidance from the API response."""
    if not interaction:
        return
    hint = interaction.get("hint", "")
    actions = interaction.get("available_actions", [])
    turn = interaction.get("turn")
    max_turns = interaction.get("max_turns")

    parts = []
    if hint:
        parts.append(f"[dim]{hint}[/dim]")
    if turn is not None and max_turns:
        parts.append(f"[dim]  (turn {turn}/{max_turns})[/dim]")
    if actions:
        # Format slash commands separately from regular actions
        regular = [a for a in actions if not a.startswith("/")]
        slash = [a for a in actions if a.startswith("/")]
        action_parts = [f"[cyan]{a}[/cyan]" for a in regular]
        if slash:
            action_parts.extend([f"[dim]{a}[/dim]" for a in slash])
        parts.append(f"[dim]  Available: {' | '.join(action_parts)}[/dim]")
    if parts:
        console.print("\n".join(parts))


_STEP_STATUS_MESSAGES = {
    "t_plan":   "Planning task...",
    "t_impl":   "Implementing...",
    "t_verify":  "Verifying...",
}

# Human-readable step names for flash toolbar
_STEP_NAMES = {
    "1": "Researcher", "2": "Architect", "3": "PM",
    "5": "Verifier",
    "t_plan": "Planner", "t_impl": "Implementer", "t_verify": "Verifier",
}


# ── SSE Flash State ──────────────────────────────────────────────────

def _start_sse_flash(server_url: str):
    """Start background SSE consumer that updates the flash toolbar via completer.flash_state."""
    import threading
    import httpx
    import cli.completer as _comp

    # Cache project names to avoid repeated API calls
    _project_name_cache: dict[str, str] = {}

    def _consumer():
        import asyncio
        async def _consume():
            async with httpx.AsyncClient(base_url=server_url.rstrip("/"), timeout=None) as client:
                async with client.stream("GET", "/api/events/stream") as resp:
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        import json as _json
                        try:
                            raw = _json.loads(line[6:])
                            log_str = raw.get("log", "")
                            if log_str == "__END__":
                                break
                            event = _json.loads(log_str) if isinstance(log_str, str) and log_str.startswith("{") else {"type": "raw_log"}
                        except _json.JSONDecodeError:
                            continue

                        etype = event.get("type", "")

                        if etype == "step_start":
                            project_id = event.get("project_id", "?")
                            step = event.get("step_id", "?")
                            task_id = event.get("task_id")
                            # Resolve project name (cache first, API fallback)
                            project_name = _project_name_cache.get(project_id)
                            if not project_name:
                                try:
                                    from cli.client import APIClient
                                    pc = APIClient(server_url.rstrip("/"))
                                    p = pc.get_project(project_id)
                                    project_name = p.get("name", project_id)
                                    _project_name_cache[project_id] = project_name
                                except Exception:
                                    project_name = project_id
                            _comp.flash_state = {
                                "project_id": project_id,
                                "project": project_name,
                                "step": step,
                                "step_name": _STEP_NAMES.get(step, step),
                                "task_id": task_id if task_id and task_id != 0 else None,
                            }
                        elif etype in ("step_end", "step_done", "pipeline_end", "pipeline_error",
                                       "project_completed", "project_failed", "project_step_done"):
                            if etype in ("pipeline_end", "pipeline_error", "project_completed", "project_failed"):
                                _comp.flash_state = None
                            elif etype == "project_step_done":
                                next_step = event.get("next_step")
                                if next_step:
                                    pid = event.get("project_id", "?")
                                    _comp.flash_state = {
                                        "project_id": pid,
                                        "project": _comp.flash_state.get("project", pid) if _comp.flash_state else pid,
                                        "step": next_step,
                                        "step_name": _STEP_NAMES.get(next_step, next_step),
                                    }
                                else:
                                    _comp.flash_state = None

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_consume())
        except Exception:
            pass  # server shutdown, network error, etc.
        finally:
            loop.close()
            _comp.flash_state = None

    t = threading.Thread(target=_consumer, daemon=True)
    t.start()


def _detect_run_command(code_dir: str) -> str:
    """Detect the correct run command for the generated project."""
    from pathlib import Path
    project = Path(code_dir)

    # Check for main.py at root
    if (project / "main.py").exists():
        return "python3 main.py"

    # Check for src/main.py — needs module run
    if (project / "src" / "main.py").exists():
        return "python3 -m src.main"

    # Check for app.py at root
    if (project / "app.py").exists():
        return "python3 app.py"

    # Check for src/app.py
    if (project / "src" / "app.py").exists():
        return "python3 -m src.app"

    # Fallback: find any .py with if __name__ == "__main__"
    for py in project.rglob("*.py"):
        try:
            if '__name__' in py.read_text() and '__main__' in py.read_text():
                rel = py.relative_to(project)
                if len(rel.parts) > 1:
                    module = ".".join(rel.with_suffix("").parts)
                    return f"python3 -m {module}"
                return f"python3 {rel}"
        except Exception:
            continue

    return "python3 main.py"

_DEFAULT_PORT = int(os.environ.get("AITELIER_PORT", "4444"))
_DEFAULT_URL = f"http://localhost:{_DEFAULT_PORT}"

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

app = typer.Typer(
    name="aitelier",
    help="AItelier — Deterministic AI Pipeline Engine",
    no_args_is_help=True,
)

# Shared state for interactive session
_state = {
    "server_url": _DEFAULT_URL,
    "project_id": "default",
    "page": "dashboard",  # "dashboard" or "in_project"
}


# ── Checkpoint monitoring ──────────────────────────────────────────


def _monitor_pipeline(project_id: str, client):
    """
    Monitor pipeline execution after submit.
    Polls project status, shows step progress, and delegates to
    _monitor_checkpoints when a checkpoint is reached.
    Returns when project reaches a terminal state (completed/failed).
    """
    import time

    # Get project name for better UX
    project_name = project_id
    try:
        proj = client.get_project(project_id)
        project_name = proj.get("name", project_id)
    except Exception:
        pass

    step_names = {"1": "Researcher", "2": "Architect", "3": "PM",
                  "t_plan": "Task Planner", "t_impl": "Implementer",
                  "t_verify": "Verifier", "5": "Verifier"}

    while True:
        try:
            project = client.get_project(project_id)
        except Exception:
            time.sleep(3)
            continue
        except KeyboardInterrupt:
            console.print("\n[yellow]Pipeline monitoring stopped. Pipeline continues on server.[/yellow]")
            console.print(f"[dim]  Restart the CLI to resume monitoring.[/dim]")
            return

        status = project.get("status", "")

        if status == "completed":
            code_dir = os.path.expanduser(f"~/.AItelier/projects/{project_id}")
            run_cmd = _detect_run_command(code_dir)
            entry_file = run_cmd.split()[-1] if run_cmd else ""
            if run_cmd and run_cmd.startswith("python3 -m"):
                entry_file = run_cmd.replace("python3 -m ", "").replace(".", "/") + "/__main__.py"
            entry_exists = os.path.exists(os.path.join(code_dir, entry_file)) if entry_file else False

            console.print(f"\n[bold green]Project completed![/bold green]")
            if entry_exists:
                console.print(f"[dim]  Code: {code_dir}[/dim]")
                console.print(f"[dim]  Run:  cd {code_dir} && {run_cmd}[/dim]")
                from rich.prompt import Confirm
                if Confirm.ask("[cyan]Run it now?[/cyan]", default=True):
                    import subprocess
                    console.print(f"[dim]$ cd {code_dir} && {run_cmd}[/dim]\n")
                    try:
                        subprocess.run(f"cd {code_dir} && {run_cmd}", shell=True, timeout=30)
                    except subprocess.TimeoutExpired:
                        console.print("\n[dim](process running — press Ctrl+C to stop)[/dim]")
                    except KeyboardInterrupt:
                        pass
            else:
                console.print(f"[dim]  Code: {code_dir}[/dim]")
            return

        if status == "failed":
            current_step = project.get("current_project_step", "")
            console.print(f"\n[bold red]Project failed.[/bold red]")
            if current_step:
                console.print(f"[dim]  Failed at step: {current_step}[/dim]")
            # Try to show error from meta_state
            meta_state = project.get("meta_state")
            if meta_state:
                try:
                    import json as _json
                    state = _json.loads(meta_state)
                    error = state.get("error", "")
                    if error:
                        console.print(f"[dim]  Error: {error[:500]}[/dim]")
                except Exception:
                    pass
            console.print(f"[dim]  Use [cyan]/errors[/cyan] for details, [cyan]/retry[/cyan] to retry.[/dim]")
            console.print(f"[dim]  Workspace: ~/.AItelier/workspaces/{project_id}/[/dim]")
            return

        if status == "waiting_user_approval":
            # Delegate to checkpoint handler
            _monitor_checkpoints(project_id, client)
            continue

        # Show progress for active steps with project name context
        current_step = project.get("current_project_step", "")
        try:
            if status == "planning" and current_step:
                step_name = step_names.get(current_step, current_step)
                status_msg = _STEP_STATUS_MESSAGES.get(current_step, f"Running {step_name}...")
                with console.status(f"[bold cyan]{project_name}[/bold cyan] — {status_msg}"):
                    time.sleep(3)
            elif status == "executing":
                with console.status(f"[bold cyan]{project_name}[/bold cyan] — Executing tasks..."):
                    time.sleep(3)
            elif status == "verifying":
                with console.status(f"[bold cyan]{project_name}[/bold cyan] — Final verification..."):
                    time.sleep(3)
            else:
                time.sleep(3)
        except KeyboardInterrupt:
            console.print("\n[yellow]Pipeline monitoring stopped. Pipeline continues on server.[/yellow]")
            console.print(f"[dim]  Restart the CLI to resume monitoring.[/dim]")
            return


def _monitor_checkpoints(project_id: str, client):
    """
    Poll for checkpoint events during pipeline execution.
    Display step output and capture user responses.
    Returns when project reaches fully_automated or completed status.
    """
    import time
    from cli.completer import repl_input, set_completion_context

    set_completion_context("checkpoint")

    while True:
        try:
            checkpoint = client.get_pending_checkpoint(project_id)
        except Exception:
            checkpoint = None

        if checkpoint is None:
            # Check if project has moved past checkpoints or completed
            try:
                project = client.get_project(project_id)
            except Exception:
                break
            status = project.get("status", "")
            if status in ("executing", "verifying", "completed", "failed"):
                return
            # Still planning or between checkpoints — poll again
            time.sleep(2)
            continue

        # Display checkpoint info with step output
        label = checkpoint.get("label", "Checkpoint")
        timeout_at = checkpoint.get("timeout_at")
        rejection_count = checkpoint.get("rejection_count", 0)

        # Display step output content
        step_output = checkpoint.get("step_output")
        if step_output:
            # Support both new format (dict with 'files' key) and old format (dict of filename->content)
            if isinstance(step_output, dict) and "files" in step_output:
                files = step_output.get("files", {})
                rejection_history = step_output.get("rejection_history")
            else:
                files = step_output
                rejection_history = None

            # Show rejection summary if available
            if rejection_history:
                console.print(f"[dim]This step was revised {len(rejection_history)} time(s) based on prior feedback.[/dim]")
                if rejection_history:
                    latest = rejection_history[-1]
                    reason = latest.get("reason", "N/A")
                    console.print(f"[dim]Last feedback: {reason[:150]}[/dim]\n")

            # Show each file's content
            if files:
                for filename, content in files.items():
                    console.print(Panel(
                        content,
                        title=f"{label} — {filename}",
                        border_style="yellow",
                    ))
            elif not rejection_history:
                remaining = f" (auto-approve in {int(max(0, timeout_at - time.time()))}s)" if timeout_at else ""
                console.print(f"\n[bold yellow]═══ {label}{remaining} ═══[/bold yellow]")
        else:
            remaining = f" (auto-approve in {int(max(0, timeout_at - time.time()))}s)" if timeout_at else ""
            console.print(f"\n[bold yellow]═══ {label}{remaining} ═══[/bold yellow]")

        if rejection_count > 0:
            console.print(f"[dim]This step has been revised {rejection_count} time(s).[/dim]")

        # Show interaction guidance from API
        _show_interaction_hint(checkpoint.get("interaction"))
        console.print("[dim]  [cyan]approve[/cyan] (or 'yes', 'y')  — accept this step[/dim]")
        console.print("[dim]  [cyan]reject[/cyan] <reason>         — request changes (reason required)[/dim]")

        # Wait for user action
        while True:
            try:
                answer = repl_input(f"{project_id}> approve/reject: ")
            except (EOFError, KeyboardInterrupt):
                console.print("[dim]Checkpoint auto-approved on exit.[/dim]")
                try:
                    client.approve_checkpoint(project_id, checkpoint["checkpoint"])
                except Exception:
                    pass
                return

            if not answer or answer.lower() in ("y", "yes", "a", "approve", "/approve"):
                try:
                    client.approve_checkpoint(project_id, checkpoint["checkpoint"])
                    console.print("[green]Checkpoint approved. Pipeline continuing...[/green]\n")
                except Exception as e:
                    console.print(f"[red]Failed to approve: {e}[/red]")
                break

            elif answer.lower().startswith("reject") or answer.lower() in ("no", "n"):
                # Require a reason
                feedback = ""
                if answer.lower().startswith("reject"):
                    feedback = answer[6:].strip()  # after "reject"

                while not feedback.strip():
                    try:
                        feedback = repl_input(f"{project_id}> reason for rejection: ")
                    except (EOFError, KeyboardInterrupt):
                        console.print("[dim]Checkpoint auto-approved on exit.[/dim]")
                        try:
                            client.approve_checkpoint(project_id, checkpoint["checkpoint"])
                        except Exception:
                            pass
                        return
                    if not feedback.strip():
                        console.print("[dim]Rejection reason is required. Please explain what needs to change.[/dim]")

                try:
                    client.reject_checkpoint(project_id, checkpoint["checkpoint"], feedback.strip())
                    console.print(f"[yellow]Checkpoint rejected with feedback. Pipeline will re-run step...[/yellow]\n")
                except Exception as e:
                    console.print(f"[red]Failed to reject: {e}[/red]")
                break

            elif answer.startswith("/"):
                cmd = answer.lower().split()[0]
                if cmd in ("/quit", "/exit", "/q"):
                    console.print("[dim]Bye. Checkpoint auto-approved.[/dim]")
                    try:
                        client.approve_checkpoint(project_id, checkpoint["checkpoint"])
                    except Exception:
                        pass
                    raise SystemExit(0)
                elif cmd == "/help":
                    _print_repl_help()
                else:
                    console.print("[dim]At checkpoint. Use 'approve' or 'reject <reason>'.[/dim]")
            else:
                console.print("[dim]Type 'approve' to continue or 'reject' (you'll be asked for a reason).[/dim]")


# ── Project dashboard ───────────────────────────────────────────


def _status_style(status: str) -> str:
    """Return Rich style string for a project/task status."""
    if status == "completed":
        return "bold green"
    elif status in ("running", "advancing"):
        return "bold yellow"
    elif status == "failed":
        return "bold red"
    return "dim"


def _project_dashboard():
    """Render the project dashboard and let the user select a project."""
    from cli.client import APIClient
    from cli.completer import repl_input, set_completion_context
    import httpx

    set_completion_context("dashboard")
    client = APIClient(_state["server_url"])
    _flash_msg = None  # one-shot message shown after re-render
    _should_clear = True

    while True:
        if _should_clear:
            console.clear()
        _should_clear = True  # reset default
        console.print(Panel(
            "[bold cyan]AItelier[/bold cyan] — Project Dashboard",
            border_style="cyan",
        ))

        # Show one-shot flash message (e.g. after /cancel)
        if _flash_msg:
            console.print(_flash_msg)
            _flash_msg = None

        try:
            projects = client.list_projects()
        except httpx.ConnectError:
            console.print("[red]Cannot connect to server.[/red]")
            console.print("[dim]Start with: aitelier server[/dim]")
            return
        except httpx.HTTPStatusError as e:
            console.print(f"[yellow]Backend error ({e.response.status_code}), restarting...[/yellow]")
            _handle_restart_cmd()
            continue

        table = Table(show_header=True, header_style="bold")
        table.add_column("#", width=4, style="dim")
        table.add_column("Project", width=24)
        table.add_column("Status", width=14)
        table.add_column("Tasks", width=24)
        table.add_column("Last Update", width=20)

        # Row 0: [new]
        table.add_row("0", "[bold green][new][/bold green]", "", "", "")

        # Rows 1..N: existing projects
        for i, p in enumerate(projects):
            proj_status = p.get("status", "planning")
            step = p.get("current_project_step") or "-"
            task_count = p.get("task_count", 0)
            completed = p.get("completed_count", 0) or 0
            running = p.get("running_count", 0) or 0
            failed = p.get("failed_count", 0) or 0
            pending = p.get("pending_count", 0) or 0
            last_update = p.get("last_update") or p.get("created_at", "")
            name = p.get("name", p["project_id"])

            # Determine display status
            has_failed = failed > 0
            if proj_status in ("planning", "executing", "verifying"):
                display_status = f"{proj_status} (step {step})"
                style = "bold red" if has_failed else "bold yellow"
            elif proj_status == "completed":
                display_status = "completed"
                style = "bold green" if not has_failed else "bold red"
            elif proj_status == "failed":
                display_status = "failed"
                style = "bold red"
            elif proj_status == "waiting_user_approval":
                display_status = "awaiting approval"
                style = "bold magenta"
            elif proj_status == "paused":
                display_status = "paused"
                style = "dim"
            else:
                display_status = proj_status
                style = "dim"

            # Task breakdown
            if task_count == 0:
                tasks_str = "-"
            else:
                parts = []
                if completed:
                    parts.append(f"[green]{completed} done[/green]")
                if running:
                    parts.append(f"[yellow]{running} run[/yellow]")
                if pending:
                    parts.append(f"[dim]{pending} pending[/dim]")
                if failed:
                    parts.append(f"[red]{failed} fail[/red]")
                tasks_str = " ".join(parts) if parts else str(task_count)

            table.add_row(
                str(i + 1),
                f"[cyan]{name}[/cyan]",
                f"[{style}]{display_status}[/{style}]",
                tasks_str,
                str(last_update)[:19] if last_update else "",
            )

        console.print(table)
        console.print(
            "[dim]Type a prompt to auto-create a project and run the pipeline.[/dim]\n"
            "[dim]  [cyan]0[/cyan] or [cyan]/new[/cyan]   — create a new project manually[/dim]\n"
            "[dim]  [cyan]<number>[/cyan]   — select an existing project[/dim]\n"
            "[dim]  [cyan]/help[/cyan]     — show all commands  |  [cyan]/[/cyan] + tab for autocomplete[/dim]"
        )

        # Check for pending assessment — show as non-blocking hint
        from cli.meta_store import load_assessment
        pending_assessment = load_assessment()
        if pending_assessment and pending_assessment.get("status") == "asking":
            preview = pending_assessment.get("prompt", "")[:50]
            age_hint = ""
            saved_at = pending_assessment.get("saved_at")
            if saved_at:
                import time as _t
                age_min = int((_t.time() - saved_at) / 60)
                if age_min < 60:
                    age_hint = f" ({age_min}m ago)"
                else:
                    age_hint = f" ({age_min // 60}h {age_min % 60}m ago)"
            console.print(
                f"[yellow]Unfinished assessment: '{preview}...'{age_hint}[/yellow] "
                f"[dim]Type [cyan]/resume[/cyan] to continue or [cyan]/cancel[/cyan] to clear.[/dim]"
            )

        try:
            user_input = repl_input("dashboard> ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye.[/dim]")
            raise SystemExit(0)

        if not user_input:
            continue

        # Slash commands on dashboard page
        if user_input.startswith("/"):
            cmd = user_input.lower().split()[0]
            rest = user_input[len(cmd):].strip()

            if cmd in ("/quit", "/exit", "/q"):
                console.print("[dim]Bye.[/dim]")
                raise SystemExit(0)
            elif cmd == "/help":
                _print_repl_help()
                _should_clear = False
                continue
            elif cmd == "/url":
                if rest:
                    _state["server_url"] = rest
                    console.print(f"[green]Server URL set to: {rest}[/green]")
                else:
                    console.print(f"Server URL: [cyan]{_state['server_url']}[/cyan]")
                _should_clear = False
                continue
            elif cmd == "/status":
                _repl_status()
                _should_clear = False
                continue
            elif cmd == "/frequency":
                _handle_frequency_cmd(rest)
                _should_clear = False
                continue
            elif cmd == "/cron":
                _handle_cron_cmd(rest)
                _should_clear = False
                continue
            elif cmd == "/restart":
                _handle_restart_cmd()
                continue
            elif cmd == "/new":
                _create_new_project(client)
                continue
            elif cmd == "/delete":
                _handle_delete_cmd(rest, client)
                continue
            elif cmd in ("/resume", "/resume-assessment"):
                from cli.meta_store import load_assessment as _load_a
                pa = _load_a()
                if pa and pa.get("status") == "asking":
                    _auto_create_and_run(pa.get("prompt", ""), client)
                    return
                else:
                    console.print("[dim]No pending assessment to resume.[/dim]")
                _should_clear = False
                continue
            elif cmd in ("/cancel", "/discard"):
                from cli.meta_store import clear_assessment
                clear_assessment()
                _flash_msg = "[dim]Assessment cleared.[/dim]"
                continue
            elif cmd == "/retry":
                _handle_retry_project_cmd(rest, client, projects)
                continue
            elif cmd == "/logs":
                _handle_logs_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/errors":
                _handle_errors_cmd(_get_client())
                _should_clear = False
                continue
            elif cmd == "/runs":
                _handle_runs_cmd(_get_client())
                _should_clear = False
                continue
            elif cmd == "/trace":
                _handle_trace_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/tree":
                _handle_tree_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/cat":
                _handle_cat_cmd(rest, _get_client())
                _should_clear = False
                continue
            else:
                console.print(f"[red]Unknown command: {cmd}[/red]  [dim]Type /help for commands[/dim]")
                _should_clear = False
                continue

        # Numeric selection
        if user_input.isdigit():
            num = int(user_input)
            if num == 0:
                _create_new_project(client)
                continue
            elif 1 <= num <= len(projects):
                selected = projects[num - 1]
                _state["project_id"] = selected["project_id"]
                _state["page"] = "in_project"
                console.print(
                    f"\n[green]Entered project: "
                    f"[cyan]{selected.get('name', selected['project_id'])}[/cyan][/green]\n"
                )
                return
            else:
                console.print(f"[red]Invalid selection: {num}[/red]")
                continue

        # Non-numeric input → auto-create project from prompt
        # A1: If a stale assessment exists, ask user what to do before consuming input
        from cli.meta_store import load_assessment as _load_a2
        pending = _load_a2()
        if pending and pending.get("status") == "asking":
            preview = pending.get("prompt", "")[:50]
            console.print(
                f"[yellow]You have an unfinished assessment: '{preview}...'{ '[/yellow]' }"
            )
            console.print(
                f"[dim]  [cyan]N[/cyan] — start a new project with your prompt (recommended)\n"
                f"  [cyan]R[/cyan] — resume the previous assessment\n"
                f"  [cyan]X[/cyan] — cancel everything and start fresh[/dim]"
            )
            from rich.prompt import Prompt
            choice = Prompt.ask("Choose", choices=["n", "r", "x"], default="n")
            if choice == "r":
                _auto_create_and_run(pending.get("prompt", ""), client)
                return
            elif choice == "x":
                from cli.meta_store import clear_assessment
                clear_assessment()
                console.print("[dim]Previous assessment cleared.[/dim]")
            # choice == "n": fall through to new prompt

        _auto_create_and_run(user_input, client)
        return


def _handle_retry_project_cmd(arg: str, client, projects: list):
    """Retry a failed project from dashboard level — uses /projects/{id}/retry API."""
    import httpx

    failed = [(i + 1, p) for i, p in enumerate(projects) if p.get("status") == "failed"]
    if not failed:
        console.print("[dim]No failed projects to retry.[/dim]")
        return

    console.print("[bold]Failed projects:[/bold]")
    for num, p in failed:
        console.print(f"  [cyan]{num}[/cyan] — {p.get('name', p['project_id'])}")

    from rich.prompt import Prompt
    choice = Prompt.ask("Select project to retry", default=str(failed[0][0]))
    if not choice.isdigit():
        return

    selected_idx = int(choice) - 1
    if selected_idx < 0 or selected_idx >= len(projects):
        return

    project = projects[selected_idx]
    project_id = project["project_id"]

    try:
        with console.status("[bold cyan]Retrying project..."):
            result = client.retry_project(project_id)
        console.print(f"[green]Project '{project_id}' retried. Pipeline restarting...[/green]")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Project '{project_id}' not found.[/red]")
        elif e.response.status_code == 400:
            detail = "Only failed projects can be retried"
            try:
                detail = e.response.json().get("detail", detail)
            except Exception:
                pass
            console.print(f"[yellow]{detail}[/yellow]")
        else:
            console.print(f"[red]Retry failed: {e.response.status_code}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_retry_task_cmd(arg: str, client):
    """Retry a failed task from project context — uses /tasks/{id}/retry API."""
    import httpx

    project_id = _state.get("project_id")

    if not arg:
        try:
            tasks = client.list_tasks_by_project(project_id)
            failed_tasks = [t for t in tasks if t.get("status") == "failed"]
        except Exception:
            failed_tasks = []

        if not failed_tasks:
            console.print("[dim]No failed tasks in current project.[/dim]")
            return

        console.print("[bold]Failed tasks:[/bold]")
        for t in failed_tasks:
            console.print(f"  [cyan]#{t['id']}[/cyan] — {t.get('prompt', '')[:60]}")

        from rich.prompt import Prompt
        choice = Prompt.ask("Enter task ID to retry", default=str(failed_tasks[0]["id"]))
        if not choice.isdigit():
            return
        task_id = int(choice)
    else:
        try:
            task_id = int(arg)
        except ValueError:
            console.print(f"[red]Invalid task ID: {arg}[/red]")
            return

    try:
        with console.status("[bold cyan]Retrying task..."):
            result = client.retry_task(task_id)
        console.print(f"[green]Task #{task_id} retried. Pipeline restarting...[/green]")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Task #{task_id} not found.[/red]")
        elif e.response.status_code == 400:
            detail = "Only failed tasks can be retried"
            try:
                detail = e.response.json().get("detail", detail)
            except Exception:
                pass
            console.print(f"[yellow]{detail}[/yellow]")
        else:
            console.print(f"[red]Retry failed: {e.response.status_code}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _create_new_project(client):
    """Interactive project creation."""
    from cli.completer import repl_input
    import httpx

    console.print("\n[bold]Create New Project[/bold]")
    try:
        project_id = repl_input("Project ID (slug): ", allow_commands=False)
        if not project_id:
            return
        name_input = repl_input("Display name (enter to use ID): ", allow_commands=False)
    except (EOFError, KeyboardInterrupt):
        return

    name = name_input if name_input else None

    # Repo type selection
    console.print("[dim]Repository type:[/dim]")
    console.print("  [cyan]1[/cyan] New (default)  — create a fresh git repo")
    console.print("  [cyan]2[/cyan] Existing      — use a local repo on disk")
    console.print("  [cyan]3[/cyan] Clone          — clone from GitHub/GitLab URL")
    try:
        type_input = repl_input("Select [1/2/3]: ", allow_commands=False)
    except (EOFError, KeyboardInterrupt):
        return

    repo_type = "new"
    repo_path = None
    repo_url = None

    if type_input == "2":
        repo_type = "existing"
        try:
            repo_path = repl_input("Local repo path: ", allow_commands=False)
        except (EOFError, KeyboardInterrupt):
            return
        if not repo_path:
            console.print("[red]Path is required for existing repo.[/red]")
            return
    elif type_input == "3":
        repo_type = "clone"
        try:
            repo_url = repl_input("Git URL (GitHub/GitLab): ", allow_commands=False)
        except (EOFError, KeyboardInterrupt):
            return
        if not repo_url:
            console.print("[red]URL is required for clone.[/red]")
            return

    with console.status("[bold cyan]Creating project...[/bold cyan]"):
        try:
            result = client.create_project(
                project_id, name=name,
                repo_type=repo_type, repo_path=repo_path, repo_url=repo_url,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 409:
                console.print(f"[yellow]Project '{project_id}' already exists. Switching to it.[/yellow]")
                _state["project_id"] = project_id
                _state["page"] = "in_project"
            else:
                detail = e.response.json().get("detail", str(e.response.status_code))
                console.print(f"[red]Error: {detail}[/red]")
            return
    console.print(f"[green]Project '{result['name']}' created.[/green]")
    _state["project_id"] = project_id
    _state["page"] = "in_project"


def _prompt_repo_type() -> str:
    """Prompt user to choose repo type when intent is 'existing_code'."""
    from rich.prompt import Prompt
    console.print("[dim]  [1] Local repository path  [2] Clone from URL[/dim]")
    choice = Prompt.ask("Select source", choices=["1", "2"], default="1")
    return "existing" if choice == "1" else "clone"


def _prompt_repo_path() -> str:
    """Prompt user for local repo path."""
    from rich.prompt import Prompt
    return Prompt.ask("Enter local repo path")


def _prompt_repo_url() -> str:
    """Prompt user for remote repo URL."""
    from rich.prompt import Prompt
    return Prompt.ask("Enter repo URL")


def _prompt_existing_code_repo(client) -> tuple[str, str | None, str | None]:
    """
    Prompt user for repo source when intent is 'existing_code'.
    Returns (repo_type, repo_path, repo_url).

    If the user has previous AItelier projects, offer them as choices
    first — the new project will share the same code repo via
    repo_type="existing" pointing to the old project's repo_path.
    """
    from rich.prompt import Prompt

    projects = client.list_projects()
    # AItelier-managed repos are those created with "new" or "clone" —
    # their code lives under ~/.AItelier/projects/<id>/ and can be
    # pointed to by a new project. Exclude projects still running.
    aitelier_projects = [
        p for p in projects
        if p.get("repo_type") in ("new", "clone")
        and p.get("status") not in ("running",)
    ]

    if aitelier_projects:
        console.print("\n[dim]Previous AItelier projects:[/dim]")
        for i, p in enumerate(aitelier_projects, 1):
            status = p.get("status", "?")
            repo_path = p.get("repo_path") or f"~/.AItelier/projects/{p['project_id']}"
            console.print(f"  [{i}] {p['project_id']}  ({status})  repo: {repo_path}")
        console.print(f"  [{len(aitelier_projects)+1}] Other (enter path manually)")
        console.print(f"  [{len(aitelier_projects)+2}] Clone from URL")

        max_choice = len(aitelier_projects) + 2
        pick = Prompt.ask(
            "Select repo source",
            choices=[str(i) for i in range(1, max_choice + 1)],
            default=str(len(aitelier_projects) + 1),
        )
        idx = int(pick)
        if idx <= len(aitelier_projects):
            selected = aitelier_projects[idx - 1]
            repo_path = selected.get("repo_path") or f"~/.AItelier/projects/{selected['project_id']}"
            console.print(f"[green]Using repo: {repo_path}[/green]")
            return ("existing", repo_path, None)
        elif idx == len(aitelier_projects) + 1:
            return ("existing", _prompt_repo_path(), None)
        else:
            return ("clone", None, _prompt_repo_url())
    else:
        repo_type = _prompt_repo_type()
        if repo_type == "existing":
            return ("existing", _prompt_repo_path(), None)
        else:
            return ("clone", None, _prompt_repo_url())


def _auto_create_and_run(prompt: str, client):
    """Assess prompt quality, gather requirements via conversation,
    create the project, and run the pipeline (skipping Nominator)."""
    from cli.meta_store import save_assessment, load_assessment, clear_assessment

    # Clear any stale assessment before starting a new one
    clear_assessment()

    # ── Phase 0: Start fresh assessment ──
    history = []
    result = None

    # Start fresh assessment
    try:
        with console.status("[bold cyan]Assessing your project idea..."):
            result = client.assess_prompt(prompt)
    except Exception as e:
        console.print(f"[yellow]Assessment service unavailable: {e}[/yellow]")
        console.print("[dim]Falling back to direct project creation.[/dim]\n")
        _auto_create_and_run_legacy(prompt, client)
        return

    # Save checkpoint
    if result.get("status") == "asking":
        save_assessment({
            "prompt": prompt,
            "history": [],
            "last_message": result.get("message"),
            "status": "asking",
        })

    # ── Phase 1: Assessment conversation loop ──
    while result is None or result.get("status") == "asking":
        if result is not None:
            message = result.get("message", "Could you tell me more about what you'd like to build?")
            console.print(f"\n{message}")
            _show_interaction_hint(result.get("interaction"))

        try:
            from cli.completer import repl_input, set_completion_context
            set_completion_context("clarify")
            answer = repl_input("clarify> ")
        except (EOFError, KeyboardInterrupt):
            console.print("[dim]Assessment paused. You can continue next time.[/dim]")
            return

        if not answer:
            continue
        if answer.startswith("/"):
            cmd = answer.lower().split()[0]
            if cmd in ("/quit", "/exit", "/q"):
                raise SystemExit(0)
            elif cmd == "/cancel":
                clear_assessment()
                console.print("[dim]Assessment cancelled.[/dim]")
                return
            elif cmd == "/help":
                _print_repl_help()
                continue
            continue

        history.append({
            "message": result.get("message", "") if result else prompt,
            "answer": answer,
        })

        try:
            with console.status("[bold cyan]Thinking..."):
                result = client.assess_prompt(answer, history=history)
        except Exception as e:
            console.print(f"[red]Assessment failed: {e}[/red]")
            console.print("[dim]Your progress has been saved. Try again later.[/dim]")
            save_assessment({
                "prompt": prompt,
                "history": history,
                "last_message": history[-1]["message"] if history else "",
                "status": "asking",
            })
            return

        # Update checkpoint
        if result.get("status") == "asking":
            save_assessment({
                "prompt": prompt,
                "history": history,
                "last_message": result.get("message"),
                "status": "asking",
            })

    # ── Phase 2: Assessment complete → submit project ──
    # NOTE: Don't clear assessment until submit succeeds, so brief can be recovered on failure
    if not result or result.get("status") != "complete":
        clear_assessment()
        console.print("[dim]Assessment ended without a result. Returning to dashboard.[/dim]")
        return

    brief = result.get("project_brief")
    intent = result.get("intent", "new_project")

    # Show compact brief summary
    if brief:
        name = brief.get("project_name", "Untitled")
        desc = brief.get("description", "")
        goals = brief.get("mvp_goals", []) or brief.get("goals", [])
        non_goals = brief.get("non_goals", [])

        summary_lines = [f"[bold]{name}[/bold]"]
        if desc:
            summary_lines.append(f"[dim]{desc[:1000]}[/dim]")
        if goals:
            summary_lines.append("[cyan]Goals:[/cyan] " + "; ".join(str(g)[:200] for g in goals[:3]))
        if non_goals:
            summary_lines.append("[dim]Non-goals:[/dim] " + "; ".join(str(ng)[:200] for ng in non_goals[:3]))
        console.print(Panel(
            "\n".join(summary_lines),
            title="Project Brief",
            border_style="cyan",
        ))

    # Determine repo settings based on intent
    repo_type = "new"
    repo_path = None
    repo_url = None
    if intent == "existing_code":
        reasoning = result.get("message", "")
        console.print(f"[yellow]Detected intent: work on existing code.[/yellow]")
        repo_type, repo_path, repo_url = _prompt_existing_code_repo(client)

    # Generate slug from brief name or prompt
    if brief and brief.get("project_name"):
        project_display = brief["project_name"]
        slug = re.sub(r'[^a-z0-9-]', '-', project_display.lower()).strip('-')[:40]
        slug = re.sub(r'-+', '-', slug)
    else:
        words = prompt.split()[:3]
        project_display = " ".join(words)
        slug = "-".join(words).lower()
        slug = re.sub(r'[^a-z0-9-]', '-', slug).strip('-')[:40]

    # ── Phase 3: Submit project to DPE ──
    with console.status("[bold cyan]Submitting project to pipeline...[/bold cyan]"):
        try:
            submit_result = client.submit_project(
                slug, brief=brief, name=project_display,
                repo_type=repo_type, repo_path=repo_path, repo_url=repo_url,
            )
        except Exception as e:
            err_str = str(e)
            # If project already exists (409), delete the old one and retry
            if "409" in err_str:
                console.print(f"[yellow]Project '{slug}' already exists — deleting and retrying.[/yellow]")
                try:
                    client.delete_project(slug)
                    submit_result = client.submit_project(
                        slug, brief=brief, name=project_display,
                        repo_type=repo_type, repo_path=repo_path, repo_url=repo_url,
                    )
                except Exception as e2:
                    console.print(f"[red]Failed to submit project: {e2}[/red]")
                    console.print("[dim]Your brief has been saved. Use /resume to retry.[/dim]")
                    save_assessment({
                        "prompt": prompt,
                        "history": history,
                        "last_message": "",
                        "status": "asking",
                        "brief_backup": brief,
                        "slug": slug,
                    })
                    return
            else:
                console.print(f"[red]Failed to submit project: {e}[/red]")
                console.print("[dim]Your brief has been saved. Use /resume to retry.[/dim]")
                # Save brief for recovery
                save_assessment({
                    "prompt": prompt,
                    "history": history,
                    "last_message": "",
                    "status": "asking",
                    "brief_backup": brief,
                    "slug": slug,
                })
                return

    # Submit succeeded — now safe to clear assessment
    clear_assessment()

    console.print(f"[green]Project [cyan]{slug}[/cyan] submitted to DPE pipeline.[/green]")
    console.print(f"[dim]  Scheduler will execute steps automatically.[/dim]")

    _state["project_id"] = slug
    _state["page"] = "in_project"

    # ── Phase 4: Monitor pipeline execution ──
    _monitor_pipeline(slug, client)

    # Return to dashboard so user sees the updated project list
    _state["page"] = "dashboard"
    console.print("\n[dim]Returning to dashboard...[/dim]")


def _auto_create_and_run_legacy(prompt: str, client):
    """Legacy fallback: create project with minimal brief and submit to DPE."""
    repo_type = "new"
    repo_path = None
    repo_url = None

    try:
        with console.status("[bold cyan]Detecting intent...[/bold cyan]"):
            intent_result = client.detect_intent(prompt)
        intent = intent_result.get("intent", "new_project")
        reasoning = intent_result.get("reasoning", "")

        if intent == "existing_code":
            console.print(f"[yellow]Detected intent: work on existing code. {reasoning}[/yellow]")
            repo_type, repo_path, repo_url = _prompt_existing_code_repo(client)
        elif intent == "unclear":
            console.print(f"[yellow]Intent unclear. {reasoning}[/yellow]")
            from rich.prompt import Confirm, Prompt
            choice = Prompt.ask(
                "Is this a new project or existing code?",
                choices=["1", "2"], default="1"
            )
            if choice == "2":
                repo_type, repo_path, repo_url = _prompt_existing_code_repo(client)
    except Exception:
        pass

    words = prompt.split()[:3]
    slug = "-".join(words).lower()
    slug = re.sub(r'[^a-z0-9-]', '-', slug).strip('-')[:40]

    # Build a minimal brief from the prompt for submit_project
    brief = {
        "project_name": " ".join(words).title(),
        "description": prompt,
        "goals": [prompt],
        "non_goals": [],
        "tech_constraints": [],
        "user_stories": [],
        "target_users": "",
        "success_criteria": prompt,
    }

    with console.status("[bold cyan]Submitting project to pipeline...[/bold cyan]"):
        try:
            submit_result = client.submit_project(
                slug, brief=brief, name=" ".join(words),
                repo_type=repo_type, repo_path=repo_path, repo_url=repo_url,
            )
        except Exception as e:
            console.print(f"[red]Failed to submit project: {e}[/red]")
            return

    console.print(f"[green]Project [cyan]{slug}[/cyan] submitted to DPE pipeline.[/green]")
    console.print(f"[dim]  Scheduler will execute steps automatically.[/dim]")

    _state["project_id"] = slug
    _state["page"] = "in_project"
    _monitor_pipeline(slug, client)
    _state["page"] = "dashboard"


# ── Main REPL loop ──────────────────────────────────────────────


def _validate_backend(max_retries=2):
    """Verify the backend responds properly. Auto-restart if it returns errors."""
    import httpx
    client = _get_client()
    for attempt in range(max_retries):
        try:
            client.list_projects()
            return  # backend is healthy
        except httpx.HTTPStatusError as e:
            if attempt < max_retries - 1:
                console.print(f"[yellow]Backend error ({e.response.status_code}), restarting...[/yellow]")
                _handle_restart_cmd()
            else:
                console.print(f"[red]Backend still failing after restart ({e.response.status_code}).[/red]")
                raise SystemExit(1)
        except httpx.ConnectError:
            if attempt < max_retries - 1:
                console.print("[yellow]Lost connection to backend, restarting...[/yellow]")
                _handle_restart_cmd()
            else:
                console.print("[red]Cannot connect to backend after restart.[/red]")
                raise SystemExit(1)


def _interactive_repl(first_prompt: str = None):
    """Interactive REPL — project dashboard + per-project prompt loop."""
    # Ensure server is running
    server_url = _state["server_url"]
    from cli.server import ensure_server_running
    with console.status("[bold cyan]Starting backend...[/bold cyan]"):
        try:
            ensure_server_running(server_url)
        except RuntimeError as e:
            console.print(f"[red]Could not start server: {e}[/red]")
            return
    console.print("[green]Backend connected.[/green]\n")

    # Start SSE flash consumer for real-time pipeline status in toolbar
    _start_sse_flash(server_url)

    # Validate backend is actually working, restart if not
    with console.status("[bold cyan]Checking backend...[/bold cyan]"):
        _validate_backend()

    # If first prompt provided via CLI, auto-create project and run
    if first_prompt:
        client = _get_client()
        _auto_create_and_run(first_prompt, client)

    # Main loop: dashboard → project prompt → dashboard → ...
    while True:
        _project_dashboard()
        if _state["page"] == "in_project":
            _project_repl_loop()


def _get_client():
    from cli.client import APIClient
    return APIClient(_state["server_url"])


def _task_dashboard(project_id: str):
    """Render the task dashboard for a project and handle user input.
    Mirrors the project dashboard pattern: table with numbered rows, command dispatch.
    Returns when user navigates back to project dashboard (via /projects or Enter).
    """
    from cli.client import APIClient
    from cli.completer import repl_input, set_completion_context
    from cli.meta_store import list_pending_task_metas
    import httpx

    set_completion_context("project")
    client = APIClient(_state["server_url"])
    _flash_msg = None
    _should_clear = True
    _skip_render = False

    # Resolve project display name
    try:
        project = client.get_project(project_id)
        project_name = project.get("name", project_id)
    except Exception:
        project_name = project_id

    while True:
        if _should_clear:
            console.clear()
        _should_clear = True  # reset default

        # Fetch current project state (used for checkpoint check + empty project hint)
        proj = None
        try:
            proj = client.get_project(project_id)
            if proj.get("status") == "waiting_user_approval":
                _monitor_checkpoints(project_id, client)
                _flash_msg = "[green]Checkpoint resolved. Pipeline continuing...[/green]"
        except Exception:
            pass

        console.print(Panel(
            f"[bold cyan]AItelier[/bold cyan] — Tasks: {project_name}",
            border_style="cyan",
        ))

        if _flash_msg:
            console.print(_flash_msg)
            _flash_msg = None

        # Fetch tasks for this project
        try:
            tasks = client.list_tasks_by_project(project_id)
        except httpx.ConnectError:
            console.print("[red]Cannot connect to server.[/red]")
            return
        except httpx.HTTPStatusError as e:
            console.print(f"[yellow]Backend error ({e.response.status_code}), restarting...[/yellow]")
            _handle_restart_cmd()
            continue

        # Build task table
        table = Table(show_header=True, header_style="bold")
        table.add_column("#", width=4, style="dim")
        table.add_column("ID", width=6)
        table.add_column("Status", width=12)
        table.add_column("Step", width=10)
        table.add_column("Prompt", max_width=60)
        table.add_column("Created", width=20)

        # Row 0: add task action
        table.add_row("", "", "[bold green]+ add task[/bold green]", "", "", "")

        # Rows 1..N: existing tasks
        for i, t in enumerate(tasks):
            s = t.get("status", "?")
            style = _status_style(s)
            step = t.get("current_step") or "-"
            prompt = t.get("prompt", "")[:60]
            created = str(t.get("created_at", ""))[:19]

            # Show completed steps progress
            completed_steps = t.get("completed_steps", "[]")
            try:
                import json as _json
                done_list = _json.loads(completed_steps) if completed_steps else []
                if done_list:
                    step = f"{step} [{len(done_list)}/3 steps]"
            except Exception:
                pass

            table.add_row(
                str(i + 1),
                str(t.get("id", "")),
                f"[{style}]{s}[/{style}]",
                step,
                prompt,
                created,
            )

        console.print(table)

        # Check for pending task metas
        pending_tasks_metas = list_pending_task_metas(project_id)
        if pending_tasks_metas:
            console.print(
                f"[yellow]{len(pending_tasks_metas)} interrupted task meta conversation(s).[/yellow] "
                f"[dim]Use [cyan]/resume-task[/cyan] to continue.[/dim]"
            )

        # Show guidance for empty projects
        if not tasks and proj and proj.get("status") in ("planning", "created", "idle"):
            console.print(
                "[bold green]This project has no tasks yet.[/bold green] "
                "[dim]Type what you'd like to build to start a conversation and kick off the pipeline.[/dim]"
            )

        console.print(
            "[dim]  [cyan]0[/cyan] or [cyan]/add-task[/cyan]  — add a new task\n"
            "[dim]  [cyan]<number>[/cyan]        — select a task\n"
            "[dim]  [cyan]<text>[/cyan]          — add task with prompt\n"
            "[dim]  [cyan]/help[/cyan]          — show all commands  |  "
            f"Enter — project list[/dim]"
        )

        try:
            user_input = repl_input(f"{project_id}> ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Bye.[/dim]")
            raise SystemExit(0)

        if not user_input:
            _state["page"] = "dashboard"
            return

        # Numeric selection
        if user_input.isdigit():
            num = int(user_input)
            if num == 0:
                _handle_add_task_cmd(_get_client())
                continue
            elif 1 <= num <= len(tasks):
                selected = tasks[num - 1]
                task_id = selected["id"]
                # Parse completed steps for progress display
                completed_steps_raw = selected.get("completed_steps", "[]")
                try:
                    done_list = json.loads(completed_steps_raw) if completed_steps_raw else []
                except Exception:
                    done_list = []
                current_step = selected.get("current_step", "-")
                status = selected.get("status", "?")

                # Build step progress bar
                all_steps = ["t_plan", "t_impl", "t_verify"]
                step_progress = []
                for s in all_steps:
                    if s in done_list:
                        step_progress.append(f"[green]{s}[/green]")
                    elif s == current_step and status == "running":
                        step_progress.append(f"[yellow]{s}[/yellow]")
                    else:
                        step_progress.append(f"[dim]{s}[/dim]")
                progress_str = " → ".join(step_progress)

                # Build detail panel
                detail_lines = [
                    f"ID: [bold]{task_id}[/bold]",
                    f"Status: [{_status_style(status)}]{status}[/{_status_style(status)}]",
                    f"Step: {current_step}",
                    f"Progress: {progress_str}",
                    f"Prompt: {selected.get('prompt', 'N/A')}",
                ]
                if done_list:
                    detail_lines.append(f"Completed: {', '.join(done_list)}")

                console.print(Panel(
                    "\n".join(detail_lines),
                    title=f"Task #{task_id}",
                    border_style="blue",
                ))
                console.print(
                    f"[dim]  [cyan]/output {task_id}[/cyan] — view output  "
                    f"[cyan]/logs {task_id}[/cyan] — view traces  "
                    f"[cyan]/retry {task_id}[/cyan] — retry if failed[/dim]"
                )
                _should_clear = False
                continue
            else:
                console.print(f"[red]Invalid selection: {num}[/red]")
                _should_clear = False
                continue

        # Handle slash commands
        if user_input.startswith("/"):
            cmd = user_input.lower().split()[0]
            rest = user_input[len(cmd):].strip()

            if cmd in ("/quit", "/exit", "/q"):
                console.print("[dim]Bye.[/dim]")
                raise SystemExit(0)
            elif cmd == "/help":
                _print_repl_help()
                _should_clear = False
                continue
            elif cmd == "/status":
                _repl_status()
                _should_clear = False
                continue
            elif cmd == "/output":
                _handle_output_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/tasks":
                # Explicit refresh — just re-render
                continue
            elif cmd == "/projects":
                _state["page"] = "dashboard"
                return
            elif cmd == "/new":
                _create_new_project(_get_client())
                # _create_new_project sets page to in_project on success
                # Return so _interactive_repl re-enters with new project
                return
            elif cmd == "/project":
                if rest:
                    _state["project_id"] = rest
                    console.print(f"[green]Project set to: {rest}[/green]")
                else:
                    console.print(f"Current project: [cyan]{_state['project_id']}[/cyan]")
                _should_clear = False
                continue
            elif cmd == "/url":
                if rest:
                    _state["server_url"] = rest
                    console.print(f"[green]Server URL set to: {rest}[/green]")
                else:
                    console.print(f"Server URL: [cyan]{_state['server_url']}[/cyan]")
                _should_clear = False
                continue
            elif cmd == "/frequency":
                _handle_frequency_cmd(rest)
                _should_clear = False
                continue
            elif cmd == "/cron":
                _handle_cron_cmd(rest)
                _should_clear = False
                continue
            elif cmd == "/restart":
                _handle_restart_cmd()
                continue
            elif cmd == "/delete":
                old_page = _state["page"]
                _handle_delete_cmd(rest, _get_client())
                if _state["page"] != old_page:
                    return
                continue
            elif cmd == "/edit":
                _handle_edit_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/pause":
                _handle_pause_cmd(_get_client())
                continue
            elif cmd == "/resume":
                _handle_resume_cmd(_get_client())
                continue
            elif cmd == "/add-task":
                _handle_add_task_cmd(_get_client())
                continue
            elif cmd == "/resume-task":
                _handle_resume_task_cmd(_get_client())
                continue
            elif cmd == "/refresh":
                _handle_refresh_cmd(_get_client())
                continue
            elif cmd == "/retry":
                _handle_retry_task_cmd(rest, _get_client())
                continue
            elif cmd == "/cancel-task":
                _handle_cancel_task_cmd(rest, _get_client())
                continue
            elif cmd == "/logs":
                _handle_logs_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/errors":
                _handle_errors_cmd(_get_client())
                _should_clear = False
                continue
            elif cmd == "/runs":
                _handle_runs_cmd(_get_client())
                _should_clear = False
                continue
            elif cmd == "/trace":
                _handle_trace_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/tree":
                _handle_tree_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/cat":
                _handle_cat_cmd(rest, _get_client())
                _should_clear = False
                continue
            elif cmd == "/rollback":
                _handle_rollback_cmd(rest, _get_client())
                continue
            else:
                console.print(f"[red]Unknown command: {cmd}[/red]  [dim]Type /help for commands[/dim]")
                _should_clear = False
                continue

        # Regular prompt → meta conversation → pipeline
        _handle_prompt(user_input)
        console.print()


def _project_repl_loop():
    """Entry point for project context. Delegates to task dashboard."""
    from cli.completer import set_completion_context
    from cli.meta_store import load_project_meta

    set_completion_context("project")
    project_id = _state["project_id"]
    client = _get_client()

    # Check for pending checkpoint first (project paused for user approval)
    try:
        project = client.get_project(project_id)
        if project.get("status") == "waiting_user_approval":
            _monitor_checkpoints(project_id, client)
    except Exception:
        pass

    # Check for resumable project meta conversation (once on entry)
    meta = load_project_meta(project_id)
    if meta and meta.get("status") == "asking":
        console.print(f"[yellow]Incomplete project meta conversation found (turn {len(meta.get('history', []))}).[/yellow]")
        _handle_prompt(meta.get("prompt", ""), resume=True)

    _task_dashboard(project_id)


# ── Meta conversation + pipeline ────────────────────────────────


def _handle_prompt(prompt: str, resume: bool = False):
    """
    Handle a user prompt: run meta conversation via API, then launch the pipeline
    with the gathered brief. History stored client-side in ~/.AItelier/meta/.
    """
    from core.meta_conversation import format_brief_as_markdown
    from cli.meta_store import save_project_meta, load_project_meta, clear_project_meta
    from cli.completer import set_completion_context

    set_completion_context("meta_chat")

    client = _get_client()
    project_id = _state["project_id"]
    brief_md = None

    # ── Phase 1: Meta conversation ──
    try:
        if resume:
            meta = load_project_meta(project_id)
            if not meta or meta.get("status") != "asking":
                resume = False
            else:
                console.print("[green]Resuming meta conversation...[/green]\n")

        if not resume:
            with console.status("[bold cyan]Thinking...[/bold cyan]"):
                result = client.meta_start(prompt, project_id)
            # Save initial state
            meta = {
                "prompt": prompt,
                "history": [],
                "last_message": result.get("message"),
                "status": result["status"],
            }
            save_project_meta(project_id, meta)
        else:
            result = {"status": "asking"}

        while result.get("status") == "asking":
            meta = load_project_meta(project_id)
            message = meta.get("last_message") or "Can you tell me more about your project?"
            console.print(f"\n{message}")
            _show_interaction_hint(result.get("interaction"))

            while True:
                try:
                    from cli.completer import repl_input, set_completion_context
                    set_completion_context("meta_chat")
                    answer = repl_input(f"{project_id}> ")
                except (EOFError, KeyboardInterrupt):
                    console.print("[dim]Meta conversation paused. Resume by re-entering the project.[/dim]")
                    return

                if not answer:
                    continue

                if answer.startswith("/"):
                    cmd = answer.lower().split()[0]
                    rest = answer[len(cmd):].strip()

                    if cmd in ("/quit", "/exit", "/q"):
                        console.print("[dim]Bye.[/dim]")
                        raise SystemExit(0)
                    elif cmd == "/help":
                        _print_repl_help()
                        continue
                    elif cmd == "/status":
                        _repl_status()
                        continue
                    elif cmd == "/skip":
                        console.print("[dim]Skipping meta conversation, proceeding with raw prompt.[/dim]\n")
                        result = {"status": "skipped"}
                        break
                    elif cmd == "/cancel":
                        clear_project_meta(project_id)
                        console.print("[dim]Meta conversation cancelled.[/dim]\n")
                        return
                    elif cmd == "/project":
                        if rest:
                            _state["project_id"] = rest
                            console.print(f"[green]Project set to: {rest}[/green]")
                        else:
                            console.print(f"Current project: [cyan]{_state['project_id']}[/cyan]")
                        continue
                    elif cmd == "/url":
                        if rest:
                            _state["server_url"] = rest
                            console.print(f"[green]Server URL set to: {rest}[/green]")
                        else:
                            console.print(f"Server URL: [cyan]{_state['server_url']}[/cyan]")
                        continue
                    elif cmd == "/frequency":
                        _handle_frequency_cmd(rest)
                        continue
                    elif cmd == "/cron":
                        _handle_cron_cmd(rest)
                        continue
                    elif cmd == "/restart":
                        _handle_restart_cmd()
                        continue
                    elif cmd == "/delete":
                        _handle_delete_cmd(rest, _get_client())
                        continue
                    else:
                        console.print(f"[red]Unknown command: {cmd}[/red]  [dim]Type /help for commands[/dim]")
                        continue
                else:
                    break

            if result.get("status") == "skipped":
                break

            # Send answer + full history to stateless server
            history = meta.get("history", [])
            with console.status("[bold cyan]Thinking...[/bold cyan]"):
                result = client.meta_next(project_id, answer, history)

            # Update local file
            if result.get("status") == "asking":
                history.append({"message": message, "answer": answer})
                meta["history"] = history
                meta["last_message"] = result.get("message")
                meta["status"] = result["status"]
                save_project_meta(project_id, meta)

        if result.get("status") == "complete":
            brief = result["project_brief"]
            brief_md = format_brief_as_markdown(brief)

            # Show the brief for review
            brief_message = result.get("message") or "Here's the project brief I've put together:"
            console.print(f"\n{brief_message}")
            console.print(Panel(brief_md, title=f"Project Brief: {brief.get('project_name', 'Untitled')}", border_style="cyan"))

            # Show interaction guidance
            _show_interaction_hint(result.get("interaction"))
            console.print("[dim]  [cyan]approve[/cyan]  — accept the brief and start pipeline[/dim]")
            console.print("[dim]  [cyan]<text>[/cyan]   — describe what to change (brief will be revised)[/dim]")
            console.print("[dim]  [cyan]restart[/cyan]   — start the conversation over[/dim]")

            # Rich review loop
            max_revisions = 5
            for _ in range(max_revisions):
                try:
                    from cli.completer import repl_input, set_completion_context
                    set_completion_context("meta_chat")
                    review_answer = repl_input(f"{project_id}> ")
                except (EOFError, KeyboardInterrupt):
                    review_answer = "yes"

                if not review_answer:
                    continue

                lower = review_answer.lower().strip()

                if lower in ("approve", "yes", "y", "looks good", "proceed", "ok", "go ahead", "lg", "done"):
                    console.print("[green]Brief approved — starting pipeline.[/green]\n")
                    break

                if lower in ("start over", "restart"):
                    clear_project_meta(project_id)
                    console.print("[dim]Starting over...[/dim]\n")
                    _handle_prompt(prompt, resume=False)
                    return

                # Treat as revision feedback
                console.print("[dim]Revising brief...[/dim]")
                try:
                    with console.status("[bold cyan]Revising brief...[/bold cyan]"):
                        revise_result = client.revise_brief(project_id, brief, review_answer)
                    brief = revise_result["project_brief"]
                    brief_md = format_brief_as_markdown(brief)
                    revise_message = revise_result.get("message") or "Updated! Here's the revised brief:"
                    console.print(f"\n{revise_message}")
                    console.print(Panel(brief_md, title=f"Project Brief: {brief.get('project_name', 'Untitled')}", border_style="cyan"))
                except Exception as e:
                    console.print(f"[yellow]Could not revise brief: {e}[/yellow]")
            else:
                console.print("[dim]Max revisions reached — proceeding with current brief.[/dim]\n")

        # Clear meta file on completion/skip
        clear_project_meta(project_id)

    except Exception as e:
        console.print(f"[red]Meta conversation failed: {e}[/red]")
        console.print("[dim]Re-enter the project and type your prompt again to retry.[/dim]")
        clear_project_meta(project_id)
        return  # Do NOT fall through to pipeline on failure

    # ── Phase 2: Submit to DPE ──
    if brief_md is None and result.get("status") != "skipped":
        console.print("[yellow]No project brief was produced. Returning to dashboard.[/yellow]")
        return

    client = _get_client()
    project_id = _state["project_id"]

    with console.status("[bold cyan]Submitting project to pipeline...[/bold cyan]"):
        try:
            submit_result = client.submit_project(
                project_id, brief=brief,
                name=brief.get("project_name", project_id),
            )
        except Exception as e:
            console.print(f"[red]Failed to submit project: {e}[/red]")
            return

    task_id = submit_result.get("task_id")
    console.print(f"[green]Task #{task_id} submitted. DPE pipeline running...[/green]" if task_id
                   else "[green]Project submitted to DPE pipeline.[/green]")

    # ── Phase 3: Monitor pipeline execution ──
    _monitor_pipeline(project_id, client)


# ── Help and status ─────────────────────────────────────────────


# ── Scheduler command helpers ───────────────────────────────────

_FREQ_PRESETS = {"slow": 300, "medium": 60, "high": 15}


def _parse_frequency(value: str) -> int:
    """Parse frequency string (preset or Xs/Xm) to seconds."""
    value = value.lower().strip()
    if value in _FREQ_PRESETS:
        return _FREQ_PRESETS[value]

    total = 0
    import re as _re
    for m in _re.finditer(r'(\d+)(m|s)', value):
        n, unit = int(m.group(1)), m.group(2)
        total += n * 60 if unit == 'm' else n
    if total == 0:
        total = int(value)
    if total < 5:
        raise ValueError("Frequency must be >= 5 seconds")
    return total


def _fmt_seconds(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    if m and s:
        return f"{m}m {s}s"
    if m:
        return f"{m}m"
    return f"{s}s"


def _handle_frequency_cmd(rest: str):
    """Handle /frequency command."""
    client = _get_client()
    if not rest:
        try:
            settings = client.get_scheduler_settings()
            if settings.get("scheduler_type") != "interval":
                console.print("[yellow]Scheduler is using cron, not interval. Use /cron to see.[/yellow]")
                return
            secs = settings.get("scheduler_interval") or 60
            console.print(f"Current frequency: [cyan]{_fmt_seconds(secs)}[/cyan] ({secs}s)")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        return

    try:
        secs = _parse_frequency(rest)
    except (ValueError, TypeError) as e:
        console.print(f"[red]Invalid frequency: {e}[/red]")
        console.print("[dim]Usage: /frequency slow|medium|high|Xs|Xm[/dim]")
        return

    try:
        client.update_scheduler_settings(scheduler_type="interval", scheduler_interval=secs)
        console.print(f"[green]Scheduler frequency set to {_fmt_seconds(secs)}[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_cron_cmd(rest: str):
    """Handle /cron command."""
    client = _get_client()
    if not rest:
        try:
            settings = client.get_scheduler_settings()
            if settings.get("scheduler_type") != "cron":
                secs = settings.get("scheduler_interval") or 60
                console.print(f"[yellow]Scheduler is using interval ({_fmt_seconds(secs)}). Use /frequency to see.[/yellow]")
                return
            console.print(f"Current cron: [cyan]{settings.get('scheduler_cron', '')}[/cyan]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
        return

    parts = rest.strip().split()
    if len(parts) != 5:
        console.print("[red]Cron expression must have 5 fields: minute hour day month weekday[/red]")
        console.print("[dim]Example: /cron */5 * * * *[/dim]")
        return

    try:
        client.update_scheduler_settings(scheduler_type="cron", scheduler_cron=rest.strip())
        console.print(f"[green]Scheduler cron set to: {rest.strip()}[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_restart_cmd():
    """Handle /restart command."""
    from cli.server import restart_server
    console.print("[dim]Restarting backend server...[/dim]")
    try:
        restart_server(_state["server_url"])
        console.print("[green]Backend server restarted.[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_delete_cmd(rest: str, client):
    """Handle /delete command. Full cascade: project + tasks + subtasks + workspace."""
    import httpx

    if not rest:
        rest = _state["project_id"]

    try:
        client.delete_project(rest)
        console.print(f"[green]Project '{rest}' deleted (tasks + workspace cleaned up).[/green]")
        if rest == _state["project_id"]:
            _state["page"] = "dashboard"
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Project '{rest}' not found.[/red]")
        else:
            console.print(f"[red]Error: {e.response.status_code}[/red]")


def _handle_edit_cmd(rest: str, client):
    """Handle /edit command. e.g. /edit name New Name, /edit priority 5"""
    parts = rest.split(None, 1)
    if len(parts) < 2:
        console.print("[dim]Usage: /edit name|brief|priority|status <value>[/dim]")
        return

    field, value = parts[0], parts[1]
    try:
        kwargs = {}
        if field == "name":
            kwargs["name"] = value
        elif field == "brief":
            kwargs["brief"] = value
        elif field == "priority":
            kwargs["priority"] = int(value)
        elif field == "status":
            kwargs["status"] = value
        else:
            console.print(f"[red]Unknown field: {field}[/red]  [dim]Options: name, brief, priority, status[/dim]")
            return
        client.update_project(_state["project_id"], **kwargs)
        console.print(f"[green]{field} updated.[/green]")
    except ValueError:
        console.print(f"[red]Invalid value for {field}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_pause_cmd(client):
    """Handle /pause command."""
    try:
        client.update_project(_state["project_id"], status="paused")
        console.print("[yellow]Project paused.[/yellow]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_resume_cmd(client):
    """Handle /resume command."""
    try:
        client.update_project(_state["project_id"], status="executing")
        console.print("[green]Project resumed.[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_refresh_cmd(client):
    """Handle /refresh command. Re-runs Researcher + Architect planning steps."""
    import httpx
    project_id = _state["project_id"]
    try:
        with console.status("[bold cyan]Refreshing planning..."):
            result = client.refresh_planning(project_id)
        steps = result.get("steps_to_rerun", [])
        if steps:
            console.print(f"[green]Planning refreshed. Steps {', '.join(steps)} will re-run.[/green]")
        else:
            console.print("[green]Planning refreshed.[/green]")
        console.print("[dim]  Monitor with /tasks or wait for pipeline to progress.[/dim]")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Project '{project_id}' not found.[/red]")
        else:
            console.print(f"[red]Refresh failed: {e.response.status_code}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_output_cmd(rest: str, client):
    """Handle /output command. View step output files."""
    import httpx
    parts = rest.split()

    if not parts:
        console.print("[dim]Usage: /output <task_id> [step_id][/dim]")
        console.print("[dim]Steps: t_plan, t_impl, t_verify[/dim]")
        return

    try:
        task_id = int(parts[0])
    except ValueError:
        console.print("[red]Invalid task ID. Usage: /output <task_id> [step_id][/red]")
        return

    step_id = parts[1] if len(parts) > 1 else None

    # Smart default: pick the most relevant step from task state
    if step_id is None:
        try:
            task = client.get_task(task_id)
            completed_steps_raw = task.get("completed_steps", "[]")
            try:
                done_list = json.loads(completed_steps_raw) if completed_steps_raw else []
            except Exception:
                done_list = []
            if done_list:
                step_id = done_list[-1]  # last completed step
            else:
                step_id = task.get("current_step", "t_impl")
        except Exception:
            step_id = "t_impl"

    try:
        result = client.get_step_output(task_id, step_id)
        files = result.get("files", {})
        if not files:
            console.print(f"[dim]No output files found for task #{task_id}, step {step_id}.[/dim]")
            return
        for filename, content in files.items():
            console.print(Panel(
                content,
                title=f"Task #{task_id} / {step_id} / {filename}",
                border_style="blue",
            ))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]No output found for task #{task_id}, step {step_id}.[/red]")
        else:
            console.print(f"[red]Error: {e.response.status_code}[/red]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_add_task_cmd(client):
    """Handle /add-task command. Task-scoped meta conversation."""
    from cli.completer import repl_input, set_completion_context
    from cli.meta_store import save_task_meta, clear_task_meta

    set_completion_context("meta_chat")

    project_id = _state["project_id"]
    try:
        description = repl_input("Task description: ", allow_commands=False)
    except (EOFError, KeyboardInterrupt):
        return
    if not description:
        return

    try:
        result = client.task_meta_start(project_id, description)
        task_id = result.get("task_id")

        # Save initial state
        history = []
        if result.get("status") == "asking":
            save_task_meta(project_id, task_id, {
                "prompt": description,
                "history": [],
                "last_message": result.get("message"),
                "status": "asking",
            })

        while result.get("status") == "asking":
            message = result.get("message", "Can you tell me more?")
            console.print(f"\n{message}")
            _show_interaction_hint(result.get("interaction"))
            try:
                answer = repl_input(f"{project_id}> ")
            except (EOFError, KeyboardInterrupt):
                console.print(f"[dim]Task meta paused. Use /resume-task to continue. Task #{task_id} created.[/dim]")
                return

            if not answer:
                continue
            if answer.startswith("/skip"):
                result = client.task_meta_force(task_id, history)
                break
            result = client.task_meta_next(task_id, answer, history)

            # Update local file
            if result.get("status") == "asking":
                history.append({"message": message, "answer": answer})
                save_task_meta(project_id, task_id, {
                    "prompt": description,
                    "history": history,
                    "last_message": result.get("message"),
                    "status": "asking",
                })

        if result.get("status") == "complete" and task_id:
            console.print(f"[green]Task #{task_id} created with enriched spec.[/green]")
        elif task_id:
            console.print(f"[green]Task #{task_id} created.[/green]")
        clear_task_meta(project_id, task_id)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


def _handle_resume_task_cmd(client):
    """Handle /resume-task command. Resume interrupted task meta conversations."""
    from cli.completer import repl_input, set_completion_context
    from cli.meta_store import list_pending_task_metas, save_task_meta, clear_task_meta

    set_completion_context("meta_chat")

    project_id = _state["project_id"]
    pending = list_pending_task_metas(project_id)

    if not pending:
        console.print("[dim]No interrupted task meta conversations.[/dim]")
        return

    if len(pending) == 1:
        chosen = pending[0]
    else:
        console.print("[bold]Pending task meta conversations:[/bold]")
        for i, p in enumerate(pending):
            tid = p.get("task_id", "?")
            prompt = p.get("prompt", "")[:50]
            turns = len(p.get("history", []))
            console.print(f"  {i + 1}. Task #{tid} ({turns} turns): {prompt}")
        try:
            sel = repl_input("Select (number): ", allow_commands=False)
        except (EOFError, KeyboardInterrupt):
            return
        if not sel.isdigit() or int(sel) < 1 or int(sel) > len(pending):
            console.print("[red]Invalid selection.[/red]")
            return
        chosen = pending[int(sel) - 1]

    task_id = chosen.get("task_id")
    history = chosen.get("history", [])
    description = chosen.get("prompt", "")

    console.print(f"[green]Resuming task #{task_id} meta conversation...[/green]\n")

    try:
        result = {"status": "asking"}
        while result.get("status") == "asking":
            message = chosen.get("last_message") or description
            console.print(f"\n{message}")
            _show_interaction_hint(result.get("interaction"))
            try:
                answer = repl_input(f"{project_id}> ")
            except (EOFError, KeyboardInterrupt):
                console.print("[dim]Task meta paused again.[/dim]")
                return

            if not answer:
                continue
            if answer.startswith("/skip"):
                result = client.task_meta_force(task_id, history)
                break
            result = client.task_meta_next(task_id, answer, history)

            if result.get("status") == "asking":
                history.append({"message": message, "answer": answer})
                save_task_meta(project_id, task_id, {
                    "prompt": description,
                    "history": history,
                    "last_message": result.get("message"),
                    "status": "asking",
                })
                chosen = {"last_message": result.get("message")}

        if result.get("status") == "complete":
            console.print(f"[green]Task #{task_id} spec completed.[/green]")
        clear_task_meta(project_id, task_id)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


# ── /logs, /errors, /cancel-task ─────────────────────────────────────


def _handle_logs_cmd(rest: str, client):
    """Handle /logs command. View pipeline trace files for a task."""
    import httpx
    project_id = _state["project_id"]
    parts = rest.split()

    # Resolve task_id
    if parts:
        try:
            task_id = int(parts[0])
        except ValueError:
            console.print("[red]Invalid task ID. Usage: /logs <task_id>[/red]")
            return
    else:
        # Default: find the latest task in the project
        try:
            tasks = client.list_tasks_by_project(project_id)
            if not tasks:
                console.print("[dim]No tasks found.[/dim]")
                return
            task_id = tasks[-1]["id"]
        except Exception:
            console.print("[red]Could not list tasks.[/red]")
            return

    # Read trace files from workspace
    from pathlib import Path
    ws_base = Path.home() / ".AItelier" / "workspaces" / project_id
    if not ws_base.exists():
        console.print(f"[dim]No workspace found for {project_id}.[/dim]")
        return

    # Find trace directories
    trace_dirs = sorted(ws_base.glob("Trace_*"))
    if not trace_dirs:
        console.print(f"[dim]No trace files found for {project_id}.[/dim]")
        return

    # Filter by step if specified
    step_filter = parts[1] if len(parts) > 1 else None

    shown = 0
    for td in trace_dirs:
        step_name = td.name.replace("Trace_", "")
        if step_filter and step_name != step_filter:
            continue
        # Show the latest attempt files
        trace_files = sorted(td.glob("attempt_*"))
        if not trace_files:
            continue

        # Show the last attempt's key files
        last_attempt_files = [f for f in trace_files if f.name.startswith("attempt_")]
        # Group by attempt number
        attempts = {}
        for f in last_attempt_files:
            parts_name = f.name.split("_", 2)
            if len(parts_name) >= 2:
                num = parts_name[1]
                attempts.setdefault(num, []).append(f)

        if not attempts:
            continue

        latest_num = sorted(attempts.keys())[-1]
        latest_files = attempts[latest_num]

        console.print(f"\n[bold]Step {step_name} (attempt {latest_num}):[/bold]")
        for f in latest_files:
            kind = f.name.split("_", 2)[-1].replace(".txt", "").replace("_", " ")
            content = f.read_text(encoding="utf-8", errors="replace")[:2000]
            if content.strip():
                console.print(Panel(
                    content[:1500],
                    title=f"{step_name} / {kind}",
                    border_style="dim",
                ))
        shown += 1

    if shown == 0:
        console.print(f"[dim]No trace files found.{(' For step ' + step_filter) if step_filter else ''}[/dim]")


def _handle_errors_cmd(client):
    """Handle /errors command. Show last pipeline error for current project."""
    project_id = _state["project_id"]
    try:
        project = client.get_project(project_id)
    except Exception as e:
        console.print(f"[red]Error fetching project: {e}[/red]")
        return

    status = project.get("status", "")
    current_step = project.get("current_project_step", "-")

    # Check meta_state for stored error
    meta_state = project.get("meta_state")
    if meta_state:
        try:
            import json as _json
            state = _json.loads(meta_state)
            error = state.get("error", "")
            step = state.get("step", current_step)
            tb = state.get("traceback", "")
            console.print(Panel(
                f"Step: {step}\nError: {error}\n\n{tb[:2000]}",
                title=f"Pipeline Error — {project_id}",
                border_style="red",
            ))
            return
        except Exception:
            pass

    # Check task-level errors from task_meta_state
    import sqlite3
    db_path = Path.home() / ".AItelier" / "aitelier.db"
    if db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute(
                "SELECT id, prompt, task_meta_state FROM tasks WHERE project_id = ? AND status = 'failed' AND task_meta_state IS NOT NULL",
                (project_id,)
            )
            failed_tasks = cur.fetchall()
            conn.close()
            if failed_tasks:
                for task in failed_tasks:
                    import json as _json
                    task_state = _json.loads(task["task_meta_state"])
                    error = task_state.get("error", "Unknown error")
                    step = task_state.get("step", "?")
                    tb = task_state.get("traceback", "")
                    task_prompt = task["prompt"][:100] if task["prompt"] else "N/A"
                    console.print(Panel(
                        f"Task #{task['id']}: {task_prompt}...\nStep: {step}\nError: {error}\n\n{tb[:1500]}",
                        title=f"Task #{task['id']} Error — {project_id}",
                        border_style="red",
                    ))
                return
        except Exception:
            pass

    # Fallback: check trace files
    from pathlib import Path
    ws_base = Path.home() / ".AItelier" / "workspaces" / project_id
    if not ws_base.exists():
        console.print(f"[dim]No workspace found for {project_id}.[/dim]")
        return

    # Look for error-related trace files
    error_files = list(ws_base.glob("Trace_*/*error*")) + list(ws_base.glob("Trace_*/*fail*"))
    if error_files:
        for ef in sorted(error_files)[-5:]:
            content = ef.read_text(encoding="utf-8", errors="replace")[:2000]
            console.print(Panel(
                content[:1500],
                title=ef.parent.name + " / " + ef.name,
                border_style="red",
            ))
        return

    if status == "failed":
        console.print(f"[yellow]Project failed at step {current_step}, but no error details found.[/yellow]")
        console.print(f"[dim]  Check workspace: ~/.AItelier/workspaces/{project_id}/[/dim]")
    else:
        console.print(f"[dim]No errors found. Project status: {status}[/dim]")


def _handle_cancel_task_cmd(rest: str, client):
    """Handle /cancel-task command. Cancel a running or pending task."""
    import httpx
    project_id = _state["project_id"]

    if not rest:
        # List cancellable tasks
        try:
            tasks = client.list_tasks_by_project(project_id)
            cancellable = [t for t in tasks if t.get("status") in ("pending", "running")]
        except Exception:
            cancellable = []

        if not cancellable:
            console.print("[dim]No pending or running tasks to cancel.[/dim]")
            return

        console.print("[bold]Cancellable tasks:[/bold]")
        for t in cancellable:
            console.print(f"  [cyan]#{t['id']}[/cyan] ({t.get('status')}) — {t.get('prompt', '')[:60]}")
        from rich.prompt import Prompt
        choice = Prompt.ask("Enter task ID to cancel", default=str(cancellable[0]["id"]))
        if not choice.isdigit():
            return
        task_id = int(choice)
    else:
        try:
            task_id = int(rest)
        except ValueError:
            console.print(f"[red]Invalid task ID: {rest}[/red]")
            return

    try:
        # Update task status to failed (cancelled)
        client.update_project(project_id, _cancel_task_id=str(task_id))
        console.print(f"[green]Task #{task_id} cancelled.[/green]")
    except Exception:
        # Fallback: use the task status update directly via patch
        try:
            from cli.client import APIClient
            c = APIClient(_state["server_url"])
            c._client.patch(f"/api/tasks/{task_id}", params={"status": "failed"})
            console.print(f"[green]Task #{task_id} cancelled.[/green]")
        except Exception as e:
            console.print(f"[red]Failed to cancel task: {e}[/red]")


# ── /runs, /trace, /run, /tree, /cat, /rollback ─────────────────────


def _handle_runs_cmd(client):
    """Handle /runs command. List pipeline runs for the current project."""
    project_id = _state["project_id"]
    try:
        data = client.list_runs(project_id)
        runs = data.get("runs", [])
    except Exception as e:
        console.print(f"[red]Error fetching runs: {e}[/red]")
        return

    if not runs:
        console.print("[dim]No pipeline runs found for this project.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Run ID", width=24)
    table.add_column("Graph", width=18)
    table.add_column("Status", width=12)
    table.add_column("Steps", width=12)
    table.add_column("Created", width=20)

    for r in runs:
        status_color = {
            "running": "yellow", "completed": "green", "failed": "red",
            "paused": "dim",
        }.get(r.get("status", ""), "white")
        steps_str = f"{r.get('completed_steps', 0)}/{r.get('step_count', 0)}"
        table.add_row(
            r.get("id", "")[:22],
            r.get("graph_name", ""),
            f"[{status_color}]{r.get('status', '')}[/{status_color}]",
            steps_str,
            r.get("created_at", "")[:19],
        )
    console.print(table)
    console.print("[dim]Use /trace <run_id> to view execution traces.[/dim]")


def _handle_trace_cmd(rest: str, client):
    """Handle /trace command. Show execution traces for a run."""
    project_id = _state["project_id"]
    parts = rest.split()

    # Resolve run_id
    if parts:
        run_id = parts[0]
    else:
        # Default: find the latest run for this project
        try:
            data = client.list_runs(project_id)
            runs = data.get("runs", [])
            if not runs:
                console.print("[dim]No runs found. Use /runs to list runs.[/dim]")
                return
            run_id = runs[0]["id"]
        except Exception as e:
            console.print(f"[red]Error fetching runs: {e}[/red]")
            return

    # Optional step filter (approximate match on step_id in traces)
    step_filter = parts[1] if len(parts) > 1 else None
    category_filter = parts[2] if len(parts) > 2 else None

    try:
        data = client.get_run_trace(run_id, category=category_filter, limit=200)
        traces = data.get("traces", [])
    except Exception as e:
        console.print(f"[red]Error fetching trace: {e}[/red]")
        return

    if not traces:
        console.print(f"[dim]No trace entries found for run {run_id}.[/dim]")
        return

    # Show run overview first
    try:
        run = client.get_run(run_id)
        console.print(f"\n[bold]Run {run_id} — {run.get('graph_name', '?')}[/bold]")
        console.print(f"Status: [yellow]{run.get('status', '?')}[/yellow]  "
                      f"Steps: {run.get('completed_steps', 0)}/{run.get('step_count', 0)}")
    except Exception:
        pass

    # Show traces
    shown = 0
    for t in traces:
        step_id = t.get("step_id", "")
        if step_filter and step_filter not in step_id:
            continue
        cat = t.get("category", "")
        event = t.get("event", "")
        payload = t.get("payload", {})

        if cat == "prompt":
            # Show prompt summary
            text = str(payload.get("user", payload.get("system", "")))[:500]
            if text.strip():
                console.print(Panel(
                    text[:400],
                    title=f"{step_id} / {event}",
                    border_style="dim",
                ))
                shown += 1
        elif cat == "response":
            text = str(payload.get("text", ""))[:500]
            if text.strip():
                console.print(Panel(
                    text[:400],
                    title=f"{step_id} / {event}",
                    border_style="green",
                ))
                shown += 1
        elif cat == "tool_call":
            params = payload.get("params", {})
            console.print(f"  [dim]{step_id}[/dim] [cyan]🔧 {event}[/cyan] — {str(params)[:200]}")
            shown += 1
        elif cat == "error":
            console.print(Panel(
                str(payload)[:500],
                title=f"{step_id} / ERROR",
                border_style="red",
            ))
            shown += 1

    if shown == 0:
        console.print(f"[dim]No matching trace entries.{' Step filter: ' + step_filter if step_filter else ''}[/dim]")
    console.print(f"\n[dim]{len(traces)} total entries, {shown} shown. Use /trace <run_id> <step> <category> to filter.[/dim]")


def _handle_tree_cmd(rest: str, client):
    """Handle /tree command. Browse workspace directory tree."""
    project_id = _state["project_id"]
    subdir = rest if rest else None
    try:
        data = client.workspace_tree(project_id, subdir=subdir)
        tree = data.get("tree", [])
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        return

    if not tree:
        console.print(f"[dim]Workspace is empty{' in ' + subdir if subdir else ''}.[/dim]")
        return

    console.print(f"\n[bold]Workspace: {project_id}{'/' + subdir if subdir else ''}[/bold]")
    for path in tree[:50]:
        console.print(f"  [dim]{path}[/dim]")
    if len(tree) > 50:
        console.print(f"  [dim]... and {len(tree) - 50} more files[/dim]")
    console.print(f"\n[dim]{len(tree)} files. Use /cat <path> to read a file.[/dim]")


def _handle_cat_cmd(rest: str, client):
    """Handle /cat command. Read a file from workspace."""
    project_id = _state["project_id"]
    if not rest:
        console.print("[red]Usage: /cat <path>[/red]")
        console.print("[dim]Use /tree to browse available paths.[/dim]")
        return

    try:
        data = client.workspace_file(project_id, rest)
        content = data.get("content", "")
    except Exception as e:
        console.print(f"[red]Error reading file: {e}[/red]")
        return

    # Syntax-highlight if available
    try:
        from rich.syntax import Syntax
        ext = rest.rsplit(".", 1)[-1] if "." in rest else "text"
        lang_map = {"py": "python", "md": "markdown", "json": "json", "yaml": "yaml",
                    "yml": "yaml", "js": "javascript", "ts": "typescript", "html": "html",
                    "css": "css", "sh": "bash", "txt": "text"}
        lexer = lang_map.get(ext, "text")
        console.print(Syntax(content[:10000], lexer, line_numbers=True,
                             theme="monokai"))
    except Exception:
        console.print(Panel(content[:5000], title=rest, border_style="dim"))


def _handle_rollback_cmd(rest: str, client):
    """Handle /rollback command. Rollback a task to a specific git commit."""
    parts = rest.split()
    if len(parts) < 2:
        console.print("[red]Usage: /rollback <task_id> <commit_hash>[/red]")
        return

    try:
        task_id = int(parts[0])
    except ValueError:
        console.print(f"[red]Invalid task ID: {parts[0]}[/red]")
        return
    commit_hash = parts[1]

    try:
        result = client.rollback(task_id, commit_hash)
        console.print(f"[green]Task #{task_id} rolled back to {commit_hash}.[/green]")
    except Exception as e:
        console.print(f"[red]Rollback failed: {e}[/red]")


# ── Help and status (original) ─────────────────────────────────────────


def _print_repl_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  [cyan]<text>[/cyan]            Run pipeline with <text> as prompt\n"
        "  [cyan]<number>[/cyan]          Select a task by row number\n"
        "  [cyan]/projects[/cyan]         Return to project dashboard\n"
        "  [cyan]/new[/cyan]              Create a new project\n"
        "  [cyan]/tasks[/cyan]            Refresh task dashboard\n"
        "  [cyan]/edit <field> <val>[/cyan] Edit project (name, brief, priority, status)\n"
        "  [cyan]/skip[/cyan]             Skip meta conversation, use raw prompt\n"
        "  [cyan]/run <prompt>[/cyan]      Submit a task to the current project\n"
        "  [cyan]/add-task[/cyan]         Add a new task via meta conversation\n"
        "  [cyan]/resume-task[/cyan]      Resume interrupted task meta conversation\n"
        "  [cyan]/output <id> [step][/cyan] View step output files\n"
        "  [cyan]/logs [task_id] [step][/cyan] View pipeline execution traces (filesystem)\n"
        "  [cyan]/trace [run_id] [step] [cat][/cyan] View execution traces (API, prompt/response pairs)\n"
        "  [cyan]/runs[/cyan]             List pipeline runs for current project\n"
        "  [cyan]/errors[/cyan]           View last pipeline error\n"
        "  [cyan]/tree [subdir][/cyan]     Browse workspace directory tree\n"
        "  [cyan]/cat <path>[/cyan]        Read a workspace file\n"
        "  [cyan]/refresh[/cyan]          Re-run Researcher + Architect planning steps\n"
        "  [cyan]/retry [task_id][/cyan]   Retry a failed task\n"
        "  [cyan]/rollback <id> <hash>[/cyan] Rollback task to a git commit\n"
        "  [cyan]/cancel-task [id][/cyan]  Cancel a running or pending task\n"
        "  [cyan]/status[/cyan]           Show project tasks\n"
        "  [cyan]/project <id>[/cyan]      Set project ID\n"
        "  [cyan]/delete [id][/cyan]       Delete project (cascade: tasks + workspace)\n"
        "  [cyan]/pause[/cyan]            Pause the current project\n"
        "  [cyan]/resume[/cyan]           Resume the paused project\n"
        "  [cyan]/url <url>[/cyan]         Set server URL\n"
        "  [cyan]/frequency [val][/cyan]   Set scheduler frequency (slow|medium|high|Xs|Xm)\n"
        "  [cyan]/cron [expr][/cyan]       Set cron schedule (e.g. */5 * * * *)\n"
        "  [cyan]/restart[/cyan]           Restart the backend server\n"
        "  [cyan]/quit[/cyan]             Exit\n"
        "  [cyan]/help[/cyan]             Show this help",
        title="AItelier REPL",
        border_style="dim",
    ))


def _repl_status():
    from cli.client import APIClient
    import httpx
    client = APIClient(_state["server_url"])
    in_project = _state.get("page") == "in_project" and _state.get("project_id")
    try:
        if in_project:
            tasks = client.list_tasks_by_project(_state["project_id"])
        else:
            tasks = client.list_tasks()
        if not tasks:
            console.print("[dim]No tasks found.[/dim]")
            return
        table = Table(show_header=True, header_style="bold")
        table.add_column("ID", width=6)
        if not in_project:
            table.add_column("Project", width=14)
        table.add_column("Status", width=10)
        table.add_column("Step", width=10)
        table.add_column("Prompt", max_width=60)
        for t in tasks[:20]:
            s = t.get("status", "?")
            style = _status_style(s)
            row = [
                str(t.get("id", "")),
            ]
            if not in_project:
                row.append(t.get("project_id", ""))
            row.extend([
                f"[{style}]{s}[/{style}]",
                t.get("current_step", "-"),
                t.get("prompt", "")[:60],
            ])
            table.add_row(*row)
        console.print(table)
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to {_state['server_url']}[/red]")


# ── Typer subcommands ───────────────────────────────────────────


@app.command()
def status(
    task_id: Optional[int] = typer.Argument(None, help="Task ID to inspect"),
    server_url: str = typer.Option(_DEFAULT_URL, "--url", "-u", help="Backend URL"),
):
    """Show task status. If task_id omitted, list all tasks."""
    from cli.client import APIClient
    import httpx
    client = APIClient(server_url)
    try:
        if task_id is None:
            tasks = client.list_tasks()
            if not tasks:
                console.print("[dim]No tasks found.[/dim]")
                return

            table = Table(title="AItelier Tasks")
            table.add_column("ID", style="bold")
            table.add_column("Project", style="cyan")
            table.add_column("Status")
            table.add_column("Created")
            table.add_column("Prompt", max_width=50)

            for t in tasks:
                s = t.get("status", "?")
                style = _status_style(s)
                table.add_row(
                    str(t.get("id", "")),
                    t.get("project_id", ""),
                    f"[{style}]{s}[/{style}]",
                    str(t.get("created_at", "")),
                    t.get("prompt", "")[:50],
                )
            console.print(table)
        else:
            task = client.get_task(task_id)
            console.print(Panel(
                f"ID: [bold]{task['id']}[/bold]\n"
                f"Project: [cyan]{task['project_id']}[/cyan]\n"
                f"Status: {task['status']}\n"
                f"Created: {task['created_at']}\n"
                f"Prompt: {task.get('prompt', 'N/A')}",
                title=f"Task #{task_id}",
                border_style="blue",
            ))
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to server at {server_url}[/red]")
        console.print("[dim]Start with: aitelier server[/dim]")
        raise typer.Exit(1)


@app.command()
def rollback(
    task_id: int = typer.Argument(..., help="Task ID"),
    commit_hash: str = typer.Argument(..., help="Git commit hash to roll back to"),
    server_url: str = typer.Option(_DEFAULT_URL, "--url", "-u", help="Backend URL"),
):
    """Rollback a task to a specific git commit."""
    from cli.client import APIClient
    import httpx
    client = APIClient(server_url)
    try:
        result = client.rollback(task_id, commit_hash)
        console.print(f"[green]Rollback successful:[/green] {result}")
    except httpx.HTTPStatusError as e:
        detail = f"Rollback failed ({e.response.status_code})"
        try:
            detail = e.response.json().get("detail", detail)
        except Exception:
            pass
        console.print(f"[red]{detail}[/red]")
        raise typer.Exit(1)
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to server at {server_url}[/red]")
        console.print("[dim]Start with: aitelier server[/dim]")
        raise typer.Exit(1)


@app.command()
def server(
    host: str = typer.Option("0.0.0.0", "--host", help="Bind host"),
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p", help="Bind port"),
):
    """Start the backend server."""
    import uvicorn
    uvicorn.run("api.main:app", host=host, port=port, reload=False)


# --- Entry point logic ---
#   aitelier              → project dashboard
#   aitelier "hello"      → auto-create project, run pipeline, then dashboard
#   aitelier --oneshot "hello" → one-shot: run pipeline and exit
#   aitelier status       → subcommand dispatch

_KNOWN_COMMANDS = {"status", "rollback", "server"}


def _entrypoint():
    """Custom entrypoint with TUI-by-default and --oneshot override."""
    args = sys.argv[1:]

    oneshot = "--oneshot" in args or "-o" in args

    # Strip --oneshot/-o from argv before passing to typer
    filtered = [sys.argv[0]] + [a for a in args if a not in ("--oneshot", "-o")]
    positional = [a for a in filtered[1:] if not a.startswith('-')]

    # Only flags like --help → let typer handle (but not if --oneshot is present)
    if not oneshot and args and all(a.startswith('-') for a in args):
        app()
        return

    if oneshot:
        # One-shot mode: aitelier --oneshot "hello" → run pipeline and exit
        if not positional or positional[0] in _KNOWN_COMMANDS:
            console.print("[red]Error: --oneshot requires a prompt argument.[/red]")
            raise SystemExit(1)

        prompt = positional[0]
        _state["server_url"] = _DEFAULT_URL
        _apply_repl_options(args)

        from cli.server import ensure_server_running
        with console.status("[bold cyan]Starting backend...[/bold cyan]"):
            try:
                ensure_server_running(_state["server_url"])
            except RuntimeError as e:
                console.print(f"[red]Could not start server: {e}[/red]")
                raise SystemExit(1)

        client = _get_client()
        _auto_create_and_run(prompt, client)
        return

    # Default: interactive REPL mode
    # But if first positional is a known subcommand, dispatch to typer
    if positional and positional[0] in _KNOWN_COMMANDS:
        sys.argv = [sys.argv[0]] + [a for a in args if a not in ("--oneshot", "-o")]
        app()
        return

    _apply_repl_options(args)

    first_prompt = positional[0] if positional else None

    # Launch Textual TUI
    from cli.server import ensure_server_running
    with console.status("[bold cyan]Starting backend...[/bold cyan]"):
        try:
            ensure_server_running(_state["server_url"])
        except RuntimeError as e:
            console.print(f"[red]Could not start server: {e}[/red]")
            raise SystemExit(1)

    from cli.tui import AItelierApp
    tui = AItelierApp(server_url=_state["server_url"], first_prompt=first_prompt)
    tui.run()


def _apply_repl_options(args):
    """Parse --project/-p and --url/-u from args into REPL state."""
    i = 0
    while i < len(args):
        if args[i] in ("--project", "-p") and i + 1 < len(args):
            _state["project_id"] = args[i + 1]
            _state["page"] = "in_project"
            i += 2
        elif args[i] in ("--url", "-u") and i + 1 < len(args):
            _state["server_url"] = args[i + 1]
            i += 2
        else:
            i += 1
