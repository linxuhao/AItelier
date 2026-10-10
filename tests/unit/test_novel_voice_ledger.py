# Character voice design + ledger/current-state split.
#
# Cards (and world/threads/arcs) hold CURRENT STATE only; per-chapter history
# is appended to novel/ledger/<kind>/<name>.jsonl and is query-only. The probe
# reads current state (+ a voice table), never the ledger. continuity_check
# enforces per-book tics and character catchphrase caps.

import json
import subprocess

import pytest

from aitelier import novel_state as ns
from aitelier.novel_ledger_migrate import migrate
from aitelier.tools.continuity_check.impl import (continuity_check,
                                                  repeated_across_chapters)
from aitelier.tools.ledger_query.impl import ledger_query
from aitelier.tools.state_probe.impl import state_probe

VOICE_A = {"rhythm": "短句，命令式", "catchphrases": [{"text": "别散。", "max_per_chapter": 1}],
           "banned_words": ["或许"], "address": {"others": {"小季": "理货的"}},
           "sample_lines": ["够我心疼一下。"]}


def _bible(ws, cards, pacing=None, threads=None, arcs=None, world=None):
    b = ns.bible_dir(ws)
    b.mkdir(parents=True, exist_ok=True)
    (b / "overview.md").write_text("# 总纲\n\n测试", encoding="utf-8")
    ns.dump_yaml(b / "pacing.yaml", pacing or {"min_chars_per_chapter": 10})
    ns.dump_yaml(b / "threads.yaml", threads or [])
    ns.dump_yaml(b / "arcs.yaml", arcs or [])
    ns.dump_yaml(b / "world.yaml", world or {})
    for c in cards:
        ns.dump_yaml(ns.character_path(ws, c["name"]), c)


def _chapter(ws, n, prose, appearances=()):
    d = ns.chapter_dir(ws, n)
    d.mkdir(parents=True, exist_ok=True)
    (d / "prose.md").write_text(prose, encoding="utf-8")
    (d / "summary.md").write_text(f"# 第{n}章\n\n摘要{n}", encoding="utf-8")
    ns.dump_yaml(d / "events.yaml", {"chapter": n, "appearances":
                                     [{"name": a} for a in appearances]})


# ── ledger vs current state ─────────────────────────────────────────────────

def test_events_overwrite_card_and_append_ledger(tmp_path):
    _bible(tmp_path, [{"name": "尹骁", "status": "alive", "power_level": 4000}])
    ns.apply_events(tmp_path, [{"entity_type": "character", "entity_name": "尹骁",
                                "changes": {"伤情": "右臂发红", "本章行动": "断后"},
                                "reason": "r1"}], 3)
    ns.apply_events(tmp_path, [{"entity_type": "character", "entity_name": "尹骁",
                                "changes": {"伤情": "右臂发白至肘"}, "reason": "r2"}], 8)
    card = ns.load_characters(tmp_path)["尹骁"]
    assert card["伤情"] == "右臂发白至肘"            # overwritten, not appended
    assert "本章行动" not in card                    # transient → ledger only
    assert "progression" not in card
    rows = ns.read_ledger(tmp_path, "characters", "尹骁")
    assert [r["chapter"] for r in rows] == [3, 8]
    assert rows[0]["changes"]["本章行动"] == "断后"
    # null clears a stale slot
    ns.apply_events(tmp_path, [{"entity_type": "character", "entity_name": "尹骁",
                                "changes": {"伤情": None}, "reason": "痊愈"}], 9)
    assert "伤情" not in ns.load_characters(tmp_path)["尹骁"]


