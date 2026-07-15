"""restage — copy prior outputs into this step's directory for a human checkpoint.

Enables a "maker → reviewer → human checkpoint" ordering. skillflow checkpoints
are UNCONDITIONAL (a checkpoint step always pauses on completion), so "review
first, human only if Red passes" needs a separate checkpoint step reached only
on the reviewer's pass. But the checkpoint modal surfaces the CHECKPOINT step's
OWN dir (api/meta_routers._read_step_output → {workspace}/{graph}/{step}/), so a
bare gate would show "no files to review". This tool re-materializes the named
sources into its staging dir (→ promoted → surfaced) so the human reviews the
real artifact. Deterministic, no LLM; generic across pipelines.

Two source kinds (either/both):
  from_repo   repo-relative paths (files or dirs) copied from the code repo
              (project_root). Use this for a maker that repo_applies its output:
              the repo holds the CUMULATIVE result, whereas the maker's step dir
              holds only the LAST iteration's files (a surgical-edit revision
              writes just the changed files) — so the step dir would show a
              partial artifact.
  from_steps  step ids whose promoted step dir is copied (e.g. a reviewer's
              review_verdict.json).

Skips skillflow bookkeeping (``_snapshot.json`` etc., ``instruction*`` and
``user_rejection_history.json`` — the latter is read per-step and must not be
inherited from a source step) and any ``.git`` tree under a repo path.
"""

from pathlib import Path

_SKIP_PREFIXES = ("_", "instruction")
_SKIP_NAMES = {"user_rejection_history.json"}


def _copy_tree(src: Path, dest_root: Path, rel_prefix: Path, copied: list[str]) -> None:
    """Copy every non-bookkeeping file under src into dest_root/rel_prefix."""
    for item in sorted(src.rglob("*")):
        if not item.is_file():
            continue
        if ".git" in item.relative_to(src).parts:
            continue
        if item.name in _SKIP_NAMES or item.name.startswith(_SKIP_PREFIXES):
            continue
        rel = rel_prefix / item.relative_to(src)
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.read_bytes())
        copied.append(rel.as_posix())


def restage(*, workspace_root: str = "", project_root: str = "", config_name: str = "",
            out_dir: str = "", from_repo=None, from_steps=None, from_step: str = "",
            **kwargs) -> dict:
    steps = [s for s in (list(from_steps) if from_steps else []) if s]
    if from_step:
        steps.append(from_step)
    repo_paths = [p for p in (list(from_repo) if from_repo else []) if p]
    if not steps and not repo_paths:
        raise ValueError("restage: no from_repo/from_steps/from_step given")
    if not out_dir:
        raise ValueError("restage: out_dir not resolved (expected $STEP_DIR)")

    dest = Path(out_dir)
    copied: list[str] = []
    missing: list[str] = []

    for rp in repo_paths:
        if not project_root:
            raise ValueError("restage: project_root not injected (needed for from_repo)")
        src = Path(project_root) / rp
        if src.is_dir():
            _copy_tree(src, dest, Path(rp), copied)
        elif src.is_file():
            target = dest / rp
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(src.read_bytes())
            copied.append(rp)
        else:
            missing.append(f"repo:{rp}")

    if steps and not (workspace_root and config_name):
        raise ValueError("restage: workspace_root/config_name not injected (needed for from_steps)")
    base = Path(workspace_root) / config_name if (workspace_root and config_name) else None
    for s in steps:
        src = base / s
        if not src.is_dir():
            missing.append(f"step:{s}")
            continue
        _copy_tree(src, dest, Path("."), copied)

    if not copied:
        raise ValueError(
            f"restage: nothing copied (from_repo={repo_paths}, from_steps={steps}; "
            f"missing: {missing}) — the checkpoint would show an empty modal")

    return {"restaged": True, "from_repo": repo_paths, "from_steps": steps,
            "files": copied, "count": len(copied), "missing": missing}
