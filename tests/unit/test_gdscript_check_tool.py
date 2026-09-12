"""gdscript_check — the per-task GDScript parse gate (host-side tool)."""

import json
import subprocess
import urllib.error
from pathlib import Path

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


# --- baseline exemption -------------------------------------------------
# A step is answerable for what IT broke. These pin the mechanism that decides
# that, because the first version of it shipped with no test and was DEAD in
# every real run: it inferred a project id from the staging path, which a
# `target: code` step's root (a per-run worktree) is not under.

def _git_repo(path, files):
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("add", "-A")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "shipped")
    return path


def test_the_baseline_is_the_checked_trees_own_head_not_an_inferred_project(
        tmp_path, monkeypatch):
    from aitelier.tools.gdscript_check.impl import _baseline_head
    import subprocess
    repo = _git_repo(tmp_path / "checkout", {"a.gd": "extends Node\n"})
    wt = tmp_path / "worktrees" / "run-1"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "r1",
                    str(wt)], check=True, capture_output=True)

    # The root a `target: code` step is validated at is the run's worktree. It
    # is NOT under workspaces/, and the project-keyed resolver would have
    # answered with the shared checkout; the tree answers for itself.
    resolved = _baseline_head(wt)
    assert resolved is not None
    top, head = resolved
    assert top.resolve() == wt.resolve()
    assert top.resolve() != repo.resolve()
    assert head == subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()


def test_a_tree_that_is_not_a_git_repo_has_no_baseline(tmp_path):
    from aitelier.tools.gdscript_check.impl import _baseline_head
    plain = tmp_path / "staging"
    plain.mkdir()
    # No baseline => the strict verdict stands. A gate that cannot check the
    # baseline must not forgive.
    assert _baseline_head(plain) is None


def _fake_builder(monkeypatch, verdicts):
    """Stub /checkgd: verdicts maps a path SUFFIX -> error_message (or None)."""
    calls = []

    class _Resp:
        def __init__(self, body): self._b = body
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _open(req, *a, **k):
        files = json.loads(req.data)["files"]
        calls.append(files)
        results = []
        for f in files:
            err = None
            for suffix, msg in verdicts.items():
                if f.endswith(suffix):
                    err = msg
                    break
            results.append({"file": f, "passed": err is None,
                            "error_message": err or ""})
        return _Resp(json.dumps({
            "all_passed": all(r["passed"] for r in results),
            "results": results}).encode())

    monkeypatch.setattr("urllib.request.urlopen", _open)
    return calls


def test_an_untouched_file_that_fails_identically_at_head_is_forgiven(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "wt", {
        "pre.gd": "extends Node\nconst K = preload('res://x.gd')\n"})
    # --check-only sees one file with no project import, so this idiom fails
    # both in the tree and at HEAD, with the same complaint.
    _fake_builder(monkeypatch, {
        "pre.gd": 'SCRIPT ERROR: Parse Error: "K" is a constant but does not '
                  'contain a type.'})

    r = gdscript_check(files=["*.gd"], workspace_root=str(repo))
    assert r["all_passed"] is True
    assert r["results"][0]["preexisting"] is True


def test_a_file_the_step_broke_is_not_forgiven_by_its_preexisting_failure(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "wt", {
        "pre.gd": "extends Node\nconst K = preload('res://x.gd')\n"})
    (repo / "pre.gd").write_text("extends Node\nfunc broken(\n")
    # HEAD complains about the preload idiom; the working copy has a real
    # syntax error. Different diagnosis => the step owns it.
    calls = []

    class _Resp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _open(req, *a, **k):
        files = json.loads(req.data)["files"]
        calls.append(files)
        in_baseline = "gdscript_baseline" in files[0]
        msg = ('SCRIPT ERROR: Parse Error: "K" is a constant but does not '
               'contain a type.') if in_baseline else (
              'SCRIPT ERROR: Parse Error: Expected closing ")" after function '
              'parameters.')
        return _Resp(json.dumps({"all_passed": False, "results": [
            {"file": f, "passed": False, "error_message": msg}
            for f in files]}).encode())

    monkeypatch.setattr("urllib.request.urlopen", _open)
    r = gdscript_check(files=["*.gd"], workspace_root=str(repo))
    assert r["all_passed"] is False
    assert len(calls) == 2          # the baseline really was consulted


