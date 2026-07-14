"""novel_state — shared state layer for the novel pipelines (novel_init / novel_chapter).

The novel's durable state lives under ``<code_repo>/novel/`` as plain files
(git-committed per chapter), following a double-entry model:

  novel/
    bible/                 ← "account balances" (current state, slow-changing)
      overview.md            总纲 (core conflict, growth arc, ending direction)
      compass.md             指南针 (endgame direction + active long lines)
      world.yaml             力量体系 / 地理 / 势力 / world rules
      pacing.yaml            爽点公式 / word-count & hook conventions (chapter RHYTHM)
      characters/<name>.yaml 人物卡 incl. progression (time series, not snapshot)
      threads.yaml           伏笔登记表 (status/hints/earliest_reveal node gate)
      arcs.yaml              故事线：有序剧情节点 nodes (plot PROGRESS)
    chapters/chNNNN/       ← immutable per-chapter record
      prose.md  summary.md  events.yaml   (events = "journal entries", append-only)
    state/
      digest.md            ← rolling recap: recent summaries full, older one-line
      index.yaml           ← derived index (never the source of truth)

PLOT IS NODE-DRIVEN, NOT CHAPTER-ANCHORED: an arc is an ordered list of plot
nodes ``{id, beat, status: pending|done, completed_chapter}``. The frontier
(what to write next) is DERIVED — the first non-done node — never stored as a
cursor. ``completed_chapter`` is a historical record, not a prediction; chapter
counts are never used to schedule plot. Threads gate their reveal on a node
(``earliest_reveal: {arc, node}``), mechanically checkable. pacing.yaml keeps
chapter-level rhythm (爽点密度/字数) — rhythm is per-chapter, progress is per-node.

``reconcile`` replays every chapter's events over the GENESIS BASELINE — the
git tag ``novel-genesis`` stamped by scaffold_bible (no duplicated snapshot
directory; git is the source of history) — and diffs the result against the
live bible. Drift means somebody bypassed the journal, and is reported loudly.

No database: the skillflow engine owns workflow enforcement, git owns audit.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path

import yaml

# ── Layout ───────────────────────────────────────────────────────────────────

NOVEL_DIR = "novel"


def novel_root(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / NOVEL_DIR


def bible_dir(ws) -> Path:
    return novel_root(ws) / "bible"


def characters_dir(ws) -> Path:
    return bible_dir(ws) / "characters"


def chapters_dir(ws) -> Path:
    return novel_root(ws) / "chapters"


def state_dir(ws) -> Path:
    return novel_root(ws) / "state"


GENESIS_TAG = "novel-genesis"  # git tag stamped by scaffold_bible = reconcile baseline


def chapter_dirname(n: int) -> str:
    return f"ch{n:04d}"


def chapter_dir(ws, n: int) -> Path:
    return chapters_dir(ws) / chapter_dirname(n)


def bible_exists(ws) -> bool:
    return bible_dir(ws).is_dir() and (bible_dir(ws) / "overview.md").is_file()


def git_commit(repo_root, message: str, subpath: str = "novel") -> bool:
    """Best-effort commit of the novel tree into the project's git repo, so the
    downloadable code repo carries per-chapter history. No-op (returns False) if
    the root isn't a git repo or git fails — content is never lost either way
    (the files are on disk; the immutable chapter records are the source of truth).
    Git identity comes from the container's AItelier gitconfig (ambient env)."""
    root = Path(repo_root)
    if not (root / ".git").is_dir():
        return False
    try:
        subprocess.run(["git", "add", subpath], cwd=root, check=True,
                       capture_output=True)
        # Nothing staged (no changes) → skip the commit cleanly.
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root)
        if staged.returncode == 0:
            return False
        subprocess.run(["git", "commit", "-m", message], cwd=root, check=True,
                       capture_output=True)
        return True
    except Exception:
        import logging
        logging.getLogger("aitelier.novel").warning(
            "git_commit failed for %s", root, exc_info=True)
        return False


# ── YAML / text helpers ──────────────────────────────────────────────────────

