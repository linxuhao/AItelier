"""Task-card write authority for code-producing loop items.

The task card is structured execution context. It is never recovered from the
rendered prompt. A loop item with a missing or invalid card is represented by a
deny-all scope; a non-loop code step has no per-card scope and keeps its own
isolated-worktree authority.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

_GENERIC = frozenset({"create", "edit", "write"})
_SLOT_PREFIXES = ("create_", "edit_", "write_", "append_")
_PATH_TOOLS = {
    "repo_remove_file": ("name",),
    "gen_image_asset": ("dest",),
    "gen_audio_asset": ("dest",),
}


def normalize_path(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (not raw or path.is_absolute() or ".." in path.parts
            or (path.parts and path.parts[0] in (".", ".git"))):
        raise ValueError(f"unsafe repo-relative path: {value!r}")
    return path.as_posix()


@dataclass(frozen=True)
class WriteScope:
    task: str
    owns: tuple[str, ...]
    shared_hotspots: tuple[str, ...] = ()
    policy: str = "task-card"

    @classmethod
    def from_card(cls, card: object, task: str) -> "WriteScope | None":
        if not isinstance(card, dict):
            return None
        owns = card.get("owns")
        if not isinstance(owns, list) or not owns:
            return None
        hotspots = card.get("shared_hotspots", [])
        if not isinstance(hotspots, list):
            return None
        try:
            return cls(str(task), tuple(normalize_path(p) for p in owns),
                       tuple(normalize_path(p) for p in hotspots))
        except ValueError:
            return None

    @classmethod
    def deny_all(cls, task: str, policy: str = "missing-task-card-deny") -> "WriteScope":
        return cls(str(task or "unknown-task"), (), (), policy)

    def authorizes(self, value: object) -> bool:
        try:
            requested = normalize_path(value)
        except ValueError:
            return False
        if requested in self.shared_hotspots:
            return True
        for owned in self.owns:
            if any(mark in owned for mark in "*?["):
                if PurePosixPath(requested).match(owned):
                    return True
            elif requested == owned or requested.startswith(owned.rstrip("/") + "/"):
                return True
        return False

    def allowed(self) -> dict:
        return {"owns": list(self.owns), "shared_hotspots": list(self.shared_hotspots)}

    def refusal(self, tool: str, path: object) -> dict:
        requested = str(path if path not in (None, "") else "(path unavailable)")
        return {
            "error": (f"{tool}: task {self.task!r} may not mutate {requested!r}; "
                      f"allowed scope is owns={list(self.owns)!r}, "
                      f"shared_hotspots={list(self.shared_hotspots)!r}"),
            "scope_violation": True,
            "task": self.task,
            "requested_path": requested,
            "allowed_scope": self.allowed(),
            "tool": tool,
            "policy": self.policy,
        }


def task_item(resolved_context: object) -> str | None:
    if not isinstance(resolved_context, dict):
        return None
    for key in ("[current_task]", "[task]", "[loop_item]"):
        value = resolved_context.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def card_from_context(resolved_context: object, task: str) -> dict | None:
    if not isinstance(resolved_context, dict) or not task:
        return None
    suffix = f"tasks/{task}.json"
    for label, value in resolved_context.items():
        if not isinstance(label, str) or not label.endswith(suffix):
            continue
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                card = json.loads(value)
            except ValueError:
                return None
            return card if isinstance(card, dict) else None
    return None


def scope_from_context(resolved_context: object) -> WriteScope | None:
    """Return None only for a non-loop step; loop items fail closed."""
    task = task_item(resolved_context)
    if task is None:
        return None
    return (WriteScope.from_card(card_from_context(resolved_context, task), task)
            or WriteScope.deny_all(task))


def scope_from_workspace(workspace_root: str, config_name: str,
                         task: str) -> WriteScope:
    """Resolve an on-deliver hook card, denying when a named task has none."""
    if not task or Path(task).name != task or "/" in task or "\\" in task:
        return WriteScope.deny_all(task, "invalid-task-id-deny")
    card_path = Path(workspace_root) / config_name / "3" / "tasks" / f"{task}.json"
    try:
        card = json.loads(card_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        card = None
    return WriteScope.from_card(card, task) or WriteScope.deny_all(task)


def is_repo_mutator(tool: str) -> bool:
    return tool in _GENERIC or tool.startswith(_SLOT_PREFIXES) or tool in _PATH_TOOLS


def mutation_paths(tool: str, params: object, output_fixed: object = None) -> list[object]:
    """Return every repo path named by one mutator call.

    A path-less recognized mutator returns [None] so a scoped task refuses it
    instead of passing a mutation whose destination was not proven.
    """
    params = params if isinstance(params, dict) else {}
    if tool in _GENERIC:
        for key in ("file", "file_path", "filename", "path"):
            if params.get(key) not in (None, ""):
                return [params[key]]
        return [None]
    if tool in _PATH_TOOLS:
        found = [params[k] for k in _PATH_TOOLS[tool] if params.get(k) not in (None, "")]
        return found or [None]
    if tool.startswith(_SLOT_PREFIXES):
        slot = next((tool[len(prefix):] for prefix in _SLOT_PREFIXES
                     if tool.startswith(prefix)), "")
        entry = (output_fixed or {}).get(slot) if isinstance(output_fixed, dict) else None
        pattern = entry.get("file") if isinstance(entry, dict) else None
        if not pattern:
            return [None]
        if "*" in pattern:
            ident = params.get("id")
            return [pattern.replace("*", str(ident), 1)] if ident not in (None, "") else [None]
        return [pattern]
    return []


def out_of_scope(paths: Iterable[object], scope: WriteScope) -> list[str]:
    return [str(path) for path in paths if not scope.authorizes(path)]
