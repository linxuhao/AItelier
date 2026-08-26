# core/model_registry.py
"""Read/write access to the two deployment tables, with the rules that keep them
runnable.

Three levels, and the names matter because two of them used to share a word —
see README "The three levels":

    provider   a registered host           `ark`                   base URL + key NAME
    endpoint   one concrete place to call  `ark/deepseek-v4-flash`
    model      an ordered list of them     `flash`                 what agent_configs name

"candidate" is kept for an endpoint's ROLE inside one model's list, which is a
different thing from what it is: a model's candidates are endpoints, in
preference order.

`llm_providers.json` (provider → base URL + key NAME) and `model_routes.json`
(model → ordered endpoints) are hand-editable files, and everything else treats
them as read-only truth. This module is the one place that CHANGES them, so the
invariants live here rather than in each caller — the HTTP API and the MCP
endpoint both come through these functions.

Three rules, all of them about not breaking a running deployment:

  * A model may only name endpoints whose provider is REGISTERED. An
    unregistered one reaches litellm as a bare `provider/model-id` it cannot place, which is a
    client-side BadRequestError — deliberately not a failover error, so the
    gateway dies on it with healthy endpoints still queued behind.
  * A model that something REFERENCES cannot be deleted, and neither can a
    provider that some model's endpoints still name. Both would fail at the
    first LLM call, far from the edit that caused it.
  * The whole table is validated by constructing `ModelRoutes` BEFORE anything
    is written. A file that parses but does not load would take every agent
    down at once.

Keys are never written here. They are secret FILES (`~/.aitelier-secrets/<NAME>`
mounted at `/run/secrets/<NAME>`); this layer only records which NAME a provider
reads, which is exactly the split that keeps credentials out of the API surface.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from core.model_routes import ModelRoutes, config_or_example, reset_cache

_REPO_ROOT = Path(__file__).resolve().parent.parent
PROVIDERS_FILE = "llm_providers.json"
ROUTES_FILE = "model_routes.json"

# A provider name is used as a path-ish key and split on "/", so keep it plain.
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class RegistryError(ValueError):
    """A refusal the caller should show verbatim — it says what to do instead."""


# ── loading ──────────────────────────────────────────────────────────────────

def _load(name: str) -> dict:
    """Current contents, falling back to the committed example."""
    try:
        return json.loads(Path(config_or_example(name)).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise RegistryError(f"{name} is not valid JSON: {e}") from e


def _write(name: str, data: dict) -> str:
    """Atomically write the REAL file (never the example) and drop the cache.

    Writing to the real path even when only the example existed is the point: an
    operator editing through the API is making this deployment's choice, and the
    example is committed content that a `git pull` would overwrite.
    """
    target = _REPO_ROOT / name
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(_REPO_ROOT), prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    reset_cache()
    return str(target)


def _providers() -> dict:
    return {k: v for k, v in _load(PROVIDERS_FILE).items()
            if not k.startswith("_") and isinstance(v, dict)}


def _routes_raw() -> dict:
    return _load(ROUTES_FILE)


def _routes() -> dict:
    return {k: v for k, v in _routes_raw().items()
            if not k.startswith("_") and isinstance(v, list)}


def _validate_or_raise(routes_doc: dict) -> None:
    """Load the proposed table the way production will, before it is written."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(routes_doc, f, ensure_ascii=False)
        ModelRoutes(tmp)
    except RuntimeError as e:
        raise RegistryError(str(e)) from e
    finally:
        Path(tmp).unlink(missing_ok=True)


# ── who is using what ────────────────────────────────────────────────────────

