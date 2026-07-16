# tests/unit/test_novel_tools.py
# Unit tests for the novel pipeline's 4 deterministic tools — exercised as one
# journey over a tmp root: scaffold_bible → state_probe (assembles the context
# bundle) → continuity_check (mechanical gate) → apply_state (books the journal +
# git commit) → reconcile drift detection.
#
# Tools take project_root (code repo) in production and fall back to
# workspace_root; the tests pass workspace_root and let both resolve to tmp_path.

import json
import subprocess

import pytest
import yaml

from aitelier import novel_state as ns
from aitelier.tools.apply_state.impl import apply_state
from aitelier.tools.continuity_check.impl import continuity_check
from aitelier.tools.scaffold_bible.impl import scaffold_bible
from aitelier.tools.state_probe.impl import state_probe


# The design step now emits SEVEN separate bible files (one small write each).
SEED_FILES = {
    "overview.md": "# 总纲\n\n主角林凡求道长生，核心矛盾是凡人之躯 vs 天道无情。",
    "compass.md": "终局：渡劫飞升。活跃长线：身世之谜。",
    "world.yaml": {"magic_system": {"境界": ["练气", "筑基", "金丹"]}},
    "pacing.yaml": {"min_chars_per_chapter": 100, "max_chars_per_chapter": 6000},
    "characters.yaml": [
        {"name": "林凡", "role": "protagonist", "is_protagonist": True,
         "power_level": 10, "tier": 1, "personality": ["坚韧"]},
        {"name": "王老", "role": "mentor", "status": "dead", "aliases": ["王长老"]},
        {"name": "赵四", "role": "rival"},
    ],
    "threads.yaml": [
        {"name": "身世之谜", "description": "林凡的真实身份", "importance": 8,
         "earliest_reveal": {"arc": "求道长生", "node": "n2"}},
        {"name": "断剑来历", "description": "旧剑的秘密", "importance": 5},
    ],
    "arcs.yaml": [
        {"name": "求道长生", "arc_type": "main", "description": "主线",
         "nodes": [
             {"id": "n1", "beat": "拜入宗门"},
             {"id": "n2", "beat": "筑基成功"},
             {"id": "n3", "beat": "触及天道之秘"},
         ]},
    ],
}


def _seed(tmp_path, git=False):
    """Scaffold a novel into tmp via the real tool. Simulates the design agent
    having already written the raw bible into the repo (novel/bible/, mode:write +
    repo_apply); scaffold_bible then normalizes it. Optionally git-init so the
    commit path is exercised."""
    if git:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    bible_out = tmp_path / "novel" / "bible"
    bible_out.mkdir(parents=True)
    for fname, content in SEED_FILES.items():
        p = bible_out / fname
        if isinstance(content, str):
            p.write_text(content, encoding="utf-8")
        else:
            p.write_text(yaml.safe_dump(content, allow_unicode=True), encoding="utf-8")
    return scaffold_bible(workspace_root=str(tmp_path))


def _write_prose(tmp_path, text):
    d = tmp_path / "novel_chapter" / "humanize"
    d.mkdir(parents=True, exist_ok=True)
    (d / "chapter_final.md").write_text(text, encoding="utf-8")


def _write_events(tmp_path, record):
    d = tmp_path / "novel_chapter" / "finalize"
    d.mkdir(parents=True, exist_ok=True)
    (d / "chapter_events.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8")


GOOD_PROSE = ("# 第1章\n\n" + "林凡握紧了剑。赵四步步紧逼，灵气翻涌。" * 20
              + "\n\n他抬头望向山门，那里站着一个不该出现的人——")


# ── scaffold_bible ───────────────────────────────────────────────────────────

def test_scaffold_normalizes_bible(tmp_path):
    result = _seed(tmp_path)
    assert result["scaffolded"] is True and result["characters"] == 3
    assert ns.bible_exists(tmp_path)
    cards = ns.load_characters(tmp_path)
    assert cards["林凡"]["power_level"] == 10 and cards["王老"]["status"] == "dead"
    # node defaults filled; no genesis snapshot DIRECTORY (git tag is baseline)
    arcs = ns.load_yaml(ns.bible_dir(tmp_path) / "arcs.yaml")
    assert all(nd["status"] == "pending" for nd in arcs[0]["nodes"])
    assert not (ns.state_dir(tmp_path) / "genesis").exists()


