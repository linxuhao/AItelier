"""The script stage is bounded by the PROGRESS a script reports, not the clock.

THE DEFECT THESE TESTS PIN (measured 2026-11-07, one unchanged tree, same
sidecar): `unit_test_runner.gd` exited rc=124 purely because the HOST was
loaded, while `run_cultivation_visual_sample.gd` produced a genuine runtime
error at `scripts/presentation/monthly_journey_view.gd:222`. Both arrived in
the stage report as the same red, so a reader could not tell "slow host" from
"broken script": the wall clock was the only bound, and it cannot distinguish
"still working" from "gone".

Real pieces, every one of them: `run_script`, `_run`, a real subprocess in a
real session (`start_new_session=True`), the real SIGKILL to that session's
process group, the streaming reader threads, the wall-clock cap, and the pass
decision (`_script_idle_verdict`). The ONLY substitute is the engine binary:
`GODOT_BIN` points at a small shell script whose behaviour per case is set
here, so no Godot engine, no display and no network are needed. These tests
therefore make NO claim about what real Godot prints — only about how a script's
own output bounds it.

The idle windows are small (well under a second) because the window is a real
parameter of the real code path; nothing here waits for a production-sized
budget and nothing here depends on how loaded the host is.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"
_spec = importlib.util.spec_from_file_location("godot_harness", _HARNESS)
gh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gh)

# The window these tests use. Small enough that the whole file runs in seconds,
# large enough that a shell `sleep 0.2` loop is comfortably inside it even on a
# loaded box — the tests must not depend on host load.
IDLE = 0.6
# The fake engine's own pacing for the "keeps printing" case: 6 steps, each
# after a 0.2 s pause, so the run legitimately lasts ~1.2 s — twice the idle
# window, and it must NOT be killed for it.
CHATTY_STEPS = 6
CHATTY_PAUSE = 0.2

# One engine for every case. `--import` (the per-copy cache build run_script
# does before the entries) finishes instantly; a script entry follows
# $FAKE_IDLE_MODE. The entry path itself is irrelevant to all of it, which is
# the point: what bounds a script here is what it PRINTS.
_ENGINE = """#!/bin/sh
case "$*" in
  *--import*) printf 'fake import ok\\n'; exit 0 ;;
esac
case "${{FAKE_IDLE_MODE:-quiet}}" in
  chatty)
    i=1
    while [ $i -le {steps} ]; do
      printf 'PASS idle_step_%s\\n' "$i"
      i=$((i+1))
      sleep {pause}
    done
    printf 'ALL IDLE ASSERTIONS PASSED\\n'
    exit 0
    ;;
  print_then_spin)
    printf 'PASS idle_first\\n'
    printf 'PASS idle_second\\n'
    sleep 60
    ;;
  never_prints)
    sleep 60
    ;;
  fail_marker)
    printf 'FAIL: idle_expected_true\\n'
    exit 0
    ;;
  nonzero_exit)
    printf 'partial idle output\\n'
    printf 'boom\\n' >&2
    exit 3
    ;;
esac
exit 0
""".format(steps=CHATTY_STEPS, pause=CHATTY_PAUSE)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Install the fake engine as GODOT_BIN; return a setter for its mode."""
    script = tmp_path / "fake-godot"
    script.write_text(_ENGINE)
    script.chmod(0o755)
    monkeypatch.setattr(gh, "GODOT_BIN", str(script))

    def mode(name):
        monkeypatch.setenv("FAKE_IDLE_MODE", name)

    return mode


def project(tmp_path):
    """A minimal project: the copy and the import pass are real, so it must be."""
    repo = tmp_path / "project"
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "project.godot").write_text("[application]\n")
    (repo / "tests" / "test_idle.gd").write_text("extends SceneTree\n")
    return repo


def run_entry(tmp_path, **kwargs):
    """Drive the real `run_script` over one entry point."""
    return gh.run_script(str(project(tmp_path)), ["res://tests/test_idle.gd"],
                         **kwargs)


