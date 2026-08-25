# api/config_routers.py
# REST endpoints exposing the registered skillflow configs and their manifests,
# so clients (CLI TUI, Web) can list available configs and render runs of any
# config generically (data-driven step labels, checkpoint kinds, …).

import os

from fastapi import APIRouter, Depends, HTTPException

from api.auth import CurrentUser, get_optional_user
from api.dependencies import get_config_registry, get_skillflow
from core.pipeline_registry import GEN_PREFIX
from core.security_jail import SecurityException, verify_path_safe

router = APIRouter(prefix="/api", tags=["Configs"])

# Real BYTES. The cap is applied to the encoded file, never to a decoded str —
# len() on a str counts code points, so a byte-named cap sliced against
# characters returns 3-4x the intended payload for CJK/emoji content.
_STATE_FILE_CAP = 512 * 1024


@router.get("/configs")
def list_configs(registry=Depends(get_config_registry)):
    """List every registered config with its manifest (labels, checkpoints,
    scheduler ownership, …)."""
    return {"configs": [m.to_dict() for m in registry.list()]}


@router.get("/configs/{config_name}/manifest")
def get_config_manifest(config_name: str, registry=Depends(get_config_registry)):
    """Return the manifest for a single config."""
    manifest = registry.get(config_name)
    if not manifest:
        raise HTTPException(status_code=404, detail=f"Config '{config_name}' not found")
    return manifest.to_dict()


def _generated_state_dir(config_name: str, registry):
    """Resolve a GENERATED pipeline's durable-state dir, or 404.

    ``create=False`` — these are read paths; provisioning here would make a GET
    side-effecting and litter the state root with empty dirs for pipelines that
    never wrote anything. The ``GEN_PREFIX`` gate is shared by both endpoints so
    they cannot disagree about what "a pipeline" is (the read path once accepted
    any registered config, including the built-ins).
    """
    if not config_name.startswith(GEN_PREFIX) or not registry.get(config_name):
        raise HTTPException(status_code=404,
                            detail=f"Generated pipeline '{config_name}' not found")
    return get_skillflow()._workspace.state_dir(config_name, create=False)


@router.get("/pipelines")
def list_pipelines(registry=Depends(get_config_registry),
                   user: CurrentUser | None = Depends(get_optional_user)):
    """The catalog of RUNNABLE pipelines — built-in and generated alike — each
    with where it came from, which addon (if any) it carries, and the durable
    cross-run state it has accumulated in ``pipeline_state/<config>/``.

    Distinct from run *history* (``/api/runs``) — this is the list of pipelines
    you can run, with the state they carry between runs (positions, memos, …).

    Built-ins are listed too: a catalog that showed only ``gen_*`` answered
    "what did I generate", not "what can I run", and left the composed
    base+addon configs (``dpe_game``) invisible even though they are the ones
    most in need of explaining — their label is inherited from the base, so
    without ``addons`` they are indistinguishable from it.
    """
    from core import addon_registry as ar
    sf = get_skillflow()
    out = []
    for m in registry.list():
        files: list[dict] = []
        d = sf._workspace.state_dir(m.config_name, create=False)
        if d.is_dir():
            # scandir: is_file() comes free from readdir's d_type, and stat() is
            # the one syscall actually needed per entry.
            with os.scandir(d) as it:
                files = sorted(
                    ({"name": e.name, "size": e.stat().st_size}
                     for e in it if e.is_file()),
                    key=lambda x: x["name"])
        try:
            decomp = ar.describe_config(m.config_name)
        except Exception:          # an un-decomposable name is still runnable
            decomp = {}
        base = decomp.get("base") or m.config_name
        out.append({"config_name": m.config_name, "label": m.label,
                    "origin": ("generated" if m.config_name.startswith(GEN_PREFIX)
                               else "native"),
                    "base": base, "addons": decomp.get("addons") or [],
                    "step_count": len(m.steps or []),
                    "state_files": files})
    return {"pipelines": out}


@router.get("/pipelines/{config_name}/graph")
def pipeline_graph(config_name: str, registry=Depends(get_config_registry),
                   user: CurrentUser | None = Depends(get_optional_user)):
    """One pipeline's COMPOSED graph: steps, edges and their match conditions.

    Composed, not the source YAML — a base+addon combo has no file of its own,
    and it is the composed product that actually runs. ``addon_steps`` marks the
    steps the overlay spliced in, which is what makes "show me the pipeline plus
    its addon" a single picture rather than two.

    READABLE WITHOUT CREDENTIALS, deliberately — the same rule as every other GET
    here (``/api/configs`` has returned step lists and checkpoints on the same
    terms since long before this endpoint existed). It was raised as a finding
    and decided: the shape of a pipeline is not a secret.

    What makes that safe is the PROJECTION, not the route. This returns names and
    structure — step ids, types, role NAMES, routing conditions — and never a
    value: no ``tool_params``, no role config, no prompt, no key. Widening it to
    include any of those would quietly turn a decision about publishing structure
    into publishing content, so ``test_the_graph_projection_publishes_names_only``
    pins the field list rather than trusting a future reader to remember.
    """
    from core import addon_registry as ar
    if not registry.get(config_name):
        raise HTTPException(status_code=404,
                            detail=f"Config '{config_name}' not found")
    view = ar.graph_view(config_name)
    if view is None:
        # Registered as a manifest but absent from the graph table — the zombie
        # shape archive_generated_pipeline exists to prevent. Say so; a 404 here
        # would read as "no such pipeline" and send the caller looking elsewhere.
        raise HTTPException(
            status_code=409,
            detail=f"Config '{config_name}' has a manifest but no registered graph")
    manifest = registry.get(config_name)
    view["label"] = manifest.label
    view["origin"] = ("generated" if config_name.startswith(GEN_PREFIX)
                      else "native")
    view["checkpoints"] = manifest.to_dict().get("checkpoints") or {}
    return view


@router.get("/pipelines/{config_name}/state/file")
def pipeline_state_file(config_name: str, name: str,
                        registry=Depends(get_config_registry),
                        user: CurrentUser | None = Depends(get_optional_user)):
    """Read the TAIL of one durable-state file of a generated pipeline, jailed
    to that pipeline's ``pipeline_state/<config>/`` dir.

    The tail, not the head: these files are append-only by design (an
    accumulating memo gets one dated block per run), so the newest entries —
    the ones the file exists for — live at EOF. Bounded read: at most
    ``_STATE_FILE_CAP`` BYTES are pulled into memory regardless of file size.
    """
    d = _generated_state_dir(config_name, registry)
    try:
        p = verify_path_safe(d, name)      # shared, unit-tested traversal guard
    except SecurityException:
        raise HTTPException(status_code=403, detail="Path traversal denied")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="state file not found")

    total_bytes = p.stat().st_size
    truncated = total_bytes > _STATE_FILE_CAP
    with p.open("rb") as f:
        if truncated:
            f.seek(total_bytes - _STATE_FILE_CAP)
        raw = f.read(_STATE_FILE_CAP)
    if truncated:
        # the seek can land mid-codepoint; drop leading UTF-8 continuation
        # bytes (0x80-0xBF) so the tail doesn't start with a stray U+FFFD
        i = 0
        while i < len(raw) and 0x80 <= raw[i] < 0xC0:
            i += 1
        raw = raw[i:]
    return {"name": name, "content": raw.decode("utf-8", errors="replace"),
            "truncated": truncated, "total_bytes": total_bytes}
