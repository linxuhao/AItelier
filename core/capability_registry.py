"""Capability definitions: the ONE place they are written, so the invariants
live here instead of in each caller.

Shaped after `core/model_registry.py`, and for the same reason. A capability is
`(name, tools, briefing, owner)`; the host registers the built-in ones from code
and generated ones from `~/.AItelier/capabilities/*.json` (boot-scanned, exactly
like generated tools and generated pipelines).

The invariants, all enforced here:

1. a capability may only grant tools that RESOLVE — an unresolvable grant hands
   a step nothing while reading, in the config, as though it handed it something;
2. a capability a config still OFFERS cannot be archived (the offer list would
   then name something that does not exist);
3. same name + different owner is a CONFLICT, never an overwrite — in a registry
   fed by the base, addons and generated artifacts, a silent redefinition changes
   what every holder is granted with nothing to read afterwards;
4. writes are atomic and drop the cache.

Definitions are global; which pipeline may *offer* which capability is the
graph's own `capabilities:` list (see `design/declarable_capabilities.md`).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ARCHIVE_DIR = "_archived"
ARCHIVE_INDEX = "archived.json"


def capabilities_dir() -> Path:
    """Where generated capability definitions live (gitignored user data,
    boot-scanned). Mirrors `pipeline_registry.generated_configs_dir`."""
    from core import datadir
    d = datadir.capabilities_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _archive_dir() -> Path:
    d = capabilities_dir() / ARCHIVE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def archived_names() -> set[str]:
    p = _archive_dir() / ARCHIVE_INDEX
    if not p.is_file():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")) or [])
    except Exception:
        log.warning("unreadable capability archive index at %s", p, exc_info=True)
        return set()


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def _unresolved(sf, tools: list[str]) -> list[str]:
    """Tool names that do not resolve. Empty when there is no loader to ask."""
    loader = getattr(sf, "_tool_loader", None)
    if loader is None:
        return []
    missing = []
    for name in tools:
        try:
            loader.load_schema(name)
        except Exception:
            missing.append(name)
    return missing


def define(sf, name: str, *, tools=(), briefing: str = "", owner: str = "host",
           context_provider=None, persist: bool = False) -> dict:
    """Register (or edit) a capability. Returns `{ok}` or `{error}`.

    `persist=True` writes it to `~/.AItelier/capabilities/<name>.json` so a
    restart brings it back — that is the path the forge uses. Built-in ones are
    code and are registered with `persist=False` on every boot.
    """
    tools = list(tools or ())
    if not name or "/" in name or name.startswith("."):
        return {"error": f"invalid capability name {name!r}"}
    missing = _unresolved(sf, tools)
    if missing:
        # Refused rather than warned: skillflow's own note on the same class of
        # miss is that a capability whose tool is missing "grants nothing just as
        # quietly". A registration that half-succeeds is the quiet version.
        return {"error": f"capability {name!r} grants tools that do not resolve: "
                         f"{missing}. Register the tool first."}
    try:
        sf.register_capability(name, tools=tools, briefing=briefing, owner=owner,
                               context_provider=context_provider)
    except ValueError as e:
        return {"error": str(e)}
    if persist:
        _write_atomic(capabilities_dir() / f"{name}.json", {
            "name": name, "tools": tools, "briefing": briefing, "owner": owner,
        })
    return {"ok": True, "name": name, "tools": tools, "owner": owner}


def offering_configs(sf, name: str) -> list[str]:
    """Every registered graph whose offer list names this capability."""
    out = []
    for gname in list(getattr(sf, "_graphs", {})):
        graph = sf._graphs.get(gname)
        if name in (getattr(graph, "capabilities", []) or []):
            out.append(gname)
    return sorted(out)


def archive(sf, name: str, *, purge: bool = False) -> dict:
    """Retire a capability: unregister live AND move its definition aside.

    Deleting only the file leaves the live registry holding it, so behaviour
    would differ before and after a restart — the zombie-pipeline hazard that
    `pipeline_registry.archive_generated_pipeline` exists for.
    """
    offers = offering_configs(sf, name)
    if offers:
        return {"error": f"capability {name!r} is still offered by {offers}. "
                         "Remove it from those pipelines first."}
    caps = getattr(sf, "_capabilities", {})
    if name not in caps and not (capabilities_dir() / f"{name}.json").is_file():
        return {"error": f"no capability {name!r}"}
    caps.pop(name, None)
    src = capabilities_dir() / f"{name}.json"
    if src.is_file():
        if purge:
            src.unlink()
        else:
            os.replace(src, _archive_dir() / f"{name}.json")
    if not purge:
        names = archived_names() | {name}
        _write_atomic(_archive_dir() / ARCHIVE_INDEX, sorted(names))
    return {"ok": True, "name": name, "purged": purge}


def load_generated(sf) -> list[str]:
    """Boot scan: register every persisted capability. Returns the names."""
    loaded = []
    skip = archived_names()
    for f in sorted(capabilities_dir().glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            log.warning("unreadable capability definition %s", f, exc_info=True)
            continue
        name = d.get("name") or f.stem
        if name in skip:
            continue
        r = define(sf, name, tools=d.get("tools") or [],
                   briefing=d.get("briefing") or "",
                   owner=d.get("owner") or f"gen:{name}")
        if r.get("ok"):
            loaded.append(name)
        else:
            log.warning("capability %s not registered: %s", name, r["error"])
    return loaded


def palette(sf, config_name: str = "") -> dict:
    """What a declarer may choose from, and what is missing.

    Without `config_name`: the whole registry. With one: the intersection of the
    graph's offer list and the registry, PLUS the difference — a capability a
    pipeline declares but this deployment never registered is a deployment gap
    that has to be visible, not an empty row.
    """
    caps = getattr(sf, "_capabilities", {}) or {}
    if not config_name:
        return {"capabilities": [_row(n, c) for n, c in sorted(caps.items())]}
    graph = getattr(sf, "_graphs", {}).get(config_name)
    if graph is None:
        return {"error": f"no pipeline {config_name!r}"}
    offers = list(getattr(graph, "capabilities", []) or [])
    return {
        "pipeline": config_name,
        "capabilities": [_row(n, caps[n]) for n in offers if n in caps],
        "offered_but_not_registered": [n for n in offers if n not in caps],
    }


def _row(name: str, cap: dict) -> dict:
    return {
        "name": name,
        "tools": list(cap.get("tools") or ()),
        "owner": cap.get("owner", "host"),
        "briefing": (cap.get("briefing") or "")[:200],
    }
