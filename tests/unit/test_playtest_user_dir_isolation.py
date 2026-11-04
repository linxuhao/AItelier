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


def _function_body(name):
    """The source of one top-level function, by name.

    The per-scenario loop moved out of `_playtest_spec` into `_playtest_spec_inner`
    when retention/raw-log ownership was added: the wrapper now carries only the
    TOTAL owned-home cleanup guarantee and delegates the run. Follow the code
    instead of pinning which function happens to hold it.
    """
    s = SRC.read_text(encoding="utf-8")
    i = s.index("def %s(" % name)
    j = s.find("\ndef ", i + 10)
    return s[i:] if j < 0 else s[i:j]


def _spec_body():
    return _function_body("_playtest_spec") + _function_body("_playtest_spec_inner")


def test_the_scenario_run_gets_a_fresh_home():
    body = _spec_body()
    assert 'mkdtemp(prefix="godot_home_")' in body, (
        "each scenario needs its own $HOME: user:// hangs off it, so a shared "
        "HOME shares save files between scenarios")
    assert re.search(r"_home_env\(sc_home\)", body), "the fresh HOME must reach the run env"
    # ...and it must reach the child AS HOME, which is what Godot derives
    # user:// from. _home_env builds the whole per-pass environment.
    assert re.search(r'"HOME":\s*home\b', _function_body("_home_env")), (
        "the per-pass environment must set HOME to that pass's own directory")


def test_the_fresh_home_is_removed_even_when_the_run_raises():
    body = _spec_body()
    i = body.index('sc_home = tempfile.mkdtemp')
    # Registered the moment it exists — never after its pass — or a raise
    # between creation and the scenario's own cleanup leaks the directory and
    # the wrapper below has nothing to remove.
    assert 'scen_homes.append(Path(sc_home))' in body[i:i + 200], (
        "the fresh home must be registered by the wrapper, not only cleaned locally")
    tail = body[i:]
    assert "finally:" in tail and "shutil.rmtree(sc_home" in tail, (
        "a scenario that times out must still drop its home, or the temp dir "
        "grows by one per scenario per sweep")
    # And the wrapper's own finally covers every registered home on a pass, a
    # failure, a timeout AND an unexpected exception from an inner call.
    wrapper = _function_body("_playtest_spec")
    assert "finally:" in wrapper and "scen_homes" in wrapper \
        and "shutil.rmtree(h" in wrapper, (
            "the wrapper must remove every home this invocation registered, on any outcome")


def test_the_probe_spec_still_reaches_the_run():
    # The env dict carries both keys; dropping the spec would make every
    # scenario run the legacy smoke instead, silently.
    body = _spec_body()
    i = body.index('sc_home = tempfile.mkdtemp')
    env = body[i:i + 1200]
    assert '"AITELIER_PROBE_SPEC"' in env and "_home_env(sc_home)" in env, (
        "the scenario's env must carry the authored spec AND its own HOME")
