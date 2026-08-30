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


class _FakeGateway:
    """Stand-in for AIGateway at the seam the tool now uses.

    The gate tests are about the GATE — parsing, batching, the tally, every way
    of going blind. They used to reach that by faking `urllib.request.urlopen`,
    because the tool carried its own HTTP. It does not any more, so the seam
    moved up one layer; the tests themselves did not have to change, which is
    the point of there being a seam at all.
    """

    def __init__(self, text, status=200, calls=None):
        self._text = text
        self._status = status
        self._calls = calls
        self.active_model = "local/v"
        self.max_output_tokens = 6144
        self.last_usage = {"served_by": "local/v"}
        self.escalated = 0

    def generate_native(self, messages, **_):
        if self._calls is not None:
            self._calls.append(messages)
        if self._status != 200:
            raise RuntimeError(f"vision endpoint answered {self._status}")
        n = len(self._calls) if self._calls is not None else 1
        body = self._text(n) if callable(self._text) else self._text
        return _turn(body)

    def escalate_output_cap(self):
        self.escalated += 1
        self.max_output_tokens *= 2
        return self.max_output_tokens


def _turn(text, reasoning="", truncated=False):
    from core.ai_router import NativeTurn

    return NativeTurn(text=text, tool_calls=[],
                      reasoning_content=reasoning, truncated=truncated)


def _sheet(**overrides):
    """A well-formed checklist reply; every answer defaults to the healthy YES.

    Includes the scope gate. YES there means "these frames are a battlefield",
    which is what keeps the battle-scoped checks in play — answer it NO and the
    tool correctly records them n/a instead of good or bad.
    """
    return "\n".join(f"{q['id']}: {overrides.get(q['id'], 'YES')} - a reason"
                     for q in _ASKED)


def _serve(monkeypatch, text, status=200, calls=None):
    gw = _FakeGateway(text, status, calls)
    monkeypatch.setattr(vision_impl, "_gateway", lambda: gw)
    return gw


