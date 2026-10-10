"""One-shot migration: in-bible history → novel/ledger/ (append-only) +
compact current-state bible.

Before this change, apply_events appended every chapter's changes INTO the
bible: ``progression`` lists on cards and factions, ``setting_log`` in
world.yaml, ``hints`` on threads, ``progress_notes`` on arcs — and flattened
every change key onto the card top level, so one-off keys (本章行动…) stayed on
the card as stale "current" fields forever.

``migrate(repo)``:
  - bible: strips every legacy history block (``strip_legacy_history``) and
    the 本章* keys, keeps every other value as written (the last-written value
    IS the current state); threads get hint_count/last_hint_chapter/last_hint,
    arcs get latest_note — exactly what the new apply_* code would have written.
  - ledger: REBUILT FROM THE IMMUTABLE JOURNALS (chapters/chNNNN/events.yaml)
    with the same row shapes and order apply_state produces, so a migrated repo
    is byte-identical to a fresh replay of its journals under the new code
    (the writing bench's replay guard depends on that). Existing ledger rows
    (chapters booked after deploy, before migration) are kept and merged in
    chapter order; chapters already present in the ledger are not re-derived.

Detection is by CONTENT: a bible with no legacy history blocks has nothing to
migrate (re-running is a no-op). All writes are one ``state_transaction`` —
a failure restores bible/ + ledger/ byte-for-byte. Never commits.
``stale_after`` is opt-in and makes the result diverge from a pure replay.
"""

from __future__ import annotations

import json
from pathlib import Path

from aitelier import novel_state as ns


def _tree_bytes(d: Path) -> int:
    return sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) \
        if d.is_dir() else 0


PROTECTED_KEYS = {"name", "role", "status", "power_level", "tier", "aliases",
                  "voice", "background", "personality", "golden_finger",
                  "is_protagonist", "first_appearance", "last_appearance",
                  "initial"}


def has_legacy_history(ws) -> bool:
    for card in ns.load_characters(ws).values():
        # Empty legacy lists (progression: [] on an untouched genesis card)
        # are what a replay leaves too — only CONTENT means "not migrated".
        if card.get("progression") or any(ns.is_transient_key(k) for k in card):
            return True
    world = ns.load_yaml(ns.bible_dir(ws) / "world.yaml", {}) or {}
    if "setting_log" in world or any(
            isinstance(f, dict) and f.get("progression")
            for f in (world.get("factions") or {}).values()):
        return True
    if any(t.get("hints") for t in ns.load_yaml(ns.bible_dir(ws) / "threads.yaml", []) or []):
        return True
    return any("progress_notes" in a
               for a in ns.load_yaml(ns.bible_dir(ws) / "arcs.yaml", []) or [])


def journal_ledger_rows(ws, genesis_cast: set[str], genesis_threads: set[str]
                        ) -> list[tuple[str, str, dict]]:
    """(kind, entity, row) in apply_state order, derived from events.yaml."""
    out: list[tuple[str, str, dict]] = []
    cards = ns.load_characters(ws)
    protagonist = ns._find_protagonist(cards)
    known = set(genesis_cast)
    threads_known = set(genesis_threads)
    arcs = {str(a.get("name")): {str(nd.get("id")) for nd in a.get("nodes") or []}
            for a in ns.load_yaml(ns.bible_dir(ws) / "arcs.yaml", []) or []}
    done: dict[str, set] = {k: set() for k in arcs}
    for n in ns.written_chapters(ws):
        rec = ns.load_yaml(ns.chapter_dir(ws, n) / "events.yaml", {}) or {}
        for ev in rec.get("events") or []:
            et, name = ev.get("entity_type"), str(ev.get("entity_name") or "")
            changes, reason = ev.get("changes") or {}, str(ev.get("reason") or "")
            if et in ("character", "protagonist"):
                if et == "protagonist" and name not in known and protagonist:
                    name = protagonist
                created = name not in known
                known.add(name)
                out.append(("characters", name, {
                    "chapter": n, "type": "create" if created else "event",
                    "changes": changes, "reason": reason}))
            elif et == "faction":
                out.append(("factions", name, {"chapter": n, "type": "event",
                                               "changes": changes, "reason": reason}))
            elif et == "world_setting":
                out.append(("settings", name, {"chapter": n, "type": "event",
                                               "changes": changes, "reason": reason}))
        for ap in rec.get("appearances") or []:
            nm = str(ap.get("name") or "")
            if nm in known:
                row = {"chapter": n, "type": "appearance"}
                if ap.get("importance") is not None:
                    row["importance"] = ap.get("importance")
                out.append(("characters", nm, row))
        for up in rec.get("thread_updates") or []:
            nm, action = str(up.get("name") or ""), up.get("action")
            if nm not in threads_known:
                if action == "register":
                    threads_known.add(nm)
                    out.append(("threads", nm, {"chapter": n, "type": "register",
                                                "detail": up.get("detail", "")}))
                continue
            if action in ("hint", "resolve", "abandon"):
                out.append(("threads", nm, {"chapter": n, "type": action,
                                            "detail": up.get("detail", "")}))
        for up in rec.get("arc_updates") or []:
            nm = str(up.get("name") or "")
            if nm not in arcs:
                continue
            for nid in up.get("nodes_completed") or []:
                nid = str(nid)
                if nid in arcs[nm] and nid not in done[nm]:
                    done[nm].add(nid)
                    out.append(("arcs", nm, {"chapter": n, "type": "node_completed",
                                             "node": nid}))
            if up.get("notes"):
                out.append(("arcs", nm, {"chapter": n, "type": "note",
                                         "detail": up["notes"]}))
    return out