def git_tag_genesis(repo_root) -> bool:
    """Stamp the current HEAD as the reconcile baseline. Idempotent-hostile on
    purpose: a second tagging attempt fails (scaffold must run once)."""
    root = Path(repo_root)
    if not (root / ".git").is_dir():
        return False
    try:
        subprocess.run(["git", "tag", GENESIS_TAG], cwd=root, check=True,
                       capture_output=True)
        return True
    except Exception:
        import logging
        logging.getLogger("aitelier.novel").warning(
            "git_tag_genesis failed for %s", root, exc_info=True)
        return False


def has_genesis_tag(repo_root) -> bool:
    root = Path(repo_root)
    if not (root / ".git").is_dir():
        return False
    r = subprocess.run(["git", "rev-parse", "-q", "--verify",
                        f"refs/tags/{GENESIS_TAG}"], cwd=root,
                       capture_output=True)
    return r.returncode == 0


def genesis_characters(repo_root) -> dict[str, dict] | None:
    """Baseline character cards read from the ``novel-genesis`` git tag —
    no duplicated snapshot directory; git IS the history. Returns None when
    the baseline is unavailable (no git / no tag), letting reconcile skip."""
    root = Path(repo_root)
    if not has_genesis_tag(root):
        return None
    try:
        ls = subprocess.run(
            # quotepath=false: Chinese filenames must come back verbatim, not
            # octal-escaped in quotes (which would break the .yaml suffix check
            # and the subsequent `git show`).
            ["git", "-c", "core.quotepath=false", "ls-tree", "-r",
             "--name-only", GENESIS_TAG, f"{NOVEL_DIR}/bible/characters/"],
            cwd=root, check=True, capture_output=True, text=True).stdout
        out: dict[str, dict] = {}
        for rel in ls.splitlines():
            rel = rel.strip()
            if not rel.endswith(".yaml"):
                continue
            show = subprocess.run(["git", "show", f"{GENESIS_TAG}:{rel}"],
                                  cwd=root, check=True, capture_output=True,
                                  text=True).stdout
            card = yaml.safe_load(show) or {}
            name = str(card.get("name") or Path(rel).stem)
            out[name] = card
        return out
    except Exception:
        import logging
        logging.getLogger("aitelier.novel").warning(
            "genesis_characters read failed for %s", root, exc_info=True)
        return None


def load_yaml(path: Path, default=None):
    if not path.is_file():
        return default
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if data is not None else default


