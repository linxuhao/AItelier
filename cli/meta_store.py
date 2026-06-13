# cli/meta_store.py
# File-based persistence for meta conversation state.
# Stores temp JSON files in ~/.AItelier/meta/ so conversations survive CLI restarts.

import json
import time
from pathlib import Path

_META_DIR = Path.home() / ".AItelier" / "meta"

# TTL for project/task meta files (24 hours)
_META_TTL_SECONDS = 24 * 60 * 60


def _ensure_dir():
    _META_DIR.mkdir(parents=True, exist_ok=True)


def _project_path(project_id: str) -> Path:
    return _META_DIR / f"{project_id}_project.json"


def _task_path(project_id: str, task_id: int) -> Path:
    return _META_DIR / f"{project_id}_task_{task_id}.json"


# ── Project meta ──

def save_project_meta(project_id: str, state: dict):
    """Save project meta conversation state."""
    _ensure_dir()
    state["type"] = "project"
    state["saved_at"] = time.time()
    _project_path(project_id).write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_project_meta(project_id: str) -> dict | None:
    """Load project meta conversation state. Returns None if not found or expired (>24h)."""
    p = _project_path(project_id)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            saved_at = data.get("saved_at", 0)
            if saved_at and time.time() - saved_at > _META_TTL_SECONDS:
                clear_project_meta(project_id)
                return None
            return data
        except json.JSONDecodeError:
            return None
    return None


def clear_project_meta(project_id: str):
    """Delete project meta conversation file."""
    p = _project_path(project_id)
    if p.exists():
        p.unlink()


# ── Task meta ──

def save_task_meta(project_id: str, task_id: int, state: dict):
    """Save task meta conversation state."""
    _ensure_dir()
    state["type"] = "task"
    state["project_id"] = project_id
    state["task_id"] = task_id
    state["saved_at"] = time.time()
    _task_path(project_id, task_id).write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_task_meta(project_id: str, task_id: int) -> dict | None:
    """Load task meta conversation state. Returns None if not found or expired (>24h)."""
    p = _task_path(project_id, task_id)
    if p.exists():
        try:
            data = json.loads(p.read_text())
            saved_at = data.get("saved_at", 0)
            if saved_at and time.time() - saved_at > _META_TTL_SECONDS:
                clear_task_meta(project_id, task_id)
                return None
            return data
        except json.JSONDecodeError:
            return None
    return None


def clear_task_meta(project_id: str, task_id: int):
    """Delete task meta conversation file."""
    p = _task_path(project_id, task_id)
    if p.exists():
        p.unlink()


def list_pending_task_metas(project_id: str) -> list[dict]:
    """List all interrupted task meta conversations for a project."""
    _ensure_dir()
    results = []
    for p in _META_DIR.glob(f"{project_id}_task_*.json"):
        try:
            data = json.loads(p.read_text())
            if data.get("status") == "asking":
                results.append(data)
        except (json.JSONDecodeError, KeyError):
            continue
    return results


# ── Pre-project assessment ──

_ASSESSMENT_PATH = _META_DIR / "_assessment.json"


def save_assessment(state: dict):
    """Save in-progress assessment conversation (before project exists)."""
    _ensure_dir()
    state["type"] = "assessment"
    state["saved_at"] = time.time()
    _ASSESSMENT_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# Assessments older than 24 hours are considered stale
_ASSESSMENT_TTL_SECONDS = 24 * 60 * 60


def load_assessment() -> dict | None:
    """Load pending assessment. Returns None if not found or expired (>24h old)."""
    if _ASSESSMENT_PATH.exists():
        try:
            data = json.loads(_ASSESSMENT_PATH.read_text())
            saved_at = data.get("saved_at", 0)
            if time.time() - saved_at > _ASSESSMENT_TTL_SECONDS:
                clear_assessment()
                return None
            return data
        except json.JSONDecodeError:
            return None
    return None


def clear_assessment():
    """Delete assessment file."""
    if _ASSESSMENT_PATH.exists():
        _ASSESSMENT_PATH.unlink()
