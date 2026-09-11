"""gen_audio_asset — generate a sound asset and write it into the repo.

The audio half of the asset channel, split by what the two jobs actually need:

  kind="sfx"  -> gen_sfx, a procedural sfxr-style synthesiser. Game effects are
                 10-200ms transients that must be exact, instant and repeatable;
                 a diffusion model is the wrong instrument for them.
  kind="bgm"  -> generate_music (Stable Audio). Fine for a background bed, and
                 the ONLY option for one, but it caps at 47s, is mono, and gives
                 no loop point — so a seamless loop is not something you get here.
  kind="voice" -> actor_tts against an actor cast once from `voice`, so a
                 character's lines keep one timbre across calls.

Writes into the engine-declared output directory. Code outputs go directly to
this run's worktree; artifact outputs remain artifacts. No code overlay or
promotion is involved.
"""

from pathlib import Path

from aitelier.mcp_client import MCPError, call_tool, fetch, urls_in

_PRESETS = ("jump", "coin", "hit", "explosion", "powerup", "laser", "select", "hurt")
_MAX_BGM_SECONDS = 47


def _ensure_actor(name: str, voice: str, seed) -> None:
    """Cast a voice once and keep it.

    Idempotent for the same reason casting a face is: the roster outlives the
    run, and re-casting would give the character a new voice that no longer
    matches the lines already sitting in the repo."""
    if name in call_tool("list_actors", {}):
        return
    if not voice:
        raise MCPError(f"{name!r} has no voice yet - pass `voice` to cast it")
    call_tool("create_actor", {"name": name, "voice": voice,
                               **({"seed": int(seed)} if seed is not None else {})})


def gen_audio_asset(*, dest: str = "", kind: str = "sfx", preset: str = "",
                    prompt: str = "", seed: int | None = None, duration: float = 20.0,
                    project_root: str = "", workspace_root: str = "",
                    step_tmp_dir: str = "", out_dir: str = "",
                    actor: str = "", voice: str = "", text: str = "",
                    speaking_rate: float = 0.0,
                    output_dir: str = "", output_target: str = "",
                    **kwargs) -> dict:
    """Generate one audio asset into the repo. Returns {written, source_url}."""
    repo = _target_root(output_dir, output_target, project_root, step_tmp_dir)
    if repo is None:
        return {"written": [], "error": "no explicit output directory or code root was injected"}
    from skillflow.output_targets import code_path
    try:
        code_path(repo, dest)
    except (ValueError, TypeError) as exc:
        return {"written": [], "error": str(exc)}
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
    elif kind == "voice":
        # Plain text-to-speech drifts line to line even at a fixed seed and
        # voice description — timbre is a function of the TEXT, so an NPC's five
        # lines come back as five different people. Casting the actor once
        # records a reference clip and every later line is spoken against it.
        if not actor or not text:
            return {"written": [], "error": "kind='voice' needs `actor` and `text`"}
        try:
            _ensure_actor(actor, voice, seed)
        except MCPError as e:
            return {"written": [], "error": str(e)}
        tool = "actor_tts"
        args = {"actor": actor, "text": text}
        if speaking_rate:
            args["speaking_rate"] = float(speaking_rate)
    else:
        return {"written": [], "error": "kind must be 'sfx', 'bgm' or 'voice'"}
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

    try:
        dst = code_path(repo, dest)
    except (ValueError, TypeError) as exc:
        return {"written": [], "error": str(exc)}
    dst.parent.mkdir(parents=True, exist_ok=True)
    from skillflow.output_targets import atomic_write_bytes
    atomic_write_bytes(dst, data)
    return {"written": [str(dst.relative_to(repo))], "source_url": urls[0]}


def _target_root(output_dir: str, output_target: str, project_root: str,
                 legacy_tmp: str = "") -> Path | None:
    """An explicit output directory wins; code never falls back to a draft."""
    if output_target not in ("", "artifact", "code"):
        return None
    if output_target == "code":
        if not project_root or not Path(project_root).is_absolute():
            return None
        if output_dir and Path(output_dir).resolve() != Path(project_root).resolve():
            return None
        return Path(project_root).resolve()
    if output_dir and Path(output_dir).is_absolute() and Path(output_dir).is_dir():
        return Path(output_dir).resolve()
    if output_target == "artifact":
        return None  # an explicit artifact destination must never fall back to code
    # Tool nodes can explicitly write code without an agent-output declaration.
    # A legacy staged agent is refused rather than reintroducing code staging.
    if legacy_tmp:
        return None
    if project_root and Path(project_root).is_absolute() and Path(project_root).is_dir():
        return Path(project_root).resolve()
    return None
