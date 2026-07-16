"""continuity_check — the CHEAP MECHANICAL gate on the humanized chapter.

Runs right AFTER humanize (draft → draft_review → humanize → HERE), and does two
mechanical jobs; anything needing reading comprehension (OOC, power regressions,
timeline, hook strength) is the Red reviewer's job on the DRAFT (draft_review,
with evidence-quoting) — string-matching a dead character's NAME both misses
indirect references and false-flags legitimate mentions, so it stays out.

1. Prose floor: word count vs pacing bounds, meta markers leaking into prose,
   and a crude known-AI-ism density scan (套话 frequency — NOT semantic
   AI-detection; the real de-AI-ing is humanize itself + the human at CP#2).
2. Humanize fidelity: diff against the approved draft on invariants that need NO
   reading comprehension — title line, length, paragraph structure. humanize
   re-emits the whole chapter with no surgical-edit constraint, so a deleted
   scene or a renamed chapter would otherwise sail past Red's sign-off (Red
   reviewed the DRAFT). Whether the CAST or the meaning survived is NOT checked
   here: that needs a reader (see the note above _para_count) — gross drift
   still shows up in the counts, and the rest is Red's + the human's at CP#2.
   The draft is the truth source and stays intact, so a violation loops back to
   humanize to re-polish from it.

Returns {"passed": bool, "error": <summary>} — `passed` drives the graph
transition (fail → back to humanize, feedback injected); the full report (hard
violations + advisories) is written for downstream context.
"""

import json
from pathlib import Path

from aitelier import novel_state as ns

# AI-ism density gate: total banned-phrase hits per 1000 chars.
BANNED_DENSITY_MAX = 3.0
# Any single banned phrase repeated more than this many times is a violation
# regardless of density (仿佛 ×7 in one chapter is slop even in a long chapter).
BANNED_SINGLE_MAX = 4

DEFAULT_MIN_CHARS = 1500
DEFAULT_MAX_CHARS = 6000

# Humanize fidelity: the polish pass promises ±10% length and an untouched
# structure. It re-emits the WHOLE chapter (create_final) with no surgical-edit
# constraint, so it can silently drift — and nothing downstream re-checks the
# substance (Red reviewed the DRAFT; the human at CP#2 won't diff line by line).
HUMANIZE_LEN_DELTA_MAX = 10.0    # percent
HUMANIZE_PARA_TOLERANCE = 0.15   # fraction of the draft's paragraph count