def test_world_threads_arcs_history_goes_to_ledger(tmp_path):
    _bible(tmp_path, [{"name": "甲", "status": "alive"}],
           threads=[{"name": "门", "status": "open"}],
           arcs=[{"name": "主线", "nodes": [{"id": "n1", "beat": "x"},
                                            {"id": "n2", "beat": "y"}]}],
           world={"factions": {"会": {"tier": 1}}})
    ns.apply_events(tmp_path, [
        {"entity_type": "faction", "entity_name": "会", "changes": {"tier": 2}, "reason": "升"},
        {"entity_type": "world_setting", "entity_name": "灵气", "changes": {"v": "1%"}, "reason": "测"},
    ], 1)
    ns.apply_thread_updates(tmp_path, [{"name": "门", "action": "hint", "detail": "门响"}], 1)
    ns.apply_thread_updates(tmp_path, [{"name": "门", "action": "hint", "detail": "门开"}], 2)
    ns.apply_arc_updates(tmp_path, [{"name": "主线", "nodes_completed": ["n1"], "notes": "半"}], 2)
    world = ns.load_yaml(ns.bible_dir(tmp_path) / "world.yaml")
    assert world["factions"]["会"] == {"tier": 2} and "setting_log" not in world
    t = ns.load_yaml(ns.bible_dir(tmp_path) / "threads.yaml")[0]
    assert t["hint_count"] == 2 and t["last_hint"] == "门开" and "hints" not in t
    a = ns.load_yaml(ns.bible_dir(tmp_path) / "arcs.yaml")[0]
    assert a["latest_note"] == {"chapter": 2, "note": "半"} and "progress_notes" not in a
    assert len(ns.query_ledger(tmp_path, kind="threads", name="门")) == 2
    assert ns.query_ledger(tmp_path, kind="settings")[0]["changes"] == {"v": "1%"}
    assert ns.query_ledger(tmp_path, kind="arcs", entry_type="node_completed")[0]["node"] == "n1"


def test_ledger_query_filters_and_tool(tmp_path):
    _bible(tmp_path, [{"name": "尹骁", "status": "alive"}])
    for ch, ch_changes in [(2, {"右臂": "红"}), (8, {"右臂伤": "白"}), (9, {"气": "低"})]:
        ns.apply_events(tmp_path, [{"entity_type": "character", "entity_name": "尹骁",
                                    "changes": ch_changes, "reason": ""}], ch)
    assert [r["chapter"] for r in ns.query_ledger(tmp_path, name="尹骁", field="右臂")] == [2, 8]
    assert [r["chapter"] for r in ns.query_ledger(tmp_path, chapter_from=8)] == [8, 9]
    assert ns.query_ledger(tmp_path, name="尹骁", limit=1)[0]["chapter"] == 9
    out = ledger_query(workspace_root=str(tmp_path), kind="characters", name="尹骁", chapter=8)
    assert out["count"] == 1 and out["rows"][0]["changes"] == {"右臂伤": "白"}
    with pytest.raises(ValueError):
        ledger_query(workspace_root=str(tmp_path), kind="nope")


def test_failed_apply_rolls_back_ledger_too(tmp_path):
    _bible(tmp_path, [{"name": "甲", "status": "alive"}])
    with pytest.raises(RuntimeError):
        with ns.state_transaction(tmp_path):
            ns.ledger_append(tmp_path, "characters", "甲", {"chapter": 1, "type": "event"})
            raise RuntimeError("boom")
    assert ns.read_ledger(tmp_path, "characters", "甲") == []


# ── probe: current state + voice table, never the ledger ────────────────────

def test_probe_reads_current_state_and_voice_not_ledger(tmp_path):
    _bible(tmp_path, [
        {"name": "季衡", "is_protagonist": True, "status": "alive",
         "voice": {"rhythm": "慢，先数数", "sample_lines": ["三十七。"]}},
        {"name": "尹骁", "status": "alive", "voice": VOICE_A},
        {"name": "邹建平", "status": "死亡（第88章）", "background": "很长的背景" * 20,
         "voice": {"rhythm": "絮叨"}},
    ], pacing={"min_chars_per_chapter": 10,
               "style": {"narration_tics": [{"text": "没平", "max_per_chapter": 1}]}})
    _chapter(tmp_path, 1, "正文")
    ns.log_appearances(tmp_path, [{"name": "尹骁"}, {"name": "季衡"}], 1)
    ns.apply_events(tmp_path, [{"entity_type": "character", "entity_name": "尹骁",
                                "changes": {"伤情": "右臂发白"},
                                "reason": "LEDGER_ONLY_REASON"}], 1)
    state_probe(workspace_root=str(tmp_path), out_dir=str(tmp_path / "out"))
    bundle = (tmp_path / "out" / "novel_context.md").read_text(encoding="utf-8")
    assert "## 本章出场角色 voice" in bundle
    assert bundle.index("本章出场角色 voice") < bundle.index("## 角色卡")
    assert "够我心疼一下" in bundle and "三十七" in bundle
    assert bundle.count("够我心疼一下") == 1          # not repeated inside the card
    assert "絮叨" not in bundle                      # dead → compact card
    assert "很长的背景" not in bundle
    assert "伤情: 右臂发白" in bundle                 # current state present
    assert "LEDGER_ONLY_REASON" not in bundle        # ledger never dumped
    assert "没平" in bundle                          # book style shown
    assert "ledger_query" in bundle                  # pointer to the query tool


