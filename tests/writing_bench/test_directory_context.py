"""Directory-mode baseline context: stale cards and world entries become preview lines."""
from __future__ import annotations

from pathlib import Path

import yaml

from aitelier import novel_state as ns
from aitelier.writing_bench.context import assemble, read_frozen


def card(name, last=None, **extra):
    value = {"name": name, "status": "alive", "location": name + "的住处", **extra}
    if last is not None:
        value["progression"] = [{"chapter": last, "changes": {"location": name + "的住处"}, "reason": "实见"}]
    return value


def tree(tmp_path, cards, world, chapters=10):
    view = tmp_path / "view"
    ns.dump_yaml(view / "novel/bible/world.yaml", world)
    for value in cards:
        ns.dump_yaml(view / f"novel/bible/characters/{value['name']}.yaml", value)
    for n in range(1, chapters + 1):
        path = view / f"novel/chapters/ch{n:04d}"
        path.mkdir(parents=True)
        (path / "prose.md").write_text(f"# 第{n}章：章{n}\n\n正文。\n")
        ns.dump_yaml(path / "events.yaml", {"chapter": n, "events": []})
    files = {str(p.relative_to(view)): p.read_bytes() for p in sorted(view.rglob("*")) if p.is_file()}
    return view, files


def build(tmp_path, cards, world=None, mode="new", chapters=(11,), mentions=""):
    view, files = tree(tmp_path, cards, world or {})
    text, manifest = assemble(view, files, mode, list(chapters), [], 10**7, mentions)
    reps = {s["path"]: s["representation"] for s in manifest["sources"]}
    return view, text, manifest, reps


CARD = "novel/bible/characters/{}.yaml".format


def test_stale_card_is_one_preview_line_and_stays_readable(tmp_path):
    view, text, manifest, reps = build(tmp_path, [card("旧人", 3, aliases=["旧人", "老旧"], 秘密="只在卡上")])
    assert reps[CARD("旧人")] == "index_line"
    assert "- 旧人 | 别称 老旧 | alive | 旧人的住处 | 末次出场第3章 | 全文 " + CARD("旧人") in text
    assert "只在卡上" not in text
    page = read_frozen(view, manifest, CARD("旧人"))
    assert "只在卡上" in page["text"] and page["complete"]


def test_recent_unchanged_and_named_cards_stay_complete(tmp_path):
    cards = [card("近人", 6, 细节="近况"), card("原人", 细节="登场设定"),
             card("回人", 2, aliases=["回人", "阿回"], 细节="回归现状"), card("远人", 2, 细节="远方")]
    _, text, _, reps = build(tmp_path, cards, mentions="阿回推门进来。")
    for name in ("近人", "原人", "回人"):
        assert reps[CARD(name)] == "current_projection"
    assert reps[CARD("远人")] == "index_line"
    assert "近况" in text and "登场设定" in text and "回归现状" in text and "远方" not in text


def test_revision_window_counts_from_the_first_revised_chapter(tmp_path):
    cards = [card("中人", 3, 细节="中段")]
    _, _, _, reps = build(tmp_path, cards, mode="revision", chapters=(7, 8))
    assert reps[CARD("中人")] == "current_projection"
    _, _, _, reps = build(tmp_path / "b", cards, mode="new", chapters=(11,))
    assert reps[CARD("中人")] == "index_line"


def test_world_entries_follow_the_same_directory_rule(tmp_path):
    world = {"rules": {"常规": "始终完整"},
             "settings": {"《旧片》任务": {"完成条件": "旧条件"}, "《新片》任务": {"完成条件": "新条件"},
                          "提名设定（旧片）": {"细节": "被点名"}, "创世设定": {"细节": "从未改动"}},
             "factions": {"旧势力": {"note": "旧势力简介", "秘闻": "旧秘闻", "progression": [{"chapter": 2, "changes": {}}]},
                          "新势力": {"note": "新势力简介", "progression": [{"chapter": 9, "changes": {}}]}},
             "setting_log": [{"chapter": 2, "name": "《旧片》任务", "changes": {}},
                             {"chapter": 9, "name": "《新片》任务", "changes": {}},
                             {"chapter": 1, "name": "提名设定（旧片）", "changes": {}}]}
    _, text, _, reps = build(tmp_path, [], world, mentions="她想起提名设定里的那件事。")
    assert reps["novel/bible/world.yaml"] == "current_projection"
    body = text[text.index("## 世界与资源当前状态"):text.index("## 世界条目目录")]
    projected = yaml.safe_load(body.split("\n\n", 2)[2])
    assert set(projected["settings"]) == {"《新片》任务", "提名设定（旧片）", "创世设定"}
    assert set(projected["factions"]) == {"新势力"} and "progression" not in projected["factions"]["新势力"]
    assert projected["rules"] == {"常规": "始终完整"} and "setting_log" not in projected
    assert "- settings/《旧片》任务 | 末次变更第2章 | 字段 完成条件" in text
    assert "- factions/旧势力 | 末次变更第2章 | 旧势力简介" in text
    assert "旧条件" not in text and "旧秘闻" not in text


def test_film_title_in_brackets_counts_as_named(tmp_path):
    world = {"settings": {"《旧片》任务": {"完成条件": "旧条件"}},
             "setting_log": [{"chapter": 2, "name": "《旧片》任务", "changes": {}}]}
    _, text, _, _ = build(tmp_path, [], world, mentions="回到旧片的车站。")
    assert "旧条件" in text and "## 世界条目目录" not in text