def _title_line(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line.strip()
    return ""


def _para_count(text: str) -> int:
    return len([p for p in text.split("\n\n") if p.strip()])


# NOTE (deliberate omission): there is no cast check here.
#
# A "did the polish drop a character" gate can only be built out of name/alias
# string matching, and that is exactly the thing this tool's contract rules out:
# it MISSES indirect references (初稿「王老曾说过」→ 润色「师父曾说过」: the person
# was never on stage, yet the name vanished) and FALSE-FLAGS legitimate ones
# (short names collide with ordinary words), while still missing the real damage
# (name kept once, the character's whole scene polished away). Noisy AND leaky —
# and it was wired to a HARD gate that stalls the chapter and forces a re-polish.
# Whether the cast survived the polish needs reading comprehension, so it belongs
# to a reader: Red on the draft (七维「角色行为」, with evidence) and the human at
# CP#2. Gross drift is still caught below by genuinely mechanical invariants —
# a deleted scene moves the length/paragraph counts.


def continuity_check(*, project_root: str = "", workspace_root: str = "",
                     out_dir: str = "", prose_step_dir: str = "", **kwargs) -> dict:
    base = project_root or workspace_root or "."           # novel/ tree (pacing)
    stage = workspace_root or project_root or "."          # step outputs (prose)
    prose_dir = Path(prose_step_dir) if prose_step_dir \
        else Path(stage) / "novel_chapter" / "humanize"
    prose_path = prose_dir / "chapter_final.md"
    draft_path = Path(stage) / "novel_chapter" / "draft" / "chapter_draft.md"

    violations: list[str] = []
    advisories: list[str] = []

    prose = prose_path.read_text(encoding="utf-8") if prose_path.is_file() else ""
    if not prose:
        violations.append(f"正文文件缺失: {prose_path}")

    n = ns.next_chapter_number(base)

    if prose:
        # ── Word count vs pacing bounds ──
        pacing = ns.load_yaml(ns.bible_dir(base) / "pacing.yaml", {}) or {}
        count = ns.char_count(prose)
        lo = int(pacing.get("min_chars_per_chapter", DEFAULT_MIN_CHARS))
        hi = int(pacing.get("max_chars_per_chapter", DEFAULT_MAX_CHARS))
        if count < lo:
            violations.append(f"字数不足: {count} 字（下限 {lo}）")
        elif count > hi:
            violations.append(f"字数超限: {count} 字（上限 {hi}）")

        # ── Known AI-ism density (crude first pass; semantic slop is Red's job) ──
        hits: dict[str, int] = {}
        for phrase in ns.BANNED_PHRASES:
            c = prose.count(phrase)
            if c:
                hits[phrase] = c
        total = sum(hits.values())
        density = (total * 1000.0 / count) if count else 0.0
        singles = {p: c for p, c in hits.items() if c > BANNED_SINGLE_MAX}
        if density > BANNED_DENSITY_MAX or singles:
            detail = "、".join(f"{p}×{c}" for p, c in sorted(
                hits.items(), key=lambda kv: -kv[1])[:8])
            violations.append(
                f"AI高频套话超标（{total} 处 / 密度 {density:.1f}/千字）: {detail} "
                "— humanize 步骤应替换为具体的动作与感知描写")
        elif hits:
            advisories.append(
                "少量高频词（未超标）: "
                + "、".join(f"{p}×{c}" for p, c in hits.items()))

        # ── Meta markers ──
        found = [m for m in ns.META_MARKERS if m in prose]
        if found:
            violations.append(f"正文含元信息标记: {found} — 正文不得出现非故事内容")

        # ── Humanize fidelity: 润色只许改语言，不得动实质（对照 draft 初稿）──
        # Red 审的是初稿；humanize 之后没有第二次实质评审，人工也不会逐行 diff。
        # 这里用机械不变量兜住漂移（语义仍归 Red/人工）。draft 是真相源且完好，
        # 违规 → 回 humanize 重润即可。
        draft = draft_path.read_text(encoding="utf-8") if draft_path.is_file() else ""
        if draft:
            d_title, f_title = _title_line(draft), _title_line(prose)
            if d_title and f_title and d_title != f_title:
                violations.append(
                    f"润色改了标题: 初稿『{d_title}』→ 终稿『{f_title}』——标题行不得改动")

            d_count = ns.char_count(draft)
            if d_count:
                delta = (count - d_count) / d_count * 100.0
                if abs(delta) > HUMANIZE_LEN_DELTA_MAX:
                    violations.append(
                        f"润色字数漂移 {delta:+.0f}%（初稿 {d_count} → 终稿 {count}），"
                        f"超出 ±{HUMANIZE_LEN_DELTA_MAX:.0f}% —— 润色只改语言，"
                        "不得增删情节/段落")

            d_paras, f_paras = _para_count(draft), _para_count(prose)
            if d_paras and abs(f_paras - d_paras) > max(2, d_paras * HUMANIZE_PARA_TOLERANCE):
                violations.append(
                    f"润色改了段落结构: 初稿 {d_paras} 段 → 终稿 {f_paras} 段 —— "
                    "段落结构不得改动（只在句内与相邻句衔接处润色）")

        # ── Advisory-only: ending shape (real hook judgment is Red's) ──
        last_para = prose.rstrip().rsplit("\n", 1)[-1].strip()
        if last_para and last_para.endswith("。") and not any(
                ch in last_para for ch in "？！—…"):
            advisories.append(
                "章末最后一段以平稳句号收尾，无疑问/破折/省略号 — 复核是否留有钩子"
                "（advisory，由评审员判断）")

    passed = not violations
    report = {"passed": passed, "chapter": n,
              "violations": violations, "advisories": advisories}
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "continuity_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    result = {"passed": passed}
    if not passed:
        result["error"] = "continuity_check 未通过:\n- " + "\n- ".join(violations)
    return result
