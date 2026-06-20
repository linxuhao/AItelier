# api/config_routers.py
# REST endpoints exposing the registered skillflow configs and their manifests,
# so clients (CLI TUI, Web) can list available configs and render runs of any
# config generically (data-driven step labels, checkpoint kinds, …).

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_config_registry

router = APIRouter(prefix="/api", tags=["Configs"])


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
