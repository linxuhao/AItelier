# core/model_registry.py
"""Read/write access to the two deployment tables, with the rules that keep them
runnable.

`llm_providers.json` (endpoint + key NAME) and `model_routes.json` (internal
model → ordered candidates) are hand-editable files, and everything else treats
them as read-only truth. This module is the one place that CHANGES them, so the
invariants live here rather than in each caller — the HTTP API and the MCP
endpoint both come through these functions.

Three rules, all of them about not breaking a running deployment:

  * A route may only name a REGISTERED provider. An unregistered one reaches
    litellm as a bare `provider/model` it cannot place, which is a client-side
    BadRequestError — deliberately not a failover error, so the gateway dies on
    it with healthy candidates still queued behind.
  * An internal model that something REFERENCES cannot be deleted, and a
    provider that a route still names cannot be deleted. Both would fail at the
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
    """Everything that would break if this internal model disappeared.

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
            if re.search(rf'{const}\s*=\s*os\.environ\.get\([^,]+,\s*"{re.escape(model)}"',
                         p.read_text(encoding="utf-8")):
                out.append(rel)
        except OSError:
            continue
    return out


def provider_consumers(provider: str) -> list[str]:
    """Routes that still name this provider."""
    return sorted(name for name, cands in _routes().items()
                  if any(c.split("/", 1)[0] == provider for c in cands))


# ── read ─────────────────────────────────────────────────────────────────────

def list_models() -> dict:
    """Every internal model, its candidates, and whether each one can serve.

    `usable` is the operationally interesting column: a candidate can be listed
    and still be unusable right now because its provider is unregistered, its
    key file is empty, or its usage window is spent and parked.
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
                "candidate": c,
                "provider": prov,
                "provider_registered": cfg is not None,
                "api_key_env": key_env,
                "key_present": bool(_read_secret(key_env)) if key_env else None,
                "cooldown_seconds": (round(cooling[c] - now)
                                     if c in cooling else 0),
            })
        models.append({"model": name, "candidates": entries,
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

def _check_candidate(candidate: str, provs: dict) -> None:
    if "/" not in (candidate or ""):
        raise RegistryError(
            f"candidate {candidate!r} must be 'provider/model'")
    prov = candidate.split("/", 1)[0]
    if prov not in provs:
        raise RegistryError(
            f"provider '{prov}' is not registered — add it first. Known: "
            f"{sorted(provs)}")


def add_model(name: str, candidates: list[str] | None = None) -> dict:
    if not _NAME_RE.match(name or "") or "/" in name:
        raise RegistryError(
            f"internal model name {name!r} must be a bare name like 'flash' — "
            f"a '/' would make it look like a concrete provider/model")
    doc = _routes_raw()
    if name in doc:
        raise RegistryError(f"model '{name}' already exists — use map/unmap")
    provs = _providers()
    for c in (candidates or []):
        _check_candidate(c, provs)
    doc[name] = list(candidates or [])
    if not doc[name]:
        raise RegistryError(
            "a model with no candidates resolves to nothing and fails at its "
            "first call — pass at least one candidate")
    _validate_or_raise(doc)
    return {"added": name, "candidates": doc[name], "file": _write(ROUTES_FILE, doc)}


def map_model(model: str, candidate: str, position: int | None = None) -> dict:
    """Point an internal model at one more concrete endpoint.

    ORDER IS THE POLICY: the first candidate is what every call binds to, and
    the rest are tried only when an endpoint fails. So `position` is not
    cosmetic — putting a pay-as-you-go endpoint first spends money that a token
    plan already covers, and putting it last is what makes a spent plan a
    slowdown instead of a stop.
    """
    doc = _routes_raw()
    if model not in doc or not isinstance(doc[model], list):
        raise RegistryError(f"no internal model '{model}' — add it first")
    _check_candidate(candidate, _providers())
    if candidate in doc[model]:
        raise RegistryError(f"'{candidate}' is already a candidate for '{model}'")
    if position is None or position >= len(doc[model]):
        doc[model].append(candidate)
    else:
        doc[model].insert(max(position, 0), candidate)
    _validate_or_raise(doc)
    return {"model": model, "candidates": doc[model],
            "file": _write(ROUTES_FILE, doc)}


def unmap_model(model: str, candidate: str) -> dict:
    doc = _routes_raw()
    if model not in doc or not isinstance(doc[model], list):
        raise RegistryError(f"no internal model '{model}'")
    if candidate not in doc[model]:
        raise RegistryError(
            f"'{candidate}' is not a candidate for '{model}' — it has "
            f"{doc[model]}")
    if len(doc[model]) == 1:
        raise RegistryError(
            f"'{candidate}' is the only candidate for '{model}'; removing it "
            f"leaves a model that resolves to nothing. Map a replacement first, "
            f"or delete the model")
    doc[model].remove(candidate)
    _validate_or_raise(doc)
    return {"model": model, "candidates": doc[model],
            "file": _write(ROUTES_FILE, doc)}


def delete_model(name: str) -> dict:
    users = model_consumers(name)
    if users:
        raise RegistryError(
            f"internal model '{name}' is referenced by {users} — repoint those "
            f"first. Deleting it makes every one of them fail at its first LLM "
            f"call, with an error pointing at the route table rather than at "
            f"the config that still names it")
    doc = _routes_raw()
    if name not in doc:
        raise RegistryError(f"no internal model '{name}'")
    doc.pop(name)
    _validate_or_raise(doc)
    return {"deleted": name, "file": _write(ROUTES_FILE, doc)}