def test_probe_strips_legacy_progression(tmp_path):
    _bible(tmp_path, [{"name": "甲", "status": "alive", "progression": [
        {"chapter": 1, "changes": {"x": 1}, "reason": "LEGACY_REASON"}]}])
    state_probe(workspace_root=str(tmp_path), out_dir=str(tmp_path / "out"))
    assert "LEGACY_REASON" not in (tmp_path / "out" / "novel_context.md").read_text(encoding="utf-8")


# ── continuity_check: per-book tics, catchphrase caps, cross-chapter repeats ─

def _stage(ws, prose):
    for step, name in (("humanize", "chapter_final.md"), ("draft", "chapter_draft.md")):
        d = ws / "novel_chapter" / step
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(prose, encoding="utf-8")


def test_book_tics_and_catchphrase_caps_are_violations(tmp_path):
    _bible(tmp_path, [{"name": "尹骁", "status": "alive", "voice": VOICE_A}],
           pacing={"min_chars_per_chapter": 10, "style": {
               "narration_tics": [{"text": "没平", "max_per_chapter": 1},
                                  {"pattern": "走到.，走到.，走到", "max_per_chapter": 0}],
               "extra_banned_phrases": ["低，平，有一点哑"]}})
    prose = ("# 第1章：试\n\n地面没平。墙也没平。\n\n“别散。”他说。“别散。”\n\n"
             "走到肩，走到肋，走到指尖。\n\n他抬手——")
    _stage(tmp_path, prose)
    r = continuity_check(workspace_root=str(tmp_path), out_dir=str(tmp_path / "cc"))
    assert r["passed"] is False
    assert "没平" in r["error"] and "尹骁 的口头禅『别散。』" in r["error"]
    assert "走到" in r["error"]


def test_no_style_config_is_a_noop(tmp_path):
    _bible(tmp_path, [{"name": "甲", "status": "alive"}])
    _stage(tmp_path, "# 第1章：试\n\n地面没平。墙也没平。没平。\n\n他抬手——")
    r = continuity_check(workspace_root=str(tmp_path), out_dir=str(tmp_path / "cc"))
    assert r["passed"] is True


def test_cross_chapter_repeat_warning(tmp_path):
    _bible(tmp_path, [{"name": "季衡", "status": "alive"}],
           pacing={"min_chars_per_chapter": 10,
                   "style": {"narration_tics": [{"text": "没平", "max_per_chapter": 3}],
                             "repeat_window": 3}})
    _chapter(tmp_path, 1, "地面没平。灰落下来没有声音。灰落下来没有声音。")
    _chapter(tmp_path, 2, "墙没平。灰落下来没有声音。又是灰落下来没有声音。")
    _stage(tmp_path, "# 第3章：试\n\n水没平。灰落下来没有声音，灰落下来没有声音。\n\n——")
    continuity_check(workspace_root=str(tmp_path), out_dir=str(tmp_path / "cc"))
    rep = json.loads((tmp_path / "cc" / "continuity_report.json").read_text(encoding="utf-8"))
    adv = "\n".join(rep["advisories"])
    assert "口癖连续 3 章出现: 没平" in adv
    assert "灰落下来没有声音" in adv