def test_a_file_the_step_created_has_no_baseline_and_is_never_forgiven(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "wt", {"keep.md": "shipped\n"})
    (repo / "new.gd").write_text("extends Node\nfunc oops(\n")
    calls = _fake_builder(monkeypatch, {
        "new.gd": 'SCRIPT ERROR: Parse Error: Expected closing ")".'})

    r = gdscript_check(files=["*.gd"], workspace_root=str(repo))
    assert r["all_passed"] is False
    # Nothing to export at HEAD, so the builder is asked exactly once.
    assert len(calls) == 1


# --- degradation is visible ---------------------------------------------
# The exemption above has three ways to fall back to the strict verdict
# silently: a baseline that fails to resolve (no git root, or `git` itself
# unavailable/erroring), a baseline export that hits an OSError, and a second
# /checkgd call that fails. None of them may widen or narrow the verdict —
# they only have to leave a line saying it happened. And a cache directory
# that is container-writable must never be trusted by presence alone.

def _capture_gate_log(monkeypatch):
    import aitelier.gate_skip_log as gsl
    lines = []
    monkeypatch.setattr(gsl, "_logger", type("L", (), {
        "info": lambda self, fmt, *a: lines.append(fmt % a)})())
    return lines


def test_a_non_git_root_leaves_a_line_and_the_verdict_stays_strict(tmp_path, monkeypatch):
    lines = _capture_gate_log(monkeypatch)
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    f = plain / "a.gd"
    f.write_text("func f(:\n")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _resp(
        {"all_passed": False, "results": [
            {"file": str(f), "passed": False,
             "error_message": "Parse Error: Expected identifier"}]}))

    r = gdscript_check(files=["*.gd"], workspace_root=str(plain))
    # A baseline that cannot be resolved must never WIDEN the exemption —
    # the strict verdict from the sidecar stands untouched.
    assert r["all_passed"] is False
    assert any("gate=gdscript_check" in l and "SKIPPED" in l
               and "baseline resolve failed" in l for l in lines)


def test_git_missing_from_path_leaves_a_line_and_the_verdict_stays_strict(
        tmp_path, monkeypatch):
    lines = _capture_gate_log(monkeypatch)
    repo = _git_repo(tmp_path / "wt", {"a.gd": "extends Node\n"})
    (repo / "a.gd").write_text("func f(:\n")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _resp(
        {"all_passed": False, "results": [
            {"file": str(repo / "a.gd"), "passed": False,
             "error_message": "Parse Error: Expected identifier"}]}))
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))     # no `git` reachable at all

    r = gdscript_check(files=["*.gd"], workspace_root=str(repo))
    assert r["all_passed"] is False
    assert any("gate=gdscript_check" in l and "baseline resolve failed" in l
               for l in lines)


def test_a_second_checkgd_call_failure_leaves_a_line_and_the_verdict_stays_strict(
        tmp_path, monkeypatch):
    lines = _capture_gate_log(monkeypatch)
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "wt", {
        "pre.gd": "extends Node\nconst K = preload('res://x.gd')\n"})
    calls = {"n": 0}

    def _open(req, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:      # the primary check, against the staged file
            return _resp({"all_passed": False, "results": [
                {"file": str(repo / "pre.gd"), "passed": False,
                 "error_message": 'SCRIPT ERROR: Parse Error: "K" is a '
                                  'constant but does not contain a type.'}]})
        raise urllib.error.URLError("no route")   # the baseline call, fails

    monkeypatch.setattr("urllib.request.urlopen", _open)
    r = gdscript_check(files=["*.gd"], workspace_root=str(repo))
    assert calls["n"] == 2
    # The baseline could not be consulted, so nothing is forgiven — strict.
    assert r["all_passed"] is False
    assert any("gate=gdscript_check" in l and "baseline checkgd call failed" in l
               for l in lines)


def test_a_baseline_export_oserror_leaves_a_line_and_the_verdict_stays_strict(
        tmp_path, monkeypatch):
    lines = _capture_gate_log(monkeypatch)
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "wt", {
        "pre.gd": "extends Node\nconst K = preload('res://x.gd')\n"})
    _fake_builder(monkeypatch, {
        "pre.gd": 'SCRIPT ERROR: Parse Error: "K" is a constant but does not '
                  'contain a type.'})
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda self, data: (
        _ for _ in ()).throw(OSError("disk full")))

    r = gdscript_check(files=["*.gd"], workspace_root=str(repo))
    # The export failed, so nothing could be exported to compare against —
    # the primary (strict) failure stands.
    assert r["all_passed"] is False
    assert any("gate=gdscript_check" in l and "baseline export failed" in l
               for l in lines)


