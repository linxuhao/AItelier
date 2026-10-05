"""Build a provenance-bearing editorial packet from an immutable novel tree."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import yaml

from aitelier import novel_state as ns
from aitelier.tools.state_probe.impl import character_context
from .storage import FILE_LIMIT, encode, read_file, relative, require, sha


WINDOW = 5  # chapters before the reviewed one whose cast and world entries stay complete


def _last(value: dict) -> int:
    marks = [value.get("last_appearance")] + [e.get("chapter") for e in value.get("progression") or []
                                              if isinstance(e, dict)]
    return max((n for n in marks if type(n) is int), default=0)


def _preview(field: object) -> str:
    if field in (None, "", []):
        return ""
    text = " ".join(str(field).split())
    return text if len(text) <= 60 else text[:60] + "…"


def _line(*fields: object) -> str:
    def single_line(field: object) -> str:
        text = str(field)
        # Identity and read addresses are exact; quote line breaks reversibly.
        return json.dumps(text) if "".join(text.splitlines()) != text else text
    return "- " + " | ".join(single_line(f) for f in fields if f not in (None, "", []))


def _named(key: str, mentions: str) -> bool:
    terms = {key, re.sub(r"[（(][^）)]*[）)]", "", key).strip(), *re.findall(r"《([^》]+)》", key)}
    return any(t and t in mentions for t in terms)


def assemble(view: Path, files: dict[str, bytes], mode: str, chapters: list[int],
             extra_paths: list[str], max_bytes: int, mentions: str) -> tuple[str, dict]:
    # Each source occurs once. A full prose read replaces a short recap for that
    # chapter. The durable index is navigation, never the full recap authority.
    sources = []
    parts = ["# 已接受基线（仅引用冻结提交）"]

    def add(name: str, label: str, projected: object = None) -> None:
        if name not in files:
            return
        if any(item["path"] == name and item["representation"] != "index_line" for item in sources):
            return
        data = files[name]
        body = data.decode() if projected is None else yaml.safe_dump(projected, allow_unicode=True, sort_keys=False)
        parts.extend(["## " + label, "来源：" + name, body])
        sources.append({"path": name, "sha256": sha(data), "bytes": len(data),
                        "representation": "complete" if projected is None else "current_projection"})

    done = ns.written_chapters(view)
    # Directory mode: an entry last changed before the window and not named in
    # the submitted prose or brief keeps one preview line; its frozen file stays
    # readable in full through novel_bench_read. Never-changed entries are
    # genesis-sized and stay complete.
    start = (min(chapters) if mode == "revision" else len(done) + 1) - WINDOW
    cards = character_context(view)
    directory = []
    # Individual sources are bound even though the compact views are grouped.
    for name in sorted(files):
        if name.startswith("novel/bible/characters/") and name.endswith(".yaml"):
            card = next((c for c in cards if c.get("name") == Path(name).stem), None)
            if card is None:
                continue
            raw = ns.load_yaml(view / name, {}) or {}
            last, aliases = _last(raw), [a for a in raw.get("aliases") or [] if a != card["name"]]
            if not last or last >= start or any(_named(str(n), mentions) for n in [card["name"], *aliases]):
                add(name, "人物当前状态", card)
                continue
            directory.append(_line(card["name"], _preview("别称 " + "、".join(map(str, aliases))) if aliases else "",
                                   _preview(card.get("status")),
                                   _preview(card.get("location") or card.get("位置")),
                                   f"末次出场第{last}章", "全文 " + name))
            sources.append({"path": name, "sha256": sha(files[name]), "bytes": len(files[name]),
                            "representation": "index_line"})
    if directory:
        parts.extend([f"## 人物目录（第{start}章前最后出场、本次正文与意图未点名；单行仅预览）",
                      "需要现状时用 novel_bench_read 读取对应全文，不将预览当作完整人物卡。", "\n".join(directory)])
    world = copy.deepcopy(ns.load_yaml(view / "novel/bible/world.yaml", {}) or {})
    touched: dict[str, int] = {}
    for item in world.pop("setting_log", None) or []:
        if isinstance(item, dict) and type(item.get("chapter")) is int:
            key = str(item.get("name"))
            touched[key] = max(touched.get(key, 0), item["chapter"])
    index = []
    for group in ("settings", "factions"):
        entries = world.get(group)
        if not isinstance(entries, dict):
            continue
        for key in list(entries):
            value = entries[key]
            last = _last(value) if group == "factions" and isinstance(value, dict) else touched.get(key, 0)
            if isinstance(value, dict):
                value.pop("progression", None)
            if last and last < start and not _named(key, mentions):
                note = value
                if isinstance(value, dict):
                    note = value.get("note") if isinstance(value.get("note"), str) else "字段 " + "、".join(map(str, list(value)[:8]))
                index.append(_line(group + "/" + key, f"末次变更第{last}章", _preview(note)))
                del entries[key]
    add("novel/bible/world.yaml", "世界与资源当前状态", world)
    if index:
        parts.extend([f"## 世界条目目录（第{start}章前最后变更、本次正文与意图未点名；单行仅预览）",
                      "需要全文时用 novel_bench_read 读取 novel/bible/world.yaml。", "\n".join(index)])
    threads = copy.deepcopy(ns.load_yaml(view / "novel/bible/threads.yaml", []) or [])
    for item in threads:
        item.pop("hints", None)
    add("novel/bible/threads.yaml", "已登记伏笔及状态；意图不等于已发生", threads)

    if mode == "revision":
        # Historical changes require the affected later chapters, not merely the
        # latest chapter ending. A large bundle fails with an explicit limit.
        full = [n for n in done if n >= min(chapters) - 1]
    else:
        full = done[-2:]
    for n in full:
        add(f"novel/chapters/ch{n:04d}/prose.md", f"已接受第{n}章完整正文")
    for n in done[-5:]:
        if n not in full:
            add(f"novel/chapters/ch{n:04d}/summary.md", f"已接受第{n}章完整摘要")
    # Old journals for all revised chapters are baseline facts, never proposals.
    for n in chapters if mode == "revision" else []:
        add(f"novel/chapters/ch{n:04d}/events.yaml", f"第{n}章旧分录（修订对照）")
    for name in extra_paths:
        relative(name)
        require(name.startswith("novel/") and name in files, "unknown extra context path")
        add(name, "显式补充上下文")

    parts.append("# 计划与创作方向（不是已发生的事件；冲突须指出）")
    for name, label in [("overview.md", "总纲"), ("compass.md", "指南针"),
                        ("pacing.yaml", "节奏约定"), ("arcs.yaml", "剧情计划及已记完成状态")]:
        add("novel/bible/" + name, label)
    parts.extend(["# 按需阅读", "novel_bench_read 只读取本提交冻结的 novel/ 文件；可查完整旧正文、分录、人物历史，以及目录里只列预览的人物卡和世界条目。",
                  "索引的单行摘要是预览。需要完整前情时读取 summary.md，不将预览当完整摘要。"])
    text = "\n\n".join(parts) + "\n"
    require(len(text.encode()) <= max_bytes, "context exceeds configured limit; nothing was silently truncated")
    catalog = {name: {"sha256": sha(raw), "bytes": len(raw)} for name, raw in sorted(files.items())}
    return text, {"sources": sources, "catalog": catalog,
                  "context_sha256": sha(text.encode()), "context_bytes": len(text.encode()),
                  "summary_contract": "index_preview_plus_full_summary_file", "truncated": False}


def read_frozen(view: Path, manifest: dict, path: str, start: int = 0,
                length: int = 12000) -> dict:
    relative(path)
    require(type(start) is int and start >= 0 and type(length) is int and 0 < length <= 20000,
            "invalid read range")
    expected = manifest["catalog"].get(path)
    require(path.startswith("novel/") and expected is not None, "path outside frozen novel")
    raw = read_file(view, path)
    require(sha(raw) == expected["sha256"], "frozen source changed")
    text = raw.decode()
    return {"path": path, "sha256": expected["sha256"], "text": text[start:start + length],
            "start": start, "total_chars": len(text), "next_start": start + length if start + length < len(text) else None,
            "complete": start == 0 and length >= len(text)}
