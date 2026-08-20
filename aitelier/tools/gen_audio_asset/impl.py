"""gen_audio_asset — generate a sound asset and write it into the repo.

The audio half of the asset channel, split by what the two jobs actually need:

  kind="sfx"  -> gen_sfx, a procedural sfxr-style synthesiser. Game effects are
                 10-200ms transients that must be exact, instant and repeatable;
                 a diffusion model is the wrong instrument for them.
  kind="bgm"  -> generate_music (Stable Audio). Fine for a background bed, and
                 the ONLY option for one, but it caps at 47s, is mono, and gives
                 no loop point — so a seamless loop is not something you get here.

Writes into the repo working tree; a later repo_apply commits it.
"""

from pathlib import Path

from aitelier.mcp_client import MCPError, call_tool, fetch, urls_in

_PRESETS = ("jump", "coin", "hit", "explosion", "powerup", "laser", "select", "hurt")
_MAX_BGM_SECONDS = 47


def gen_audio_asset(*, dest: str = "", kind: str = "sfx", preset: str = "",
                    prompt: str = "", seed: int | None = None, duration: float = 20.0,
                    project_root: str = "", workspace_root: str = "",
                    step_tmp_dir: str = "", out_dir: str = "",
                    **kwargs) -> dict:
    """Generate one audio asset into the repo. Returns {written, source_url}."""
    repo = _target_root(step_tmp_dir, project_root, workspace_root)
    if repo is None:
        return {"written": [], "error": "no staging dir and no repo to write into"}
    if not dest:
        return {"written": [], "error": "dest is required"}

    if kind == "sfx":
        if preset not in _PRESETS:
            return {"written": [], "error": f"preset must be one of {list(_PRESETS)}"}
        tool, args = "gen_sfx", {"preset": preset}
    elif kind == "bgm":
        if not prompt:
            return {"written": [], "error": "kind='bgm' needs a prompt"}
        tool = "generate_music"
        args = {"prompt": prompt, "duration": min(float(duration), _MAX_BGM_SECONDS)}
    else:
        return {"written": [], "error": "kind must be 'sfx' or 'bgm'"}
    if seed is not None:
        args["seed"] = int(seed)

    try:
        reply = call_tool(tool, args)
        urls = urls_in(reply)
        if not urls:
            raise MCPError(f"{tool} returned no URL: {reply[:200]}")
        data = fetch(urls[0])
    except MCPError as e:
        return {"written": [], "error": str(e)}

    dst = (repo / dest).resolve()
    if not str(dst).startswith(str(repo)):        # keep writes inside the jail
        return {"written": [], "error": f"dest escapes the repo: {dest}"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    return {"written": [str(dst.relative_to(repo))], "source_url": urls[0]}


def _target_root(step_tmp_dir: str, project_root: str, workspace_root: str) -> Path | None:
    """Where a generated asset must be written.

    The STEP'S STAGING dir, whenever there is one. An agent step's delivery is
    reconciled against staging, so a binary written straight into the working
    tree looks like it landed and is then deleted again by that reconciliation
    as "a file this step never delivered" — which is exactly how the first batch
    of generated sprites disappeared, commit `t_impl delete ... 4 file(s)`.
    Falling back to the repo keeps the tool usable from a tool node (like
    scaffold), where no staging dir exists."""
    for cand in (step_tmp_dir, project_root, workspace_root):
        if cand and Path(cand).is_dir():
            return Path(cand).resolve()
    return None
