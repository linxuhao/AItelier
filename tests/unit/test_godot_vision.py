"""A vision gate that cannot see must fail, not pass.

Same lineage as tests/unit/test_blind_gate.py: godot_compile's `gate_skipped`
(infra failed → loud) and `blind_builder` (the checker cannot see the thing it
checks → hard fail). godot_vision is the second kind throughout, because it
exists to catch precisely this defect one level up — run jinyong-turn passed
`two_phase_skill_unlock_and_hp_gate` 20/20 on `SkillButton5..8.disabled` while
the rendered frames showed eight pixel-identical buttons, no tile grid, and five
ellipsised names. A readability gate that reports green without looking would
reproduce that failure inside the fix for it.

No live model anywhere: the endpoint is patched at urllib, so these run on any
box (the GPU host is not a test dependency). The PNGs are header-only stubs —
the tool reads IHDR for the token cost and never decodes the pixels.
"""

import json
import struct
import urllib.error
import urllib.request

import pytest

from aitelier.tools.godot_vision import impl as vision_impl
from aitelier.tools.godot_vision.impl import _GATE, _GATE_ID, _QUESTIONS, godot_vision

# What the tool actually puts in one request: the scope gate rides along with
# the checks, so a stand-in reply that answers only _QUESTIONS is short by one
# and every call comes back `unparseable_response`. Derived, never restated —
# this file listed the checks by hand and silently stopped testing the gate the
# day the gate was added.
_ASKED = [_GATE] + _QUESTIONS
# A scenario that captured one frame is asked the gate plus the non-differential
# checks; nothing can be compared across a single frame.
_STATIC = [_GATE_ID] + [q["id"] for q in _QUESTIONS if not q["differential"]]


