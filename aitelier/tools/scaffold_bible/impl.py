"""scaffold_bible — novel_init's finalize tool step.

By the time this runs, the design agent has already written the seven raw bible
files INTO the code repo (mode:write + repo_apply, DPE-style):

  novel/bible/{overview.md, compass.md, world.yaml, pacing.yaml,
               characters.yaml (a YAML list of cards), threads.yaml, arcs.yaml}

This tool normalizes them into the runtime layout and freezes the baseline:
  - splits characters.yaml into per-character files (progression/reconcile stay
    per-card; git diffs stay readable), then removes characters.yaml
  - fills mechanical defaults (status/progression on cards, status/hints on
    threads, status on arcs + per-node status:pending on plot nodes)
  - builds the derived index, git-commits the normalized tree, then stamps the
    ``novel-genesis`` git TAG as the reconcile baseline (no duplicated snapshot
    directory — git is the history)

Deterministic, no LLM. Guards against a second run via state/index.yaml.
"""

from pathlib import Path

from aitelier import novel_state as ns


def _require(path: Path, label: str) -> str:
    if not path.is_file():
        raise ValueError(f"scaffold_bible: design did not write {label} at {path}")
    return path.read_text(encoding="utf-8")


def scaffold_bible(*, project_root: str = "", workspace_root: str = "",
                   **kwargs) -> dict:
    # The bible lives in the code repo (project_root); design's repo_apply put it
    # there. In unit tests project_root is empty → fall back to workspace_root.
    ws = Path(project_root or workspace_root or ".")
    bib = ns.bible_dir(ws)

    if (ns.state_dir(ws) / "index.yaml").exists():
        raise ValueError(
            "scaffold_bible: novel already scaffolded (state/index.yaml exists) "
            "— novel_init runs once per novel; edit the bible through chapter runs.")

    overview = _require(bib / "overview.md", "novel/bible/overview.md")
    if not overview.strip():
        raise ValueError("scaffold_bible: overview.md (总纲) is empty")
    _require(bib / "compass.md", "novel/bible/compass.md")

    # ── Phase 1: VALIDATE everything before touching anything (a failed
    # scaffold must not leave a half-normalized bible behind) ──
    chars_path = bib / "characters.yaml"
    characters = ns.load_yaml(chars_path, None)
    if not isinstance(characters, list) or not characters:
        raise ValueError("scaffold_bible: novel/bible/characters.yaml must be a "
                         "non-empty YAML list of character cards")
    for card in characters:
        if not str((card or {}).get("name") or "").strip():
            raise ValueError(f"scaffold_bible: character without a name: {card}")

    threads = ns.load_yaml(bib / "threads.yaml", []) or []
    arcs = ns.load_yaml(bib / "arcs.yaml", []) or []
    for a in arcs:
        nodes = a.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise ValueError(
                f"scaffold_bible: arc '{a.get('name')}' has no plot nodes — "
                "plot is node-driven; every arc needs an ordered `nodes:` list")
        seen_ids: set[str] = set()
        for nd in nodes:
            nid = str((nd or {}).get("id") or "").strip()
            if not nid or not str(nd.get("beat") or "").strip():
                raise ValueError(
                    f"scaffold_bible: arc '{a.get('name')}' node missing id/beat: {nd}")
            if nid in seen_ids:
                raise ValueError(
                    f"scaffold_bible: arc '{a.get('name')}' duplicate node id '{nid}'")
            seen_ids.add(nid)

    # Thread reveal gates must point at real nodes (fail loud now, not chapter 200).
    arc_names = {str(a.get("name")): a for a in arcs}
    for t in threads:
        gate = t.get("earliest_reveal")
        if isinstance(gate, dict):
            ga, gn = str(gate.get("arc") or ""), str(gate.get("node") or "")
            a = arc_names.get(ga)
            ids = {str(nd.get("id")) for nd in (a.get("nodes") or [])} if a else set()
            if a is None or gn not in ids:
                raise ValueError(
                    f"scaffold_bible: thread '{t.get('name')}' earliest_reveal "
                    f"points at unknown node {ga}/{gn}")

    # ── Phase 2: normalize + write ──
    for card in characters:
        card.setdefault("status", "alive")
        card.setdefault("progression", [])
        ns.dump_yaml(ns.character_path(ws, str(card["name"]).strip()), card)
    chars_path.unlink()

    for t in threads:
        t.setdefault("status", "open")
        t.setdefault("hints", [])
    ns.dump_yaml(bib / "threads.yaml", threads)

    for a in arcs:
        a.setdefault("status", "active")
        for nd in a["nodes"]:
            nd.setdefault("status", "pending")
    ns.dump_yaml(bib / "arcs.yaml", arcs)

    ns.chapters_dir(ws).mkdir(parents=True, exist_ok=True)
    ns.rebuild_index(ws)

    committed = ns.git_commit(ws, "世界设定：初始化归一化")
    tagged = ns.git_tag_genesis(ws) if committed else False

    return {"scaffolded": True, "characters": len(characters),
            "threads": len(threads), "arcs": len(arcs),
            "committed": committed, "genesis_tagged": tagged}
