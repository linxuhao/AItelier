"""The authored playtest report names a headless fallback where its verdicts are read.

Measured 2026-11-28 (iss-c22ea691c3d14d7b): a carrier whose first (render) pass
produced no snapshot was retried headless by `_run_probe`, and the report carried
`render_mode: headless` with 30 advisory assertion failures and nothing else — a
caller reading `passed`/`behavior.all_passed` saw a product red instead of a pass
that could not see. The retry stays a degradation (not a HARD failure); it just
has to be visible on the report itself, not only in the `timing` ledger.
"""
import importlib.util
import json
from pathlib import Path
import pytest

HARNESS = Path(__file__).resolve().parents[2] / 'docker/godot/godot_harness.py'
spec = importlib.util.spec_from_file_location('gh_headless_marker', HARNESS)
gh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gh)


def authored():
    """One admissible authored scenario: a capture, an input, an assertion."""
    return {'scenarios': [{'name': 'late', 'timeline': [
        {'at': 38, 'capture_before': [{'id': 'b', 'node': 'N', 'attr': 'x', 'action_frame': 39}]},
        {'at': 39, 'actions': ['advance']},
        {'at': 40, 'assert': [{'node': 'N', 'attr': 'x', 'mode': 'changed', 'before': 'b'}]}]}]}


def engine(monkeypatch, render_snapshot):
    """Fake the engine pass.

    `render_snapshot=False` is the shape a broken X/GL setup produces: the render
    pass writes no state file, so `_run_probe` retries the identical spec headless.
    Every pass that does produce a snapshot reports an all-passed assertion table.
    """
    calls = []

    def once(args, env, path, timeout, render, **kwargs):
        timeline = json.loads(Path(env['AITELIER_PROBE_SPEC']).read_text())['timeline']
        frames = int(env['AITELIER_PROBE_FRAMES'])
        calls.append({'render': render, 'timeline': timeline, 'frames': frames})
        kwargs.get('timing', {}).update(game_usec=int(frames / 60 * 1000000))
        if render and not render_snapshot:
            return {}, [], False
        asserts = [{'name': str(i), 'passed': True}
                   for i in range(sum(len(e.get('assert', [])) for e in timeline))]
        return ({'frames': frames, 'complete': True,
                 'nodes': {'N': {'x': bool(timeline)}}, 'asserts': asserts}, [], False)

    monkeypatch.setattr(gh, '_probe_once', once)
    return calls


def test_render_fallback_is_named_on_the_authored_report(monkeypatch, tmp_path):
    calls = engine(monkeypatch, render_snapshot=False)
    report = gh._playtest_spec(tmp_path / 'project', authored(), 180, 0.75)
    # The retry itself: the scenario's first pass drew nothing, the second is headless.
    assert [c['render'] for c in calls[:2]] == [True, False], calls
    assert report['render_mode'] == 'headless', report['render_mode']
    assert report['headless_retry'] is True, report
    # The marker names the degradation; it does not manufacture a verdict. A clean
    # run stays passed, and no spec_error is invented (`hard_passed` is unchanged).
    assert report['passed'] is True, report
    assert report['spec_errors'] == [], report['spec_errors']
    assert report['behavior']['all_passed'] is True, report['behavior']


def test_a_drawn_pass_reports_no_retry(monkeypatch, tmp_path):
    calls = engine(monkeypatch, render_snapshot=True)
    report = gh._playtest_spec(tmp_path / 'project', authored(), 180, 0.75)
    assert calls[0]['render'] is True, calls
    assert report['render_mode'] == 'render', report['render_mode']
    assert report['headless_retry'] is False, report
    assert report['passed'] is True, report


def test_the_ledger_row_and_the_report_agree(monkeypatch, tmp_path):
    """The rollup is the ledger's own fact, not a second measurement."""
    ledger = {}
    engine(monkeypatch, render_snapshot=False)
    report = gh._playtest_spec(tmp_path / 'project', authored(), 180, 0.75, ledger=ledger)
    rows = ledger['scenarios']
    assert [bool(r['headless_retry']) for r in rows] == [True], rows
    assert report['headless_retry'] is True