def _png(w=960, h=704):
    """Just enough PNG for _png_size: signature + IHDR width/height."""
    return (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00")


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project.godot").write_text("config_version=5\n")
    return repo


def _workspace(tmp_path, scenarios=(("scen_a", 4),), size=(960, 704),
               write_frames=True, render_mode="render"):
    step = tmp_path / "ws" / "cfg" / "5_compile"
    (step / "frames").mkdir(parents=True)
    caps = []
    for si, (name, n) in enumerate(scenarios):
        for i in range(n):
            rel = f"frames/s{si}_frame_{i:04d}.png"
            if write_frames:
                (step / rel).write_bytes(_png(*size))
            caps.append({"frame": i * 10, "file": rel, "scenario": name})
    (step / "playtest_report.json").write_text(json.dumps(
        {"passed": True, "render_mode": render_mode, "captures": caps}))
    return tmp_path / "ws"


def _run(tmp_path, **over):
    """Defaults are built lazily — a test that made its own repo/workspace must
    not have a second one scaffolded over the top of it."""
    kw = dict(config_name="cfg", from_step="5_compile",
              out_dir=str(tmp_path / "out"))
    kw.update(over)
    kw.setdefault("project_root", None)
    kw.setdefault("workspace_root", None)
    if kw["project_root"] is None:
        kw["project_root"] = str(_repo(tmp_path))
    if kw["workspace_root"] is None:
        kw["workspace_root"] = str(_workspace(tmp_path))
    godot_vision(**kw)
    return json.loads((tmp_path / "out" / "vision_report.json").read_text())


def _reply(text, status=200):
    class _Resp:
        def __init__(self):
            self.status = status
            self._b = json.dumps(
                {"choices": [{"message": {"content": text}}]}).encode()

        def read(self):
            return self._b

        def getcode(self):
            return status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


def _sheet(**overrides):
    """A well-formed checklist reply; every answer defaults to the healthy YES.

    Includes the scope gate. YES there means "these frames are a battlefield",
    which is what keeps the battle-scoped checks in play — answer it NO and the
    tool correctly records them n/a instead of good or bad.
    """
    return "\n".join(f"{q['id']}: {overrides.get(q['id'], 'YES')} - a reason"
                     for q in _ASKED)


def _serve(monkeypatch, text, status=200, calls=None):
    def _fake(req, timeout=0):
        if calls is not None:
            calls.append(req)
        body = text(len(calls)) if callable(text) else text
        return _reply(body, status)

    monkeypatch.setattr(urllib.request, "urlopen", _fake)


def _never_called(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("the vision endpoint must not be called here")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)


def _assert_blind(rep, reason):
    """One shape for every way of not seeing: fail, name it, say so out loud."""
    assert rep["passed"] is False
    assert rep["blind"] is True
    assert rep["blind_reason"] == reason
    assert rep[reason] is True          # the greppable named marker
    assert rep["summary"]


# ── The one legitimate pass without looking ──────────────────────────────────
def test_a_non_game_project_is_a_noop(tmp_path, monkeypatch):
    _never_called(monkeypatch)
    (tmp_path / "app.py").write_text("print(1)\n")
    rep = _run(tmp_path, project_root=str(tmp_path))
    assert rep["passed"] is True
    assert rep["blind"] is False
    assert "not a Godot project" in rep["summary"]


# ── Blind branches ───────────────────────────────────────────────────────────
def test_a_missing_playtest_report_fails_loudly(tmp_path, monkeypatch):
    _never_called(monkeypatch)
    ws = _workspace(tmp_path)
    (ws / "cfg" / "5_compile" / "playtest_report.json").unlink()
    rep = _run(tmp_path, workspace_root=str(ws))
    _assert_blind(rep, "no_playtest_report")
    assert "UNSEEN" in rep["summary"]


def test_an_uninjected_workspace_fails_loudly(tmp_path, monkeypatch):
    # workspace_root/config_name not injected → the step dir cannot be located
    # at all. Same verdict: no frames were looked at.
    _never_called(monkeypatch)
    rep = _run(tmp_path, workspace_root="", config_name="")
    _assert_blind(rep, "no_playtest_report")


def test_an_unreadable_playtest_report_fails_loudly(tmp_path, monkeypatch):
    _never_called(monkeypatch)
    ws = _workspace(tmp_path)
    (ws / "cfg" / "5_compile" / "playtest_report.json").write_text("{ not json")
    rep = _run(tmp_path, workspace_root=str(ws))
    _assert_blind(rep, "no_playtest_report")


def test_zero_captures_fails_loudly(tmp_path, monkeypatch):
    # render_mode "headless" — the play-test fell back to the pixel-less dummy
    # renderer and captures[] is empty. Nothing to judge is not "nothing wrong".
    _never_called(monkeypatch)
    ws = _workspace(tmp_path, scenarios=(), render_mode="headless")
    rep = _run(tmp_path, workspace_root=str(ws))
    _assert_blind(rep, "no_captures")
    assert "headless" in rep["summary"]


def test_frames_named_but_absent_from_disk_fail_loudly(tmp_path, monkeypatch):
    _never_called(monkeypatch)
    ws = _workspace(tmp_path, write_frames=False)
    rep = _run(tmp_path, workspace_root=str(ws))
    _assert_blind(rep, "missing_frames")
    assert "4 of 4" in rep["summary"]


def test_a_truncated_png_counts_as_a_missing_frame(tmp_path, monkeypatch):
    _never_called(monkeypatch)
    ws = _workspace(tmp_path)
    (ws / "cfg" / "5_compile" / "frames" / "s0_frame_0002.png").write_bytes(b"\x89PNG")
    rep = _run(tmp_path, workspace_root=str(ws))
    _assert_blind(rep, "missing_frames")
    assert "1 of 4" in rep["summary"]


def test_an_unreachable_endpoint_fails_loudly(tmp_path, monkeypatch):
    def _down(req, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _down)
    rep = _run(tmp_path)
    _assert_blind(rep, "endpoint_unreachable")
    assert "NOT judged" in rep["summary"]


def test_a_timeout_fails_loudly(tmp_path, monkeypatch):
    def _slow(req, timeout=0):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _slow)
    _assert_blind(_run(tmp_path), "endpoint_unreachable")


def test_a_non_200_answer_fails_loudly(tmp_path, monkeypatch):
    _serve(monkeypatch, _sheet(), status=503)
    _assert_blind(_run(tmp_path), "endpoint_unreachable")


def test_an_unparseable_reply_fails_loudly(tmp_path, monkeypatch):
    _serve(monkeypatch, "The game looks great to me!")
    rep = _run(tmp_path)
    _assert_blind(rep, "unparseable_response")
    assert f"answered 0 of the {len(_ASKED)}" in rep["summary"]


def test_a_reply_answering_fewer_questions_than_asked_fails_loudly(tmp_path, monkeypatch):
    _serve(monkeypatch, "Q1: YES - fine\nQ2: YES - fine")
    rep = _run(tmp_path)
    _assert_blind(rep, "unparseable_response")
    assert "Q3" in rep["summary"] and "Q6" in rep["summary"]


def test_a_reasoning_block_that_never_closed_is_unparseable(tmp_path, monkeypatch):
    # max_tokens ran out inside <think>: the answers never arrived, and the
    # reasoning text must not be mined for a verdict.
    _serve(monkeypatch, "<think>Q1 looks like YES, Q2 is probably YES too, and")
    _assert_blind(_run(tmp_path), "unparseable_response")


