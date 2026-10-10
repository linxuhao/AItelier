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

import copy
import shutil

GENESIS_CARDS = [
    {"name": "季衡", "role": "protagonist", "is_protagonist": True, "status": "alive",
     "power_level": 1, "personality": ["数数"], "progression": []},
    {"name": "尹骁", "role": "mentor", "status": "alive", "power_level": 4000,
     "progression": []},
]
JOURNALS = {
    1: {"events": [{"entity_type": "character", "entity_name": "尹骁",
                    "changes": {"伤情": "右臂发红", "本章行动": "断后"}, "reason": "a"},
                   {"entity_type": "world_setting", "entity_name": "灵气",
                    "changes": {"v": "1%"}, "reason": "测"}],
        "appearances": [{"name": "季衡"}, {"name": "尹骁", "importance": 9}],
        "thread_updates": [{"name": "门", "action": "hint", "detail": "门响"}],
        "arc_updates": [{"name": "主线", "nodes_completed": ["n1"], "notes": "半"}]},
    2: {"events": [{"entity_type": "character", "entity_name": "尹骁",
                    "changes": {"伤情": "右臂发白", "status": "dead"}, "reason": "b"},
                   {"entity_type": "character", "entity_name": "唐栀", "create": True,
                    "changes": {"role": "ally", "power_level": 5}, "reason": "登场"},
                   {"entity_type": "protagonist", "entity_name": "主角",
                    "changes": {"power_level": 2}, "reason": "c"}],
        "appearances": [{"name": "季衡"}, {"name": "唐栀"}],
        "thread_updates": [{"name": "门", "action": "hint", "detail": "门开"},
                           {"name": "钥匙", "action": "register", "detail": "新"}],
        "arc_updates": [{"name": "主线", "nodes_completed": [], "notes": "推进"}]},
}


def _legacy_genesis(ws):
    b = ns.bible_dir(ws)
    b.mkdir(parents=True, exist_ok=True)
    (b / "overview.md").write_text("# 总纲", encoding="utf-8")
    ns.dump_yaml(b / "pacing.yaml", {"min_chars_per_chapter": 10})
    ns.dump_yaml(b / "world.yaml", {"factions": {}})
    ns.dump_yaml(b / "threads.yaml", [{"name": "门", "status": "open", "hints": []}])
    ns.dump_yaml(b / "arcs.yaml", [{"name": "主线", "status": "active", "nodes": [
        {"id": "n1", "beat": "x", "status": "pending"}, {"id": "n2", "beat": "y", "status": "pending"}]}])
    for c in GENESIS_CARDS:
        c = copy.deepcopy(c)
        c["initial"] = {k: v for k, v in c.items() if k not in ("initial", "progression")}
        ns.dump_yaml(ns.character_path(ws, c["name"]), c)


def _journals(ws, journals=None):
    for n, rec in (journals or JOURNALS).items():
        d = ns.chapter_dir(ws, n)
        d.mkdir(parents=True, exist_ok=True)
        (d / "prose.md").write_text("正文", encoding="utf-8")
        (d / "summary.md").write_text(f"# 第{n}章\n\n摘要", encoding="utf-8")
        ns.dump_yaml(d / "events.yaml", {"chapter": n, **copy.deepcopy(rec)})


def _old_code_apply(ws, journals=None):
    """What the PRE-ledger apply_* wrote (history inside the bible)."""
    for n, rec in (journals or JOURNALS).items():
        cards = ns.load_characters(ws)
        prot = ns._find_protagonist(cards)
        world = ns.load_yaml(ns.bible_dir(ws) / "world.yaml", {})
        for ev in rec["events"]:
            name, ch = ev["entity_name"], ev["changes"]
            if ev["entity_type"] in ("character", "protagonist"):
                if ev["entity_type"] == "protagonist" and name not in cards:
                    name = prot
                card = cards.get(name)
                created = card is None
                if created:
                    card = cards[name] = {"name": name, "status": "alive", "progression": []}
                for k, v in ch.items():
                    card[k] = v
                if created:
                    card["initial"] = {k: v for k, v in card.items() if k not in ("initial", "progression")}
                card["progression"].append({"chapter": n, "changes": ch, "reason": ev["reason"]})
                ns.dump_yaml(ns.character_path(ws, name), card)
            elif ev["entity_type"] == "world_setting":
                st = world.setdefault("settings", {})
                new = ev["entity_name"] not in st
                e = st.setdefault(ev["entity_name"], {})
                e.update(ch)
                if new:
                    e["initial"] = dict(ch)
                world.setdefault("setting_log", []).append(
                    {"chapter": n, "name": ev["entity_name"], "changes": ch, "reason": ev["reason"]})
        ns.dump_yaml(ns.bible_dir(ws) / "world.yaml", world)
        cards = ns.load_characters(ws)
        for ap in rec["appearances"]:
            c = cards[ap["name"]]
            c["last_appearance"] = n
            c.setdefault("first_appearance", n)
            ns.dump_yaml(ns.character_path(ws, ap["name"]), c)
        threads = ns.load_yaml(ns.bible_dir(ws) / "threads.yaml", [])
        for up in rec["thread_updates"]:
            t = next((t for t in threads if t["name"] == up["name"]), None)
            if t is None:
                threads.append({"name": up["name"], "status": "open", "introduced_chapter": n, "hints": []})
            elif up["action"] == "hint":
                t["hints"].append({"chapter": n, "hint": up["detail"]})
            elif up["action"] == "resolve":
                t["status"], t["resolution_chapter"], t["resolution"] = "resolved", n, up["detail"]
            elif up["action"] == "abandon":
                t["status"], t["abandon_reason"] = "abandoned", up["detail"]
        ns.dump_yaml(ns.bible_dir(ws) / "threads.yaml", threads)
        arcs = ns.load_yaml(ns.bible_dir(ws) / "arcs.yaml", [])
        for up in rec["arc_updates"]:
            a = arcs[0]
            for nid in up["nodes_completed"]:
                nd = next(x for x in a["nodes"] if x["id"] == nid)
                nd["status"], nd["completed_chapter"] = "done", n
            if up.get("notes"):
                a.setdefault("progress_notes", []).append({"chapter": n, "note": up["notes"]})
            if all(x.get("status") == "done" for x in a["nodes"]) and a.get("status") != "completed":
                a["status"], a["end_chapter"] = "completed", n
        ns.dump_yaml(ns.bible_dir(ws) / "arcs.yaml", arcs)