# ── 1. progress keeps a slow script alive ──────────────────────────────────
def test_a_script_that_keeps_printing_is_never_idle_killed(engine, tmp_path):
    """A script that keeps printing finishes however slow the host is.

    The same fake is then run under a wall-clock-only budget equal to the idle
    window: that run is killed by the CLOCK (unchanged: `timed out after ...`),
    which is what the progress bound replaces for a live script. Nothing here
    raises the cap — the second run simply uses the smaller budget the first
    run's window is meant to be measured against.
    """
    engine("chatty")
    report = run_entry(tmp_path, timeout=60, idle_window=IDLE)
    entry = report["results"][0]

    assert report["passed"] is True and entry["passed"] is True
    assert entry["returncode"] == 0
    assert entry["idle_killed"] is False and entry["idle_reason"] is None
    # It outlived the idle window because it never went quiet for that long.
    assert entry["wall_sec"] > IDLE
    # ... and it is green by its OWN assertions, all of which the report kept.
    for step in (1, CHATTY_STEPS):
        assert "PASS idle_step_%d" % step in entry["stdout"]
    assert "ALL IDLE ASSERTIONS PASSED" in entry["stdout"]
    assert entry["last_output"] == "ALL IDLE ASSERTIONS PASSED"

    # The contrast, in the same tree: a wall-clock-only bound of the same size
    # kills this script mid-suite. That is the red the old stage could not tell
    # apart from a broken script.
    engine("chatty")
    clocked = run_entry(tmp_path, timeout=IDLE, idle_window=30)["results"][0]
    assert clocked["returncode"] == 124
    assert clocked["idle_killed"] is False
    assert "timed out after %ss" % IDLE in clocked["stderr"]
    assert "no output" not in clocked["stderr"]


# ── 2. silence is killed AT the window, and says what it was doing ─────────
def test_a_script_that_falls_idle_is_killed_at_the_window_with_its_last_line(engine, tmp_path):
    """Prints twice, then spins silently: killed at the window, not at the cap.

    The report must carry the three facts the wall-clock red can never carry —
    which window expired, how long it actually ran, and the last line it printed
    — so a reader can tell this apart from "the host was slow".
    """
    engine("print_then_spin")
    report = run_entry(tmp_path, timeout=60, idle_window=IDLE)
    entry = report["results"][0]

    assert report["passed"] is False and entry["passed"] is False
    assert entry["returncode"] == 124
    assert entry["idle_killed"] is True
    assert entry["idle_reason"] == "idle after output"
    assert entry["idle_window_sec"] == IDLE
    assert entry["idle_elapsed_sec"] >= IDLE      # the window really expired
    assert entry["idle_elapsed_sec"] < 15         # ... not the 60 s wall cap
    assert entry["wall_sec"] >= IDLE and entry["wall_sec"] < 15
    assert entry["last_output"] == "PASS idle_second"
    assert entry["last_output_stream"] == "stdout"

    # The suite's own words survive the kill (the pre-kill branches kept them).
    assert "PASS idle_first" in entry["stdout"]
    assert "no output for %ss (idle window)" % gh._seconds(IDLE) in entry["stderr"]
    assert "PASS idle_second" in entry["stderr"]
    assert "timed out after" not in entry["stderr"]     # NOT the clock's red
    assert "idle window" in report["summary"]
    assert "PASS idle_second" in report["summary"]


# ── 3. never-printed is a DIFFERENT observation ────────────────────────────
def test_a_script_that_never_prints_is_named_no_output_since_start(engine, tmp_path):
    engine("never_prints")
    report = run_entry(tmp_path, timeout=60, idle_window=IDLE)
    entry = report["results"][0]

    assert report["passed"] is False and entry["passed"] is False
    assert entry["idle_killed"] is True
    assert entry["idle_reason"] == "no output since start"
    assert "no output since start" in entry["stderr"]
    # It NAMES its start rather than talking about a gap it never had.
    assert entry["idle_started_at"] and entry["idle_started_at"] in entry["stderr"]
    assert "no output for" not in entry["stderr"]
    assert "timed out after" not in entry["stderr"]
    assert entry["last_output"] is None and entry["last_output_stream"] is None


def test_the_idle_reasons_distinguish_the_three_reds():
    """Three different facts must not read as one word.

    Silent-from-start, silent-after-output and the unchanged wall clock are
    told apart by `reason`, by the sentence, and never by "timed out".
    """
    start = gh.ScriptIdleSilence(["godot"], 120, elapsed_sec=120.0, last_line=None,
                                 last_stream=None, started_at="2026-11-07T00:00:00+00:00",
                                 never_printed=True)
    after = gh.ScriptIdleSilence(["godot"], 120, elapsed_sec=300.0,
                                 last_line="PASS idle_x", last_stream="stdout",
                                 started_at="2026-11-07T00:00:00+00:00",
                                 never_printed=False)

    assert start.reason != after.reason
    assert "no output since start" in start.describe()
    assert "PASS idle_x" in after.describe()
    for exc in (start, after):
        # Still an INCOMPLETE run for anything that only knows TimeoutExpired.
        assert isinstance(exc, subprocess.TimeoutExpired)
        assert "timed out after" not in exc.describe()
        assert "idle window" in exc.describe() and "120s" in exc.describe()