def test_a_frame_too_big_for_the_context_is_refused_not_trimmed(tmp_path, monkeypatch):
    _never_called(monkeypatch)
    monkeypatch.setattr(vision_impl, "_CONTEXT_TOKENS", 3148)   # budget 500
    ws = _workspace(tmp_path, size=(960, 704))                  # 663 per frame
    rep = _run(tmp_path, workspace_root=str(ws))
    _assert_blind(rep, "budget_exceeded")
    assert "0 such frames fit" in rep["summary"]
    assert "Refusing to look at a subset" in rep["summary"]


def test_a_question_no_scenario_could_answer_fails_loudly(tmp_path, monkeypatch):
    # Every scenario captured a single frame, so the differential questions were
    # never asked of anything. Their requirements are UNVERIFIED — not passed.
    _serve(monkeypatch, _sheet())
    ws = _workspace(tmp_path, scenarios=(("a", 1), ("b", 1), ("c", 1)))
    rep = _run(tmp_path, workspace_root=str(ws))
    _assert_blind(rep, "unchecked_questions")
    assert "Q3, Q4" in rep["summary"]
    assert [b["questions_asked"] for b in rep["batches"]] == [_STATIC] * 3


# ── Verdicts ─────────────────────────────────────────────────────────────────
def test_a_readable_game_passes(tmp_path, monkeypatch):
    _serve(monkeypatch, _sheet())
    rep = _run(tmp_path)
    assert rep["passed"] is True
    assert rep["blind"] is False
    assert rep["failures"] == []
    assert rep["frames_checked"] == 4 and rep["calls"] == 1


def test_the_measured_defect_fails(tmp_path, monkeypatch):
    # jinyong-turn, verbatim: no grid, eight identical buttons, no recognisable
    # health bar, ellipsised names — and a turn indicator that does change.
    _serve(monkeypatch, _sheet(Q1="NO", Q2="NO", Q3="NO", Q5="NO", Q6="NO"))
    rep = _run(tmp_path)
    assert rep["passed"] is False
    assert rep["blind"] is False          # it saw fine; the GAME is unreadable
    assert {f.split()[0] for f in rep["failures"]} == {"Q1", "Q2", "Q3", "Q5", "Q6"}
    assert "可读性硬要求 #1" in rep["failures"][0]
    assert "READABILITY FAILED" in rep["summary"]


def test_one_dissenting_scenario_does_not_flip_the_verdict(tmp_path, monkeypatch):
    # Measured on the real frames: 8 of 9 scenarios called the skill buttons
    # identical and one called them distinct. A single outlier must not clear a
    # requirement, and a single outlier must not condemn one either.
    calls = []
    _serve(monkeypatch,
           lambda n: _sheet(Q1="YES" if n == 1 else "NO",
                            Q2="NO" if n == 1 else "YES"),
           calls=calls)
    ws = _workspace(tmp_path, scenarios=(("a", 2), ("b", 2), ("c", 2)))
    rep = _run(tmp_path, workspace_root=str(ws))
    verdict = {q["id"]: q["failed"] for q in rep["questions"]}
    assert verdict["Q1"] is True and verdict["Q2"] is False
    assert len(calls) == 3


@pytest.mark.parametrize("bad,failed", [(1, False), (2, True), (3, True)])
def test_a_tie_counts_against_the_game(tmp_path, monkeypatch, bad, failed):
    # 2 of 4 scenarios calling a requirement broken is not a clean bill of
    # health: a check the model cannot call right more often than wrong has not
    # shown the game to be readable.
    _serve(monkeypatch, lambda n: _sheet(Q1="NO" if n <= bad else "YES"),
           calls=[])
    ws = _workspace(tmp_path, scenarios=tuple((c, 2) for c in "abcd"))
    rep = _run(tmp_path, workspace_root=str(ws))
    assert {q["id"]: q["failed"] for q in rep["questions"]}["Q1"] is failed


def test_a_menu_screen_is_scoped_out_rather_than_counted_against_the_game(
        tmp_path, monkeypatch):
    """A battle check asked about a menu is n/a, not a failure.

    The gate exists because a run went 12 NO of 21 on the skill-button checks
    and every one of the NOs was a menu or a transition with no skill bar in it
    — a gate that gets structurally redder each time the game grows a screen,
    for reasons that have nothing to do with readability. Scenario 'a' is a
    battlefield and 'b' is a menu: 'b' must contribute neither a good answer
    nor a bad one to the battle-scoped checks, while the unscoped Q6 is still
    answered by both.
    """
    calls = []
    _serve(monkeypatch, lambda n: _sheet(**({} if n == 1 else {_GATE_ID: "NO"})),
           calls=calls)
    ws = _workspace(tmp_path, scenarios=(("a", 2), ("b", 2)))
    rep = _run(tmp_path, workspace_root=str(ws))

    assert [b["screen_kind"] for b in rep["batches"]] == ["battle", "menu"]
    assert rep["batches"][0]["not_applicable"] == []
    assert rep["batches"][1]["not_applicable"] == [
        q["id"] for q in _QUESTIONS if q["applies_to"] == "battle"]

    by_id = {q["id"]: q for q in rep["questions"]}
    assert by_id["Q1"]["scenarios_answered"] == 1        # the battle one only
    assert by_id["Q1"]["scenarios_not_applicable"] == 1
    assert by_id["Q6"]["scenarios_answered"] == 2        # applies_to "any"
    assert rep["passed"] is True and rep["blind"] is False