def _new_code_replay(ws):
    """The writing bench's replay: legacy genesis → new apply_* (no pre-pass)."""
    for n, rec in JOURNALS.items():
        ns.apply_events(ws, copy.deepcopy(rec["events"]), n)
        ns.log_appearances(ws, rec["appearances"], n)
        ns.apply_thread_updates(ws, rec["thread_updates"], n)
        ns.apply_arc_updates(ws, rec["arc_updates"], n)
    ns.rebuild_index(ws)


def _files(root):
    base = ns.novel_root(root)
    return {str(p.relative_to(base)): p.read_bytes()
            for sub in ("bible", "ledger", "state/index.yaml")
            for p in ([base / sub] if (base / sub).is_file() else sorted((base / sub).rglob("*")))
            if p.is_file()}


def test_migrated_legacy_repo_equals_new_code_replay(tmp_path):
    """The writing bench's replay guard needs this byte-for-byte."""
    legacy, replay = tmp_path / "legacy", tmp_path / "replay"
    _legacy_genesis(legacy)
    _journals(legacy)
    shutil.copytree(legacy, replay)
    _old_code_apply(legacy)
    ns.rebuild_index(legacy)
    assert "progression" in ns.load_characters(legacy)["尹骁"]
    rep = migrate(legacy)
    assert rep["migrated"] is True and rep["dropped_transient_keys"] == 1
    _new_code_replay(replay)
    a, b = _files(legacy), _files(replay)
    assert a.keys() == b.keys()
    diff = [k for k in a if a[k] != b[k]]
    assert not diff, {k: (a[k].decode()[:400], b[k].decode()[:400]) for k in diff}
    card = ns.load_characters(legacy)["尹骁"]
    assert card["伤情"] == "右臂发白" and "本章行动" not in card
    assert [r["type"] for r in ns.read_ledger(legacy, "characters", "唐栀")] == ["create", "appearance"]
    assert migrate(legacy)["migrated"] is False          # content-detected no-op


