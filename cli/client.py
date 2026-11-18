# cli/client.py
# AItelier HTTP API client + SSE consumer thread.

import asyncio
import json
import os
import queue
import threading
from typing import Optional

import httpx

from core.dotenv_boot import load_repo_dotenv

# Load .env so AITELIER_ADMIN_TOKEN (write-gate bypass for the CLI) is available.
# This module is a boot entry and calls the shared loader explicitly;
# api/main.py deliberately does not, so importing the app adds no env key.
load_repo_dotenv()

_DEFAULT_URL = f"http://localhost:{os.environ.get('AITELIER_PORT', '4444')}"


def _auth_headers() -> dict:
    """Credential header so the CLI passes the server's write-gate.

    A per-driver token (core/driver_credentials.py) when one is configured,
    else the legacy admin token exactly as before.
    """
    from core.driver_credentials import auth_headers
    return auth_headers()


# ── Refusals ───────────────────────────────────────────────────────
#
# A refusal used to leave this module as whatever the bare httpx status check
# raised: "Client error '403 Forbidden' for url ..." and nothing else, because
# the server's own {"detail": ..., "code": ...} body was never read. Every call
# below goes through raise_for_refusal instead.

# Refusal code the server sends as a header when the body carries none
# (api/authz.py: the stable code travels in the header rather than the body).
_DENIAL_CODE_HEADER = "X-AItelier-Denial"

# httpx's own words for the status class, reproduced so a refusal with no
# server sentence reads exactly as it did before.
_ERROR_TYPES = {
    1: "Informational response",
    3: "Redirect response",
    4: "Client error",
    5: "Server error",
}