def test_a_menu_that_answers_no_is_not_laundered_into_a_pass(tmp_path,
                                                             monkeypatch):
    """Scoping out must not be able to hide a real failure.

    If EVERY scenario is a menu, the battle requirements were not satisfied —
    they were never looked at, and the tool must say so rather than report a
    clean sheet over the checks that happened to survive.
    """
    _serve(monkeypatch, _sheet(**{_GATE_ID: "NO"}))
    ws = _workspace(tmp_path, scenarios=(("a", 2), ("b", 2)))
    rep = _run(tmp_path, workspace_root=str(ws))

    _assert_blind(rep, "unchecked_questions")
    assert "scoped out as a non-battle screen" in rep["summary"]


# ── Batching ─────────────────────────────────────────────────────────────────
def test_frames_are_split_across_calls_never_dropped(tmp_path, monkeypatch):
    calls = []
    _serve(monkeypatch, _sheet(), calls=calls)
    monkeypatch.setattr(vision_impl, "_CONTEXT_TOKENS", 4048)   # budget 1400
    ws = _workspace(tmp_path, scenarios=(("a", 4),))            # 663 each → 2+2
    rep = _run(tmp_path, workspace_root=str(ws))
    assert len(calls) == 2
    sent = [f for b in rep["batches"] for f in b["frames"]]
    assert sent == [f"frames/s0_frame_{i:04d}.png" for i in range(4)]
    assert all(len(b["frames"]) == 2 for b in rep["batches"])
    assert rep["passed"] is True


def test_a_lone_trailing_frame_is_pulled_back_into_a_comparable_batch(tmp_path, monkeypatch):
    # 7 frames at a 3-frame budget pack as 3+3+1, and a batch of one can answer
    # nothing differential; rebalance to 3+2+2. Still 7 frames, none dropped.
    calls = []
    _serve(monkeypatch, _sheet(), calls=calls)
    monkeypatch.setattr(vision_impl, "_CONTEXT_TOKENS", 4700)   # budget 2052 → 3
    ws = _workspace(tmp_path, scenarios=(("a", 7),))
    rep = _run(tmp_path, workspace_root=str(ws))
    assert [len(b["frames"]) for b in rep["batches"]] == [3, 2, 2]
    assert len(calls) == 3
    assert rep["frames_checked"] == 7


def test_each_scenario_is_judged_on_its_own_frames(tmp_path, monkeypatch):
    calls = []
    _serve(monkeypatch, _sheet(), calls=calls)
    ws = _workspace(tmp_path, scenarios=(("a", 2), ("b", 3)))
    rep = _run(tmp_path, workspace_root=str(ws))
    assert len(calls) == 2
    assert [(b["scenario"], len(b["frames"])) for b in rep["batches"]] == [
        ("a", 2), ("b", 3)]
    assert rep["scenarios"] == 2 and rep["frames_checked"] == 5


# ── Parsing ──────────────────────────────────────────────────────────────────
def test_the_think_block_and_the_over_answering_are_parsed_through(tmp_path, monkeypatch):
    # Measured: the chat template injects <think>, and asked 5 numbered
    # questions the model once produced 24. The reasoning must not be read as
    # answers, and the first answer per id is the one that was asked for.
    noise = ("<think>\nQ1: I think NO here, let me reconsider.\n</think>\n"
             + _sheet(Q6="NO")
             + "\nQ7: YES - invented\nQ8: NO - invented\nQ1: NO - restated")
    _serve(monkeypatch, noise)
    rep = _run(tmp_path)
    answers = rep["batches"][0]["answers"]
    assert answers["Q1"]["answer"] == "YES"        # the think block is not a vote
    assert answers["Q6"]["answer"] == "NO"
    assert rep["batches"][0]["extra_answers"] == ["Q7", "Q8"]  # counted, ignored
    assert [q["id"] for q in rep["questions"] if q["failed"]] == ["Q6"]


def test_markdown_and_loose_punctuation_still_parse(tmp_path, monkeypatch):
    _serve(monkeypatch, "\n".join(
        f"- **{q['id']}**: {'no' if q['id'] == 'Q5' else 'Yes'} — reason"
        for q in _ASKED))
    rep = _run(tmp_path)
    assert [q["id"] for q in rep["questions"] if q["failed"]] == ["Q5"]