def test_migration_keeps_post_deploy_rows_and_rolls_back(tmp_path, monkeypatch):
    ws = tmp_path / "w"
    _legacy_genesis(ws)
    _journals(ws)
    _old_code_apply(ws)
    # a chapter booked by the new code before anyone migrated
    ns.ledger_append(ws, "characters", "季衡", {"chapter": 3, "type": "appearance"})
    before = _files(ws)
    import aitelier.novel_ledger_migrate as m
    monkeypatch.setattr(m.ns, "rebuild_index", lambda w: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        migrate(ws)
    assert _files(ws) == before                          # transaction rolled back
    monkeypatch.undo()
    migrate(ws)
    rows = ns.read_ledger(ws, "characters", "季衡")
    assert [r["chapter"] for r in rows] == [1, 2, 2, 3]   # ch2 = event + appearance


def test_migration_dry_run_writes_nothing(tmp_path):
    ws = tmp_path / "w"
    _legacy_genesis(ws)
    _journals(ws)
    _old_code_apply(ws)
    before = _files(ws)
    assert migrate(ws, dry_run=True)["dry_run"] is True
    assert _files(ws) == before


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


def test_bad_tic_regex_is_advisory_not_crash(tmp_path):
    _bible(tmp_path, [{"name": "甲", "status": "alive"}],
           pacing={"min_chars_per_chapter": 10,
                   "style": {"narration_tics": [{"pattern": "走到(", "max_per_chapter": 0}]}})
    _stage(tmp_path, "# 第1章：试\n\n走到肩。\n\n他抬手——")
    r = continuity_check(workspace_root=str(tmp_path), out_dir=str(tmp_path / "cc"))
    assert r["passed"] is True
    rep = json.loads((tmp_path / "cc" / "continuity_report.json").read_text(encoding="utf-8"))
    assert any("正则无效" in a for a in rep["advisories"])


def test_negative_limit_does_not_drop_rows(tmp_path):
    _bible(tmp_path, [{"name": "甲", "status": "alive"}])
    for ch in (1, 2):
        ns.ledger_append(tmp_path, "characters", "甲", {"chapter": ch, "type": "event"})
    assert len(ns.query_ledger(tmp_path, limit=-1)) == 2
    assert ledger_query(workspace_root=str(tmp_path), limit=-5)["count"] == 1


def test_migration_cli_runs(tmp_path):
    _bible(tmp_path, [{"name": "甲", "status": "alive"}])
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / "scripts" / "novel_ledger.py"
    out = subprocess.run([sys.executable, str(script), "context-size", str(tmp_path)],
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["chars"] > 0



# ── genesis-tagged books: migration == bench replay, by construction ────────

JOURNALS_HARD = {
    **JOURNALS,
    3: {"events": [{"entity_type": "character", "entity_name": "陆竞", "create": True,
                    "changes": {"role": "ally", "本章行动": "堵门", "伤情": None}, "reason": "登场"},
                   {"entity_type": "world_setting", "entity_name": "雾",
                    "changes": {"浓度": "高", "本章表现": "涌入"}, "reason": "首现"}],
        "appearances": [{"name": "陆竞"}],
        "thread_updates": [{"name": "门", "action": "resolve", "detail": "门后是走廊"},
                           {"name": "钥匙", "action": "hint", "detail": "铁锈味"}],
        "arc_updates": [{"name": "主线", "nodes_completed": ["n2"], "notes": "收束"}]},
    4: {"events": [], "appearances": [],
        "thread_updates": [{"name": "钥匙", "action": "abandon", "detail": "放弃"}],
        "arc_updates": []},
}


def _git(ws, *args):
    return subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True,
                          text=True).stdout


def _tagged_legacy_book(ws, journals):
    ws.mkdir(parents=True)
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    _legacy_genesis(ws)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "genesis")
    _git(ws, "tag", ns.GENESIS_TAG)
    _journals(ws, journals)
    _old_code_apply(ws, journals)
    ns.rebuild_index(ws)
    ns.rebuild_digest(ws)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "legacy chapters")


def test_tagged_book_migrates_by_replay_and_passes_bench_replay(tmp_path):
    """All three field-order shapes: hint→resolve/abandon, note→arc completed,
    mid-book create whose first event carries 本章*/null keys."""
    from aitelier.writing_bench.bench import Bench
    from aitelier.writing_bench.storage import git_files
    ws = tmp_path / "book"
    _tagged_legacy_book(ws, JOURNALS_HARD)
    rep = migrate(ws)
    assert rep["mode"] == "replay" and rep["drift"] == [], rep
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "migrate")
    # the bench's own replay over the migrated commit must change nothing
    genesis_sha = _git(ws, "rev-parse", ns.GENESIS_TAG + "^{commit}").strip()
    Bench._reset_replay(object(), ws, git_files(ws, genesis_sha))
    assert _git(ws, "status", "--porcelain") == ""
    t = {x["name"]: x for x in ns.load_yaml(ns.bible_dir(ws) / "threads.yaml")}
    assert t["门"]["status"] == "resolved" and t["门"]["hint_count"] == 2
    assert t["钥匙"]["status"] == "abandoned"
    arc = ns.load_yaml(ns.bible_dir(ws) / "arcs.yaml")[0]
    assert arc["status"] == "completed" and arc["latest_note"]["note"] == "收束"
    lu = ns.load_characters(ws)["陆竞"]
    assert "本章行动" not in lu and "本章行动" not in lu["initial"] and "伤情" not in lu
    assert migrate(ws)["migrated"] is False


def test_tagged_book_with_off_journal_edit_refuses_then_force(tmp_path):
    ws = tmp_path / "book"
    _tagged_legacy_book(ws, JOURNALS)
    card = ns.load_yaml(ns.character_path(ws, "尹骁"))
    card["power_level"] = 9999                         # hand edit, no journal entry
    ns.dump_yaml(ns.character_path(ws, "尹骁"), card)
    before = _files(ws)
    from aitelier.novel_ledger_migrate import MigrationDrift
    with pytest.raises(MigrationDrift) as e:
        migrate(ws)
    assert any("power_level" in d for d in e.value.drift)
    assert _files(ws) == before                        # rolled back
    dry = migrate(ws, dry_run=True)
    assert dry["dry_run"] and dry["drift"] and _files(ws) == before
    assert migrate(ws, force=True)["mode"] == "replay"
    assert ns.load_characters(ws)["尹骁"]["power_level"] == 4000   # journal wins


def test_untagged_book_uses_strip_mode(tmp_path):
    ws = tmp_path / "w"
    _legacy_genesis(ws)
    _journals(ws)
    _old_code_apply(ws)
    assert migrate(ws).get("mode", "strip") == "strip"
