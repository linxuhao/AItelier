"""restage — copy prior steps' promoted outputs into this step's directory.

Enables a "maker → reviewer → human checkpoint" ordering. skillflow checkpoints
are UNCONDITIONAL (a checkpoint step always pauses on completion — core.py
`if node.checkpoint: return None`), so "review first, human only if Red passes"
cannot live on the maker or reviewer step; it needs a separate checkpoint step
reached only on the reviewer's pass. But the checkpoint modal surfaces the
CHECKPOINT step's own dir (api/meta_routers._read_step_output reads
{workspace}/{graph}/{step}/), so a bare gate would show "no files to review".

This tool, run on that checkpoint step, copies the named steps' promoted outputs
into its staging dir (→ promoted → surfaced), so the human reviews the real
artifact (e.g. the maker's files + the reviewer's verdict). Deterministic, no
LLM; generic across pipelines.

Skips skillflow bookkeeping (``_snapshot.json`` etc., ``instruction*``,
``user_rejection_history.json`` — the last is read separately per-step and must
not be inherited from a source step).
"""

from pathlib import Path

_SKIP_PREFIXES = ("_", "instruction")
_SKIP_NAMES = {"user_rejection_history.json"}


def restage(*, workspace_root: str = "", config_name: str = "", out_dir: str = "",
            from_steps=None, from_step: str = "", **kwargs) -> dict:
    steps = [s for s in (list(from_steps) if from_steps else []) if s]
    if from_step:
        steps.append(from_step)
    if not steps:
        raise ValueError("restage: no from_steps/from_step given")
    if not out_dir:
        raise ValueError("restage: out_dir not resolved (expected $STEP_DIR)")
    if not workspace_root or not config_name:
        raise ValueError("restage: workspace_root/config_name not injected")

    dest = Path(out_dir)
    base = Path(workspace_root) / config_name
    copied: list[str] = []
    missing: list[str] = []
    for s in steps:
        src = base / s
        if not src.is_dir():
            missing.append(s)
            continue
        for item in sorted(src.rglob("*")):
            if not item.is_file():
                continue
            if item.name in _SKIP_NAMES or item.name.startswith(_SKIP_PREFIXES):
                continue
            rel = item.relative_to(src)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
            copied.append(str(rel))

    if not copied:
        raise ValueError(
            f"restage: nothing copied from {steps} under {base} "
            f"(missing dirs: {missing}) — the checkpoint would show an empty modal")

    return {"restaged": True, "from_steps": steps, "files": copied,
            "count": len(copied), "missing": missing}
