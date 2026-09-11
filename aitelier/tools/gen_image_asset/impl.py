"""gen_image_asset — generate a real image asset and write it into the repo.

The visual half of the asset channel. Without it a generated game can only ship
`ColorRect`/`Polygon2D` placeholders, which is most of why generated games look
primitive: perceived quality is mostly art, and the pipeline had no way to make
any. Wraps three MCP tools in the order that actually produces a usable sprite:

    generate_image  ->  remove_bg (real alpha)  ->  slice_sheet (grid -> frames)

The middle step is not optional for sprites: the image model has no alpha
channel at all, and asked for a transparent background it PAINTS the grey-white
checkerboard as opaque pixels. Skipping it yields a sprite sitting on a mosaic.

A fourth step guards the one failure no pixel metric can see: the image model
happily draws the WRONG SUBJECT. A shared style line that names the game's
objects ("palette: green pipes, sandy ground, yellow bird") bleeds them into
every prompt, so the "ground" sprite comes back as a bird. `vision_critique`
is asked whether the image depicts the requested subject and its verdict rides
back in `warning` — semantics are a question for a vision model, not for an
alpha histogram.

Writes into the engine-declared output directory. Code outputs go directly to
this run's worktree; artifact outputs remain artifacts. No code overlay or
promotion is involved.
"""

from pathlib import Path

from aitelier.mcp_client import MCPError, call_tool, fetch, urls_in

_MAX_DIM = 768          # the image service clamps above this; ask for what we get


_CAST_TOOL = {"character": "create_character", "animal": "create_animal",
              "object": "create_object"}


def _ensure_cast(name: str, appearance: str, kind: str, seed) -> None:
    """Cast `name` once, then reuse it forever.

    Casting is idempotent on purpose: the roster lives on the MCP server and
    outlives any single run, so a re-run of the same pipeline must NOT re-cast
    (that would hand the character a new face and silently break continuity with
    every asset already in the repo). `appearance` is therefore only consulted
    the first time; a deliberate change is a `force` re-cast, which is a decision
    for a person, not for a retry loop."""
    tool = _CAST_TOOL.get(kind)
    if tool is None:
        raise MCPError(f"cast_kind must be one of {sorted(_CAST_TOOL)}, got {kind!r}")
    if name in call_tool("list_subjects", {}):
        return
    if not appearance:
        raise MCPError(f"{name!r} is not cast yet — pass `appearance` to cast it")
    call_tool(tool, {"name": name, "appearance": appearance,
                     **({"seed": int(seed)} if seed is not None else {})})