# ── 4. everything that was red stays exactly as red ────────────────────────
@pytest.mark.parametrize("mode,rc", [("fail_marker", 0), ("nonzero_exit", 3)])
def test_a_red_script_stays_red_however_short_the_idle_run(engine, tmp_path, mode, rc):
    """A FAIL line or a non-zero exit is red on its own merits — the idle window
    never rescues it, and it is never even reached: the run ends by itself."""
    engine(mode)
    report = run_entry(tmp_path, timeout=60, idle_window=IDLE)
    entry = report["results"][0]

    assert report["passed"] is False and entry["passed"] is False
    assert entry["idle_killed"] is False
    assert entry["returncode"] == rc
    assert entry["wall_sec"] < IDLE          # short: no window was required
    if mode == "fail_marker":
        assert "FAIL: idle_expected_true" in entry["stdout"]
    else:
        assert "partial idle output" in entry["stdout"]
        assert "boom" in entry["stderr"]


# ── 5. silence never becomes a pass ────────────────────────────────────────
def test_a_zero_exit_after_an_idle_kill_is_not_a_pass(engine, tmp_path, monkeypatch):
    """An idle-killed run is red even with no FAIL marker and an exit code of 0.

    Two layers, because the engine's own return code is the one thing this gate
    must not trust after a kill: (a) `run_script` driven through a real idle
    kill whose captured streams say nothing at all, and (b) the verdict itself
    for the exit-code-0 shape (a wrapper, or the kill racing a clean exit).
    """
    real_run = gh._run

    def killed_with_empty_streams(args, timeout, extra_env=None, render=False,
                                 idle_window=None):
        real = real_run(args, timeout, extra_env=extra_env, render=render,
                        idle_window=idle_window)
        if "-s" not in args:              # the import pass is not an entry point
            return real
        raise gh.ScriptIdleSilence(
            args, idle_window, elapsed_sec=IDLE, last_line=None, last_stream=None,
            started_at="2026-11-07T00:00:00+00:00", never_printed=True,
            stdout=real.stdout, stderr=real.stderr)

    monkeypatch.setattr(gh, "_run", killed_with_empty_streams)
    engine("quiet")                      # exits 0 and prints nothing at all
    report = run_entry(tmp_path, timeout=60, idle_window=IDLE)
    entry = report["results"][0]

    assert report["passed"] is False and entry["passed"] is False
    assert entry["idle_killed"] is True
    assert entry["stdout"] == ""
    assert entry["stderr"].startswith("no output since start")
    # The red cannot be the suite's own words: it printed none, and the harness's
    # sentence is not a FAIL marker (`FAILED.` is prose, `FAILED:` is a marker).
    assert gh._has_failure_marker(entry["stdout"], entry["stderr"]) is False

    # (b) rc 0 + no FAIL marker + the kill: still FAILED (`_script_idle_verdict`
    # answers "is this entry failed?", so True here is the red).
    assert gh._script_idle_verdict(0, "PASS idle_everything\n", "", [],
                                   idle_killed=True) is True
    # ... while the same run without the kill is the pass it always was.
    assert gh._script_idle_verdict(0, "PASS idle_everything\n", "", [],
                                   idle_killed=False) is False


def _idle_silence_is_success_mutant(rc, stdout, stderr, blocking, idle_killed=False):
    """MUTANT: the verdict as it was before this change — `idle_killed` dropped.

    Kept as a live mutation (not a comment about one): a later edit that
    "simplifies" the verdict back to this is exactly the regression that makes a
    wedged script look green, and the check below is what notices.
    """
    return bool(rc != 0 or blocking or gh._has_failure_marker(stdout, stderr))


def test_the_mutant_that_treats_idle_silence_as_success_reds_the_guarding_test():
    """Mutation check for `test_a_zero_exit_after_an_idle_kill_is_not_a_pass`.

    That test is the guard: it asserts an idle-killed run is FAILED. Under the
    mutant above, the same inputs come back as a PASS, so its
    `entry["passed"] is False` / `_script_idle_verdict(...) is False` assertions
    fail — the guard reds, which is what says the guard is real and not a
    restatement of the implementation.
    """
    killed = (0, "", "", [])
    # The guard says FAILED; the mutant says "not failed", i.e. a pass — which
    # is exactly the assertion the guarding test makes and the mutant breaks.
    assert gh._script_idle_verdict(*killed, idle_killed=True) is True
    assert _idle_silence_is_success_mutant(*killed, idle_killed=True) is False

    # The mutation is EXACTLY the idle term: every other verdict is unchanged,
    # so the guard is measuring the one thing it claims to measure.
    for case in ((0, "PASS idle_ok\n", "", []),
                 (1, "", "", []),
                 (0, "FAIL: idle_nope\n", "", []),
                 (0, "SCRIPT ERROR: boom\n", "", []),
                 (0, "", "", [{"native_class": "deferred_call", "blocking": True}])):
        assert (gh._script_idle_verdict(*case)
                == _idle_silence_is_success_mutant(*case))


