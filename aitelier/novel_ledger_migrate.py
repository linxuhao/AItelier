"""One-shot migration: in-bible history → novel/ledger/ (append-only) +
compact current-state bible.

Before this change, apply_events appended every chapter's changes INTO the
bible: ``progression`` lists on cards and factions, ``setting_log`` in
world.yaml, ``hints`` on threads, ``progress_notes`` on arcs — and flattened
every change key onto the card top level, so one-off keys (本章行动…) stayed on
the card as stale "current" fields forever.

``migrate(repo)`` moves each of those histories into the entity's ledger file
and leaves the bible as a snapshot:
  - card: progression removed; 本章* keys removed (they live in the ledger);
    ``initial`` deep-copied (no YAML anchors); everything else is already the
    last-written value, i.e. the current state.
  - world: factions[*].progression and setting_log → ledger.
  - threads: hints → ledger; thread keeps hint_count/last_hint_chapter/last_hint.
  - arcs: progress_notes + node completions → ledger; arc keeps latest_note.
Appearances are re-derived from chapters/chNNNN/events.yaml.

Refuses to run twice (a non-empty ledger dir means already migrated). Writes
nothing in dry_run mode. Never commits — review ``git diff`` and commit by hand.
"""

from __future__ import annotations

import copy
from pathlib import Path

from aitelier import novel_state as ns


def _tree_bytes(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) \
        if d.is_dir() else 0


PROTECTED_KEYS = {"name", "role", "status", "power_level", "tier", "aliases",
                  "voice", "background", "personality", "golden_finger",
                  "is_protagonist", "first_appearance", "last_appearance",
                  "initial"}


