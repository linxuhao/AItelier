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

This module is the ONE place allowed to reach into skillflow's private
`_capabilities` dict, and only where the public `sf.capabilities()` accessor
cannot serve: it needs the live `context_provider` callable (deliberately not
exposed) and it needs to mutate on archive. Every other reader — the palette,
the emit gate, the catalog, the validation tool — goes through the accessor, so
a rename across the repo boundary is an AttributeError instead of a silent
empty table.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

# A briefing rides the holder step's per-turn context (deliberately not the
# cacheable preamble), and every listing shows only its first line — so an
# oversized one is an invisible per-turn payload, the same leak this whole
# mechanism removed.
MAX_BRIEFING_BYTES = 4096

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
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
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


def _normalize_tools(tools) -> list[str]:
    """Normalize the executable iterable shapes supported by SkillFlow 1.5.75."""
    if tools is None:
        return []
    if isinstance(tools, dict):
        values = list(tools)
    elif isinstance(tools, (list, tuple)):
        values = list(tools)
    else:
        raise ValueError(
            "capability tools must be a list or mapping of non-empty names")
    if not all(isinstance(tool, str) and tool for tool in values):
        raise ValueError(
            "capability tools must be a list or mapping of non-empty names")
    return values


def _release_graph_update_error(sf, name: str, candidate: dict) -> str:
    """Reject a capability edit that would invalidate a live or persisted graph."""
    import yaml
    from core import pipeline_registry
    from core.release_gate_migration import (
        release_gate_ownership_error, release_graph_uses_capability)

    # by-name-ok: registry mutation validates all live config definitions, no run
    for graph_name, graph in list(getattr(sf, "_graphs", {}).items()):
        to_dict = getattr(graph, "to_dict", None)
        if not callable(to_dict):
            continue
        document = to_dict()
        if not release_graph_uses_capability(document, name):
            continue
        error = release_gate_ownership_error(
            document, sf=sf, capability_overrides={name: candidate})
        if error:
            return f"capability {name!r} would invalidate live graph " \
                   f"{graph_name!r}: {error}"

    config_dir = pipeline_registry.generated_configs_dir()
    for path in sorted(config_dir.glob("gen_*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not release_graph_uses_capability(document, name):
            continue
        roles = None
        roles_path = path.with_suffix(".roles.json")
        if roles_path.exists():
            try:
                roles = json.loads(roles_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                return (f"capability {name!r} cannot inspect persisted graph "
                        f"{path.name!r} roles: {exc}")
        error = release_gate_ownership_error(
            document, roles, sf=sf, capability_overrides={name: candidate})
        if error:
            return f"capability {name!r} would invalidate persisted graph " \
                   f"{path.name!r}: {error}"
    return ""


def define(sf, name: str = "", *, tools=(), briefing: str = "", owner: str = "host",
           context_provider=None, persist: bool = False,
           host: bool = False) -> dict:
    """Register (or edit) a capability. Returns `{ok}` or `{error}`.

    `persist=True` writes it to `~/.AItelier/capabilities/<name>.json` so a
    restart brings it back — that is the path the forge uses. Built-in ones are
    code and are registered with `persist=False` on every boot.
    """
    try:
        tools = _normalize_tools(tools)
    except ValueError as exc:
        return {"error": str(exc)}
    # The name becomes a FILENAME. `/` and a leading `.` were rejected and
    # everything else waved through, so a 300-char name reached _write_atomic and
    # came back as OSError(ENAMETOOLONG) from a function documented to report
    # rather than raise, and a backslash wrote a file called `a\b.json`.
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        return {"error": f"invalid capability name {name!r} — lowercase letters, "
                         f"digits, '_' and '-', starting with a letter or digit, "
                         f"at most 64 characters"}
    nbytes = len((briefing or "").encode("utf-8"))
    if nbytes > MAX_BRIEFING_BYTES:
        # The cap belongs HERE, not only in the tool: define() is what persists,
        # and the boot scan reloads whatever is on disk in full. Bytes, not
        # characters — a character cap is ~3x looser than it reads for CJK.
        return {"error": f"briefing is {nbytes} bytes, over the "
                         f"{MAX_BRIEFING_BYTES} limit. It is re-sent on every "
                         f"turn of every step holding this capability."}
    missing = _unresolved(sf, tools)
    if missing:
        # Refused rather than warned: skillflow's own note on the same class of
        # miss is that a capability whose tool is missing "grants nothing just as
        # quietly". A registration that half-succeeds is the quiet version.
        return {"error": f"capability {name!r} grants tools that do not resolve: "
                         f"{missing}. Register the tool first."}
    prev = (getattr(sf, "_capabilities", {}) or {}).get(name) or {}
    if context_provider is None and prev.get("context_provider") is not None:
        # An edit replaces the whole definition. A caller that passes no
        # context_provider is not asking to remove one — and removing
        # `stateful`'s is not a small mistake: state_dir injection dies for every
        # pipeline, silently, and the boot scan re-applies the loss on every
        # restart. Keep what you were not asked to change.
        context_provider = prev["context_provider"]
    # A capability the HOST defined in code is not editable from a generated
    # artifact, whatever owner string it presents. The registry's owner check is
    # about accidents; this is about a tool that an LLM step calls.
    if prev and prev.get("owner") == "host" and not host:
        # `owner` was a caller-supplied string, so "is this the host?" could be
        # answered by presenting the right word — and the refusal message names
        # that word. Only the boot path (which re-registers the same definitions
        # on every start) passes host=True; nothing reachable from a step can.
        return {"error": f"capability {name!r} is defined by the host in code; "
                         f"it cannot be redefined at runtime. Pick another name."}
    candidate = {
        "tools": tools, "briefing": briefing, "owner": owner,
        "context_provider": context_provider,
    }
    from core import datadir
    from core.registration_transaction import RegistrationTransaction
    cap_dir = datadir.capabilities_dir()

    def publish():
        release_error = _release_graph_update_error(sf, name, candidate)
        if release_error:
            return {"error": release_error}
        sf.register_capability(
            name, tools=tools, briefing=briefing, owner=owner,
            context_provider=context_provider)
        if persist:
            cap_dir.mkdir(parents=True, exist_ok=True)
            _write_atomic(cap_dir / f"{name}.json", {
                "name": name, "tools": tools, "briefing": briefing,
                "owner": owner,
            })
            # Re-defining lifts the tombstone. Without this the definition is
            # live now but silently disappears at the next boot.
            stale = archived_names()
            if name in stale:
                _write_atomic(_archive_dir() / ARCHIVE_INDEX,
                              sorted(stale - {name}))
                (_archive_dir() / f"{name}.json").unlink(missing_ok=True)
        return None

    try:
        if all(hasattr(sf, attr) for attr in (
                "_lock", "_conn", "_graphs", "_resolvers", "agent_registry")):
            with RegistrationTransaction(sf, paths=[cap_dir]) as transaction:
                error = publish()
                if error:
                    return error
                transaction.commit()
        else:
            error = publish()
            if error:
                return error
    except TypeError as e:
        # A skillflow older than the contract this host calls (briefing=/owner=
        # arrived in 1.5.45). The dev box runs an editable checkout and the
        # container installs from PyPI, so this is the one failure a test here
        # can never see — it must be a legible error, not a TypeError out of
        # get_skillflow() that 500s every request.
        return {"error": f"capability {name!r} not registered: this skillflow "
                         f"does not accept the briefing/owner contract "
                         f"(need >=1.5.45) — {e}"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"capability {name!r} was not committed: {e}"}
    return {"ok": True, "name": name, "tools": tools, "owner": owner}


def offering_configs(sf, name: str) -> list[str]:
    """Every registered graph whose offer list names this capability."""
    out = []
    # by-name-ok: capability definitions are global — no run in scope
    for gname in list(getattr(sf, "_graphs", {})):
        # by-name-ok: capability definitions are global — no run in scope
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
    # An already-archived name still exists as a tombstone plus a file under
    # _archived/. `purge` has to be able to reach those, or a name can be
    # archived and then never fully removed.
    known = (name in caps
             or (capabilities_dir() / f"{name}.json").is_file()
             or (purge and ((_archive_dir() / f"{name}.json").is_file()
                            or name in archived_names())))
    if not known:
        return {"error": f"no capability {name!r}"}
    # Disk first, live second. If the move fails, the capability is still
    # registered AND still on disk — consistent. The other order drops it live
    # while the file survives, and the next boot brings it back.
    src = capabilities_dir() / f"{name}.json"
    if src.is_file():
        if purge:
            src.unlink()
        else:
            os.replace(src, _archive_dir() / f"{name}.json")
    elif purge:
        # Already archived once: purge must reach into _archived/, or the
        # tombstone outlives the thing it marks.
        stale = _archive_dir() / f"{name}.json"
        if stale.is_file():
            stale.unlink()
    if not purge:
        names = archived_names() | {name}
        _write_atomic(_archive_dir() / ARCHIVE_INDEX, sorted(names))
    else:
        _write_atomic(_archive_dir() / ARCHIVE_INDEX,
                      sorted(archived_names() - {name}))
    caps.pop(name, None)
    return {"ok": True, "name": name, "purged": purge}


def load_generated(sf) -> list[str]:
    """Boot scan: register every persisted capability. Returns the names."""
    def load_batch():
        definitions = []
        skip = archived_names()
        for f in sorted(capabilities_dir().glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"capability definition {f} is unreadable: {exc}") from exc
            except OSError:
                log.warning("unreadable capability definition %s", f, exc_info=True)
                continue
            if not isinstance(d, dict):
                raise ValueError(f"capability definition {f} is not an object")
            name = d.get("name") or f.stem
            if name in skip:
                continue
            briefing = d.get("briefing") or ""
            if not isinstance(name, str) or not isinstance(briefing, str):
                raise ValueError(
                    f"capability definition {f} has malformed name/briefing")
            tools = _normalize_tools(d.get("tools"))
            definitions.append((name, tools, briefing))

        loaded = []
        for name, tools, briefing in definitions:
            # The owner is NOT read from the file. This directory IS the generated
            # namespace; a JSON claiming `owner: "host"` for a name the host does
            # not define would otherwise mint an uneditable capability from disk.
            r = define(
                sf, name, tools=tools, briefing=briefing, owner=f"gen:{name}")
            if not r.get("ok"):
                raise ValueError(r["error"])
            loaded.append(name)
        return loaded

    if all(hasattr(sf, attr) for attr in (
            "_lock", "_conn", "_graphs", "_resolvers", "agent_registry")):
        from core.registration_transaction import RegistrationTransaction
        with RegistrationTransaction(
                sf, paths=[capabilities_dir()]) as transaction:
            try:
                loaded = load_batch()
            except Exception as exc:
                log.warning("generated capability boot rolled back: %s", exc)
                return []
            transaction.commit()
            return loaded

    # Minimal compatibility for unit doubles without SkillFlow's registries/DB.
    capability_mapping = getattr(sf, "_capabilities", {})
    before = dict(capability_mapping)
    try:
        return load_batch()
    except Exception as exc:
        capability_mapping.clear()
        capability_mapping.update(before)
        sf._capabilities = capability_mapping
        log.warning("generated capability boot rolled back: %s", exc)
        return []


def palette(sf, config_name: str = "") -> dict:
    """What a declarer may choose from, and what is missing.

    Without `config_name`: the whole registry. With one: the intersection of the
    graph's offer list and the registry, PLUS the difference — a capability a
    pipeline declares but this deployment never registered is a deployment gap
    that has to be visible, not an empty row.
    """
    caps = sf.capabilities()
    if not config_name:
        return {"capabilities": [_row(n, c) for n, c in sorted(caps.items())]}
    # by-name-ok: capability definitions are global — no run in scope
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
    """One palette row: what it is called, what it grants, one line of purpose.

    NOT the briefing. The palette is read by a planner on every run, and a
    200-char slice of a capability's discipline is both useless out of context
    (it is written for the step that HOLDS the capability) and a standing cost
    on a pipeline that may declare nothing — the exact thing this mechanism
    exists to remove. The first non-empty line is enough to choose by; the
    discipline arrives with the grant.
    """
    first = next((ln.strip() for ln in (cap.get("briefing") or "").splitlines()
                  if ln.strip() and not ln.startswith("#")), "")
    return {
        "name": name,
        "tools": list(cap.get("tools") or ()),
        "owner": cap.get("owner", "host"),
        "purpose": first[:120],
    }
