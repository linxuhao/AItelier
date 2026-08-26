# api/model_routers.py
# REST access to the two deployment tables. Three levels, and the names are
# deliberate — see README "The three levels":
#
#   provider   a registered host          `ark`                  (base_url + key NAME)
#   endpoint   one concrete place to call `ark/deepseek-v4-flash`
#   model      an ordered list of them    `flash`                (what agent_configs name)
#
# Every rule lives in core/model_registry, not here — the MCP endpoint exposes
# the same operations, and a refusal that only one of the two surfaces enforces
# is not a rule. This module is transport: parse, call, translate the refusal
# into a 400 whose body is the message the registry wrote.
#
# Mutations use non-safe METHODS on purpose: `api/main.py:write_gate` gates
# every non-GET request, so authorization is inherited rather than re-declared.

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core import model_registry as reg

router = APIRouter(prefix="/api/models", tags=["Models"])


def _guard(fn, *args, **kwargs):
    """A refusal is a 400 carrying the registry's own sentence.

    These messages say what to do instead ("unmap it from those models first"),
    so flattening them into a generic error would throw away the only part the
    caller can act on.
    """
    try:
        return fn(*args, **kwargs)
    except reg.RegistryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class ProviderIn(BaseModel):
    name: str = Field(..., description="Registry key; becomes the part before "
                                       "the '/' in an endpoint, e.g. 'ark'.")
    base_url: str = Field(..., description="OpenAI-compatible base URL.")
    api_key_env: str = Field("", description="NAME of the secret this endpoint "
                                             "reads. The key itself is a file, "
                                             "never set through this API.")


class ProviderPatch(BaseModel):
    base_url: str = ""
    api_key_env: str | None = None


class ModelIn(BaseModel):
    name: str = Field(..., description="Model name, e.g. 'flash'. Bare — a "
                                       "'/' would make it read as an endpoint.")
    endpoints: list[str] = Field(default_factory=list,
                                 description="Ordered endpoints "
                                             "(`provider/model-id`); the FIRST "
                                             "is what calls bind to.")


class MapIn(BaseModel):
    endpoint: str = Field(..., description="An endpoint, `provider/model-id` "
                                           "— e.g. 'ark/deepseek-v4-flash'.")
    position: int | None = Field(None, description="Index to insert at. Order "
                                                   "is policy: the first "
                                                   "endpoint is bound, the "
                                                   "rest are failover only.")


# ── read ─────────────────────────────────────────────────────────────────────

@router.get("")
def get_available_models():
    """Every model, its ordered endpoints, and whether each one can serve.

    `provider_registered` / `key_present` / `cooldown_seconds` are the three
    ways a listed endpoint can still be unusable right now.
    """
    return reg.list_models()


@router.get("/providers")
def get_providers():
    return reg.list_providers()


# ── providers ────────────────────────────────────────────────────────────────

@router.post("/providers")
def add_provider(body: ProviderIn):
    return _guard(reg.add_provider, body.name, body.base_url, body.api_key_env)


@router.patch("/providers/{name}")
def update_provider(name: str, body: ProviderPatch):
    return _guard(reg.update_provider, name, body.base_url, body.api_key_env)


@router.delete("/providers/{name}")
def delete_provider(name: str):
    return _guard(reg.delete_provider, name)


# ── models ────────────────────────────────────────────────────────────────────

@router.post("")
def add_model(body: ModelIn):
    return _guard(reg.add_model, body.name, body.endpoints)


@router.post("/{model}/endpoints")
def map_endpoint(model: str, body: MapIn):
    """Point a model at one more endpoint."""
    return _guard(reg.map_model, model, body.endpoint, body.position)


@router.delete("/{model}/endpoints")
def unmap_endpoint(model: str, endpoint: str):
    """Remove one endpoint. It is a query param because it contains a '/' and
    would otherwise have to be URL-encoded into the path."""
    return _guard(reg.unmap_model, model, endpoint)


@router.delete("/{model}")
def delete_model(model: str):
    return _guard(reg.delete_model, model)
