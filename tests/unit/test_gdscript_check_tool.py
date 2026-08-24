"""gdscript_check — the per-task GDScript parse gate (host-side tool)."""

import json
import urllib.error

import pytest

from aitelier.tools.gdscript_check.impl import gdscript_check


def test_no_gd_files_passes(tmp_path):
    # A step that wrote only .tscn or docs has nothing to parse.
    (tmp_path / "notes.md").write_text("hi")
    assert gdscript_check(files=["*.gd"], workspace_root=str(tmp_path)) == {
        "all_passed": True, "results": []}


def test_paths_are_reported_relative_to_the_staging_root(tmp_path, monkeypatch):
    (tmp_path / "scripts").mkdir()
    f = tmp_path / "scripts" / "a.gd"
    f.write_text("extends Node\n")

    class _Resp:
        def read(self):
            return json.dumps({"all_passed": False, "results": [
                {"file": str(f), "passed": False, "error_message": "boom"}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    r = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    # The absolute container path is noise in the implementer's retry prompt.
    assert r["results"][0]["file"] == "scripts/a.gd"


def test_an_unreachable_builder_skips_loudly_instead_of_failing(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.gd").write_text("extends Node\n")

    def _boom(*a, **k):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    r = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    # A sidecar that is down must not fail every task in the loop...
    assert r["all_passed"] is True
    assert r["gate_skipped"] is True
    # ...but a skipped gate must never read as a green one.
    assert "GATE SKIPPED" in capsys.readouterr().out


def _resp(payload):
    class _R:
        def read(self):
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    return _R()


def test_a_parse_error_fails_the_gate(tmp_path, monkeypatch):
    """The primary oracle — and it had no test.

    Every existing case here covered a boundary: no files, path formatting, an
    unreachable sidecar, a short result set. Nothing asserted the one thing the
    gate exists to do. A gate whose main path is untested is a gate nobody has
    watched fail.
    """
    (tmp_path / "a.gd").write_text("func f(:\n")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _resp(
        {"all_passed": False, "results": [
            {"file": str(tmp_path / "a.gd"), "passed": False,
             "error_message": "Parse Error: Expected identifier"}]}))

    r = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    assert r["all_passed"] is False
    assert "Parse Error" in r["results"][0]["error_message"]


def test_a_parse_error_reaches_the_step_validator_as_an_error(tmp_path, monkeypatch):
    """Test the gate through its CONSUMER, not just its return value.

    StepValidator._add_issues is the real oracle: the tool's dict is only an
    input to it. A gate can return a perfectly good failure and still have no
    effect on the step if the shape does not match what the validator reads.
    """
    from skillflow.step_validation import StepValidator

    (tmp_path / "a.gd").write_text("func f(:\n")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _resp(
        {"all_passed": False, "results": [
            {"file": str(tmp_path / "a.gd"), "passed": False,
             "error_message": "Parse Error: Expected identifier"}]}))

    errors, warnings = [], []
    StepValidator._add_issues(
        gdscript_check(files=["*.gd"], workspace_root=str(tmp_path)),
        "gdscript_check", "fail", errors, warnings)
    assert errors and "Parse Error" in errors[0]["error_message"]


def test_a_skipped_gate_is_invisible_to_the_step_validator(tmp_path, monkeypatch):
    """Pin WHY the skip needs a log: `gate_skipped` cannot reach any reader.

    _add_issues returns the moment `all_passed` is true and drops the rest of the
    dict. So the flag is dead data on this path — it is not a signal, it is a
    comment. If skillflow ever grows a warn channel for a passing-but-degraded
    validator, this test goes red and the log stops being the only surface.
    """
    from skillflow.step_validation import StepValidator

    (tmp_path / "a.gd").write_text("extends Node\n")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.URLError("no route")))

    result = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    assert result["gate_skipped"] is True

    errors, warnings = [], []
    StepValidator._add_issues(result, "gdscript_check", "fail", errors, warnings)
    assert (errors, warnings) == ([], []), (
        "the validator now carries the skip — stop relying on the log alone")


def test_a_skipped_gate_is_recorded_where_it_outlives_the_run(tmp_path, monkeypatch):
    """The container log is not mounted; ~/.AItelier/logs is. A skip that only
    printed to stdout was gone the next time the container was recreated."""
    import aitelier.gate_skip_log as gsl

    lines = []
    monkeypatch.setattr(gsl, "_logger", type("L", (), {
        "info": lambda self, fmt, *a: lines.append(fmt % a)})())

    (tmp_path / "a.gd").write_text("extends Node\n")
    (tmp_path / "b.gd").write_text("extends Node\n")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.URLError("no route")))

    gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    assert len(lines) == 1
    assert "gate=gdscript_check SKIPPED" in lines[0]
    assert "unchecked_files=2" in lines[0]      # how much shipped unverified


def test_the_skip_log_never_raises_into_the_gate(tmp_path, monkeypatch):
    import aitelier.gate_skip_log as gsl

    monkeypatch.setattr(gsl, "_get_logger", lambda: (_ for _ in ()).throw(
        RuntimeError("disk full")))
    (tmp_path / "a.gd").write_text("extends Node\n")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(
        urllib.error.URLError("no route")))

    r = gdscript_check(files=["*.gd"], workspace_root=str(tmp_path))
    assert r["all_passed"] is True and r["gate_skipped"] is True
