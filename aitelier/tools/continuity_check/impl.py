"""continuity_check — the CHEAP MECHANICAL gate before the Red review.

Only checks that need no reading comprehension live here — word count vs the
pacing bounds, meta markers leaking into prose, and a crude known-AI-ism density
scan. Everything that needs to understand the text (dead characters reappearing,
OOC, power regressions, timeline, hook strength) is the Red reviewer's job
(draft_review, with evidence-quoting) — string-matching a dead character's NAME
both misses indirect references and false-flags legitimate mentions, so it does
not belong in a deterministic gate.

Returns {"passed": bool, "error": <summary>} — `passed` drives the graph
transition (fail → back to draft, feedback injected); the full report (hard
violations + advisories) is written for the Red reviewer's context.
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


def continuity_check(*, project_root: str = "", workspace_root: str = "",
                     out_dir: str = "", prose_step_dir: str = "", **kwargs) -> dict:
    base = project_root or workspace_root or "."           # novel/ tree (pacing)
    stage = workspace_root or project_root or "."          # step outputs (prose)
    prose_dir = Path(prose_step_dir) if prose_step_dir \
        else Path(stage) / "novel_chapter" / "humanize"
    prose_path = prose_dir / "chapter_final.md"

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