def gen_image_asset(*, prompt: str = "", dest: str = "", project_root: str = "",
                    workspace_root: str = "", width: int = 512, height: int = 512,
                    seed: int | None = None, transparent: bool = True,
                    rows: int = 0, cols: int = 0, subject: str = "",
                    verify: bool = True, step_tmp_dir: str = "", out_dir: str = "",
                    cast: str = "", appearance: str = "", cast_kind: str = "character",
                    output_dir: str = "", output_target: str = "",
                    **kwargs) -> dict:
    """Generate one image asset (optionally cut into frames) into the repo.

    dest is repo-relative. When rows*cols > 1 it is treated as a stem and the
    frames land as ``<stem>_0.png``, ``<stem>_1.png``, ... Returns
    {written, source_url, warning}.

    CONTINUITY (`cast`): plain text-to-image returns a DIFFERENT-LOOKING person
    every call, so a hero's portrait, battle sprite and map token come back as
    three strangers — which is why a generated game can only ever afford ONE
    picture of each character. Passing `cast="Yang Guo"` locks the look: the
    first call casts the subject from `appearance` (identity — age, build, face,
    hair, the prop that follows them everywhere) and every later call renders
    that same subject doing whatever `prompt` describes. `appearance` is the
    person; `prompt` is the moment. Costume lives in `prompt`: "wearing heavy
    red armor" re-dresses without changing the face.

    `cast_kind` picks the casting call: "character" | "animal" | "object". An
    object's identity is its GEOMETRY (silhouette, whether the lid is flat or
    domed, where the hinges sit) — colour and material alone do not hold it
    still across angles."""
    repo = _target_root(output_dir, output_target, project_root, step_tmp_dir)
    if repo is None:
        return {"written": [], "error": "no explicit output directory or code root was injected"}
    from skillflow.output_targets import code_path
    try:
        code_path(repo, dest)
    except (ValueError, TypeError) as exc:
        return {"written": [], "error": str(exc)}
    if not prompt or not dest:
        return {"written": [], "error": "prompt and dest are both required"}

    args = {"prompt": prompt, "width": min(int(width), _MAX_DIM),
            "height": min(int(height), _MAX_DIM)}
    if seed is not None:
        args["seed"] = int(seed)

    warning = None
    written = []
    try:
        if cast:
            _ensure_cast(cast, appearance, cast_kind, seed)
            url = _one_url(call_tool("subject_image", {
                "subject": cast, "scene": prompt,
                "width": args["width"], "height": args["height"],
                **({"seed": args["seed"]} if seed is not None else {})}),
                "subject_image")
        else:
            url = _one_url(call_tool("generate_image", args), "generate_image")
        # Check the SUBJECT before cutting it out: the raw render is already RGB,
        # so the vision model can read it without a composite step.
        if verify:
            warning = _subject_warning(url, subject or prompt)
        if transparent:
            reply = call_tool("remove_bg", {"image_url": url})
            url = _one_url(reply, "remove_bg")
            # remove_bg flags a cutout it thinks is degenerate; a silent bad
            # matte is exactly the failure a reviewer cannot see in a report.
            if "warn" in reply.lower() or "⚠" in reply:
                warning = "; ".join(x for x in (warning, reply.strip()) if x)
        if rows * cols > 1:
            frames = urls_in(call_tool("slice_sheet",
                                       {"image_url": url, "rows": int(rows), "cols": int(cols)}))
            if not frames:
                return {"written": [], "error": "slice_sheet returned no frames"}
            stem = dest[:-4] if dest.endswith(".png") else dest
            targets = [(f"{stem}_{i}.png", u) for i, u in enumerate(frames)]
        else:
            targets = [(dest, url)]
        for rel, u in targets:
            written.append(_write(repo, rel, fetch(u)))
    except (MCPError, OSError) as e:
        return {"written": written, "error": str(e)}

    return {"written": written, "source_url": url, "warning": warning}


def _one_url(reply: str, tool: str) -> str:
    urls = urls_in(reply)
    if not urls:
        raise MCPError(f"{tool} returned no URL: {reply[:200]}")
    return urls[0]


def _write(repo: Path, rel: str, data: bytes) -> str:
    from skillflow.output_targets import code_path
    try:
        dst = code_path(repo, rel)
    except (ValueError, TypeError) as exc:
        raise MCPError(str(exc)) from exc
    dst.parent.mkdir(parents=True, exist_ok=True)
    from skillflow.output_targets import atomic_write_bytes
    atomic_write_bytes(dst, data)
    return str(dst.relative_to(repo))


def _subject_warning(url: str, subject: str) -> str | None:
    """Ask the vision model whether the render shows the requested subject.

    Advisory by design: a false "wrong subject" must never block an asset, so a
    failed or unparseable critique returns None rather than a warning."""
    ask = (f'This is a generated game asset that should show ONLY: {subject}. '
           'Reply on one line, starting with YES or NO, then a short reason. '
           'Answer NO if any other distinct object is also present.')
    try:
        verdict = call_tool("vision_critique", {"image_url": url, "prompt": ask}).strip()
    except MCPError:
        return None
    return f"subject check: {verdict[:300]}" if verdict[:3].upper() == "NO" else None


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