def model_consumers(model: str) -> list[str]:
    """Everything that would break if this model disappeared.

    Two populations, and the second is why this is not a one-line grep of the
    agent configs: an AGENT's model is config, but a TOOL's is a Python constant
    (`godot_vision` holds `_ROUTE = "vision"`). Missing that is what let the
    vision route stay invisible to key derivation for a while.
    """
    out: list[str] = []
    cfg_dir = _REPO_ROOT / "agent_configs"
    if cfg_dir.is_dir():
        pattern = re.compile(
            r'^\s+model:\s*"?' + re.escape(model) + r'"?\s*$', re.M)
        for f in sorted(cfg_dir.glob("*.yaml")):
            try:
                if pattern.search(f.read_text(encoding="utf-8")):
                    out.append(f"agent_configs/{f.name}")
            except OSError:
                continue
    for rel, const in (("aitelier/tools/godot_vision/impl.py", "_ROUTE"),):
        p = _REPO_ROOT / rel
        try:
            src = p.read_text(encoding="utf-8")
        except OSError:
            continue
        # EXTRACT the default and compare, rather than embedding `model` in the
        # pattern. Embedding it made a miss indistinguishable from "no such
        # dependency": reformat the assignment — wrap it for line length, switch
        # quote style — and the guard silently reports no consumers, so
        # `delete_model("vision")` succeeds and the readability gate loses the
        # model it resolves. So: quote-agnostic, and tolerant of a wrap after
        # `get(` or after the comma (`\s*` spans newlines in both places).
        #
        # The env-var half is `[^,\n]`, NOT `[^,]`, and the newline matters:
        # unbounded, a call that has lost its default
        # (`os.environ.get("GODOT_VISION_ROUTE")`) lets the scan run past the
        # closing paren to the next comma anywhere later in the file and capture
        # an unrelated literal — a WRONG consumer, which is worse than none. It
        # either hides the real dependency or blocks a legitimate delete. An
        # argument is a string literal; it cannot span a line anyway.
        #
        # `test_the_tool_dependency_guard_still_matches_the_real_source` fails
        # loudly if the shape drifts far enough to break even this.
        m = re.search(
            rf'{const}\s*=\s*os\.environ\.get\(\s*[^,\n]+,\s*["\']([^"\']+)["\']',
            src)
        if m and m.group(1) == model:
            out.append(rel)
    return out


def provider_consumers(provider: str) -> list[str]:
    """Models with an endpoint served by this provider."""
    return sorted(name for name, cands in _routes().items()
                  if any(c.split("/", 1)[0] == provider for c in cands))


# ── read ─────────────────────────────────────────────────────────────────────

def list_models() -> dict:
    """Every model, its endpoints, and whether each one can serve.

    An endpoint can be listed and still be unusable right now: its provider is
    unregistered, its key file is empty, or its usage window is spent and
    parked. Those three are reported separately because the fix differs.
    """
    from core.ai_router import _read_secret, endpoint_cooldowns

    provs = _providers()
    cooling = endpoint_cooldowns()
    import time as _t
    now = _t.time()
    models = []
    for name, cands in sorted(_routes().items()):
        entries = []
        for c in cands:
            prov = c.split("/", 1)[0]
            cfg = provs.get(prov)
            key_env = (cfg or {}).get("api_key_env")
            entries.append({
                "endpoint": c,
                "provider": prov,
                "provider_registered": cfg is not None,
                "api_key_env": key_env,
                "key_present": bool(_read_secret(key_env)) if key_env else None,
                "cooldown_seconds": (round(cooling[c] - now)
                                     if c in cooling else 0),
            })
        models.append({"model": name, "endpoints": entries,
                       "used_by": model_consumers(name)})
    return {"models": models,
            "providers": sorted(provs),
            "routes_file": config_or_example(ROUTES_FILE),
            "providers_file": config_or_example(PROVIDERS_FILE)}


def list_providers() -> dict:
    provs = _providers()
    return {"providers": [
        {"name": n, "base_url": c.get("base_url"),
         "api_key_env": c.get("api_key_env"),
         "used_by_models": provider_consumers(n)}
        for n, c in sorted(provs.items())]}


# ── provider write ───────────────────────────────────────────────────────────

def add_provider(name: str, base_url: str, api_key_env: str = "") -> dict:
    if not _NAME_RE.match(name or ""):
        raise RegistryError(
            f"provider name {name!r} must be alphanumeric (it becomes the part "
            f"before the '/' in a candidate)")
    if not (base_url or "").strip():
        raise RegistryError("base_url is required")
    doc = _load(PROVIDERS_FILE)
    if name in doc:
        raise RegistryError(f"provider '{name}' already exists — use update")
    entry = {"base_url": base_url.strip()}
    if api_key_env:
        entry["api_key_env"] = api_key_env
    doc[name] = entry
    path = _write(PROVIDERS_FILE, doc)
    return {"added": name, "file": path, **_key_hint(api_key_env)}


def update_provider(name: str, base_url: str = "",
                    api_key_env: str | None = None) -> dict:
    doc = _load(PROVIDERS_FILE)
    if name not in doc or not isinstance(doc[name], dict):
        raise RegistryError(f"no provider '{name}'")
    if base_url:
        doc[name]["base_url"] = base_url.strip()
    if api_key_env is not None:
        if api_key_env:
            doc[name]["api_key_env"] = api_key_env
        else:
            doc[name].pop("api_key_env", None)
    path = _write(PROVIDERS_FILE, doc)
    return {"updated": name, "file": path,
            **_key_hint(doc[name].get("api_key_env", ""))}


