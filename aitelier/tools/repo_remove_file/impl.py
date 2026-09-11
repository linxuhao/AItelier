"""Delete one code file directly in the run worktree; no deferred manifest."""
from pathlib import Path


def repo_remove_file(name: str, *, project_root: str = "", output_target: str = "",
                     run_id: str = "", step_id: str = "", **kwargs) -> dict:
    if output_target != "code":
        return {"error": "repo_remove_file requires output.target=code; artifact outputs "
                         "use their artifact delete tool. Legacy code staging is not supported."}
    if not project_root or not Path(project_root).is_absolute():
        return {"error": "repo_remove_file requires an injected absolute code root"}
    from skillflow.output_targets import code_path
    try:
        target = code_path(Path(project_root), name)
        if not target.is_file():
            return {"error": f"Code file does not exist: {name}"}
        target.unlink()
        return {"deleted": target.relative_to(Path(project_root).resolve()).as_posix(),
                "output_target": "code", "note": "Deleted from this run's worktree; validation/review still required."}
    except (ValueError, TypeError, OSError) as exc:
        return {"error": str(exc)}