def migrate(repo, dry_run: bool = False, force: bool = False,
            stale_after: int | None = None) -> dict:
    """``stale_after=N``: also drop card state keys whose LAST update is more
    than N chapters before the latest chapter (opt-in — the value stays in the
    ledger, but the current-state card stops carrying it). Profile keys and
    anything in the card's ``initial`` are never dropped."""
    ws = Path(repo)
    if not ns.bible_exists(ws):
        raise ValueError(f"migrate: no novel bible under {ws}")
    led = ns.ledger_dir(ws)
    if led.is_dir() and any(led.rglob("*.jsonl")) and not force:
        raise ValueError(f"migrate: {led} already has ledger files — already "
                         "migrated? (pass force=True to append anyway)")

    bib = ns.bible_dir(ws)
    before = _tree_bytes(bib)
    rows: dict[tuple[str, str], list[dict]] = {}

    def add(kind, name, row):
        rows.setdefault((kind, str(name)), []).append({**row, "migrated": True})

    report = {"characters": 0, "character_rows": 0, "dropped_transient_keys": 0,
              "faction_rows": 0, "setting_rows": 0, "thread_rows": 0,
              "arc_rows": 0, "appearance_rows": 0, "dropped_stale_keys": 0}
    chapters_done = ns.written_chapters(ws)
    latest = chapters_done[-1] if chapters_done else 0

    # ── characters ──
    new_cards: dict[Path, dict] = {}
    for p in sorted(ns.characters_dir(ws).glob("*.yaml")):
        card = ns.load_yaml(p, {}) or {}
        name = str(card.get("name") or p.stem)
        last_set: dict[str, int] = {}
        for e in card.get("progression") or []:
            for k in (e.get("changes") or {}):
                last_set[k] = e.get("chapter") or 0
        if stale_after is not None:
            keep = PROTECTED_KEYS | set(card.get("initial") or {})
            for k, ch in last_set.items():
                if k in card and k not in keep and latest - ch > stale_after:
                    card.pop(k)
                    report["dropped_stale_keys"] += 1
        for e in card.pop("progression", None) or []:
            add("characters", name, {"chapter": e.get("chapter"), "type": "event",
                                     "changes": e.get("changes") or {},
                                     "reason": e.get("reason", "")})
            report["character_rows"] += 1
        for k in [k for k in card if ns.is_transient_key(k)]:
            card.pop(k)
            report["dropped_transient_keys"] += 1
        if "initial" in card:
            card["initial"] = copy.deepcopy(card["initial"])
        new_cards[p] = copy.deepcopy(card)
        report["characters"] += 1

    for n in ns.written_chapters(ws):
        ev = ns.load_yaml(ns.chapter_dir(ws, n) / "events.yaml", {}) or {}
        for ap in ev.get("appearances") or []:
            if ap.get("name"):
                row = {"chapter": n, "type": "appearance"}
                if ap.get("importance") is not None:
                    row["importance"] = ap["importance"]
                add("characters", ap["name"], row)
                report["appearance_rows"] += 1

    # ── world: factions + settings ──
    world_path = bib / "world.yaml"
    world = ns.load_yaml(world_path, {}) or {}
    for fname, f in (world.get("factions") or {}).items():
        if isinstance(f, dict):
            for e in f.pop("progression", None) or []:
                add("factions", fname, {"chapter": e.get("chapter"), "type": "event",
                                        "changes": e.get("changes") or {},
                                        "reason": e.get("reason", "")})
                report["faction_rows"] += 1
    for e in world.pop("setting_log", None) or []:
        add("settings", e.get("name", "_unnamed"),
            {"chapter": e.get("chapter"), "type": "event",
             "changes": e.get("changes") or {}, "reason": e.get("reason", "")})
        report["setting_rows"] += 1

    # ── threads ──
    threads_path = bib / "threads.yaml"
    threads = ns.load_yaml(threads_path, []) or []
    for t in threads:
        tname = t.get("name")
        if t.get("introduced_chapter"):
            add("threads", tname, {"chapter": t["introduced_chapter"],
                                   "type": "register", "detail": ""})
        hints = t.pop("hints", None) or []
        for h in hints:
            add("threads", tname, {"chapter": h.get("chapter"), "type": "hint",
                                   "detail": h.get("hint", "")})
            report["thread_rows"] += 1
        if hints:
            t["hint_count"] = len(hints)
            t["last_hint_chapter"] = hints[-1].get("chapter")
            t["last_hint"] = hints[-1].get("hint", "")
        if t.get("resolution_chapter"):
            add("threads", tname, {"chapter": t["resolution_chapter"],
                                   "type": "resolve",
                                   "detail": t.get("resolution", "")})

    # ── arcs ──
    arcs_path = bib / "arcs.yaml"
    arcs = ns.load_yaml(arcs_path, []) or []
    for a in arcs:
        aname = a.get("name")
        for nd in a.get("nodes") or []:
            if nd.get("completed_chapter"):
                add("arcs", aname, {"chapter": nd["completed_chapter"],
                                    "type": "node_completed",
                                    "node": str(nd.get("id"))})
                report["arc_rows"] += 1
        notes = a.pop("progress_notes", None) or []
        for nt in notes:
            add("arcs", aname, {"chapter": nt.get("chapter"), "type": "note",
                                "detail": nt.get("note", "")})
            report["arc_rows"] += 1
        if notes:
            a["latest_note"] = notes[-1]

    report["ledger_files"] = len(rows)
    if dry_run:
        report["dry_run"] = True
        return report

    for p, card in new_cards.items():
        ns.dump_yaml(p, card)
    if world:
        ns.dump_yaml(world_path, world)
    if threads_path.is_file():
        ns.dump_yaml(threads_path, threads)
    if arcs_path.is_file():
        ns.dump_yaml(arcs_path, arcs)
    for (kind, name), rs in rows.items():
        rs.sort(key=lambda r: (r.get("chapter") or 0))
        for r in rs:
            ns.ledger_append(ws, kind, name, r)
    ns.rebuild_index(ws)

    report["bible_bytes_before"] = before
    report["bible_bytes_after"] = _tree_bytes(bib)
    report["ledger_bytes"] = _tree_bytes(led)
    return report