def delete_provider(name: str) -> dict:
    users = provider_consumers(name)
    if users:
        raise RegistryError(
            f"provider '{name}' still serves {users} — unmap it from those "
            f"models first, or every call that reaches it dies with a "
            f"BadRequestError that does not fail over")
    doc = _load(PROVIDERS_FILE)
    if name not in doc:
        raise RegistryError(f"no provider '{name}'")
    doc.pop(name)
    return {"deleted": name, "file": _write(PROVIDERS_FILE, doc)}


def _key_hint(api_key_env: str) -> dict:
    """The API records the key's NAME; the key itself is a file, deliberately."""
    if not api_key_env:
        return {"note": "no api_key_env — this endpoint is treated as needing "
                        "no credential"}
    return {"next_step":
            f"the key itself is NOT set through this API. Create the secret "
            f"file: printf '%s' \"<key>\" > ~/.aitelier-secrets/{api_key_env} "
            f"&& chmod 600 \"$_\", and add {api_key_env} to docker-compose.yml's "
            f"`secrets:` block (both the service list and the top-level "
            f"definition), then recreate the container — `restart` does not "
            f"attach a new secret mount."}


# ── route write ──────────────────────────────────────────────────────────────

def _check_endpoint(endpoint: str, provs: dict) -> None:
    if "/" not in (endpoint or ""):
        raise RegistryError(
            f"endpoint {endpoint!r} must be 'provider/model-id', e.g. "
            f"'ark/deepseek-v4-flash'")
    prov = endpoint.split("/", 1)[0]
    if prov not in provs:
        raise RegistryError(
            f"provider '{prov}' is not registered — add it first. Known: "
            f"{sorted(provs)}")


def add_model(name: str, endpoints: list[str] | None = None) -> dict:
    if not _NAME_RE.match(name or "") or "/" in name:
        raise RegistryError(
            f"model name {name!r} must be a bare name like 'flash' — a '/' "
            f"would make it read as an endpoint")
    doc = _routes_raw()
    if name in doc:
        raise RegistryError(f"model '{name}' already exists — use map/unmap")
    provs = _providers()
    for c in (endpoints or []):
        _check_endpoint(c, provs)
    doc[name] = list(endpoints or [])
    if not doc[name]:
        raise RegistryError(
            "a model with no endpoints resolves to nothing and fails at its "
            "first call — pass at least one endpoint")
    _validate_or_raise(doc)
    return {"added": name, "endpoints": doc[name],
            "file": _write(ROUTES_FILE, doc)}


def map_model(model: str, endpoint: str, position: int | None = None) -> dict:
    """Point a model at one more endpoint.

    ORDER IS THE POLICY: the first endpoint is what every call binds to, and
    the rest are tried only when an endpoint fails. So `position` is not
    cosmetic — putting a pay-as-you-go endpoint first spends money that a token
    plan already covers, and putting it last is what makes a spent plan a
    slowdown instead of a stop.
    """
    doc = _routes_raw()
    if model not in doc or not isinstance(doc[model], list):
        raise RegistryError(f"no model '{model}' — add it first")
    _check_endpoint(endpoint, _providers())
    if endpoint in doc[model]:
        raise RegistryError(f"'{endpoint}' is already an endpoint of '{model}'")
    if position is None or position >= len(doc[model]):
        doc[model].append(endpoint)
    else:
        doc[model].insert(max(position, 0), endpoint)
    _validate_or_raise(doc)
    return {"model": model, "endpoints": doc[model],
            "file": _write(ROUTES_FILE, doc)}


def unmap_model(model: str, endpoint: str) -> dict:
    doc = _routes_raw()
    if model not in doc or not isinstance(doc[model], list):
        raise RegistryError(f"no model '{model}'")
    if endpoint not in doc[model]:
        raise RegistryError(
            f"'{endpoint}' is not an endpoint of '{model}' — it has "
            f"{doc[model]}")
    if len(doc[model]) == 1:
        raise RegistryError(
            f"'{endpoint}' is the only endpoint of '{model}'; removing it "
            f"leaves a model that resolves to nothing. Map a replacement first, "
            f"or delete the model")
    doc[model].remove(endpoint)
    _validate_or_raise(doc)
    return {"model": model, "endpoints": doc[model],
            "file": _write(ROUTES_FILE, doc)}


def delete_model(name: str) -> dict:
    users = model_consumers(name)
    if users:
        raise RegistryError(
            f"model '{name}' is referenced by {users} — repoint those "
            f"first. Deleting it makes every one of them fail at its first LLM "
            f"call, with an error pointing at the route table rather than at "
            f"the config that still names it")
    doc = _routes_raw()
    if name not in doc:
        raise RegistryError(f"no model '{name}'")
    doc.pop(name)
    _validate_or_raise(doc)
    return {"deleted": name, "file": _write(ROUTES_FILE, doc)}
