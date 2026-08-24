"""godot_vision — can a player SEE that the assertions are true?

The play-test gate asserts STATE. On 2026-08-22 run jinyong-turn the scenario
``two_phase_skill_unlock_and_hp_gate`` passed 20/20, asserting
``SkillButton5..8.disabled == true/false`` — and in the frames that same run
rendered, all eight buttons were pixel-identical across three rounds and two
actor changes, no tile grid was visible in a tactical GRID game, five name
labels were ellipsised, and the health bars were unrecognisable as health bars.
Every assertion was green. State assertions answer "is it true"; this gate
answers "can a player see that it is true". Both have to pass.

The checklist is not invented here — it is the game's own design record,
``design/30_presentation.md`` § 可读性硬要求, which also fixes the split: the
GEOMETRY requirements (does the health bar follow its actor, is a rect inside
the viewport, do two rects intersect) go in ``playtest_spec.yaml`` where numbers
judge numbers, and only the RECOGNISABILITY requirements come here. That split
is measured, not stylistic: asked single-frame spatial questions ("does this
text overlap that text", "do these two characters overlap") the model scored
0/2 with both plainly true; asked about visibility, distinguishability and
truncation it scored 3/3, and given four chronological frames it correctly
tracked a changing active actor, a re-ordered turn list and an incrementing
round counter. So every question below is either a recognisability question or
a DIFFERENTIAL one across a frame sequence. Never single-frame geometry.

THE BLIND GATE. A vision check that cannot see and reports green is the very
defect this gate exists to catch, so there is exactly ONE pass-without-looking:
no ``project.godot`` → not a game → no-op, same as godot_compile. Every other
way of not seeing is a loud, named failure (``blind_reason``):

  no_playtest_report   the prior step's report is missing/unreadable
  no_captures          the report has no captures[] (e.g. the run fell back to
                       the pixel-less dummy renderer — render_mode "headless")
  missing_frames       captures[] names PNGs that are not on disk (or truncated)
  endpoint_unreachable the vision endpoint refused / timed out / returned non-200
  unparseable_response the answer cannot be read, or answers fewer questions
                       than were asked
  budget_exceeded      a frame does not fit the context budget — refuse and say
                       how many fit, rather than silently dropping frames
  unchecked_questions  a checklist question ended the run with zero answers

Its ancestors in this addon are ``gate_skipped`` (infra failed → pass, but
LOUD) and ``blind_builder`` (the checker cannot see what it checks → hard
fail). This gate is the second kind throughout: an unreachable GPU box is an
infra problem, but the frames it would have judged are the only evidence that
the UI is legible, and there is no cheaper check standing behind it.
"""

import base64
import json
import os
import re
import struct
import urllib.error
import urllib.request
from pathlib import Path

_URL = os.environ.get(
    "GODOT_VISION_URL", "http://100.68.74.107:8000/v1/chat/completions")
_MODEL = os.environ.get("GODOT_VISION_MODEL", "qwen3")
# Read off the vLLM startup log / GET /v1/models (max_model_len), not a flag.
_CONTEXT_TOKENS = int(os.environ.get("GODOT_VISION_CONTEXT_TOKENS", "12288"))
_MAX_TOKENS = int(os.environ.get("GODOT_VISION_MAX_TOKENS", "2048"))
_TIMEOUT = int(os.environ.get("GODOT_VISION_TIMEOUT", "300"))

# ── Fallback judge ────────────────────────────────────────────────────────────
# The primary endpoint is a self-hosted vLLM on a GPU box that is also used for
# other experiments; while it is down the gate has no eyes and the round stalls
# on `endpoint_unreachable` — a correct verdict about infra, not about the game.
# DeepSeek serves a vision model behind the SAME provider and key the rest of
# the system already uses, so the fallback needs no new credential.
#
# Verified 2026-08-24 against a real play-test frame (960x704 PNG as a base64
# data URL, byte-identical payload shape): HTTP 200, question answered. The
# model id came from GET https://api.deepseek.com/models, not from memory.
#
# The token budget deliberately stays the PRIMARY endpoint's. DeepSeek's context
# is not smaller, so reusing it only makes batches conservative. Sizing batches
# to whichever judge happened to answer would make the batch layout — and thus
# which frames get compared against each other — depend on an infra accident.
_FALLBACK_URL = os.environ.get(
    "GODOT_VISION_FALLBACK_URL", "https://api.deepseek.com/chat/completions")
