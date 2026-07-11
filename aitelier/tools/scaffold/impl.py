"""scaffold — drop an addon's static asset files into the project repo.

The mechanical (tool) channel of the addon system: an addon that needs a
guaranteed file in every project it applies to (e.g. a Godot `.gitignore`) ships
it under configs/addons/<addon>/assets/, and injects a scaffold tool step. Unlike
a prompt instruction, this doesn't depend on the LLM remembering — the file is
always there. Writes into the repo working tree (like knowledge_sync); a later
repo_apply commits it. Never clobbers an existing file.

Convention: an asset named `dot_<x>` is written as `.<x>` in the repo, so a
literal `.gitignore` in the addon's own asset dir doesn't act as a real ignore
inside the AItelier repo.
"""

from pathlib import Path

_CONFIGS = Path(__file__).resolve().parents[3] / "configs"


def scaffold(*, project_root: str = "", workspace_root: str = "", addon: str = "",
             out_dir: str = "", **kwargs) -> dict:
    repo = Path(project_root or workspace_root).resolve() if (project_root or workspace_root) else None
    if not repo or not repo.is_dir():
        return {"written": [], "reason": f"repo not found: {repo}", "addon": addon}
    assets = _CONFIGS / "addons" / addon / "assets"
    if not assets.is_dir():
        return {"written": [], "reason": f"no assets for addon '{addon}'", "addon": addon}

    written, merged, skipped = [], [], []
    for src in sorted(assets.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(assets)
        name = ("." + rel.name[4:]) if rel.name.startswith("dot_") else rel.name
        dst = repo / rel.parent / name
        try:
            asset_text = src.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped.append(str(rel))  # binary asset — not line-mergeable, skip
            continue
        if dst.exists():
            # An existing file is never clobbered. For line-additive files
            # (.gitignore) MERGE missing lines so the Godot `.godot/` lands even
            # atop a Python default; for anything else, skip (appending lines to a
            # structured config like export_presets.cfg would corrupt it).
            if not name.endswith("ignore"):
                skipped.append(str(dst.relative_to(repo)))
                continue
            existing = dst.read_text(encoding="utf-8")
            have = {ln.strip() for ln in existing.splitlines()}
            add = [ln for ln in asset_text.splitlines()
                   if ln.strip() and not ln.strip().startswith("#") and ln.strip() not in have]
            if add:
                sep = "" if existing.endswith("\n") else "\n"
                dst.write_text(existing + sep + "\n".join(add) + "\n", encoding="utf-8")
                merged.append(str(dst.relative_to(repo)))
            else:
                skipped.append(str(dst.relative_to(repo)))
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(asset_text, encoding="utf-8")
        written.append(str(dst.relative_to(repo)))
    return {"written": written, "merged": merged, "skipped": skipped, "addon": addon}