def test_scaffold_refuses_rescaffold_and_bad_seed(tmp_path):
    _seed(tmp_path)
    with pytest.raises(ValueError, match="already scaffolded"):
        scaffold_bible(workspace_root=str(tmp_path))
    empty = tmp_path / "empty_ws"
    empty.mkdir()
    with pytest.raises(ValueError, match="did not write"):
        scaffold_bible(workspace_root=str(empty))


def test_scaffold_rejects_nodeless_arc_and_bad_gate(tmp_path):
    bible_out = tmp_path / "novel" / "bible"
    bible_out.mkdir(parents=True)
    files = dict(SEED_FILES)
    files["arcs.yaml"] = [{"name": "求道长生", "arc_type": "main",
                           "description": "主线"}]  # no nodes
    for fname, content in files.items():
        (bible_out / fname).write_text(
            content if isinstance(content, str)
            else yaml.safe_dump(content, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="no plot nodes"):
        scaffold_bible(workspace_root=str(tmp_path))
    # bad thread gate → fail loud at scaffold, not at chapter 200
    (bible_out / "arcs.yaml").write_text(
        yaml.safe_dump(SEED_FILES["arcs.yaml"], allow_unicode=True), encoding="utf-8")
    (bible_out / "threads.yaml").write_text(yaml.safe_dump(
        [{"name": "x", "description": "y",
          "earliest_reveal": {"arc": "求道长生", "node": "n99"}}],
        allow_unicode=True), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown node"):
        scaffold_bible(workspace_root=str(tmp_path))


def test_scaffold_git_commit_and_genesis_tag(tmp_path):
    result = _seed(tmp_path, git=True)
    assert result["committed"] is True and result["genesis_tagged"] is True
    assert ns.has_genesis_tag(tmp_path)
    baseline = ns.genesis_characters(tmp_path)
    assert set(baseline) == {"林凡", "王老", "赵四"}


# ── state_probe ──────────────────────────────────────────────────────────────

def test_probe_fails_loud_without_bible(tmp_path):
    with pytest.raises(ValueError, match="novel_init"):
        state_probe(workspace_root=str(tmp_path))


def test_probe_assembles_context_bundle(tmp_path):
    _seed(tmp_path)
    out = tmp_path / "novel_chapter" / "probe"
    flags = state_probe(workspace_root=str(tmp_path), out_dir=str(out))
    assert flags == {"next_chapter": 1, "has_previous": False}
    pack = (out / "novel_context.md").read_text(encoding="utf-8")
    # whole bible present — no mention filtering, all cards go in
    for token in ("总纲", "指南针", "节奏与爽点约定", "林凡", "王老", "赵四",
                  "身世之谜", "求道长生"):
        assert token in pack, token
    # node-driven sections: frontier shown; gated thread locked, ungated revealable
    assert "剧情前沿" in pack and "n1" in pack and "拜入宗门" in pack
    assert "未解锁伏笔" in pack and "可回收伏笔" in pack
    lock_sec = pack.split("未解锁伏笔")[1]
    assert "身世之谜" in lock_sec           # gated on n2 (pending) → locked
    reveal_sec = pack.split("可回收伏笔")[1].split("未解锁伏笔")[0]
    assert "断剑来历" in reveal_sec          # no gate → revealable
    assert json.loads((out / "probe.json").read_text(encoding="utf-8"))["next_chapter"] == 1


# ── continuity_check (mechanical only) ───────────────────────────────────────

def test_continuity_passes_clean_prose(tmp_path):
    _seed(tmp_path)
    _write_prose(tmp_path, GOOD_PROSE)
    assert continuity_check(workspace_root=str(tmp_path),
                            out_dir=str(tmp_path / "cc")) == {"passed": True}


def test_continuity_fails_on_meta_marker_and_short(tmp_path):
    _seed(tmp_path)
    _write_prose(tmp_path, "# 第1章\n\n短。\nTODO 补一段")
    r = continuity_check(workspace_root=str(tmp_path), out_dir=str(tmp_path / "cc"))
    assert r["passed"] is False
    assert "元信息" in r["error"] and "字数不足" in r["error"]


def test_continuity_fails_on_ai_slop_density(tmp_path):
    _seed(tmp_path)
    _write_prose(tmp_path, GOOD_PROSE + "他不禁一愣，仿佛看到了什么，" * 6)
    r = continuity_check(workspace_root=str(tmp_path))
    assert r["passed"] is False and "套话" in r["error"]


def test_continuity_no_dead_char_check(tmp_path):
    # Dead-character detection was removed (it's the Red reviewer's semantic job).
    # A dead character named in prose must NOT hard-fail the mechanical gate.
    _seed(tmp_path)
    _write_prose(tmp_path, GOOD_PROSE + "\n王老的身影浮现在脑海。")
    assert continuity_check(workspace_root=str(tmp_path))["passed"] is True


# ── apply_state + reconcile ──────────────────────────────────────────────────

def _book_chapter_one(tmp_path):
    _write_prose(tmp_path, GOOD_PROSE)
    _write_events(tmp_path, {
        "chapter": 1, "title": "第1章标题",
        "summary": "林凡与赵四冲突，山门出现神秘人。",
        "events": [{"entity_type": "protagonist", "entity_name": "林凡",
                    "changes": {"power_level": 15}, "reason": "初战突破"}],
        "appearances": [{"name": "林凡"}, {"name": "赵四"}],
        "thread_updates": [{"name": "身世之谜", "action": "hint", "detail": "神秘人出现"}],
        "arc_updates": [{"name": "求道长生", "nodes_completed": ["n1"],
                         "notes": "n2 已推进（开始筑基）"}],
    })
    return apply_state(workspace_root=str(tmp_path),
                       out_dir=str(tmp_path / "novel_chapter" / "apply"))


def test_apply_state_books_the_chapter(tmp_path):
    _seed(tmp_path)
    result = _book_chapter_one(tmp_path)
    # non-git tmp → reconcile skips with a note; that's the only warning
    assert result["applied"] is True and result["chapter"] == 1
    assert all("reconcile skipped" in w for w in result["warnings"])
    ch = ns.chapter_dir(tmp_path, 1)
    assert (ch / "prose.md").is_file() and (ch / "events.yaml").is_file()
    lin = ns.load_characters(tmp_path)["林凡"]
    assert lin["power_level"] == 15 and lin["last_appearance"] == 1
    assert lin["progression"][-1]["chapter"] == 1
    threads = ns.load_yaml(ns.bible_dir(tmp_path) / "threads.yaml")
    assert threads[0]["hints"][0]["chapter"] == 1
    # node booked: n1 done with completed_chapter record, frontier moved to n2
    arcs = ns.load_yaml(ns.bible_dir(tmp_path) / "arcs.yaml")
    n1 = arcs[0]["nodes"][0]
    assert n1["status"] == "done" and n1["completed_chapter"] == 1
    assert ns.arc_frontier(arcs[0])["id"] == "n2"
    index = ns.load_yaml(ns.state_dir(tmp_path) / "index.yaml")
    assert index["next_chapter"] == 2 and index["chapters_written"] == 1
    assert index["arc_frontiers"]["求道长生"] == "n2"
    assert (ns.state_dir(tmp_path) / "digest.md").is_file()
    assert state_probe(workspace_root=str(tmp_path))["next_chapter"] == 2


def test_apply_state_node_warnings_and_arc_autocomplete(tmp_path):
    _seed(tmp_path)
    _write_prose(tmp_path, GOOD_PROSE)
    _write_events(tmp_path, {
        "chapter": 1, "title": "x", "summary": "一章完成全部节点。",
        "events": [], "appearances": [],
        "thread_updates": [
            # resolving a thread whose gate (n2) isn't done YET at check time?
            # updates apply in order: nodes complete in arc_updates AFTER thread
            # updates — so this resolve happens while n2 is still pending → warn.
            {"name": "身世之谜", "action": "resolve", "detail": "真相大白"}],
        "arc_updates": [{"name": "求道长生",
                         "nodes_completed": ["n2", "n1", "n3", "n9"]}],
    })
    result = apply_state(workspace_root=str(tmp_path))
    joined = " ".join(result["warnings"])
    assert "提前揭开" in joined          # gated resolve before node done
    assert "unknown node 'n9'" in joined
    assert "out of order" in joined      # n2 completed while frontier was n1
    arcs = ns.load_yaml(ns.bible_dir(tmp_path) / "arcs.yaml")
    assert arcs[0]["status"] == "completed"  # all real nodes done → auto-complete


def test_apply_state_git_commits_chapter(tmp_path):
    _seed(tmp_path, git=True)
    result = _book_chapter_one(tmp_path)
    assert result["committed"] is True
    log = subprocess.run(["git", "log", "--oneline"], cwd=tmp_path,
                         capture_output=True, text=True).stdout
    assert "第1章" in log


def test_apply_state_rejects_mismatched_chapter_and_double_booking(tmp_path):
    _seed(tmp_path)
    _write_prose(tmp_path, GOOD_PROSE)
    _write_events(tmp_path, {"chapter": 7, "title": "x", "summary": "x", "events": []})
    with pytest.raises(ValueError, match="mismatch|declares"):
        apply_state(workspace_root=str(tmp_path))
    _book_chapter_one(tmp_path)
    with pytest.raises(ValueError, match="chapter 1|append-only|declares"):
        _book_chapter_one(tmp_path)


def test_apply_state_unknown_character_requires_create(tmp_path):
    _seed(tmp_path)
    _write_prose(tmp_path, GOOD_PROSE)
    _write_events(tmp_path, {
        "chapter": 1, "title": "x", "summary": "x",
        "events": [{"entity_type": "character", "entity_name": "神秘人",
                    "changes": {"tier": 9}, "reason": "登场"}]})
    with pytest.raises(ValueError, match="create"):
        apply_state(workspace_root=str(tmp_path))


def test_apply_state_warns_on_power_regression_and_dead_change(tmp_path):
    _seed(tmp_path)
    _write_prose(tmp_path, GOOD_PROSE)
    _write_events(tmp_path, {
        "chapter": 1, "title": "x", "summary": "x",
        "events": [
            {"entity_type": "protagonist", "entity_name": "林凡",
             "changes": {"power_level": 5}, "reason": "重伤"},
            {"entity_type": "character", "entity_name": "王老",
             "changes": {"tier": 3}, "reason": "??"},
        ]})
    joined = " ".join(apply_state(workspace_root=str(tmp_path))["warnings"])
    assert "regressed" in joined and "dead" in joined


def test_reconcile_detects_hand_edited_drift(tmp_path):
    # Baseline comes from the novel-genesis git TAG now → needs a git repo.
    _seed(tmp_path, git=True)
    _book_chapter_one(tmp_path)
    assert ns.reconcile(tmp_path) == []
    card_path = ns.character_path(tmp_path, "林凡")
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    card["power_level"] = 9999
    card_path.write_text(yaml.safe_dump(card, allow_unicode=True), encoding="utf-8")
    drift = ns.reconcile(tmp_path)
    assert len(drift) == 1 and "power_level" in drift[0]


def test_reconcile_skips_without_git(tmp_path):
    _seed(tmp_path)  # no git → no novel-genesis tag
    out = ns.reconcile(tmp_path)
    assert len(out) == 1 and "reconcile skipped" in out[0]


def test_restage_stages_full_bible_from_repo_plus_verdict(tmp_path):
    # design_gate must show the CUMULATIVE bible (from the repo — the maker's
    # step dir only holds the last surgical-edit subset) + the reviewer verdict
    # (from its step dir).
    from aitelier.tools.restage.impl import restage

    ws, cfg = tmp_path / "ws", "novel_init"
    repo = tmp_path / "repo"
    # Repo has the full bible (all 7 files accumulated via repo_apply).
    bible = repo / "novel" / "bible"
    bible.mkdir(parents=True)
    for f in ["overview.md", "compass.md", "world.yaml", "pacing.yaml",
              "characters.yaml", "threads.yaml", "arcs.yaml"]:
        (bible / f).write_text("x", encoding="utf-8")
    (repo / ".git" / "objects").mkdir(parents=True)  # .git must be skipped
    (repo / ".git" / "config").write_text("[core]", encoding="utf-8")
    # The reviewer's step dir holds the verdict.
    review = ws / cfg / "design_review"
    review.mkdir(parents=True)
    (review / "review_verdict.json").write_text('{"passed": true}', encoding="utf-8")

    out = ws / cfg / "design_gate.tmp"
    res = restage(workspace_root=str(ws), project_root=str(repo), config_name=cfg,
                  out_dir=str(out), from_repo=["novel/bible"],
                  from_steps=["design_review"])

    assert res["restaged"] is True
    staged = {p.relative_to(out).as_posix()
              for p in out.rglob("*") if p.is_file()}
    assert staged == {
        "novel/bible/overview.md", "novel/bible/compass.md",
        "novel/bible/world.yaml", "novel/bible/pacing.yaml",
        "novel/bible/characters.yaml", "novel/bible/threads.yaml",
        "novel/bible/arcs.yaml", "review_verdict.json"}
    assert not (out / ".git").exists()  # .git tree excluded


def test_restage_fails_loud_when_nothing_to_copy(tmp_path):
    from aitelier.tools.restage.impl import restage
    with pytest.raises(ValueError, match="nothing copied"):
        restage(project_root=str(tmp_path / "repo"), out_dir=str(tmp_path / "out"),
                from_repo=["novel/bible"])


def test_probe_bundle_survives_prompt_context_whole():
    # Regression: [Pre-resolved Context] used to clip every entry at 6000 CHARS,
    # which cut the 578-line probe bundle at line 267 — losing world rules
    # (模拟器插入时序), character cards and the plot frontier. The outliner then
    # invented lore contradicting the bible. A bible-sized bundle must arrive whole.
    from core.prompt_assembler import PromptAssembler, MAX_CONTEXT_LINES

    bundle = "### novel_context.md\n" + "\n".join(
        f"设定行 {i}" for i in range(560)) + "\n世界规则: 模拟器插入时序——传送中截停"
    out = PromptAssembler._clip_context_entry("Step probe", bundle, "outline")
    assert out == bundle                      # untouched
    assert "模拟器插入时序" in out             # the rule that got silently cut
    assert "[上下文截断" not in out


def test_oversized_context_truncation_is_recoverable():
    # When a cut IS necessary it must be line-based and tell the agent where to
    # resume — skillflow's read() pages by 0-based start_line — not die silently.
    from core.prompt_assembler import PromptAssembler, MAX_CONTEXT_LINES

    huge = "### novel_context.md\n" + "\n".join(
        f"line {i}" for i in range(MAX_CONTEXT_LINES + 500))
    out = PromptAssembler._clip_context_entry("Step probe", huge, "outline")
    assert "[上下文截断" in out
    assert f"共 {MAX_CONTEXT_LINES + 501} 行" in out          # honest total
    assert f"start_line={MAX_CONTEXT_LINES}" in out           # resume point
    assert "novel_context.md" in out                          # names the file to read


def _write_draft(tmp_path, text):
    d = tmp_path / "novel_chapter" / "draft"
    d.mkdir(parents=True, exist_ok=True)
    (d / "chapter_draft.md").write_text(text, encoding="utf-8")


DRAFT = ("# 第1章：初试\n\n林凡握紧了剑。赵四步步紧逼，灵气翻涌。" * 1 + "\n\n"
         + "林凡握紧了剑。赵四步步紧逼，灵气翻涌。" * 19
         + "\n\n他抬头望向山门，那里站着一个不该出现的人——")


def test_humanize_fidelity_passes_language_only_polish(tmp_path):
    # A legitimate polish: same title/cast/paragraphs, length within ±10%.
    _seed(tmp_path)
    _write_draft(tmp_path, DRAFT)
    _write_prose(tmp_path, DRAFT.replace("步步紧逼", "一步步压上来"))
    assert continuity_check(workspace_root=str(tmp_path),
                            out_dir=str(tmp_path / "cc")) == {"passed": True}


def test_humanize_fidelity_catches_substance_drift(tmp_path):
    # humanize re-emits the whole chapter, so it can silently drift. The draft is
    # the truth source — these must fail the gate and loop back to re-polish.
    _seed(tmp_path)
    _write_draft(tmp_path, DRAFT)

    # 1) renamed the chapter
    _write_prose(tmp_path, DRAFT.replace("# 第1章：初试", "# 第1章：血战"))
    r = continuity_check(workspace_root=str(tmp_path), out_dir=str(tmp_path / "cc"))
    assert r["passed"] is False and "标题" in r["error"]

    # 2) cut the story down instead of just polishing language — a deleted
    #    scene shows up in the counts (this is what replaces the cast check:
    #    "did a character survive" needs a reader, "did the text shrink" doesn't)
    _write_prose(tmp_path, "# 第1章：初试\n\n林凡握紧了剑。\n\n他抬头望向山门——")
    r = continuity_check(workspace_root=str(tmp_path), out_dir=str(tmp_path / "cc"))
    assert r["passed"] is False
    assert "字数" in r["error"] or "段落" in r["error"]