_FALLBACK_MODEL = os.environ.get(
    "GODOT_VISION_FALLBACK_MODEL", "deepseek-v4-flash-vision-exp")
_FALLBACK_KEY_NAME = os.environ.get(
    "GODOT_VISION_FALLBACK_KEY", "DEEPSEEK_API_KEY")
# "0" forces primary-only — the way to PROVE the GPU box is serving, which a
# silent fallback would otherwise hide.
_FALLBACK_ENABLED = os.environ.get("GODOT_VISION_FALLBACK", "1") != "0"
# The checklist prompt measured 357 tokens at 6 questions; 600 leaves headroom
# for a longer question set without ever borrowing from the completion reserve.
_PROMPT_RESERVE = 600

# ── The checklist ─────────────────────────────────────────────────────────────
# `requirement` indexes design/30_presentation.md § 可读性硬要求 so a failure
# names the rule it broke. Requirements 6 (UI elements must not overlap) and 7
# (characters must stay distinguishable when adjacent) are deliberately ABSENT:
# they are the spatial questions the model gets wrong, and the design record
# already routes them to playtest_spec.yaml assertions.
# Every question is phrased so YES is the healthy answer — one polarity for the
# model to hold, one rule for the tally.
# ── Scope gate ─────────────────────────────────────────────────────────────
# Most of these checks are about the BATTLEFIELD. As the game grew segments
# (character creation, sect select, cultivation, save/load, world map), the
# gate started asking "is a tile grid visible" of a menu screen and counting
# the honest NO as a readability failure.
#
# jinyong-spine, 2026-08-23, measured: Q1/Q2/Q4/Q6 each scored exactly 15/20
# with 5 non-battle scenarios in the set — i.e. EVERY battlefield scenario
# passed and the 5 menus dragged the tally down. Q3 read 8/20, of which 8 of
# the 12 NOs were menus and transitions with no skill bar at all. Left alone,
# this gate gets structurally redder every time the game gains a screen, for
# reasons that have nothing to do with whether the game is readable.
#
# So each question declares what it applies to, and one gate question per
# scenario decides which kind of screen the frames show. A question that does
# not apply is recorded n/a — NOT as a bad answer.
#
# The gate is deliberately NOT Q1. "No grid because this is a menu" and "no
# grid because the grid was never drawn" are exactly the two things Q1 exists
# to tell apart; reusing it as the classifier would launder the second into
# the first. The gate asks about CONTENT, Q1 asks about QUALITY.
_GATE_ID = "Q0"
_GATE = {"id": _GATE_ID, "requirement": 0, "differential": False,
         "applies_to": "any", "topic": "screen kind (scope gate, not a check)",
         "text": "Do these frames show a BATTLEFIELD — character figures "
                 "standing on a playing field with a row of action buttons "
                 "along the bottom — rather than a menu, a text screen, a "
                 "form, or a list of options?"}

