# cli/client.py
# AItelier HTTP API client + SSE consumer thread.

import asyncio
import json
import os
import queue
import threading
from typing import Optional

import httpx

_DEFAULT_URL = f"http://localhost:{os.environ.get('AITELIER_PORT', '4444')}"


class APIClient:
    """Sync HTTP client for the AItelier REST API."""

    def __init__(self, base_url: str = _DEFAULT_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(base_url=self.base_url, timeout=10.0)

    def create_task(self, project_id: str, prompt: str,
                    project_brief: Optional[str] = None) -> dict:
        body = {"project_id": project_id, "prompt": prompt}
        if project_brief:
            body["project_brief"] = project_brief
        resp = self._client.post("/api/tasks", json=body)
        resp.raise_for_status()
        return resp.json()

    def get_task(self, task_id: int) -> dict:
        resp = self._client.get(f"/api/tasks/{task_id}")
        resp.raise_for_status()
        return resp.json()

    def list_tasks(self, limit: int = 50) -> list[dict]:
        resp = self._client.get("/api/tasks", params={"limit": limit})
        resp.raise_for_status()
        return resp.json()

    def get_step_output(self, task_id: int, step_id: str) -> dict:
        resp = self._client.get(f"/api/tasks/{task_id}/steps/{step_id}/output")
        resp.raise_for_status()
        return resp.json()

    def rollback(self, task_id: int, commit_hash: str) -> dict:
        resp = self._client.post(
            f"/api/tasks/{task_id}/rollback",
            json={"commit_hash": commit_hash},
        )
        resp.raise_for_status()
        return resp.json()

    def retry_task(self, task_id: int) -> dict:
        """Retry a failed task — resets to first task step and PENDING status."""
        resp = self._client.post(f"/api/tasks/{task_id}/retry")
        resp.raise_for_status()
        return resp.json()

    def retry_project(self, project_id: str) -> dict:
        """Retry a failed project — resets project and ALL tasks (failed or pending)."""
        resp = self._client.post(f"/api/projects/{project_id}/retry")
        resp.raise_for_status()
        return resp.json()


    def health(self) -> bool:
        try:
            resp = self._client.get("/health", timeout=3.0)
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False

    def list_projects(self) -> list[dict]:
        resp = self._client.get("/api/projects")
        resp.raise_for_status()
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
        resp.raise_for_status()
        return resp.json()

    def get_project(self, project_id: str) -> dict:
        resp = self._client.get(f"/api/projects/{project_id}")
        resp.raise_for_status()
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
        resp.raise_for_status()
        return resp.json()

    def submit_task(self, project_id: str, prompt: str, task_spec: dict = None) -> dict:
        """Submit a task to an existing project. Wakes scheduler."""
        body = {"project_id": project_id, "prompt": prompt}
        if task_spec:
            body["task_spec"] = task_spec
        resp = self._client.post("/api/projects/submit-task", json=body, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def delete_project(self, project_id: str) -> dict:
        resp = self._client.delete(f"/api/projects/{project_id}")
        resp.raise_for_status()
        return resp.json()

    def refresh_planning(self, project_id: str) -> dict:
        """Re-queue P1.5 (Researcher) and P2 (Architect) for re-execution."""
        resp = self._client.post(f"/api/projects/{project_id}/refresh-planning")
        resp.raise_for_status()
        return resp.json()

    def update_project(self, project_id: str, **kwargs) -> dict:
        """Partially update a project (name, brief, priority, status)."""
        resp = self._client.patch(f"/api/projects/{project_id}", params=kwargs)
        resp.raise_for_status()
        return resp.json()

    def list_tasks_by_project(self, project_id: str) -> list[dict]:
        """List all tasks for a project."""
        resp = self._client.get(f"/api/projects/{project_id}/tasks")
        resp.raise_for_status()
        return resp.json()

    # ── Settings API ──

    def get_scheduler_settings(self) -> dict:
        """Get current scheduler settings."""
        resp = self._client.get("/api/settings/scheduler")
        resp.raise_for_status()
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
        resp.raise_for_status()
        return resp.json()

    # ── Meta Conversation API ──

    def detect_intent(self, prompt: str) -> dict:
        """Detect whether prompt is about a new project or existing code."""
        resp = self._client.post("/api/meta/detect-intent", json={"prompt": prompt}, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    def assess_prompt(self, prompt: str, history: list[dict] = None) -> dict:
        """Unified assessment: validate prompt, detect intent, gather brief."""
        resp = self._client.post("/api/meta/assess",
            json={"prompt": prompt, "history": history or []}, timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    def meta_start(self, prompt: str, project_id: str) -> dict:
        """Start a meta conversation."""
        resp = self._client.post("/api/meta/start", json={"prompt": prompt, "project_id": project_id}, timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    def meta_next(self, project_id: str, answer: str, history: list[dict]) -> dict:
        """Send answer + history. Server is stateless."""
        resp = self._client.post("/api/meta/next", json={"project_id": project_id, "answer": answer, "history": history}, timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    def meta_force(self, project_id: str, history: list[dict]) -> dict:
        """Force brief generation."""
        resp = self._client.post("/api/meta/force", json={"project_id": project_id, "history": history}, timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    def revise_brief(self, project_id: str, brief: dict, feedback: str) -> dict:
        """Revise an existing brief based on user feedback."""
        resp = self._client.post("/api/meta/revise-brief",
                                 json={"project_id": project_id, "project_brief": brief, "feedback": feedback},
                                 timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    # ── Task Meta API ──

    def task_meta_start(self, project_id: str, prompt: str) -> dict:
        """Start a task-scoped meta conversation."""
        resp = self._client.post("/api/meta/task/start", json={"project_id": project_id, "prompt": prompt}, timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    def task_meta_next(self, task_id: int, answer: str, history: list[dict]) -> dict:
        """Continue task meta conversation."""
        resp = self._client.post("/api/meta/task/next", json={"task_id": task_id, "answer": answer, "history": history}, timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    def task_meta_force(self, task_id: int, history: list[dict]) -> dict:
        """Force task meta completion."""
        resp = self._client.post("/api/meta/task/force", json={"task_id": task_id, "history": history}, timeout=120.0)
        resp.raise_for_status()
        return resp.json()

    # ── Checkpoint API ──

    def get_pending_checkpoint(self, project_id: str) -> Optional[dict]:
        """Get the current pending checkpoint for a project. Returns None if not waiting."""
        resp = self._client.get(f"/api/meta/{project_id}/checkpoint")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data.get("checkpoint"):
            return None
        return data

    def approve_checkpoint(self, project_id: str, checkpoint: str, feedback: str = "") -> dict:
        """Approve a checkpoint and resume the pipeline."""
        resp = self._client.post(
            f"/api/meta/{project_id}/checkpoint/approve",
            json={"project_id": project_id, "checkpoint": checkpoint, "feedback": feedback},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()

    def reject_checkpoint(self, project_id: str, checkpoint: str, feedback: str) -> dict:
        """Reject a checkpoint with feedback. Pipeline will re-run the step."""
        resp = self._client.post(
            f"/api/meta/{project_id}/checkpoint/reject",
            json={"project_id": project_id, "checkpoint": checkpoint, "feedback": feedback},
            timeout=30.0,
        )
        resp.raise_for_status()
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
                async with httpx.AsyncClient(base_url=base_url) as client:
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
        resp.raise_for_status()
        return resp.json()

    def get_run(self, run_id: str) -> dict:
        """Get a single run with all step statuses."""
        resp = self._client.get(f"/api/runs/{run_id}")
        resp.raise_for_status()
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
        resp.raise_for_status()
        return resp.json()

    # ── Workspace browsing ──

    def workspace_tree(self, project_id: str, subdir: str = None) -> dict:
        """Get directory tree of a project's DPS workspace."""
        params = {}
        if subdir:
            params["subdir"] = subdir
        resp = self._client.get(f"/api/projects/{project_id}/workspace/tree", params=params)
        resp.raise_for_status()
        return resp.json()

    def workspace_file(self, project_id: str, path: str) -> dict:
        """Read a file from the project workspace."""
        resp = self._client.get(f"/api/projects/{project_id}/workspace/file", params={"path": path})
        resp.raise_for_status()
        return resp.json()
