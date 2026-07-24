# api/config_routers.py
# REST endpoints exposing the registered skillflow configs and their manifests,
# so clients (CLI TUI, Web) can list available configs and render runs of any
# config generically (data-driven step labels, checkpoint kinds, …).

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_config_registry

router = APIRouter(prefix="/api", tags=["Configs"])

_STATE_FILE_CAP = 512 * 1024  # bytes; larger state files are truncated for display


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


@router.get("/pipelines")
def list_pipelines(registry=Depends(get_config_registry)):
    """The catalog of GENERATED pipelines (``gen_*``): each manifest plus the
    durable cross-run state it has accumulated in ``pipeline_state/<config>/``.

    Distinct from run *history* (``/api/runs``) — this is the list of pipelines
    you can run, with the state they carry between runs (positions, memos, …).
    """
    from api.dependencies import get_skillflow
    sf = get_skillflow()
    out = []
    for m in registry.list():
        if not m.config_name.startswith("gen_"):
            continue
        try:
            d = sf._workspace.state_dir(m.config_name)   # per-config durable dir
            files = sorted(
                ({"name": f.name, "size": f.stat().st_size}
                 for f in d.iterdir() if f.is_file()),
                key=lambda x: x["name"])
        except Exception:
            files = []
        entry = m.to_dict()
        entry["state_files"] = files
        out.append(entry)
    return {"pipelines": out}


@router.get("/pipelines/{config_name}/state/file")
def pipeline_state_file(config_name: str, name: str,
                        registry=Depends(get_config_registry)):
    """Read one durable-state file of a generated pipeline, jailed to that
    pipeline's ``pipeline_state/<config>/`` dir."""
    if not registry.get(config_name):
        raise HTTPException(status_code=404, detail=f"Config '{config_name}' not found")
    from api.dependencies import get_skillflow
    sf = get_skillflow()
    d = sf._workspace.state_dir(config_name).resolve()
    p = (d / name).resolve()
    if not str(p).startswith(str(d) + "/") or not p.is_file():
        raise HTTPException(status_code=404, detail="state file not found")
    text = p.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > _STATE_FILE_CAP
    return {"name": name, "content": text[:_STATE_FILE_CAP], "truncated": truncated}