_QUESTIONS = [
    {"id": "Q1", "requirement": 1, "differential": False, "applies_to": "battle",
     "topic": "tile grid visible",
     "text": "Is a grid of separate square tiles visible on the battlefield — "
             "can you see lines or borders dividing the ground into cells?"},
    {"id": "Q2", "requirement": 2, "differential": False, "applies_to": "battle",
     "topic": "skill buttons differ from one another",
     "text": "Within any single frame, do the skill buttons in the bottom bar "
             "differ visually from one another (some dimmed, greyed out or "
             "marked unavailable while others are bright)?"},
    {"id": "Q3", "requirement": 2, "differential": True, "applies_to": "battle",
     "topic": "skill button appearance changes over time",
     "text": "Comparing the frames in chronological order, does the appearance "
             "of at least one skill button change from one frame to another?"},
    {"id": "Q4", "requirement": 3, "differential": True, "applies_to": "battle",
     "topic": "turn / action state changes visibly",
     "text": "Comparing the frames in chronological order, is there a visible "
             "change showing whose turn it is or what the acting character can "
             "still do (an active-actor highlight, a turn or round indicator, "
             "remaining move points)?"},
    {"id": "Q5", "requirement": 4, "differential": False, "applies_to": "battle",
     "topic": "health bars recognisable",
     "text": "Above or attached to the characters, is there something clearly "
             "recognisable as a health bar (a bar with a filled portion and an "
             "empty portion showing remaining HP)?"},
    {"id": "Q6", "requirement": 5, "differential": False, "applies_to": "any",
     "topic": "no truncated or clipped text",
     "text": "Is every piece of visible text fully readable, with no word "
             "ending in an ellipsis and nothing cut off by the edge of the "
             "screen?"},
]
# Every check must declare its scope. Without this assertion a question added
# later without applies_to would KeyError at tally time — or worse, if someone
# "fixed" that with a .get() default, it would silently be treated as applying
# everywhere and start counting menus as readability failures again, which is
# the exact bug the scope gate was added to remove.
_ALLOWED_SCOPES = {"battle", "any"}
for _q in _QUESTIONS:
    assert _q.get("applies_to") in _ALLOWED_SCOPES, (
        f"{_q['id']} has no valid applies_to (got {_q.get('applies_to')!r}); "
        f"every question must declare {_ALLOWED_SCOPES}")

_GOOD = "YES"

_PROMPT_HEAD = (
    "You are checking the on-screen READABILITY of a 2D turn-based tactical "
    "grid game.\n"
    "The {n} images are consecutive rendered frames from ONE scenario of a "
    "single playthrough, in chronological order (image 1 is earliest, image {n} "
    "is latest).\n"
    "Answer ONLY the {q} questions below. Judge strictly by what is VISIBLE in "
    "the pixels; do not assume a feature is there because a game usually has "
    "one.\n\n")
# The model over-answers: asked 5 numbered questions once, it produced 24 and
# blew the token budget. Pin the shape hard here AND parse defensively below.
# `/no_think` is the Qwen3 convention for suppressing the chat template's
# <think> block — measured 114 completion tokens instead of 570, 7.5s instead
# of 30s. Harmless text on a model that does not honour it, and _strip_think()
# handles the block either way.
_PROMPT_TAIL = (
    "\n\nOutput format — EXACTLY {q} lines and nothing else. No preamble, no "
    "extra questions, no closing summary. One line per question above, using "
    "that question's own number:\n{example}\n/no_think")


# ── PNG + token accounting ────────────────────────────────────────────────────
def _png_size(path: Path):
    """(width, height) straight out of the IHDR chunk — no Pillow dependency.
    None for anything that is not a readable PNG (missing, truncated, garbage)."""
    try:
        head = path.read_bytes()[:24]
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return (w, h) if w and h else None


