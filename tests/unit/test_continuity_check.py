"""continuity_check — the negative controls the existing suite was missing.

`test_novel_tools.py` already exercises this gate (prose floor, meta markers,
AI-ism density, and the humanize-fidelity trio). Mutation-testing every hard rule
against it showed four that survived being deleted — the gate would have gone on
saying `passed: true` with the rule gone and the suite green:

    rule            caught by test_novel_tools?
    prose-missing   no
    draft-missing   no      <- was a real fail-open, fixed with this file
    length-drift    no
    paragraphs      no

The last two are one oracle's fault, and it is the shape unlazy's gate rules warn
about: `assert "字数" in error or "段落" in error` passes as long as EITHER rule
fires, so each rule certifies the other's absence. Two rules, one disjunction, zero
independent controls. They are isolated here instead.

`draft-missing` was the real defect. A missing `chapter_draft.md` silently disabled
all three fidelity checks (title / length drift / paragraph structure) and the gate
still returned `passed: true`. Missing PROSE was loud; missing DRAFT was mute — same
tool, same run, two files, two treatments. That is `godot_compile`'s blind_builder
shape: a checker that is reachable but blind fails, it does not skip.

Rules covered in `test_novel_tools.py` are deliberately not duplicated here.
"""

import json

import pytest

from aitelier.tools.continuity_check.impl import continuity_check

TITLE = "# 第一章 山门"
# Deliberately free of ns.BANNED_PHRASES and ns.META_MARKERS — the fixture must be
# clean, or a test that provokes one violation would be certified by another.
SENTENCE = "他把柴刀插回腰间，抬头看了看天色。"
PARAS = 24
REPS = 4


def _chapter(title: str = TITLE, paras: int = PARAS, reps: int = REPS) -> str:
    body = "\n\n".join([SENTENCE * reps] * paras)
    return f"{title}\n\n{body}\n"


def _stage(tmp_path, *, prose: str | None = None, draft: str | None = None):
    """Lay out the step dirs the gate reads, and return (workspace_root, out_dir)."""
    if draft is not None:
        d = tmp_path / "novel_chapter" / "draft"
        d.mkdir(parents=True)
        (d / "chapter_draft.md").write_text(draft, encoding="utf-8")
    if prose is not None:
        h = tmp_path / "novel_chapter" / "humanize"
        h.mkdir(parents=True)
        (h / "chapter_final.md").write_text(prose, encoding="utf-8")
    return str(tmp_path), str(tmp_path / "out")


def _run(tmp_path, **kw):
    ws, out = _stage(tmp_path, **kw)
    result = continuity_check(workspace_root=ws, out_dir=out)
    report = json.loads((tmp_path / "out" / "continuity_report.json")
                        .read_text(encoding="utf-8"))
    return result, report


# ── Positive control ─────────────────────────────────────────────────────────

def test_a_faithful_polish_passes(tmp_path):
    """Without this, every negative control below could be passing for free."""
    text = _chapter()
    result, report = _run(tmp_path, prose=text, draft=text)
    assert result == {"passed": True}
    assert report["violations"] == []
    assert report["blind_gate"] is False


def test_the_fixture_clears_the_prose_floor_it_is_measured_against(tmp_path):
    """Pin the fixture above DEFAULT_MIN_CHARS so a shrink shows up here, not as a
    mystery failure in an unrelated test."""
    from aitelier import novel_state as ns
    from aitelier.tools.continuity_check.impl import DEFAULT_MIN_CHARS
    assert ns.char_count(_chapter()) > DEFAULT_MIN_CHARS


# ── The gate cannot see what it is supposed to read ──────────────────────────

def test_a_missing_draft_raises_instead_of_skipping_the_fidelity_checks(tmp_path):
    """With no draft, title/length/paragraph comparison is impossible — and the
    gate RAISES rather than returning passed:false.

    Returning false was the first fix and it was still wrong: the graph's only
    `passed:false` edge routes back to humanize (max_loop 2, feedback), and
    humanize can only re-emit prose. A missing draft would have burned two 600s
    LLM rounds re-polishing an untouched chapter and then died on "cycle limit
    exceeded", with the real cause buried in the last feedback block. A broken
    precondition fails the run now — the same way state_probe raises when the
    bible is missing.
    """
    ws, out = _stage(tmp_path, prose=_chapter(), draft=None)
    with pytest.raises(ValueError, match="初稿缺失") as exc:
        continuity_check(workspace_root=ws, out_dir=out)
    # The message must name the real cause: humanize's prose is not what broke.
    assert "重润没有用" in str(exc.value) and "promotion" in str(exc.value)
    # The report still lands, so the failure is inspectable after the fact.
    report = json.loads((tmp_path / "out" / "continuity_report.json")
                        .read_text(encoding="utf-8"))
    assert report["blind_gate"] is True and report["passed"] is False


def test_a_missing_prose_fails(tmp_path):
    result, _ = _run(tmp_path, prose=None, draft=_chapter())
    assert result["passed"] is False
    assert "正文文件缺失" in result["error"]


# ── The two rules the existing disjunction could not tell apart ─────────────

def test_length_drift_beyond_the_band_fails_on_its_own(tmp_path):
    draft = _chapter(paras=24)
    result, _ = _run(tmp_path, prose=_chapter(paras=30), draft=draft)
    assert result["passed"] is False
    assert "润色字数漂移" in result["error"]


def test_paragraph_restructuring_fails_on_its_own(tmp_path):
    draft = _chapter(paras=24)
    # Split every paragraph in two: identical character count (char_count strips
    # whitespace), so ONLY the paragraph-structure rule can catch this one.
    prose = draft.replace(SENTENCE * REPS, SENTENCE * 2 + "\n\n" + SENTENCE * 2)
    result, _ = _run(tmp_path, prose=prose, draft=draft)
    assert result["passed"] is False
    assert "润色改了段落结构" in result["error"]
    assert "润色字数漂移" not in result["error"]  # isolation: only the one rule fired


# ── Advisory must stay advisory ──────────────────────────────────────────────

def test_an_overlong_chapter_is_advisory_not_a_violation(tmp_path):
    # Deliberate: the upper bound fought the ±10% drift rule and burned a whole
    # chapter's loop budget (see the impl's comment). It must stay advisory.
    long = _chapter(paras=120)
    result, report = _run(tmp_path, prose=long, draft=long)
    assert result["passed"] is True
    assert any("字数偏长" in a for a in report["advisories"])


@pytest.mark.parametrize("out_dir", ["", None])
def test_the_gate_still_answers_without_an_out_dir(tmp_path, out_dir):
    ws, _ = _stage(tmp_path, prose=_chapter(), draft=_chapter())
    assert continuity_check(workspace_root=ws, out_dir=out_dir or "") == {"passed": True}