def test_repeated_across_chapters_excludes_names():
    texts = ["季衡看着前面。季衡看着前面。"] * 3
    assert repeated_across_chapters(texts, {"季衡"}) == ["看着前面"]  # name never part of a hit
    assert repeated_across_chapters(texts, set()) == ["季衡看着前面"]


# ── migration ───────────────────────────────────────────────────────────────

def test_migration_splits_history_and_is_idempotent_guarded(tmp_path):
    legacy = {"name": "尹骁", "status": "dead", "power_level": 4000,
              "progression": [
                  {"chapter": 3, "changes": {"伤情": "红", "本章行动": "断后"}, "reason": "a"},
                  {"chapter": 8, "changes": {"伤情": "白"}, "reason": "b"}],
              "initial": {"name": "尹骁", "status": "alive"},
              "伤情": "白", "本章行动": "断后"}
    _bible(tmp_path, [legacy],
           threads=[{"name": "门", "status": "open", "introduced_chapter": 1,
                     "hints": [{"chapter": 2, "hint": "h1"}, {"chapter": 5, "hint": "h2"}]}],
           arcs=[{"name": "主线", "nodes": [{"id": "n1", "status": "done", "completed_chapter": 4}],
                  "progress_notes": [{"chapter": 4, "note": "完"}]}],
           world={"factions": {"会": {"tier": 2, "progression": [{"chapter": 1, "changes": {"tier": 2}}]}},
                  "setting_log": [{"chapter": 1, "name": "灵气", "changes": {"v": 1}}]})
    _chapter(tmp_path, 1, "正文", appearances=["尹骁"])
    dry = migrate(tmp_path, dry_run=True)
    assert dry["character_rows"] == 2 and "progression" in ns.load_characters(tmp_path)["尹骁"]
    rep = migrate(tmp_path)
    card = ns.load_characters(tmp_path)["尹骁"]
    assert "progression" not in card and "本章行动" not in card and card["伤情"] == "白"
    assert rep["dropped_transient_keys"] == 1 and rep["appearance_rows"] == 1
    rows = ns.read_ledger(tmp_path, "characters", "尹骁")
    assert [r["type"] for r in rows] == ["appearance", "event", "event"]
    t = ns.load_yaml(ns.bible_dir(tmp_path) / "threads.yaml")[0]
    assert t["hint_count"] == 2 and t["last_hint_chapter"] == 5 and "hints" not in t
    world = ns.load_yaml(ns.bible_dir(tmp_path) / "world.yaml")
    assert "setting_log" not in world and "progression" not in world["factions"]["会"]
    assert ns.query_ledger(tmp_path, kind="arcs", entry_type="note")[0]["detail"] == "完"
    assert rep["bible_bytes_after"] < rep["bible_bytes_before"]
    with pytest.raises(ValueError, match="already"):
        migrate(tmp_path)


def test_migration_cli_runs(tmp_path):
    _bible(tmp_path, [{"name": "甲", "status": "alive"}])
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / "scripts" / "novel_ledger.py"
    out = subprocess.run([sys.executable, str(script), "context-size", str(tmp_path)],
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["chars"] > 0


def test_migration_stale_after_drops_old_state_keys_only(tmp_path):
    legacy = {"name": "甲", "status": "alive", "power_level": 1,
              "progression": [{"chapter": 1, "changes": {"旧伤": "x", "power_level": 1}},
                              {"chapter": 9, "changes": {"新伤": "y"}}],
              "initial": {"name": "甲", "power_level": 1}, "旧伤": "x", "新伤": "y"}
    _bible(tmp_path, [legacy])
    for n in range(1, 11):
        _chapter(tmp_path, n, "正文")
    rep = migrate(tmp_path, stale_after=5)
    card = ns.load_characters(tmp_path)["甲"]
    assert "旧伤" not in card and card["新伤"] == "y" and card["power_level"] == 1
    assert rep["dropped_stale_keys"] == 1
    assert ns.query_ledger(tmp_path, name="甲", field="旧伤")  # still queryable