# ── 6. nothing the report already measured is dropped ──────────────────────
def test_an_idle_kill_keeps_every_field_and_records_cost_and_host_load(engine, tmp_path,
                                                                      monkeypatch):
    """The retained entry keeps every field it had, and gains the cost facts.

    `raw_logs` rows still go to retention for the same pass (the manifest is
    written from them), the legacy per-entry fields are all present, and the
    entry now also carries its own CPU seconds and the host load average at
    start and end — without which a red cannot be read back later.
    """
    seen = {}
    real_retain = gh._retain_copy

    def capture(declared, homes, raw_logs, corr, **kwargs):
        seen["raw"] = [dict(row) for row in raw_logs]
        return real_retain(declared, homes, raw_logs, corr, **kwargs)

    monkeypatch.setattr(gh, "_retain_copy", capture)
    monkeypatch.setenv("GODOT_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    engine("print_then_spin")
    report = run_entry(tmp_path, timeout=60, idle_window=IDLE, retain_requested=True)
    entry = report["results"][0]

    assert report["idle_window_sec"] == IDLE

    # Legacy fields, all of them, unchanged in meaning.
    for key in ("script", "returncode", "passed", "stdout", "stderr",
                "stdout_truncated", "stderr_truncated", "errors",
                "native_errors", "native_blocking"):
        assert key in entry, key
    assert entry["script"] == "res://tests/test_idle.gd"
    assert entry["errors"] == [] and entry["native_errors"] == []
    assert entry["native_blocking"] == []

    # The raw-log row for this pass, as retention receives it.
    rows = {row["label"]: row for row in seen["raw"]}
    assert "import" in rows                      # the cache build, unchanged
    row = rows["test_idle.gd"]
    assert row["label"] == "test_idle.gd"
    assert row["returncode"] == 124 and row["timed_out"] is True
    assert "PASS idle_second" in row["stdout"]
    assert "idle window" in row["stderr"]
    assert report.get("retention") is not None

    # Criterion 1: wall seconds (already there), the script's OWN CPU seconds,
    # and the host load average at both ends.
    assert entry["wall_sec"] >= IDLE
    assert isinstance(entry["cpu_sec"], float) and entry["cpu_sec"] >= 0.0
    if sys.platform.startswith("linux"):
        assert len(entry["load_avg_start"]) == 3
        assert len(entry["load_avg_end"]) == 3
        assert all(isinstance(v, float) for v in entry["load_avg_start"])


# ── 7. the window is a real parameter, and the cap is not derived from it ──
def test_the_idle_window_comes_from_the_caller_and_never_lifts_the_cap(engine, tmp_path):
    """The wall-clock cap is `timeout`, full stop: the window never raises it.

    The production default is named and sits well under the default entry
    budget, so a silent script is named in minutes rather than ten — and a
    caller can only ever shorten or lengthen the SILENCE bound, never the cap.
    """
    assert gh.SCRIPT_IDLE_WINDOW_SEC == 120.0
    assert gh.SCRIPT_IDLE_WINDOW_SEC < 600          # run_script's default timeout

    engine("print_then_spin")
    report = run_entry(tmp_path, timeout=60, idle_window=0.3)
    entry = report["results"][0]
    assert entry["idle_window_sec"] == 0.3
    assert entry["idle_killed"] is True and entry["idle_elapsed_sec"] >= 0.3

    # A non-positive or absurd window is refused outright, exactly like a
    # negative timeout: it can never be used to make the bound unreachable.
    for bad in (0, -1, float("inf")):
        with pytest.raises(ValueError):
            run_entry(tmp_path, timeout=60, idle_window=bad)


def test_the_default_window_is_used_when_the_caller_names_none(engine, tmp_path):
    """An omitted window is the production one — reported, not inferred."""
    engine("fail_marker")
    report = run_entry(tmp_path, timeout=60)       # no idle_window kwarg
    assert report["idle_window_sec"] == gh.SCRIPT_IDLE_WINDOW_SEC
    assert report["results"][0]["idle_window_sec"] == gh.SCRIPT_IDLE_WINDOW_SEC