def dump_yaml(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def char_count(text: str) -> int:
    """网文字数: non-whitespace character count (CJK prose convention)."""
    return len(re.sub(r"\s", "", text or ""))


_FNAME_BAD = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def safe_filename(name: str) -> str:
    """Character names become filenames (Chinese is fine); strip path hazards."""
    cleaned = _FNAME_BAD.sub("_", (name or "").strip())
    return cleaned or "_unnamed"


# ── Chapter numbering ────────────────────────────────────────────────────────

_CH_RE = re.compile(r"^ch(\d{4})$")


def written_chapters(ws) -> list[int]:
    d = chapters_dir(ws)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        m = _CH_RE.match(p.name)
        if m and p.is_dir() and (p / "prose.md").is_file():
            out.append(int(m.group(1)))
    return sorted(out)


def next_chapter_number(ws) -> int:
    done = written_chapters(ws)
    return (done[-1] + 1) if done else 1


# ── Characters ───────────────────────────────────────────────────────────────

def load_characters(ws) -> dict[str, dict]:
    """name → card. Also indexes aliases so mention-scans catch both."""
    out: dict[str, dict] = {}
    d = characters_dir(ws)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.yaml")):
        card = load_yaml(p, {})
        name = str(card.get("name") or p.stem)
        card.setdefault("name", name)
        out[name] = card
    return out


def character_path(ws, name: str) -> Path:
    return characters_dir(ws) / f"{safe_filename(name)}.yaml"


# ── Events (journal entries) ─────────────────────────────────────────────────

KNOWN_ENTITY_TYPES = ("character", "protagonist", "faction", "world_setting")


def _find_protagonist(characters: dict[str, dict]) -> str | None:
    for name, card in characters.items():
        if card.get("is_protagonist") or card.get("role") == "protagonist":
            return name
    return None


def apply_events(ws, events: list[dict], chapter: int) -> list[str]:
    """Post journal entries to the bible balances. Returns human warnings.

    Character/protagonist changes shallow-merge into the card and append a
    ``progression`` entry {chapter, changes, reason} — the card stays a time
    series, never just a snapshot. Unknown entities are a hard error unless
    the entry carries ``create: true`` (new characters must be deliberate).
    """
    warnings: list[str] = []
    characters = load_characters(ws)
    world_path = bible_dir(ws) / "world.yaml"
    world = load_yaml(world_path, {}) or {}

    for ev in events or []:
        etype = ev.get("entity_type")
        name = str(ev.get("entity_name") or "")
        changes = ev.get("changes") or {}
        reason = str(ev.get("reason") or "")
        if etype not in KNOWN_ENTITY_TYPES:
            raise ValueError(f"events: unknown entity_type {etype!r} (entry: {ev})")
        if not name:
            raise ValueError(f"events: entity_name missing (entry: {ev})")

        if etype in ("character", "protagonist"):
            if etype == "protagonist" and name not in characters:
                resolved = _find_protagonist(characters)
                if resolved:
                    name = resolved
            card = characters.get(name)
            if card is None:
                if not ev.get("create"):
                    raise ValueError(
                        f"events: character '{name}' not in bible and create!=true — "
                        "new characters must be explicitly created")
                card = {"name": name, "status": "alive", "progression": []}
                characters[name] = card
            # Guardrails the reviewer relies on:
            if card.get("status") == "dead" and changes.get("status") not in ("alive",):
                warnings.append(
                    f"character '{name}' is dead but received changes "
                    f"({list(changes)}) — check for 吃书")
            old_p = card.get("power_level")
            new_p = changes.get("power_level")
            if isinstance(old_p, (int, float)) and isinstance(new_p, (int, float)) \
                    and new_p < old_p:
                warnings.append(
                    f"character '{name}' power_level regressed {old_p}→{new_p} "
                    f"(reason: {reason or 'none'})")
            for k, v in changes.items():
                card[k] = v
            card.setdefault("progression", []).append(
                {"chapter": chapter, "changes": changes, "reason": reason})
            dump_yaml(character_path(ws, name), card)

        elif etype == "faction":
            factions = world.setdefault("factions", {})
            if name not in factions and not ev.get("create"):
                raise ValueError(
                    f"events: faction '{name}' not in world.yaml and create!=true")
            entry = factions.setdefault(name, {})
            for k, v in changes.items():
                entry[k] = v
            entry.setdefault("progression", []).append(
                {"chapter": chapter, "changes": changes, "reason": reason})
            dump_yaml(world_path, world)

        else:  # world_setting
            settings = world.setdefault("settings", {})
            entry = settings.setdefault(name, {})
            if isinstance(entry, dict):
                for k, v in changes.items():
                    entry[k] = v
            else:
                settings[name] = changes
            world.setdefault("setting_log", []).append(
                {"chapter": chapter, "name": name, "changes": changes,
                 "reason": reason})
            dump_yaml(world_path, world)

    return warnings


def log_appearances(ws, appearances: list[dict], chapter: int) -> list[str]:
    """Update characters' last_appearance (the 防配角蒸发 ledger)."""
    warnings: list[str] = []
    characters = load_characters(ws)
    for ap in appearances or []:
        name = str(ap.get("name") or "")
        card = characters.get(name)
        if card is None:
            warnings.append(f"appearance logged for unknown character '{name}'")
            continue
        card["last_appearance"] = chapter
        if not card.get("first_appearance"):
            card["first_appearance"] = chapter
        dump_yaml(character_path(ws, name), card)
    return warnings


# ── Plot nodes (arcs) / threads — NODE-DRIVEN, chapter numbers are records ───

def arc_frontier(arc: dict) -> dict | None:
    """The first non-done node = what this arc advances next. DERIVED, never
    stored — no cursor state to rot. None when the arc is fully done."""
    for node in arc.get("nodes") or []:
        if node.get("status") != "done":
            return node
    return None


def node_done(arcs: list[dict], arc_name: str, node_id: str) -> bool:
    for a in arcs:
        if str(a.get("name")) == str(arc_name):
            for nd in a.get("nodes") or []:
                if str(nd.get("id")) == str(node_id):
                    return nd.get("status") == "done"
    return False


def thread_revealable(thread: dict, arcs: list[dict]) -> bool:
    """A thread may be RESOLVED only after its gate node is done. No gate =
    always revealable. Mechanically checkable — no chapter predictions."""
    gate = thread.get("earliest_reveal")
    if not isinstance(gate, dict):
        return True
    return node_done(arcs, gate.get("arc", ""), gate.get("node", ""))


def apply_thread_updates(ws, updates: list[dict], chapter: int) -> list[str]:
    warnings: list[str] = []
    path = bible_dir(ws) / "threads.yaml"
    threads = load_yaml(path, []) or []
    arcs = load_yaml(bible_dir(ws) / "arcs.yaml", []) or []
    by_name = {str(t.get("name")): t for t in threads}
    for up in updates or []:
        name = str(up.get("name") or "")
        action = up.get("action")
        t = by_name.get(name)
        if t is None:
            if action == "register":
                t = {"name": name, "status": "open",
                     "introduced_chapter": chapter, "hints": []}
                for k in ("description", "type", "importance", "earliest_reveal"):
                    if up.get(k) is not None:
                        t[k] = up[k]
                threads.append(t)
                by_name[name] = t
                continue
            warnings.append(f"thread update for unknown thread '{name}' ({action})")
            continue
        if action == "hint":
            t.setdefault("hints", []).append(
                {"chapter": chapter, "hint": up.get("detail", "")})
        elif action == "resolve":
            if not thread_revealable(t, arcs):
                gate = t.get("earliest_reveal", {})
                warnings.append(
                    f"thread '{name}' resolved BEFORE its gate node "
                    f"{gate.get('arc')}/{gate.get('node')} is done — 提前揭开?")
            t["status"] = "resolved"
            t["resolution_chapter"] = chapter   # historical record, not a plan
            t["resolution"] = up.get("detail", "")
        elif action == "abandon":
            t["status"] = "abandoned"
            t["abandon_reason"] = up.get("detail", "")
        else:
            warnings.append(f"thread '{name}': unknown action {action!r}")
    dump_yaml(path, threads)
    return warnings


def apply_arc_updates(ws, updates: list[dict], chapter: int) -> list[str]:
    """Post node completions. A chapter declares which plot nodes it FINISHED
    (possibly none — advancing a multi-chapter node is fine). completed_chapter
    is written as a record; when every node is done the arc auto-completes."""
    warnings: list[str] = []
    path = bible_dir(ws) / "arcs.yaml"
    arcs = load_yaml(path, []) or []
    by_name = {str(a.get("name")): a for a in arcs}
    for up in updates or []:
        name = str(up.get("name") or "")
        a = by_name.get(name)
        if a is None:
            warnings.append(f"arc update for unknown arc '{name}'")
            continue
        nodes = a.get("nodes") or []
        by_id = {str(nd.get("id")): nd for nd in nodes}
        for nid in up.get("nodes_completed") or []:
            nd = by_id.get(str(nid))
            if nd is None:
                warnings.append(f"arc '{name}': unknown node '{nid}'")
                continue
            if nd.get("status") == "done":
                warnings.append(f"arc '{name}': node '{nid}' already done")
                continue
            # Out-of-order completion (skipping pending predecessors) is
            # suspicious but not forbidden — plots can interleave. Warn only.
            frontier = arc_frontier(a)
            if frontier is not None and str(frontier.get("id")) != str(nid):
                warnings.append(
                    f"arc '{name}': completed node '{nid}' out of order "
                    f"(frontier is '{frontier.get('id')}') — 跳步?")
            nd["status"] = "done"
            nd["completed_chapter"] = chapter
        if up.get("notes"):
            a.setdefault("progress_notes", []).append(
                {"chapter": chapter, "note": up["notes"]})
        if a.get("nodes") and arc_frontier(a) is None \
                and a.get("status") != "completed":
            a["status"] = "completed"
            a["end_chapter"] = chapter
    dump_yaml(path, arcs)
    return warnings


# ── Digest / index (derived state) ───────────────────────────────────────────

RECENT_FULL = 10  # last N chapter summaries kept verbatim in the digest


def rebuild_digest(ws) -> None:
    """digest.md — 近细远粗: last RECENT_FULL summaries verbatim, older one-line."""
    done = written_chapters(ws)
    old, recent = done[:-RECENT_FULL], done[-RECENT_FULL:]
    lines: list[str] = ["# 前情摘要（自动生成，近细远粗）", ""]
    if old:
        lines.append("## 更早章节（单行）")
        for n in old:
            summ = (chapter_dir(ws, n) / "summary.md")
            first = ""
            if summ.is_file():
                for ln in summ.read_text(encoding="utf-8").splitlines():
                    if ln.strip() and not ln.startswith("#"):
                        first = ln.strip()
                        break
            lines.append(f"- 第{n}章：{first}")
        lines.append("")
    if recent:
        lines.append("## 最近章节（完整摘要）")
        for n in recent:
            summ = chapter_dir(ws, n) / "summary.md"
            lines.append(f"### 第{n}章")
            if summ.is_file():
                lines.append(summ.read_text(encoding="utf-8").strip())
            lines.append("")
    state_dir(ws).mkdir(parents=True, exist_ok=True)
    (state_dir(ws) / "digest.md").write_text("\n".join(lines), encoding="utf-8")


def rebuild_index(ws) -> dict:
    """Derived aggregates — never authoritative, cheap to recompute."""
    characters = load_characters(ws)
    threads = load_yaml(bible_dir(ws) / "threads.yaml", []) or []
    done = written_chapters(ws)
    arcs = load_yaml(bible_dir(ws) / "arcs.yaml", []) or []
    index = {
        "chapters_written": len(done),
        "last_chapter": done[-1] if done else 0,
        "next_chapter": (done[-1] + 1) if done else 1,
        "open_threads": [t.get("name") for t in threads
                         if t.get("status", "open") == "open"],
        "arc_frontiers": {
            str(a.get("name")): (arc_frontier(a) or {}).get("id")
            for a in arcs if a.get("status", "active") == "active"
        },
        "characters": {
            name: {"status": card.get("status", "alive"),
                   "last_appearance": card.get("last_appearance")}
            for name, card in characters.items()
        },
    }
    dump_yaml(state_dir(ws) / "index.yaml", index)
    return index


# ── Reconcile (double-entry audit) ───────────────────────────────────────────

def reconcile(ws) -> list[str]:
    """Replay all chapters' events over the genesis BASELINE (the
    ``novel-genesis`` git tag — no duplicated snapshot dir, git is the history);
    diff character balances against the live bible. Drift = state changed
    outside the journal.
    """
    baseline = genesis_characters(ws)
    if baseline is None:
        return [f"reconcile skipped: no genesis baseline (git tag '{GENESIS_TAG}' "
                "missing — non-git novel?)"]
    replayed: dict[str, dict] = {n: copy.deepcopy(c) for n, c in baseline.items()}

    for n in written_chapters(ws):
        ev_path = chapter_dir(ws, n) / "events.yaml"
        rec = load_yaml(ev_path, {}) or {}
        for ev in rec.get("events") or []:
            if ev.get("entity_type") not in ("character", "protagonist"):
                continue
            name = str(ev.get("entity_name") or "")
            if name not in replayed:
                if ev.get("create"):
                    replayed[name] = {"name": name, "status": "alive"}
                else:
                    continue  # already reported at apply time
            for k, v in (ev.get("changes") or {}).items():
                replayed[name][k] = v

    drift: list[str] = []
    live = load_characters(ws)
    tracked = ("status", "power_level", "tier")
    for name, expect in replayed.items():
        actual = live.get(name)
        if actual is None:
            drift.append(f"reconcile: character '{name}' in journal but missing from bible")
            continue
        for field in tracked:
            if field in expect and expect.get(field) != actual.get(field):
                drift.append(
                    f"reconcile: '{name}.{field}' journal says "
                    f"{expect.get(field)!r} but bible has {actual.get(field)!r}")
    for name in live:
        if name not in replayed:
            drift.append(
                f"reconcile: character '{name}' in bible but absent from genesis+journal "
                "(edited by hand?)")
    return drift


# ── 去AI味 / meta-marker lint material (shared by continuity_check) ──────────

# High-frequency AI-isms (NWS 铁律 + slop research). Density-gated, not any-hit —
# 仿佛/宛如 are legitimate words at low frequency.
BANNED_PHRASES = [
    "不禁", "仿佛", "宛如", "映入眼帘", "只见", "脸色一变", "嘴角微扬",
    "嘴角上扬", "心中暗道", "目光如炬", "眼中闪过一丝", "空气仿佛凝固",
    "注定是一个不平凡", "未来的路还很长", "前途无量", "挥之不去",
]

META_MARKERS = ["[说明]", "TODO", "（待补充）", "（此处", "[待", "<!--"]