def _never_called(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("the vision endpoint must not be called here")

    monkeypatch.setattr(vision_impl, "_gateway", _boom)


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
    """The gateway raises once every candidate is spent; the gate must go
    blind and NAME it, not pass."""
    def _down(messages, **_):
        raise urllib.error.URLError("connection refused")

    gw = _FakeGateway("")
    gw.generate_native = _down
    monkeypatch.setattr(vision_impl, "_gateway", lambda: gw)
    rep = _run(tmp_path)
    _assert_blind(rep, "endpoint_unreachable")
    assert "NOT judged" in rep["summary"]


def test_a_timeout_fails_loudly(tmp_path, monkeypatch):
    def _slow(messages, **_):
        raise TimeoutError("timed out")

    gw = _FakeGateway("")
    gw.generate_native = _slow
    monkeypatch.setattr(vision_impl, "_gateway", lambda: gw)
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
    # Pin the completion reserve too: the image budget is
    # `_CONTEXT_TOKENS - _MAX_TOKENS - _PROMPT_RESERVE`, so a test that
    # patches only the context is really asserting a batch layout that
    # depends on whatever the primary judge's budget happens to be.
    # Raising that budget on 2026-08-29 (2048 -> 6144, the primary was
    # starving) turned this test's budget NEGATIVE and it failed for a
    # reason that has nothing to do with batching. What it means to pin
    # is the algorithm, so both inputs are now explicit.
    monkeypatch.setattr(vision_impl, "_MAX_TOKENS", 2048)
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
    # Pin the completion reserve too: the image budget is
    # `_CONTEXT_TOKENS - _MAX_TOKENS - _PROMPT_RESERVE`, so a test that
    # patches only the context is really asserting a batch layout that
    # depends on whatever the primary judge's budget happens to be.
    # Raising that budget on 2026-08-29 (2048 -> 6144, the primary was
    # starving) turned this test's budget NEGATIVE and it failed for a
    # reason that has nothing to do with batching. What it means to pin
    # is the algorithm, so both inputs are now explicit.
    monkeypatch.setattr(vision_impl, "_MAX_TOKENS", 2048)
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


# ── Differential questions: any, not majority ────────────────────────────────
def test_a_quiet_scenario_cannot_outvote_a_scenario_that_showed_the_change(
        tmp_path, monkeypatch):
    """Q3/Q4 are answered over the frames SAMPLED from a scenario, and whether
    those frames straddle a state change is a property of the sampling, not of
    the UI. Most scenarios are quiet by design (a save/load round-trip, a menu
    walk), so their honest answer is NO.

    jinyong-encounter 2026-08-23: Q3 read 7 bad / 7 good and failed the run on
    the tie, while the play-test asserted the same button states on live nodes
    and passed `skill_button_visual_states` 9/9. The buttons were changing; the
    gate had photographed quiet moments.
    """
    diff = [q["id"] for q in _QUESTIONS if q["differential"]]
    assert diff, "this test is meaningless without a differential question"

    # Four scenarios: one shows the change, three were sampled while quiet.
    def _per_call(n):
        # _serve appends before it asks, so the call index is 1-based.
        return _sheet(**{q: ("YES" if n == 1 else "NO") for q in diff})

    calls = []
    _serve(monkeypatch, _per_call, calls=calls)
    ws = _workspace(tmp_path, scenarios=(("a", 2), ("b", 2), ("c", 2), ("d", 2)))
    rep = _run(tmp_path, workspace_root=str(ws))

    for q in rep["questions"]:
        if q["id"] in diff:
            assert q["failed"] is False, f"{q['id']} failed on 1 good / 3 bad"
    assert rep["passed"] is True


def test_a_ui_that_never_changes_in_any_scenario_still_fails(tmp_path, monkeypatch):
    """The any-rule must not turn the check off. Zero good answers across every
    scenario IS the static UI this question exists for."""
    diff = [q["id"] for q in _QUESTIONS if q["differential"]]
    _serve(monkeypatch, _sheet(**{q: "NO" for q in diff}))
    ws = _workspace(tmp_path, scenarios=(("a", 2), ("b", 2), ("c", 2)))
    rep = _run(tmp_path, workspace_root=str(ws))

    assert sorted(q["id"] for q in rep["questions"] if q["failed"]) == sorted(diff)
    assert rep["passed"] is False


def test_a_per_frame_question_still_needs_a_majority(tmp_path, monkeypatch):
    """Only differential questions moved. Q2/Q5 judge a single frame, where a
    NO is a fact about the screen and not about when it was photographed."""
    static = [q["id"] for q in _QUESTIONS
              if not q["differential"] and q["applies_to"] == "battle"]
    victim = static[0]

    def _per_call(n):
        return _sheet(**{victim: ("YES" if n == 1 else "NO")})

    calls = []
    _serve(monkeypatch, _per_call, calls=calls)
    ws = _workspace(tmp_path, scenarios=(("a", 2), ("b", 2), ("c", 2)))
    rep = _run(tmp_path, workspace_root=str(ws))

    assert [q["id"] for q in rep["questions"] if q["failed"]] == [victim]
    assert rep["passed"] is False


# ── Fallback judge (primary vLLM down → DeepSeek) ────────────────────────────
# The primary endpoint is a GPU box shared with other experiments, so "the judge
# is offline" is an expected operating state, not an incident.

class TestTheGateStillOwnsItsBudget:
    """What survived the move to AIGateway.

    Resolving the route, walking an ordered panel, retrying a mid-flight break,
    parking a spent endpoint — all of that is the gateway's, tested in
    tests/unit/test_ai_router.py, and testing it again through a fake here
    would only pin a copy of it. What is still THIS tool's: it makes exactly
    one call per batch, it escalates the output cap once when a judge writes no
    verdict, and it refuses to call an unanswered batch a verdict.
    """

    def _gw(self, texts):
        gw = _FakeGateway("")
        seq = list(texts)
        gw.seen = []

        def _gen(messages, **_):
            gw.seen.append(messages)
            return _turn(seq.pop(0) if seq else "")

        gw.generate_native = _gen
        return gw

    def test_one_call_per_batch_no_second_opinion(self):
        """A judge that answers is never asked twice. Re-asking would silently
        swap the verdict's author and paper over a real regression."""
        gw = self._gw(["Q0: YES - fine"])
        text, served_by = vision_impl._ask(gw, [], [{"id": "Q0", "text": "q"}])
        assert text == "Q0: YES - fine"
        assert served_by == "local/v"
        assert len(gw.seen) == 1

    def test_served_by_comes_from_the_usage_not_a_guess(self):
        """The report's attribution must be what actually answered — after a
        failover inside the gateway that is NOT the first candidate."""
        gw = self._gw(["Q0: YES - fine"])
        gw.last_usage = {"served_by": "payg/eyes"}
        assert vision_impl._ask(gw, [], [{"id": "Q0", "text": "q"}])[1] == "payg/eyes"

    def test_a_starved_judge_escalates_and_actually_retries(self):
        """An escalation that only logs is how the planner starved nine times
        in a row while announcing a retry it could never take (a3846df)."""
        gw = self._gw(["", "Q0: YES - fine"])
        text, _ = vision_impl._ask(gw, [], [{"id": "Q0", "text": "q"}])
        assert text == "Q0: YES - fine"
        assert gw.escalated == 1
        assert len(gw.seen) == 2

    def test_a_judge_that_stays_starved_is_not_a_verdict(self):
        """Two empties must raise ReasoningStarved — the TYPE is what sends the
        report at the budget instead of at a GPU box that was serving fine."""
        gw = self._gw(["", ""])
        with pytest.raises(vision_impl.ReasoningStarved):
            vision_impl._ask(gw, [], [{"id": "Q0", "text": "q"}])
        assert gw.escalated == 1

    def test_a_cap_already_at_the_ceiling_still_raises(self):
        """escalate_output_cap() returns None at the ceiling. The retry is then
        impossible, and the gate must say starved rather than loop or pass."""
        gw = self._gw([""])
        gw.escalate_output_cap = lambda: None
        with pytest.raises(vision_impl.ReasoningStarved):
            vision_impl._ask(gw, [], [{"id": "Q0", "text": "q"}])
        assert len(gw.seen) == 1

    def test_every_frame_reaches_the_judge_as_its_own_image(self, tmp_path):
        """Frames are batched, never sampled down: N frames must produce N
        image parts in the one message, plus exactly one text part."""
        files = []
        for i in range(4):
            f = tmp_path / f"f{i}.png"
            f.write_bytes(_png())
            files.append(f)
        gw = self._gw(["Q0: YES - fine"])
        vision_impl._ask(gw, files, [{"id": "Q0", "text": "q"}])
        content = gw.seen[0][0]["content"]
        assert sum(c["type"] == "image_url" for c in content) == 4
        assert sum(c["type"] == "text" for c in content) == 1
        assert all(c["image_url"]["url"].startswith("data:image/png;base64,")
                   for c in content if c["type"] == "image_url")


class TestVisionJudgeResolution:
    """The panel still comes from the route table, and the report still says so."""

    def test_the_panel_comes_from_the_route_not_from_hardcoded_urls(self):
        panel = vision_impl._candidates()
        assert panel, "the vision route must resolve to at least one candidate"
        assert all("/" in c for c in panel), panel

    def test_the_gateway_is_built_on_that_route_with_thinking_off(self, monkeypatch):
        """Thinking off is the whole reason a 71-call gate is minutes not hours,
        and the route name is what keeps the judge visible in model_routes.json."""
        seen = {}

        class _Spy:
            def __init__(self, model, **kw):
                seen["model"] = model
                seen.update(kw)

        import core.ai_router as router
        monkeypatch.setattr(router, "AIGateway", _Spy)
        vision_impl._gateway()
        assert seen["model"] == vision_impl._ROUTE
        assert seen["enable_thinking"] is False
        assert seen["max_output_tokens"] == vision_impl._MAX_TOKENS
        assert seen["temperature"] == 0.0


def test_a_broken_registry_goes_blind_rather_than_crashing(tmp_path, monkeypatch):
    """Building the judge READS CONFIG, and that read used to be unguarded.

    `AIGateway.__init__` resolves the route and `_bind` json.loads
    llm_providers.json with no handler of its own, so a malformed registry
    raised straight out of `godot_vision` and no vision_report.json was written
    at all. The graph routes to the human checkpoint on
    `from_file: vision_report.json, field: blind` — with no file the match
    cannot fire and the run FAILS instead of pausing for a person, which is the
    opposite of this gate's whole contract.
    """
    def _boom():
        raise ValueError("llm_providers.json: Expecting value: line 1 column 1")

    monkeypatch.setattr(vision_impl, "_gateway", _boom)
    rep = _run(tmp_path)
    _assert_blind(rep, "endpoint_unreachable")
    assert "could not be built" in rep["summary"]
    assert "NOT judged" in rep["summary"]


def test_the_gate_reports_the_judge_the_gateway_actually_bound(tmp_path, monkeypatch):
    """Attribution must come from the gateway, not from a list this tool
    resolved separately: a rotating route makes the two disagree, and the
    report would then say `fallback` about a run the primary judge served."""
    gw = _FakeGateway(_sheet())
    gw.active_model = "second/judge"
    gw.last_usage = {"served_by": "second/judge"}
    monkeypatch.setattr(vision_impl, "_gateway", lambda: gw)
    rep = _run(tmp_path)
    assert rep["served_by"] == "second/judge"
    assert rep["backend"] == "primary", (
        "the endpoint the gateway bound IS the primary for this run")
    assert rep["fallback_used"] is False