def test_healthy_path_writes_no_degradation_line(tmp_path, monkeypatch):
    """Negative control: none of the three failure paths fire on a clean run,
    including one that legitimately consults and finds a matching baseline."""
    lines = _capture_gate_log(monkeypatch)
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "wt", {
        "pre.gd": "extends Node\nconst K = preload('res://x.gd')\n"})
    _fake_builder(monkeypatch, {
        "pre.gd": 'SCRIPT ERROR: Parse Error: "K" is a constant but does not '
                  'contain a type.'})

    r = gdscript_check(files=["*.gd"], workspace_root=str(repo))
    assert r["all_passed"] is True            # forgiven normally, via a real baseline
    assert r["results"][0]["preexisting"] is True
    assert lines == []


def test_a_forged_baseline_cache_entry_is_not_trusted(tmp_path, monkeypatch):
    """The cache directory is under the mounted, container-writable data root.

    Before the fix, `dest.is_file()` alone decided reuse: planting a file at
    the exact `gdscript_baseline/<head12>/<rel>` path a real export would use
    let a genuine, freshly-introduced syntax error be "forgiven" as identical
    to a HEAD failure it never actually had. The fake godot-builder below
    parses the FILE CONTENT it is handed (like the real one does), so this
    only stays green if the forged cache is actually treated as the baseline.
    """
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    # HEAD ships the (idiomatic-but-check-only-hostile) preload constant.
    repo = _git_repo(tmp_path / "wt", {
        "pre.gd": "extends Node\nconst K = preload('res://x.gd')\n"})
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    # The step then breaks the file for real, with an UNRELATED syntax error.
    (repo / "pre.gd").write_text("extends Node\nfunc broken(\n")

    # Plant a forged cache entry that claims to be the HEAD blob but is
    # actually a copy of the step's own broken content — an attempt to make
    # the real error look "identical to what HEAD already had".
    cache = tmp_path / "home" / "gdscript_baseline" / head[:12] / "pre.gd"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("extends Node\nfunc broken(\n")

    def _content_aware_open(req, *a, **k):
        files = json.loads(req.data)["files"]
        results = []
        for f in files:
            text = Path(f).read_text()
            if "func broken(" in text:
                msg = ('SCRIPT ERROR: Parse Error: Expected closing ")" '
                      'after function parameters.')
            elif "preload(" in text:
                msg = ('SCRIPT ERROR: Parse Error: "K" is a constant but '
                      'does not contain a type.')
            else:
                msg = None
            results.append({"file": f, "passed": msg is None,
                            "error_message": msg or ""})
        return _resp({"all_passed": all(r["passed"] for r in results),
                      "results": results})

    monkeypatch.setattr("urllib.request.urlopen", _content_aware_open)
    r = gdscript_check(files=["*.gd"], workspace_root=str(repo))
    # If the forged cache had been trusted by presence alone, the baseline
    # call would have echoed the SAME broken diagnosis as the staged file,
    # and the real, newly-introduced error would have been forgiven.
    assert r["all_passed"] is False
    assert not r["results"][0].get("preexisting")
    # The forged content must have been overwritten with the real HEAD blob.
    assert cache.read_text() == "extends Node\nconst K = preload('res://x.gd')\n"


def test_an_untampered_cache_hit_is_still_reused(tmp_path, monkeypatch):
    """Positive control for the same mechanism: a cache entry that genuinely
    matches HEAD must still be reused, not re-fetched every time — the fix
    is a content check, not a ban on reuse."""
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    repo = _git_repo(tmp_path / "wt", {
        "pre.gd": "extends Node\nconst K = preload('res://x.gd')\n"})
    from aitelier.tools.gdscript_check.impl import _baseline_head, _export_baseline
    top, head = _baseline_head(repo)
    first = _export_baseline(top, head, ["pre.gd"])
    cache_path = next(iter(first))
    mtime_before = Path(cache_path).stat().st_mtime_ns

    fetch_calls = {"n": 0}
    real_git = subprocess.run
    def _counting_run(args, *a, **k):
        if "cat-file" in args:
            fetch_calls["n"] += 1
        return real_git(args, *a, **k)
    monkeypatch.setattr(subprocess, "run", _counting_run)

    second = _export_baseline(top, head, ["pre.gd"])
    assert second == first
    assert fetch_calls["n"] == 0        # no re-fetch: the cache hit was trusted
    assert Path(cache_path).stat().st_mtime_ns == mtime_before