def _status_headline(resp: httpx.Response) -> str:
    error_type = _ERROR_TYPES.get(resp.status_code // 100, "Invalid status code")
    return f"{error_type} '{resp.status_code} {resp.reason_phrase}' for url '{resp.url}'"


def _httpx_status_message(resp: httpx.Response) -> str:
    """httpx's own wording for a failed response, reproduced verbatim.

    Used when the body carries no server sentence, so nothing the CLI printed
    before changes shape.
    """
    headline = _status_headline(resp)
    if resp.has_redirect_location:
        return (
            f"{headline}\n"
            f"Redirect location: '{resp.headers['location']}'\n"
            f"For more information check: "
            f"https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{resp.status_code}"
        )
    return (
        f"{headline}\n"
        f"For more information check: "
        f"https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/{resp.status_code}"
    )


def _refusal_sentence(resp: httpx.Response) -> tuple[str, str] | None:
    """`(detail, code)` from a refusal body, or None when there is no sentence.

    Only a JSON object whose `detail` is a string carries the server's own
    sentence. `code` falls back to the denial header, which is where a refusal
    that never reached the handler (the write gate) puts its stable code.
    """
    try:
        body = resp.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if not isinstance(detail, str):
        return None
    code = body.get("code")
    if not isinstance(code, str) or not code:
        code = resp.headers.get(_DENIAL_CODE_HEADER, "")
    return detail, code


def raise_for_refusal(resp: httpx.Response) -> None:
    """Raise `httpx.HTTPStatusError` for a refused response, quoting the server.

    Returns without raising on a response httpx itself considers successful, so
    a caller needs no status check of its own.

    The exception TYPE is preserved on purpose: nine sites in cli/app.py catch
    `httpx.HTTPStatusError` and read `.response` (status code, body), so the
    request and the response are attached exactly as httpx attaches them. Only
    the message changes, to carry the server's own sentence and code:

        Client error '403 Forbidden' for url 'http://.../api/projects/x'
        not allowed here (code: not_authenticated)

    A body that is not JSON, not an object, or carries no string `detail` keeps
    httpx's wording verbatim -- and nothing but `HTTPStatusError` escapes, which
    is why the body read is guarded rather than allowed to raise.
    """
    if resp.is_success:
        return
    detail = _refusal_sentence(resp)
    if detail is None:
        message = _httpx_status_message(resp)
    else:
        sentence, code = detail
        message = _status_headline(resp) + "\n" + sentence
        if code:
            message += f" (code: {code})"
    raise httpx.HTTPStatusError(message, request=resp.request, response=resp)


class APIClient:
    """Sync HTTP client for the AItelier REST API."""

    def __init__(self, base_url: str = _DEFAULT_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url, timeout=10.0, headers=_auth_headers(),
        )

    def create_task(self, project_id: str, prompt: str,
                    project_brief: Optional[str] = None) -> dict:
        body = {"project_id": project_id, "prompt": prompt}
        if project_brief:
            body["project_brief"] = project_brief
        resp = self._client.post("/api/tasks", json=body)
        raise_for_refusal(resp)
        return resp.json()

    def get_task(self, task_id: int) -> dict:
        resp = self._client.get(f"/api/tasks/{task_id}")
        raise_for_refusal(resp)
        return resp.json()

    def list_tasks(self, limit: int = 50) -> list[dict]:
        resp = self._client.get("/api/tasks", params={"limit": limit})
        raise_for_refusal(resp)
        return resp.json()

    def get_step_output(self, task_id: int, step_id: str) -> dict:
        resp = self._client.get(f"/api/tasks/{task_id}/steps/{step_id}/output")
        raise_for_refusal(resp)
        return resp.json()

    def rollback(self, task_id: int, commit_hash: str) -> dict:
        resp = self._client.post(
            f"/api/tasks/{task_id}/rollback",
            json={"commit_hash": commit_hash},
        )
        raise_for_refusal(resp)
        return resp.json()

    def retry_task(self, task_id: int) -> dict:
        """Retry a failed task — resets to first task step and PENDING status."""
        resp = self._client.post(f"/api/tasks/{task_id}/retry")
        raise_for_refusal(resp)
        return resp.json()

    def retry_project(self, project_id: str) -> dict:
        """Retry a failed project — resets project and ALL tasks (failed or pending)."""
        resp = self._client.post(f"/api/projects/{project_id}/retry")
        raise_for_refusal(resp)
        return resp.json()


    def health(self) -> bool:
        try:
            resp = self._client.get("/health", timeout=3.0)
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    def list_projects(self) -> list[dict]:
        resp = self._client.get("/api/projects")
        raise_for_refusal(resp)
        return resp.json()

    # ── Config-run generic surface ─────────────────────────────────

    def list_configs(self) -> list[dict]:
        """All registered configs with manifests."""
        resp = self._client.get("/api/configs")
        raise_for_refusal(resp)
        return resp.json().get("configs", [])

    def get_config_manifest(self, config_name: str) -> dict:
        resp = self._client.get(f"/api/configs/{config_name}/manifest")
        raise_for_refusal(resp)
        return resp.json()

    def list_all_runs(self, config_name: str = None, status: str = None) -> list[dict]:
        """All config runs (optionally filtered)."""
        params = {}
        if config_name:
            params["config_name"] = config_name
        if status:
            params["status"] = status
        resp = self._client.get("/api/runs", params=params)
        raise_for_refusal(resp)
        return resp.json().get("runs", [])

    def start_config_run(self, config_name: str, seed_text: str = None,
                         name: str = None) -> dict:
        body = {"config_name": config_name}
        if seed_text is not None:
            body["seed_text"] = seed_text
        if name:
            body["name"] = name
        resp = self._client.post("/api/runs", json=body)
        raise_for_refusal(resp)
        return resp.json()

    def create_project(self, project_id: str, name: str = None,
                       repo_type: str = "new", repo_path: str = None,
                       repo_url: str = None) -> dict:
        body = {"project_id": project_id, "repo_type": repo_type}
        if name:
            body["name"] = name
        if repo_path:
            body["repo_path"] = repo_path
        if repo_url:
            body["repo_url"] = repo_url
        resp = self._client.post("/api/projects", json=body)
        raise_for_refusal(resp)
        return resp.json()

    def get_project(self, project_id: str) -> dict:
        resp = self._client.get(f"/api/projects/{project_id}")
        raise_for_refusal(resp)
        return resp.json()

    def submit_project(self, project_id: str, brief: dict, name: str = None,
                       repo_type: str = "new", repo_path: str = None,
                       repo_url: str = None) -> dict:
        """Submit a new project with brief from meta conversation. Seeds goals, sets planning, wakes scheduler."""
        body = {"project_id": project_id, "brief": brief, "repo_type": repo_type}
        if name:
            body["name"] = name
        if repo_path:
            body["repo_path"] = repo_path
        if repo_url:
            body["repo_url"] = repo_url
        resp = self._client.post("/api/projects/submit", json=body, timeout=30.0)
        raise_for_refusal(resp)
        return resp.json()

    def delete_project(self, project_id: str) -> dict:
        resp = self._client.delete(f"/api/projects/{project_id}")
        raise_for_refusal(resp)
        return resp.json()

    def update_project(self, project_id: str, **kwargs) -> dict:
        """Partially update a project (name, brief, priority, status)."""
        resp = self._client.patch(f"/api/projects/{project_id}", params=kwargs)
        raise_for_refusal(resp)
        return resp.json()

    def list_tasks_by_project(self, project_id: str) -> list[dict]:
        """List all tasks for a project."""
        resp = self._client.get(f"/api/projects/{project_id}/tasks")
        raise_for_refusal(resp)
        return resp.json()

    # ── Settings API ──

    def get_scheduler_settings(self) -> dict:
        """Get current scheduler settings."""
        resp = self._client.get("/api/settings/scheduler")
        raise_for_refusal(resp)
        return resp.json()

    def update_scheduler_settings(self, scheduler_type: str,
                                  scheduler_interval: int = None,
                                  scheduler_cron: str = None) -> dict:
        """Update scheduler settings (hot-reloads the running scheduler)."""
        body = {"scheduler_type": scheduler_type}
        if scheduler_interval is not None:
            body["scheduler_interval"] = scheduler_interval
        if scheduler_cron is not None:
            body["scheduler_cron"] = scheduler_cron
        resp = self._client.post("/api/settings/scheduler", json=body)
        raise_for_refusal(resp)
        return resp.json()

    # ── Meta Conversation API ──

    def detect_intent(self, prompt: str) -> dict:
        """Detect whether prompt is about a new project or existing code."""
        resp = self._client.post("/api/meta/detect-intent", json={"prompt": prompt}, timeout=30.0)
        raise_for_refusal(resp)
        return resp.json()

    def assess_prompt(self, prompt: str, history: list[dict] = None) -> dict:
        """Unified assessment: validate prompt, detect intent, gather brief."""
        resp = self._client.post("/api/meta/assess",
            json={"prompt": prompt, "history": history or []}, timeout=120.0)
        raise_for_refusal(resp)
        return resp.json()

    def meta_start(self, prompt: str, project_id: str) -> dict:
        """Start a meta conversation."""
        resp = self._client.post("/api/meta/start", json={"prompt": prompt, "project_id": project_id}, timeout=120.0)
        raise_for_refusal(resp)
        return resp.json()

    def meta_next(self, project_id: str, answer: str, history: list[dict]) -> dict:
        """Send answer + history. Server is stateless."""
        resp = self._client.post("/api/meta/next", json={"project_id": project_id, "answer": answer, "history": history}, timeout=120.0)
        raise_for_refusal(resp)
        return resp.json()

    def meta_force(self, project_id: str, history: list[dict]) -> dict:
        """Force brief generation."""
        resp = self._client.post("/api/meta/force", json={"project_id": project_id, "history": history}, timeout=120.0)
        raise_for_refusal(resp)
        return resp.json()

    def revise_brief(self, project_id: str, brief: dict, feedback: str) -> dict:
        """Revise an existing brief based on user feedback."""
        resp = self._client.post("/api/meta/revise-brief",
                                 json={"project_id": project_id, "project_brief": brief, "feedback": feedback},
                                 timeout=120.0)
        raise_for_refusal(resp)
        return resp.json()

    # ── Task Meta API ──

    def task_meta_start(self, project_id: str, prompt: str) -> dict:
        """Start a task-scoped meta conversation."""
        resp = self._client.post("/api/meta/task/start", json={"project_id": project_id, "prompt": prompt}, timeout=120.0)
        raise_for_refusal(resp)
        return resp.json()

    def task_meta_next(self, task_id: int, answer: str, history: list[dict]) -> dict:
        """Continue task meta conversation."""
        resp = self._client.post("/api/meta/task/next", json={"task_id": task_id, "answer": answer, "history": history}, timeout=120.0)
        raise_for_refusal(resp)
        return resp.json()

    def task_meta_force(self, task_id: int, history: list[dict]) -> dict:
        """Force task meta completion."""
        resp = self._client.post("/api/meta/task/force", json={"task_id": task_id, "history": history}, timeout=120.0)
        raise_for_refusal(resp)
        return resp.json()

    # ── Checkpoint API ──

    def get_pending_checkpoint(self, project_id: str) -> Optional[dict]:
        """Get the current pending checkpoint for a project. Returns None if not waiting."""
        resp = self._client.get(f"/api/meta/{project_id}/checkpoint")
        if resp.status_code == 404:
            return None
        raise_for_refusal(resp)
        data = resp.json()
        if not data.get("checkpoint"):
            return None
        return data

    def approve_checkpoint(self, project_id: str, checkpoint: dict) -> dict:
        """Approve a checkpoint and resume the pipeline.

        Carries no feedback: skillflow's approve_checkpoint(run_id) has no
        feedback parameter, so the endpoint refuses an approval that sends one.
        Use reject_checkpoint to get text to the step.
        """
        resp = self._client.post(
            f"/api/meta/{project_id}/checkpoint/approve",
            json={"project_id": project_id, "checkpoint": checkpoint["checkpoint"],
                  "run_id": checkpoint["run_id"],
                  "expected_checkpoint": checkpoint.get("expected_checkpoint"),
                  "decision_id": checkpoint.get("decision_id", ""),
                  "rescue_failed": checkpoint.get("failed_rescue", False)},
            timeout=30.0,
        )
        raise_for_refusal(resp)
        return resp.json()

    def reject_checkpoint(self, project_id: str, checkpoint: str, feedback: str, *, run_id: str = "") -> dict:
        """Reject a checkpoint with feedback. Pipeline will re-run the step."""
        resp = self._client.post(
            f"/api/meta/{project_id}/checkpoint/reject",
            json={"project_id": project_id, "checkpoint": checkpoint, "feedback": feedback, "run_id": run_id},
            timeout=30.0,
        )
        raise_for_refusal(resp)
        return resp.json()

    def stream_events(self, task_id: str) -> queue.Queue:
        """
        Start SSE consumer in a background thread.
        Returns a queue.Queue that yields parsed event dicts.
        Queue receives None as sentinel when the stream ends.
        """
        event_queue: queue.Queue = queue.Queue()
        base_url = self.base_url

        def _sse_consumer():
            async def _consume():
                # The task-log stream is a private execution read now, so this
                # reader must present the same admin token every other call
                # does -- without it the guard refuses and the CLI's log tail
                # goes silently empty.
                async with httpx.AsyncClient(base_url=base_url,
                                             headers=_auth_headers()) as client:
                    async with client.stream(
                        "GET", f"/api/tasks/{task_id}/stream"
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            raw = json.loads(line[6:])
                            log_str = raw.get("log", "")
                            if log_str == "__END__":
                                event_queue.put({"type": "__END__"})
                                return
                            try:
                                event = json.loads(log_str)
                            except json.JSONDecodeError:
                                event = {"type": "raw_log", "data": {"text": log_str}}
                            event_queue.put(event)
                event_queue.put(None)  # sentinel: stream closed

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_consume())
            except Exception as e:
                event_queue.put({"type": "stream_error", "error": str(e)})
            finally:
                loop.close()

        t = threading.Thread(target=_sse_consumer, daemon=True)
        t.start()
        return event_queue

    # ── Run / Trace API ──

    def list_runs(self, project_id: str, status: str = None) -> dict:
        """List all pipeline runs for a project."""
        params = {}
        if status:
            params["status"] = status
        resp = self._client.get(f"/api/projects/{project_id}/runs", params=params)
        raise_for_refusal(resp)
        return resp.json()

    def get_run(self, run_id: str) -> dict:
        """Get a single run with all step statuses."""
        resp = self._client.get(f"/api/runs/{run_id}")
        raise_for_refusal(resp)
        return resp.json()

    def get_run_trace(self, run_id: str, step_instance_id: int = None,
                      category: str = None, limit: int = 100) -> dict:
        """Read execution traces for a run. Optionally filter by step_instance_id and/or category."""
        params = {"limit": limit}
        if step_instance_id is not None:
            params["step_instance_id"] = step_instance_id
        if category:
            params["category"] = category
        resp = self._client.get(f"/api/runs/{run_id}/trace", params=params)
        raise_for_refusal(resp)
        return resp.json()

    # ── Workspace browsing ──

    def workspace_tree(self, project_id: str, subdir: str = None) -> dict:
        """Get directory tree of a project's DPS workspace."""
        params = {}
        if subdir:
            params["subdir"] = subdir
        resp = self._client.get(f"/api/projects/{project_id}/workspace/tree", params=params)
        raise_for_refusal(resp)
        return resp.json()

    def workspace_file(self, project_id: str, path: str) -> dict:
        """Read a file from the project workspace."""
        resp = self._client.get(f"/api/projects/{project_id}/workspace/file", params={"path": path})
        raise_for_refusal(resp)
        return resp.json()