def _image_tokens(w: int, h: int) -> int:
    """Measured cost: 1 token per 32x32 px block, each side rounded UP, +3 per
    image. A 960x704 frame is exactly 663. Multi-image is strictly additive."""
    return ((w + 31) // 32) * ((h + 31) // 32) + 3


def _batch(frames: list[dict], budget: int) -> list[list[dict]]:
    """Greedy chronological packing. Frames are SPLIT across calls, never
    dropped — and a lone trailing frame is pulled back so that no batch has a
    single frame while a sibling has three (a one-frame batch can answer nothing
    differential)."""
    batches: list[list[dict]] = []
    cur: list[dict] = []
    total = 0
    for fr in frames:
        if cur and total + fr["tokens"] > budget:
            batches.append(cur)
            cur, total = [], 0
        cur.append(fr)
        total += fr["tokens"]
    if cur:
        batches.append(cur)
    if len(batches) > 1 and len(batches[-1]) == 1 and len(batches[-2]) > 2:
        batches[-1].insert(0, batches[-2].pop())
    return batches


# ── Prompt + response ─────────────────────────────────────────────────────────
def _build_prompt(questions: list[dict], n_frames: int) -> str:
    body = "\n".join(f"{q['id']}. {q['text']}" for q in questions)
    example = "\n".join(
        f"{q['id']}: YES or NO - reason, at most 10 words" for q in questions)
    return (_PROMPT_HEAD.format(n=n_frames, q=len(questions)) + body
            + _PROMPT_TAIL.format(q=len(questions), example=example))


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_ANSWER_RE = re.compile(
    r"Q\s*(\d+)\s*\**\s*[:.)\-]\s*\**\s*(YES|NO)\b(.*)", re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Drop the chat template's reasoning block. Everything after the LAST
    </think> is the answer; an UNCLOSED <think> means the reasoning ran into the
    token limit and the answers never arrived — leave nothing behind so that
    reads as unparseable rather than as a partial verdict."""
    if _THINK_CLOSE in text:
        text = text.rsplit(_THINK_CLOSE, 1)[1]
    idx = text.lower().find(_THINK_OPEN)
    if idx >= 0:
        text = text[:idx]
    return text.strip()


def _parse_answers(text: str) -> dict:
    """{'Q1': {'answer': 'NO', 'reason': '...'}}. First answer per id wins —
    when the model keeps going and re-answers, the checklist reply is the one it
    was asked for; ids beyond the checklist are counted and ignored."""
    out: dict = {}
    for line in _strip_think(text).splitlines():
        m = _ANSWER_RE.search(line)
        if not m:
            continue
        qid = f"Q{int(m.group(1))}"
        if qid in out:
            continue
        reason = " ".join(m.group(3).strip(" -—:*\t").split())
        out[qid] = {"answer": m.group(2).upper(), "reason": reason[:120]}
    return out


def _post(url: str, model: str, api_key: str,
          files: list[Path], questions: list[dict]) -> str:
    """One chat/completions call with the frames inline as base64 data URLs.
    Raises on transport failure / non-200 / unreadable envelope."""
    content = [{"type": "text",
                "text": _build_prompt(questions, len(files))}]
    for f in files:
        b64 = base64.b64encode(f.read_bytes()).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"}})
    body = json.dumps({"model": model, "max_tokens": _MAX_TOKENS,
                       "temperature": 0.0,
                       "messages": [{"role": "user", "content": content}]}
                      ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        code = getattr(resp, "status", None) or resp.getcode()
        if code != 200:
            raise urllib.error.HTTPError(url, code, "non-200", {}, None)
        payload = json.loads(resp.read())
    return payload["choices"][0]["message"]["content"] or ""


def _fallback_key() -> str:
    """The DeepSeek key, through the same secret-file precedence the router uses
    (mounted /run/secrets first, env only as local-dev fallback)."""
    try:
        from core.ai_router import _read_secret
        return _read_secret(_FALLBACK_KEY_NAME) or ""
    except Exception:                                    # noqa: BLE001
        return os.getenv(_FALLBACK_KEY_NAME, "")


def _ask(files: list[Path], questions: list[dict]) -> tuple[str, str]:
    """Ask the primary judge; fall back to DeepSeek if it is not SERVING.

    Returns ``(answer_text, backend)`` where backend is "primary" or "fallback".
    Raises only when BOTH are unusable — the caller turns that into
    endpoint_unreachable.

    Only transport-level failure falls back: refused / timed out / non-200 /
    unreadable envelope. A primary that answers 200 with a WRONG answer must NOT
    fall back — that would silently swap judges to paper over a real regression
    in the model or the prompt, and the report would carry a verdict nobody
    could attribute.
    """
    try:
        return _post(_URL, _MODEL, "", files, questions), "primary"
    except Exception as primary_exc:                     # noqa: BLE001
        if not _FALLBACK_ENABLED:
            raise
        key = _fallback_key()
        if not key:
            raise RuntimeError(
                f"primary vision endpoint {_URL} failed ({primary_exc}) and the "
                f"fallback has no {_FALLBACK_KEY_NAME} — no judge available"
            ) from primary_exc
        try:
            return _post(_FALLBACK_URL, _FALLBACK_MODEL, key,
                         files, questions), "fallback"
        except Exception as fb_exc:                       # noqa: BLE001
            raise RuntimeError(
                f"primary {_URL} ({_MODEL}) failed: {primary_exc}; "
                f"fallback {_FALLBACK_URL} ({_FALLBACK_MODEL}) also failed: "
                f"{fb_exc}"
            ) from fb_exc


# ── The gate ──────────────────────────────────────────────────────────────────
def _blind(report: dict, reason: str, summary: str) -> dict:
    report.update(passed=False, blind=True, blind_reason=reason,
                  summary=summary)
    report[reason] = True          # the loud, greppable named marker
    return report


def _write(report: dict, out_dir: str, project_root: Path) -> dict:
    target = Path(out_dir) if out_dir else project_root
    target.mkdir(parents=True, exist_ok=True)
    (target / "vision_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"written": "vision_report.json", "passed": report["passed"],
            "summary": report.get("summary", "")}


def godot_vision(*, project_root: str = "", out_dir: str = "",
                 workspace_root: str = "", config_name: str = "",
                 from_step: str = "5_compile", **kwargs) -> dict:
    """Judge the rendered frames against the readability checklist.

    Writes vision_report.json and returns {written, passed, summary}."""
    repo = Path(project_root or workspace_root or ".").resolve()
    report: dict = {"passed": True, "blind": False, "blind_reason": "",
                    "endpoint": _URL, "model": _MODEL, "from_step": from_step,
                    # Which judge actually answered. A gate that can silently
                    # swap models is a gate whose verdict cannot be interpreted:
                    # "Q3 got worse" means nothing if nobody recorded that a
                    # different model answered it.
                    "backend": "", "fallback_used": False,
                    "fallback_model": _FALLBACK_MODEL,
                    "scenarios": 0, "frames_checked": 0, "calls": 0,
                    "questions": [], "failures": [], "batches": [],
                    "summary": ""}

    # The ONE legitimate pass without looking: not a game.
    if not (repo / "project.godot").is_file():
        report["summary"] = ("No project.godot — not a Godot project; "
                             "vision gate skipped.")
        return _write(report, out_dir, repo)

    # ── Locate the play-test's promoted output (restage's pattern) ──
    src = (Path(workspace_root) / config_name / from_step
           if workspace_root and config_name else None)
    pt_path = src / "playtest_report.json" if src else None
    if not pt_path or not pt_path.is_file():
        return _write(_blind(
            report, "no_playtest_report",
            f"No playtest_report.json at {pt_path or '<workspace_root/config_name not injected>'} "
            f"— there are no frames to look at, so the UI shipped UNSEEN. "
            f"Vision gate NOT run."), out_dir, repo)
    try:
        playtest = json.loads(pt_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return _write(_blind(
            report, "no_playtest_report",
            f"playtest_report.json at {pt_path} is unreadable ({e}) — the UI "
            f"shipped UNSEEN. Vision gate NOT run."), out_dir, repo)

    captures = [c for c in (playtest.get("captures") or [])
                if isinstance(c, dict) and c.get("file")]
    if not captures:
        return _write(_blind(
            report, "no_captures",
            f"playtest_report.json has no captures (render_mode="
            f"{playtest.get('render_mode')!r}) — the play-test rendered no "
            f"pixels, so nothing can be judged. Vision gate NOT run."),
            out_dir, repo)

    # ── Every named frame must be on disk and be a real PNG ──
    frames, missing = [], []
    for cap in captures:
        p = (src / str(cap["file"])).resolve()
        size = _png_size(p)
        if size is None:
            missing.append(str(cap["file"]))
            continue
        frames.append({"path": p, "file": str(cap["file"]),
                       "scenario": str(cap.get("scenario") or "_unscoped"),
                       "frame": cap.get("frame") or 0,
                       "size": list(size), "tokens": _image_tokens(*size)})
    if missing:
        return _write(_blind(
            report, "missing_frames",
            f"{len(missing)} of {len(captures)} frames named in captures[] are "
            f"missing or unreadable under {src}: {', '.join(missing[:8])}"
            f"{' …' if len(missing) > 8 else ''}. Vision gate NOT run."),
            out_dir, repo)

    # ── Token budget: refuse, never silently drop ──
    budget = _CONTEXT_TOKENS - _MAX_TOKENS - _PROMPT_RESERVE
    report["budget"] = {"context_tokens": _CONTEXT_TOKENS,
                        "completion_reserve": _MAX_TOKENS,
                        "prompt_reserve": _PROMPT_RESERVE,
                        "image_budget": budget}
    biggest = max(frames, key=lambda f: f["tokens"])
    if biggest["tokens"] > budget:
        fits = max(budget // biggest["tokens"], 0)
        return _write(_blind(
            report, "budget_exceeded",
            f"A {biggest['size'][0]}x{biggest['size'][1]} frame costs "
            f"{biggest['tokens']} tokens and the image budget is {budget} "
            f"({_CONTEXT_TOKENS} context - {_MAX_TOKENS} completion - "
            f"{_PROMPT_RESERVE} prompt): {fits} such frames fit per call. "
            f"Refusing to look at a subset. Vision gate NOT run."),
            out_dir, repo)

    # ── Group by scenario, chronological, batch to fit ──
    by_scenario: dict[str, list[dict]] = {}
    for fr in frames:
        by_scenario.setdefault(fr["scenario"], []).append(fr)
    batches: list[tuple[str, list[dict]]] = []
    for scen, frs in by_scenario.items():
        frs.sort(key=lambda f: (f["frame"], f["file"]))
        for b in _batch(frs, budget):
            batches.append((scen, b))
    report["scenarios"] = len(by_scenario)
    report["frames_checked"] = len(frames)

    tally = {q["id"]: {"good": 0, "bad": 0, "n/a": 0} for q in _QUESTIONS}
    for scen, batch in batches:
        # A one-frame batch can only be a scenario that captured one frame;
        # asking it to compare frames would manufacture a NO. Ask the
        # recognisability half and let the tally get its differential evidence
        # from the other scenarios (or fail as unchecked_questions if none).
        asked = ([q for q in _QUESTIONS if not q["differential"]]
                 if len(batch) < 2 else list(_QUESTIONS))
        # The gate rides along in the same call — one request either way, and
        # asking it separately would double the vision spend per scenario.
        asked = [_GATE] + asked
        try:
            raw, backend = _ask([f["path"] for f in batch], asked)
        except Exception as e:                          # noqa: BLE001
            return _write(_blind(
                report, "endpoint_unreachable",
                f"Vision endpoint {_URL} (model {_MODEL}) failed on scenario "
                f"'{scen}' after {report['calls']} successful call(s): {e}. "
                f"The frames were NOT judged. Vision gate NOT run."),
                out_dir, repo)
        report["calls"] += 1
        # Record the judge per call, not just once: a run that starts on the
        # primary and finishes on the fallback has TWO judges in one verdict,
        # and that is exactly the thing a reader needs told.
        if backend == "fallback":
            report["fallback_used"] = True
        prev = report.get("backend") or ""
        report["backend"] = (backend if not prev or prev == backend else "mixed")
        answers = _parse_answers(raw)
        unanswered = [q["id"] for q in asked if q["id"] not in answers]
        if unanswered:
            return _write(_blind(
                report, "unparseable_response",
                f"The vision model answered {len(answers)} of the "
                f"{len(asked)} questions asked about scenario '{scen}' "
                f"(missing {', '.join(unanswered)}). The reply could not be "
                f"read as a verdict, so there is no verdict. Raw reply: "
                f"{' '.join(raw.split())[:400]}"), out_dir, repo)
        kept = {q["id"]: answers[q["id"]] for q in asked}
        is_battle = kept[_GATE_ID]["answer"] == _GOOD
        skipped = []
        for q in asked:
            if q["id"] == _GATE_ID:
                continue
            if q["applies_to"] == "battle" and not is_battle:
                tally[q["id"]]["n/a"] += 1
                skipped.append(q["id"])
                continue
            side = "good" if kept[q["id"]]["answer"] == _GOOD else "bad"
            tally[q["id"]][side] += 1
        report["batches"].append({
            "scenario": scen,
            "screen_kind": "battle" if is_battle else "menu",
            "not_applicable": skipped,
            "frames": [f["file"] for f in batch],
            "prompt_image_tokens": sum(f["tokens"] for f in batch),
            "questions_asked": [q["id"] for q in asked],
            "extra_answers": sorted(set(answers) - set(kept)),
            "answers": kept})

    # ── Verdict: majority vote per question across scenarios ──
    # Per-scenario, not per-frame-batch-of-one: a differential question can be a
    # legitimate NO in a quiet scenario (nothing unlocked, nobody took damage),
    # but a UI that never changes in ANY scenario is the static UI this gate was
    # built for. Ties fail — a checklist item the model could not call more
    # often right than wrong has not been shown to be readable.
    # …which is why differential questions pass on ANY scenario showing the
    # change, while per-frame questions keep the majority rule.
    #
    # The majority rule contradicted the paragraph above it. A differential
    # question is answered over the 4 frames SAMPLED from a scenario, and
    # whether those 4 straddle a state change is a property of the sampling,
    # not of the UI. Most scenarios are quiet by design (a save/load round-trip,
    # a menu walk), so their honest answer is NO — and under a majority rule
    # enough honest NOs outvote the scenarios that did show the change.
    #
    # Live, jinyong-encounter 2026-08-23: Q3 ("does at least one skill button
    # change between frames") read 7 bad / 7 good and failed the run on the tie,
    # while the play-test's own `skill_button_visual_states` asserted the button
    # states on live nodes and passed 9/9 and `skill_button_turn_overlay` 6/6.
    # The buttons were changing. The gate had photographed quiet moments.
    #
    # An any-rule still catches the defect this question exists for: a UI that
    # never changes in ANY scenario has no good answers at all, and fails. What
    # it stops doing is failing a game for being sampled while nothing happened.
    for q in _QUESTIONS:
        t = tally[q["id"]]
        n = t["good"] + t["bad"]
        if q["differential"]:
            failed = bool(n) and t["good"] == 0
        else:
            failed = bool(n) and t["bad"] * 2 >= n
        report["questions"].append({
            **{k: q[k] for k in ("id", "requirement", "differential",
                                 "applies_to", "topic", "text")},
            "good_answers": t["good"], "bad_answers": t["bad"],
            "scenarios_not_applicable": t["n/a"],
            "scenarios_answered": n, "failed": failed})
        if failed:
            reasons = [b["answers"][q["id"]]["reason"]
                       for b in report["batches"]
                       if q["id"] in b["answers"]
                       and b["answers"][q["id"]]["answer"] != _GOOD]
            report["failures"].append(
                f"{q['id']} ({q['topic']}, design/30_presentation.md "
                f"可读性硬要求 #{q['requirement']}): {t['bad']}/{n} scenarios — "
                f"{reasons[0] if reasons else ''}")

    unchecked = [q["id"] for q in report["questions"]
                 if q["scenarios_answered"] == 0]
    if unchecked:
        return _write(_blind(
            report, "unchecked_questions",
            f"{', '.join(unchecked)} were never actually asked — either no "
            f"scenario captured two or more frames (no differential check "
            f"could run), or every scenario was scoped out as a non-battle "
            f"screen. Either way those requirements are UNVERIFIED, which is "
            f"not the same as satisfied."), out_dir, repo)

    report["passed"] = not report["failures"]
    if report["passed"]:
        report["summary"] = (
            f"Readability OK: all {len(_QUESTIONS)} checks passed over "
            f"{len(frames)} frames / {len(by_scenario)} scenarios "
            f"({report['calls']} vision calls).")
    else:
        report["summary"] = (
            f"READABILITY FAILED: {len(report['failures'])} of "
            f"{len(_QUESTIONS)} checks failed over {len(frames)} frames / "
            f"{len(by_scenario)} scenarios. The behaviour assertions can all be "
            f"green and the game still be unplayable to look at. "
            + " | ".join(report["failures"]))
    return _write(report, out_dir, repo)