def strip_legacy_history(ws, touched: set[tuple[str, str]],
                         stale_after: int | None = None) -> dict:
    """Bring a pre-ledger bible to the new current-state shape, in place.

    Mirrors what the new apply_* code does to an entity it TOUCHES (pops the
    legacy ``progression``/``hints`` block): only entities with a journal row
    (``touched``) are rewritten, so an untouched genesis card keeps its empty
    ``progression: []`` exactly as a replay of the journals would."""
    rep = {"characters": 0, "dropped_transient_keys": 0, "dropped_stale_keys": 0}
    latest = (ns.written_chapters(ws) or [0])[-1]
    bib = ns.bible_dir(ws)
    for p in sorted(ns.characters_dir(ws).glob("*.yaml")):
        card = ns.load_yaml(p, {}) or {}
        cname = str(card.get("name") or p.stem)
        if ("characters", cname) not in touched and not card.get("progression") \
                and not any(ns.is_transient_key(k) for k in card):
            continue
        before = dict(card)
        prog = card.pop("progression", None) or []
        for k in [k for k in card if ns.is_transient_key(k)]:
            card.pop(k)
            rep["dropped_transient_keys"] += 1
        if stale_after is not None:
            last_set: dict[str, int] = {}
            for e in prog:
                for k in (e.get("changes") or {}):
                    last_set[k] = e.get("chapter") or 0
            keep = PROTECTED_KEYS | set(card.get("initial") or {})
            for k, ch in last_set.items():
                if k in card and k not in keep and latest - ch > stale_after:
                    card.pop(k)
                    rep["dropped_stale_keys"] += 1
        rep["characters"] += 1
        if card != before:
            ns.dump_yaml(p, card)
    wp = bib / "world.yaml"
    world = ns.load_yaml(wp, None)
    if isinstance(world, dict):
        changed = world.pop("setting_log", None) is not None
        for fname, f in (world.get("factions") or {}).items():
            if isinstance(f, dict) and "progression" in f and (
                    ("factions", str(fname)) in touched or f["progression"]):
                f.pop("progression")
                changed = True
        if changed:
            ns.dump_yaml(wp, world)
    tp = bib / "threads.yaml"
    threads = ns.load_yaml(tp, None)
    if isinstance(threads, list):
        hit = [t for t in threads if "hints" in t and (
            ("threads", str(t.get("name"))) in touched or t.get("hints"))]
        for t in hit:
            ns.absorb_legacy_hints(t)
        if hit:
            ns.dump_yaml(tp, threads)
    ap = bib / "arcs.yaml"
    arcs = ns.load_yaml(ap, None)
    if isinstance(arcs, list) and any("progress_notes" in a for a in arcs):
        for a in arcs:
            notes = a.pop("progress_notes", None) or []
            if notes:
                a["latest_note"] = notes[-1]
        ns.dump_yaml(ap, arcs)
    return rep


def migrate(repo, dry_run: bool = False, force: bool = False,
            stale_after: int | None = None) -> dict:
    ws = Path(repo)
    if not ns.bible_exists(ws):
        raise ValueError(f"migrate: no novel bible under {ws}")
    if not has_legacy_history(ws) and not force:
        return {"migrated": False, "reason": "no legacy history in bible — "
                "nothing to migrate"}
    bib, led = ns.bible_dir(ws), ns.ledger_dir(ws)
    before = _tree_bytes(bib)

    # Genesis cast/threads = entities never created/registered by a journal.
    created, registered = set(), set()
    for n in ns.written_chapters(ws):
        rec = ns.load_yaml(ns.chapter_dir(ws, n) / "events.yaml", {}) or {}
        created |= {str(e.get("entity_name")) for e in rec.get("events") or []
                    if e.get("create")}
        registered |= {str(u.get("name")) for u in rec.get("thread_updates") or []
                       if u.get("action") == "register"}
    cast = set(ns.load_characters(ws)) - created
    threads = {str(t.get("name")) for t in
               ns.load_yaml(bib / "threads.yaml", []) or []} - registered
    # Legacy bibles may list an early-registered thread in genesis too; a
    # thread registered by a journal is only "known" from that chapter on.

    existing: dict[tuple[str, str], list[dict]] = {}
    booked: set[int] = set()
    if led.is_dir():
        for kind in ns.LEDGER_KINDS:
            for f in sorted((led / kind).glob("*.jsonl")) if (led / kind).is_dir() else []:
                rows = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
                if rows:
                    existing[(kind, rows[0].get("entity", f.stem))] = rows
                    booked |= {r.get("chapter") for r in rows}

    derived = [(k, nm, r) for k, nm, r in journal_ledger_rows(ws, cast, threads)
               if r["chapter"] not in booked]
    rows: dict[tuple[str, str], list[dict]] = {}
    for k, nm, r in derived:
        rows.setdefault((k, nm), []).append({"entity": nm, **r})
    for key, rs in existing.items():
        rows.setdefault(key, []).extend(rs)

    report = {"migrated": True, "ledger_rows": len(derived),
              "kept_existing_rows": sum(len(v) for v in existing.values()),
              "ledger_files": len(rows)}
    if dry_run:
        report["dry_run"] = True
        return report

    with ns.state_transaction(ws):
        touched = {(k, nm) for k, nm, _ in journal_ledger_rows(ws, cast, threads)} \
            | set(existing)
        report.update(strip_legacy_history(ws, touched, stale_after=stale_after))
        for (kind, name), rs in rows.items():
            rs.sort(key=lambda r: (r.get("chapter") or 0))   # stable: keeps order within a chapter
            p = ns.ledger_path(ws, kind, name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                                 for r in rs), encoding="utf-8")
        ns.rebuild_index(ws)

    report["bible_bytes_before"] = before
    report["bible_bytes_after"] = _tree_bytes(bib)
    report["ledger_bytes"] = _tree_bytes(led)
    return report
