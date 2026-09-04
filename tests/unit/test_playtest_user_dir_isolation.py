"""Every scenario runs with its own user:// — a save must not cross scenarios.

Measured 2026-09-04: Godot derives user:// from $HOME, the harness passed the
container's $HOME to every run, and app_userdata/<project>/ held save_1.json,
save_2.json and settings.cfg shared by every scenario, every sweep and every
tree. `menu_load_continues` failed its `load_available: changed` assert with
baseline true / current true — the frame-0 baseline saw a save an earlier
scenario had written. The same unchanged tree gave 0, then 1, then 6 reds with
disjoint sets; that is order-dependence, not chance.
"""
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "docker/godot/godot_harness.py"


def _spec_body():
    s = SRC.read_text(encoding="utf-8")
    i = s.index("def _playtest_spec(")
    j = s.index("\ndef ", i + 10)
    return s[i:j]


def test_the_scenario_run_gets_a_fresh_home():
    body = _spec_body()
    assert "mkdtemp(prefix=\"godot_home_\")" in body, (
        "each scenario needs its own $HOME: user:// hangs off it, so a shared "
        "HOME shares save files between scenarios")
    assert re.search(r'"HOME":\s*sc_home', body), "the fresh HOME must reach the run env"


def test_the_fresh_home_is_removed_even_when_the_run_raises():
    body = _spec_body()
    i = body.index("sc_home = tempfile.mkdtemp")
    tail = body[i:i + 1200]
    assert "finally:" in tail and "rmtree(sc_home" in tail, (
        "a scenario that times out must still drop its home, or the temp dir "
        "grows by one per scenario per sweep")


def test_the_probe_spec_still_reaches_the_run():
    # The env dict carries both keys; dropping the spec would make every
    # scenario run the legacy smoke instead, silently.
    body = _spec_body()
    i = body.index("sc_home = tempfile.mkdtemp")
    tail = body[i:i + 1200]
    assert "AITELIER_PROBE_SPEC" in tail
