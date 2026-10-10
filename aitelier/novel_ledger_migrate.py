"""One-shot migration: in-bible history → novel/ledger/ (append-only) +
compact current-state bible.

Before this change, apply_events appended every chapter's changes INTO the
bible (``progression`` on cards/factions, ``setting_log``, thread ``hints``,
arc ``progress_notes``) and flattened every change key onto the card, so
one-off keys (本章行动…) stayed on the card forever.

Two modes, picked automatically:

* **replay** (repo has the ``novel-genesis`` git tag — every scaffolded book):
  reset exactly the paths a replay rebuilds (``ns.REPLAY_MANAGED``) to the
  genesis bible and re-book every ``chapters/*/events.yaml`` with the new code
  (``ns.replay_chapter``, the same function the writing bench's replay guard
  uses). The result is byte-identical to a bench replay BY CONSTRUCTION.
  Safety: before committing to it, the replayed current state is compared
  (as data) with the legacy bible minus its history blocks; any difference
  means the bible was edited outside the journal, and the migration refuses
  (rolls back) unless ``force=True``. The differences are reported.
* **strip** (no genesis tag): history blocks and 本章* keys are removed from
  the bible in place, the ledger is derived from the journals. Correct
  current state, but NOT guaranteed byte-identical to a replay (key order of
  summary fields may differ) — such books cannot use the writing bench's
  replay guard.

Detection is by CONTENT (no legacy history left = no-op). All writes are one
``state_transaction``. Never commits. ``stale_after`` is opt-in and breaks
replay equality.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

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
            stale_after: int | None = None, mode: str = "auto") -> dict:
    ws = Path(repo)
    if not ns.bible_exists(ws):
        raise ValueError(f"migrate: no novel bible under {ws}")
    if not has_legacy_history(ws) and not force:
        return {"migrated": False, "reason": "no legacy history in bible — "
                "nothing to migrate"}
    if mode == "auto":
        mode = "replay" if ns.has_genesis_tag(ws) else "strip"
    if mode == "replay":
        return _migrate_replay(ws, dry_run=dry_run, force=force,
                               stale_after=stale_after)
    if mode != "strip":
        raise ValueError(f"migrate: unknown mode {mode!r}")
    return _migrate_strip(ws, dry_run=dry_run, force=force,
                          stale_after=stale_after)


def _genesis_files(ws: Path) -> dict[str, bytes]:
    names = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only",
         "-z", ns.GENESIS_TAG, "--", "novel"], cwd=ws, check=True,
        capture_output=True).stdout.decode("utf-8").split("\0")
    out = {}
    for name in filter(None, names):
        if any(name == rel or name.startswith(rel + "/") for rel in ns.REPLAY_MANAGED):
            out[name] = subprocess.run(["git", "show", f"{ns.GENESIS_TAG}:{name}"],
                                       cwd=ws, check=True, capture_output=True).stdout
    return out


def _norm(x):
    """Ignore what the new code never stores as state: 本章* keys and null
    values (null now CLEARS a key), at any depth."""
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()
                if v is not None and not ns.is_transient_key(k)}
    if isinstance(x, list):
        return [_norm(v) for v in x]
    return x


def _state_snapshot(ws: Path) -> dict:
    """Current-state DATA (not bytes) of the replay-managed bible files."""
    bib = ns.bible_dir(ws)
    return _norm({"characters": {n: c for n, c in ns.load_characters(ws).items()},
            "world": ns.load_yaml(bib / "world.yaml", {}) or {},
            "threads": ns.load_yaml(bib / "threads.yaml", []) or [],
            "arcs": ns.load_yaml(bib / "arcs.yaml", []) or []})


def _diff(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in list(a) + [k for k in b if k not in a]:
            if k not in b:
                out.append(f"{path}{k}: 只在旧 bible 里")
            elif k not in a:
                out.append(f"{path}{k}: 只在重放结果里")
            else:
                out += _diff(a[k], b[k], f"{path}{k}.")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b) \
            and all(isinstance(x, dict) for x in a + b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _diff(x, y, f"{path}{x.get('name', x.get('id', i))}.")
        return out
    return [] if a == b else [f"{path.rstrip('.')}: 旧={str(a)[:80]!r} 重放={str(b)[:80]!r}"]


def _migrate_replay(ws: Path, dry_run: bool, force: bool,
                    stale_after: int | None) -> dict:
    bib, led = ns.bible_dir(ws), ns.ledger_dir(ws)
    before = _tree_bytes(bib)
    genesis = _genesis_files(ws)
    if not any(n.startswith("novel/bible/characters/") for n in genesis):
        raise ValueError("migrate: novel-genesis tag carries no character cards")

    # Expected current state = legacy bible minus history (strip mode on a copy).
    with tempfile.TemporaryDirectory(prefix="ledger_mig_") as td:
        shutil.copytree(ns.novel_root(ws), Path(td) / "novel", symlinks=True)
        _migrate_strip(Path(td), dry_run=False, force=True, stale_after=None)
        expected = _state_snapshot(Path(td))

    report = {"migrated": True, "mode": "replay"}
    try:
        _replay_into(ws, genesis, expected, report, dry_run, force, stale_after)
    except _DryRun:
        report["dry_run"] = True
        return report
    report["bible_bytes_before"] = before
    report["bible_bytes_after"] = _tree_bytes(bib)
    report["ledger_bytes"] = _tree_bytes(led)
    return report


def _drop_stale_from_ledger(ws: Path, stale_after: int) -> int:
    latest = (ns.written_chapters(ws) or [0])[-1]
    dropped = 0
    for p in sorted(ns.characters_dir(ws).glob("*.yaml")):
        card = ns.load_yaml(p, {}) or {}
        name = str(card.get("name") or p.stem)
        last: dict[str, int] = {}
        for r in ns.read_ledger(ws, "characters", name):
            for k in (r.get("changes") or {}):
                last[k] = r.get("chapter") or 0
        keep = PROTECTED_KEYS | set(card.get("initial") or {})
        gone = [k for k, ch in last.items()
                if k in card and k not in keep and latest - ch > stale_after]
        for k in gone:
            card.pop(k)
        if gone:
            dropped += len(gone)
            ns.dump_yaml(p, card)
    return dropped


def _replay_into(ws, genesis, expected, report, dry_run, force, stale_after):
    with ns.state_transaction(ws):
        for rel in ns.REPLAY_MANAGED:
            p = ws / rel
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
        for name, raw in genesis.items():
            dest = ws / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        warnings: list[str] = []
        for n in ns.written_chapters(ws):
            rec = ns.load_yaml(ns.chapter_dir(ws, n) / "events.yaml", {}) or {}
            if rec.get("chapter") != n:
                raise ValueError(f"migrate: ch{n:04d}/events.yaml declares chapter "
                                 f"{rec.get('chapter')!r}")
            warnings += ns.replay_chapter(ws, n, rec)
        ns.rebuild_digest(ws)
        ns.rebuild_index(ws)
        drift = _diff(expected, _state_snapshot(ws))
        report.update(replay_warnings=warnings, drift=drift,
                      ledger_rows=sum(len(ns.read_ledger(ws, k, f.stem))
                                      for k in ns.LEDGER_KINDS
                                      for f in (ns.ledger_dir(ws) / k).glob("*.jsonl")))
        if dry_run:
            raise _DryRun(report)          # rolls the transaction back
        if drift and not force:
            raise MigrationDrift(drift)
        if stale_after is not None:
            report["dropped_stale_keys"] = _drop_stale_from_ledger(ws, stale_after)


class MigrationDrift(ValueError):
    def __init__(self, drift: list[str]):
        self.drift = drift
        super().__init__(
            f"migrate: 重放结果与旧 bible 当前状态有 {len(drift)} 处不同（bible 曾被绕过记账"
            "直接修改？）。已回滚、未写入。前几处：\n- " + "\n- ".join(drift[:20])
            + "\n确认以重放结果为准请加 --force；或改用 --mode strip（保留旧值，但不能用"
            " bench 重放）。")


class _DryRun(Exception):
    def __init__(self, report):
        self.report = report


def _migrate_strip(ws: Path, dry_run: bool = False, force: bool = False,
                   stale_after: int | None = None) -> dict:
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

    report = {"migrated": True, "mode": "strip", "ledger_rows": len(derived),
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
