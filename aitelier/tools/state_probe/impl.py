"""state_probe — head-of-loop tool for novel_chapter.

Two jobs, both deterministic:
1. Guard + route: fail loud if the bible is missing (novel_init not run);
   compute the next unwritten chapter number.
2. Assemble the writing context into ``novel_context.md`` (its step output),
   which the outline/draft/finalize agents read via a ``{step: probe}`` context
   source. This bridges the code repo (where the novel tree lives, for git +
   download) into the prompt — skillflow's native ``{from: workspace}`` /
   ``{from: repository}`` context sources read the skillflow workspace, NOT the
   code repo, so a tool must carry the bible across. No LLM, no mention-based
   selection: the whole (small) bible goes in; semantic relevance is the agent's
   job, and scaling to a huge cast is a future RAG concern, not this tool's.
"""

import json
from pathlib import Path

import yaml

from aitelier import novel_state as ns

ENDING_CHARS = 800  # 上章结尾节选长度（文风衔接用）


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def _yaml_block(data) -> str:
    return "```yaml\n" + yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False).strip() + "\n```"


def state_probe(*, project_root: str = "", workspace_root: str = "",
                out_dir: str = "", recent_summaries: int = 3,
                thread_horizon: int = 12, **kwargs) -> dict:
    base = project_root or workspace_root or "."
    if not ns.bible_exists(base):
        raise ValueError(
            "state_probe: 该项目没有小说 bible（novel/bible/overview.md 缺失）。"
            "请先对本项目运行 novel_init 完成世界设定初始化，再写章节。")

    n = ns.next_chapter_number(base)
    bib = ns.bible_dir(base)

    # ── Assemble the context bundle (whole bible + rolling recap + last ending) ──
    parts: list[str] = [f"# 第{n}章 创作上下文（自动装配，来自 bible 真相源）", ""]

    overview = _read(bib / "overview.md")
    if overview.strip():
        parts += ["## 总纲", "", overview.strip(), ""]
    compass = _read(bib / "compass.md")
    if compass.strip():
        parts += ["## 指南针（终局方向与活跃长线，不可违背）", "", compass.strip(), ""]

    world = ns.load_yaml(bib / "world.yaml", {}) or {}
    if world:
        parts += ["## 世界设定", "", _yaml_block(world), ""]
    pacing = ns.load_yaml(bib / "pacing.yaml", {}) or {}
    if pacing:
        parts += ["## 节奏与爽点约定", "", _yaml_block(pacing), ""]

    characters = ns.load_characters(base)
    if characters:
        # Whole cast, current balances (drop progression history — git has it).
        cards = []
        for name in sorted(characters):
            c = dict(characters[name])
            c.pop("progression", None)
            cards.append(c)
        parts += ["## 角色卡（当前状态；出场角色仅限于此，新增角色须在章纲阶段提案）",
                  "", _yaml_block(cards), ""]

    # ── Plot frontier (node-driven: what each arc advances next) ──
    arcs = ns.load_yaml(bib / "arcs.yaml", []) or []
    active_arcs = [a for a in arcs if a.get("status", "active") == "active"]
    if active_arcs:
        views = []
        for a in active_arcs:
            nodes = a.get("nodes") or []
            done_nodes = [nd for nd in nodes if nd.get("status") == "done"]
            frontier = ns.arc_frontier(a)
            idx = nodes.index(frontier) if frontier in nodes else len(nodes)
            views.append({
                "name": a.get("name"), "arc_type": a.get("arc_type"),
                "description": a.get("description"),
                "最近完成": [{"id": nd.get("id"), "beat": nd.get("beat"),
                             "chapter": nd.get("completed_chapter")}
                            for nd in done_nodes[-2:]],
                "当前推进节点(frontier)": ({"id": frontier.get("id"),
                                          "beat": frontier.get("beat")}
                                         if frontier else "全部完成"),
                "后续节点预告": [{"id": nd.get("id"), "beat": nd.get("beat")}
                               for nd in nodes[idx + 1:idx + 3]],
            })
        parts += ["## 剧情前沿（节点驱动：本章应推进 frontier 节点；一个节点可跨多章，"
                  "完成时在记账中声明 nodes_completed）", "", _yaml_block(views), ""]

    # ── Threads: mechanically split by reveal gate ──
    threads = ns.load_yaml(bib / "threads.yaml", []) or []
    open_threads = [t for t in threads if t.get("status", "open") == "open"]
    revealable, locked = [], []
    for t in open_threads:
        (revealable if ns.thread_revealable(t, arcs) else locked).append(t)

    def _slim(t, with_stale=False):
        s = {"name": t.get("name"), "importance": t.get("importance"),
             "description": t.get("description")}
        hints = t.get("hints") or []
        last = max((h.get("chapter", 0) for h in hints),
                   default=t.get("introduced_chapter", 0) or 0)
        if with_stale and n - last >= thread_horizon and (t.get("importance") or 0) >= 6:
            s["⚠️陈旧"] = f"已 {n - last} 章无暗示，考虑本章埋一笔"
        if not with_stale:
            g = t.get("earliest_reveal") or {}
            s["解锁条件"] = f"{g.get('arc')}/{g.get('node')} 完成后"
        return s

    if revealable:
        parts += ["## 可回收伏笔（门控节点已完成或无门控；可揭开，也可继续埋设）",
                  "", _yaml_block([_slim(t, with_stale=True) for t in revealable]), ""]
    if locked:
        parts += ["## 未解锁伏笔（门控节点未完成——只可埋设暗示，不得揭开）", "",
                  _yaml_block([_slim(t) for t in locked]), ""]

    # Rolling recap + last chapter ending (the genuinely dynamic bits).
    done = ns.written_chapters(base)
    summaries = []
    for m in done[-max(0, int(recent_summaries)):]:
        sp = ns.chapter_dir(base, m) / "summary.md"
        if sp.is_file():
            summaries.append((m, sp.read_text(encoding="utf-8").strip()))
    if summaries:
        parts += ["## 最近章节摘要"]
        for m, s in summaries:
            parts += ["", f"### 第{m}章", s]
        parts += [""]
    digest = _read(ns.state_dir(base) / "digest.md")
    if digest.strip() and len(done) > int(recent_summaries):
        parts += ["## 前情摘要（近细远粗）", "", digest.strip(), ""]
    if done:
        prev = ns.chapter_dir(base, done[-1]) / "prose.md"
        if prev.is_file():
            ending = prev.read_text(encoding="utf-8").rstrip()[-ENDING_CHARS:]
            parts += ["## 上一章结尾（衔接文风与场景，正文必须自然承接）", "",
                      "```", ending, "```", ""]
    else:
        # First chapter: make the ABSENCE of a predecessor explicit, so the
        # outliner opens the story here instead of inventing an "上一章" (and
        # thereby drifting the chapter number forward on a revision reject).
        parts += [f"## 本章为第{n}章（全书开篇）", "",
                  "这是本书【第一章】——之前没有任何章节，没有上一章结尾，没有已发生的前情。"
                  f"直接从这里开场，不要承接、不要假设读者已知任何剧情。章号锁定为第{n}章。", ""]

    result = {"next_chapter": n, "has_previous": bool(done)}
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "novel_context.md").write_text("\n".join(parts), encoding="utf-8")
        (out / "probe.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
