"""Corrections to the whole-gate retention path (2026-10-06).

SOURCE-only tests: no Godot, no engine, no network beyond the harness' own
stdlib HTTP server. `_run` — the one seam that would shell out to the engine —
is stubbed; the filesystem copies and the pinned-descriptor walk run for real.

Each test pins one of the corrections the independent review required, so a
regression to the previously observed shape is red rather than silent:

  * `run_script` records the retention result in `retention_cell` so a later
    unexpected error cannot attempt a SECOND retention of the same invocation;
  * `_playtest_spec` forwards `dst/spec/frames/timeout/ledger/cap_limit` and
    the inner body sees `raw_logs`/`user_dir_name`;
  * `playtest_project` keeps a finite per-run `timeout` (default 120) and
    forwards it — the engine run is bounded, never unbounded;
  * a literal and a pattern that select the SAME artifact of the SAME pass are
    copied once (no duplicate row, no double byte count) while distinct passes
    stay distinct;
  * a literal `saves`/`saved_games`/`profile`/`profiles` path is refused
    BEFORE any home is touched, matching the documented pre-effect rejection;
  * `{"retain": {}}` is an explicit raw-only request distinct from an omitted
    `retain` key;
  * the pattern walk enumerates with `os.scandir` and charges the search budget
    per entry before collecting/sorting names;
  * `playtest_project` keeps the original 97b5 positional order
    (project_dir, frames, input_action, spec, timeout=120, captures, ...) with
    the retention parameters appended after them;
  * a `None` corr reaching `_playtest_spec_inner` is normalised to `{}` before
    the retention expansion, while the server-mandated mapping survives;
  * the `_Handler` validates BOTH the literal relpath declaration and the
    pattern selectors BEFORE owner admission/copy/import/run — a bad shape or
    bad semantics returns 400 with zero effects;
  * a pattern that SELECTED a regular artifact which then vanished (or changed
    inode) before the pinned copy opened it names that rel in `missing` and
    hard-fails — never a silent "matched nothing" green — with the zero-match
    outcome kept distinct and NO retry/reopen/path fallback;
  * an unallocatable owned destination reports NO raw rows (a fabricated row
    pointing at bytes never written would be a false green) while the
    allocation failure alone makes `ok` False;
  * an explicit retention request on an empty-but-valid project still writes a
    bounded, truthful manifest (raw-only, since zero suites ever ran, or a hard
    failure naming a declaration no pass can satisfy) under a unique
    server-owned invocation directory. The no-entry verdict itself is a FAILED
    SCRIPT verdict in both modes with or without retention — an omitted
    `retain` writes no retention at all; retention success is never a pass.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path


_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    monkeypatch.setenv("GODOT_DEPLOYMENT_LOCK", str(tmp_path / "deployment.lock"))
    monkeypatch.setenv("GODOT_RENDER_EFFECT_LOCK", str(tmp_path / "effect.lock"))
    monkeypatch.setenv("GODOT_EVIDENCE_ROOT", str(tmp_path / "evidence-root"))
    spec = importlib.util.spec_from_file_location("gh_retention_corrections", _HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── run_script keeps the retention cell assigned ────────────────────────────

def test_run_script_assigns_the_retention_cell(monkeypatch, tmp_path):
    """The unexpected-error handler only re-retains when `retention_cell` is
    still empty. A normal retention that is never recorded therefore re-runs on
    any later error: a second copy of the same invocation, and a second
    manifest. The assignment is the seam that prevents it, so it is pinned."""
    src = _HARNESS.read_text(encoding="utf-8")
    start = src.index("def run_script(")
    end = src.index("\n# ── HTTP transport", start)
    body = src[start:end]
    assert 'retention_cell["value"] = retention' in body, (
        "run_script must record the retention result in retention_cell")


# ── playtest_project: finite timeout, forwarded ─────────────────────────────

def test_playtest_project_bounds_each_engine_run_with_a_finite_timeout():
    """An unbounded per-run timeout on the playtest path means one wedged
    engine run holds the render owner forever. The default must be finite, and
    the request's own `timeout` must reach the playtest body."""
    spec = importlib.util.spec_from_file_location("gh_retention_timeout", _HARNESS)
    gh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gh)
    params = inspect.signature(gh.playtest_project).parameters
    assert params["timeout"].default == 120, (
        "playtest_project's per-run timeout default must be finite (120)")

    src = _HARNESS.read_text(encoding="utf-8")
    i = src.index('self.path == "/playtest"')
    j = src.index("self._send_timed(200", i)
    body = src[i:j]
    assert 'timeout=req.get("timeout", 120)' in body, (
        "the /playtest route must forward its own finite timeout")


def test_playtest_project_forwards_timeout_to_the_spec_body(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    (tmp_path / "project.godot").write_text("[application]\nconfig/name=\"P\"\n")
    monkeypatch.setattr(gh, "_copy_project", lambda p: tmp_path / "work" / "proj")
    (tmp_path / "work" / "proj").mkdir(parents=True)
    monkeypatch.setattr(gh, "_inject_probe", lambda d: None)
    monkeypatch.setattr(gh, "_import_resources", lambda d, t, raw_logs=None: None)
    monkeypatch.setattr(gh.shutil, "rmtree", lambda *a, **k: None)
    seen = {}

    def spec_body(dst, spec, frames, timeout, **kwargs):
        seen.update(timeout=timeout, frames=frames)
        return {"passed": True}

    monkeypatch.setattr(gh, "_playtest_spec", spec_body)
    gh.playtest_project(str(tmp_path), frames=7,
                        spec={"scenarios": [{"name": "s", "timeline": []}]},
                        timeout=345)
    assert seen == {"timeout": 345, "frames": 7}


def test_playtest_spec_forwards_its_inner_params(monkeypatch, tmp_path):
    """`_playtest_spec_inner` must still see `raw_logs` and `user_dir_name`:
    the wrapper owns the total-cleanup guarantee and forwards them."""
    gh = _load(tmp_path, monkeypatch)
    # The REAL function's reference and signature are captured IMMEDIATELY
    # after _load and BEFORE monkeypatch.setattr: once the stub is installed
    # the module attribute IS the stub, and a signature read then would only
    # describe the stub itself — an assertion that could never fail and
    # therefore proved nothing. All signature/optional-corr assertions run
    # against the captured original; the forwarding assertions below keep
    # using the captured call.
    real_inner = gh._playtest_spec_inner
    params = inspect.signature(real_inner).parameters
    assert "raw_logs" in params and "user_dir_name" in params
    assert "corr" in params and params["corr"].default is None
    captured = {}

    def inner(dst, spec, frames, timeout, **kwargs):
        captured.update(kwargs)
        return {"passed": True}

    monkeypatch.setattr(gh, "_playtest_spec_inner", inner)
    logs = gh._InvocationLogs([], []) if hasattr(gh, "_InvocationLogs") else []
    gh._playtest_spec(tmp_path / "proj", {"scenarios": []}, 5, 120,
                      ledger={}, cap_limit=4, raw_logs=logs,
                      user_dir_name="P")
    for key in ("ledger", "cap_limit", "raw_logs", "user_dir_name",
                "retention_cell", "scen_homes", "scen_labels"):
        assert key in captured, key
    assert captured["user_dir_name"] == "P"


# ── raw-only: explicit empty mapping vs omitted key ─────────────────────────

def test_explicit_empty_retain_mapping_is_raw_only_but_omitted_is_not(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    assert gh._retain_requested({"retain": {}}) is True
    assert gh._retain_requested({"retain": {"files": []}}) is True
    assert gh._retain_requested({"retain": {"patterns": []}}) is True
    assert gh._retain_requested({}) is False
    assert gh._retain_requested({"retain": None}) is False
    assert gh._retain_requested({"retain": "nope"}) is False
    # A non-mapping retain with no files/patterns normalises to an empty
    # declaration, not a raw-only request.
    assert gh._retain_declared({"retain": {}}) == ([], [])
    assert gh._retain_declared({}) == ([], [])


# ── literal saves/profile paths are refused BEFORE any home is touched ──────

def test_literal_saves_and_profile_paths_are_refused_before_any_effect(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    for bad in ("saves/save_1.json", "saved_games/slot.json",
                "profile/player.json", "profiles/p.json",
                "user://saves/save_1.json", "nested/profile/p.png"):
        accepted, refused = gh._retain_relpaths([bad])
        assert accepted == [], bad
        assert refused and "saves/profile" in refused[0], (bad, refused)
    # A HOME that holds nothing is not touched by the refused declaration.
    home = tmp_path / "home"
    home.mkdir()
    r = gh._retain_copy(["saves/save_1.json"], [home], [], {})
    assert r["ok"] is False and r["files"] == []
    assert any("saves/profile" in x for x in r["refused"])


# ── a literal and a pattern selecting the same pass artifact are taken once ─

def test_literal_and_pattern_overlap_is_copied_once_per_pass(monkeypatch, tmp_path):
    """The literal `reports/summary.json` and the pattern `reports/*.json`
    select the SAME artifact of the SAME pass. It must appear once (one row,
    one byte count); the same relative path from a DISTINCT pass stays a
    distinct artifact under its own prefixed path."""
    gh = _load(tmp_path, monkeypatch)
    homes = []
    for i in range(2):
        h = tmp_path / ("home%d" % i)
        (h / "reports").mkdir(parents=True)
        (h / "reports" / "summary.json").write_text('{"pass": %d}' % i)
        homes.append(h)
    r = gh._retain_copy(["reports/summary.json"], homes, [], {},
                        patterns=["reports/*.json"],
                        pass_labels=[{"script": "a"}, {"script": "b"}],
                        requested=True)
    assert r["ok"] is True, r
    assert len(r["files"]) == 2, r["files"]
    assert sorted(row["pass"] for row in r["files"]) == [0, 1]
    root = Path(r["retained_dir"])
    first = (root / r["files"][0]["path"]).read_text()
    second = (root / r["files"][1]["path"]).read_text()
    assert {first, second} == {'{"pass": 0}', '{"pass": 1}'}


# ── the pattern walk charges the budget BEFORE collecting names ─────────────

def test_pattern_walk_enumerates_with_scandir_and_bounds_before_sorting():
    """A wide directory must cost at most the declared search budget, never one
    allocation of every name in it. The walk therefore enumerates lazily and
    charges each entry against the budget before any list is sorted."""
    src = _HARNESS.read_text(encoding="utf-8")
    start = src.index("def _retain_walk_pattern(")
    end = src.index("\ndef _sha256_file", start)
    body = src[start:end]
    assert "os.scandir(" in body, "the walk must enumerate with os.scandir"
    assert "os.listdir(" not in body, (
        "listing every name before the budget check allocates foreign entries")
    assert "for entry in scanner" in body
    assert 'budget["entries"] += 1' in body, (
        "each enumerated entry must be charged against the search budget")


# ── playtest_project keeps the original 97b5 positional order ───────────────

def test_playtest_project_keeps_the_original_positional_order():
    """Callers written against 97b5 pass positionally
    `(project_dir, frames, input_action, spec, timeout, captures)`. The
    retention parameters were appended AFTER them — never by moving an
    existing position, which would silently reinterpret an old call."""
    spec = importlib.util.spec_from_file_location("gh_retention_order", _HARNESS)
    gh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gh)
    names = list(inspect.signature(gh.playtest_project).parameters)
    assert names[:6] == ["project_dir", "frames", "input_action",
                         "spec", "timeout", "captures"], names
    assert inspect.signature(gh.playtest_project).parameters["timeout"].default == 120


# ── corr=None is normalised to {} inside _playtest_spec_inner ───────────────

def test_playtest_spec_inner_normalises_a_none_corr(monkeypatch, tmp_path):
    """`corr` is optional and internal callers pass None. The retention
    expansion `{**corr, "mode": ...}` must therefore see a mapping, never
    None — while the HTTP handler's server-mandated mapping survives."""
    gh = _load(tmp_path, monkeypatch)
    seen = {}
    result = {"ok": True, "files": [], "refused": [], "missing": [],
              "limit_hit": None, "manifest": None}

    def fake_retain_copy(declared, homes, raw_logs, corr, **kwargs):
        seen.update(corr=dict(corr))
        return dict(result)

    monkeypatch.setattr(gh, "_retain_copy", fake_retain_copy)
    # A direct private call supplies the REAL wrapper-owned cell, exactly as
    # `_playtest_spec` does — the inner body records the retention it ran into
    # that cell, so a caller passing none would crash at the assignment.
    cell = {}
    report = gh._playtest_spec_inner(
        tmp_path / "dst", {"scenarios": []}, 5, 120, corr=None,
        retain_requested=True, retention_cell=cell)
    # No scenario ran, so the HARD verdict stays False even though retention
    # itself succeeded — that is the point: the None corr did not crash the
    # retention expansion on the way out.
    assert report["passed"] is False
    # The retention expansion received the EMPTY caller corr plus the
    # INTENTIONAL mode=headless marker added by the inner body itself.
    assert seen["corr"] == {"mode": "headless"}, (
        "a None corr must reach retention as {} plus the intentional "
        "mode=headless")
    assert cell == {"value": result}, (
        "the inner body must record the retention it ran in the caller-owned "
        "cell")

    # The server-mandated mapping is preserved, not rebuilt from scratch, and
    # the same intentional mode marker rides on top of it.
    seen.clear()
    server_corr = {"project_id": "p", "run_id": "r", "operation_id": "o"}
    cell2 = {}
    gh._playtest_spec_inner(tmp_path / "dst", {"scenarios": []}, 5, 120,
                            corr=server_corr, retain_requested=True,
                            retention_cell=cell2)
    assert seen["corr"] == {**server_corr, "mode": "headless"}
    assert cell2 == {"value": result}



# ── the _Handler validates literal AND pattern retention BEFORE effects ─────

def test_handler_refuses_bad_retention_before_any_owner_or_effect(monkeypatch, tmp_path):
    """A /script or /playtest request whose LITERAL or PATTERN retention
    declaration is invalid gets a 400 with zero effects: no render owner is
    admitted, no project is copied, imported, validated or run."""
    import http.client
    import threading

    gh = _load(tmp_path, monkeypatch)
    for name in ("acquire_render_owner_waiting", "_validate_script_selection",
                 "run_script", "playtest_project", "_copy_project",
                 "_import_resources"):
        def _boom(*a, _name=name, **k):
            raise AssertionError("%s must not be reached on a 400" % _name)
        monkeypatch.setattr(gh, name, _boom)

    server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        for route in ("/script", "/playtest"):
            for bad_retain in ({"files": ["../escape.json"]},
                               {"files": ["/etc/passwd.json"]},
                               {"files": ["saves/save_1.json"]},
                               {"files": ["notes.txt"]},
                               {"patterns": ["**/x.json"]},
                               {"patterns": ["*.json"]}):
                body = json.dumps({"project_dir": str(tmp_path),
                                   "retain": bad_retain})
                conn.request("POST", route, body,
                             {"Content-Type": "application/json"})
                resp = conn.getresponse()
                payload = json.loads(resp.read())
                assert resp.status == 400, (route, bad_retain, resp.status, payload)
                assert "error" in payload
        conn.close()
    finally:
        server.shutdown()
        server.server_close()


# ── the pattern walk rejects excluded subtrees case-insensitively, any depth ─

def test_pattern_walk_skips_uppercase_and_mixed_case_saves_profile_subtrees(
        monkeypatch, tmp_path):
    """Literal declarations reject the saves/profile subtrees on lowercased
    components, but the pattern walk compared matched names CASE-SENSITIVELY,
    so a wildcard pattern like `S*/*.json` (literal first prefix `S`) still
    traversed `Saves/`, `Saved_Games/`, `Profile/` and `Profiles/` and copied
    saved games and player profiles out of the invocation-owned home. The walk
    must normalise each matched component at EVERY traversal depth, BEFORE
    opening it, while a valid `Reports/` twin is still retained."""
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    for d in ("Saves", "Saved_Games", "Profile", "Profiles", "Reports"):
        (home / d).mkdir(parents=True)
    (home / "Saves" / "slot.json").write_text('{"save": 1}')
    (home / "Saved_Games" / "slot.json").write_text('{"save": 2}')
    (home / "Profile" / "player.json").write_text('{"profile": 3}')
    (home / "Profiles" / "settings.json").write_text('{"profile": 4}')
    (home / "Reports" / "summary.json").write_text('{"pass": 1}')

    r = gh._retain_copy([], [home], [], {},
                        patterns=["S*/*.json", "P*/*.json", "Re*/*.json"],
                        requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["S*/*.json", "P*/*.json"], r
    assert r["refused"] == [] and r["limit_hit"] is None, r
    assert [row["path"] for row in r["files"]] == ["Reports/summary.json"], r
    retained = "\n".join(
        (Path(r["retained_dir"]) / row["path"]).read_text()
        for row in r["files"])
    assert "save" not in retained and "profile" not in retained, retained
    manifest = json.loads((Path(r["retained_dir"]) / "manifest.json").read_text())
    assert manifest["ok"] is False
    assert manifest["missing"] == ["S*/*.json", "P*/*.json"]
    assert manifest["refused"] == [] and manifest["limit_hit"] is None
    assert manifest["patterns"] == ["S*/*.json", "P*/*.json", "Re*/*.json"]
    assert manifest["files"] == r["files"]


def test_pattern_walk_skips_an_excluded_directory_at_an_inner_depth(
        monkeypatch, tmp_path):
    """The case-insensitive exclusion applies at every traversal depth, not
    only the last: a mixed-case excluded directory BETWEEN two wildcarded
    components must never be opened, while its valid sibling is."""
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / "Reports" / "Saves").mkdir(parents=True)
    (home / "Reports" / "sub").mkdir(parents=True)
    (home / "Reports" / "Saves" / "deep.json").write_text('{"save": 9}')
    (home / "Reports" / "sub" / "deep.json").write_text('{"pass": 2}')

    r = gh._retain_copy([], [home], [], {},
                        patterns=["Re*/Sav*/deep.json", "Re*/sub/deep.json"],
                        requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["Re*/Sav*/deep.json"], r
    assert r["refused"] == [] and r["limit_hit"] is None, r
    assert [row["path"] for row in r["files"]] == ["Reports/sub/deep.json"], r
    manifest = json.loads((Path(r["retained_dir"]) / "manifest.json").read_text())
    assert manifest["ok"] is False
    assert manifest["missing"] == ["Re*/Sav*/deep.json"]
    assert manifest["refused"] == [] and manifest["limit_hit"] is None
    assert manifest["patterns"] == ["Re*/Sav*/deep.json", "Re*/sub/deep.json"]
    assert manifest["files"] == r["files"]


def test_literal_paths_reject_excluded_subtree_names_in_any_case(
        monkeypatch, tmp_path):
    """Literal consistency: the same normalisation applies to the literal
    declaration path — uppercase and mixed-case `Saves`/`Profiles` components
    are refused exactly like the lowercase spelling, while a valid `Reports`
    path in the same declaration is accepted."""
    gh = _load(tmp_path, monkeypatch)
    accepted, refused = gh._retain_relpaths(
        ["Saves/slot.json", "PROFILES/settings.json",
         "Reports/summary.json"])
    assert accepted == ["Reports/summary.json"], (accepted, refused)
    assert len(refused) == 2, refused
    assert all("saves/profile" in x for x in refused), refused


# ── Reports/settings.json is retained positively and survives home cleanup ──

def test_reports_settings_json_retained_with_bytes_and_hash_survives_cleanup(
        monkeypatch, tmp_path):
    """A valid, explicitly selected dynamically named artifact under the
    Reports subtree is retained with POSITIVE bytes and a matching SHA256
    bound to its manifest row; the invocation-owned home is disposable, so
    after it is cleaned up the retained bytes still hash to the recorded
    digest and the manifest still stands beside them. No Saves/Profiles
    subtree — lowercase or uppercase — is ever copied into the destination."""
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / "Reports").mkdir(parents=True)
    body = b'{"frames": 42, "scenario": "s1"}'
    (home / "Reports" / "settings.json").write_bytes(body)
    # Hostile decoys: neither case of the excluded subtrees may be retained.
    (home / "saves").mkdir()
    (home / "Profiles").mkdir()
    (home / "saves" / "slot.json").write_text('{"save": 1}')
    (home / "Profiles" / "settings.json").write_text('{"profile": 2}')

    r = gh._retain_copy(["Reports/settings.json"], [home], [], {},
                        pass_labels=[{"script": "a"}], requested=True)
    assert r["ok"] is True, r
    assert r["missing"] == [] and r["refused"] == [], r
    assert len(r["files"]) == 1, r["files"]
    row = r["files"][0]
    assert row["path"] == "Reports/settings.json", row
    assert row["source"] == "user://" and row["user_path"] == "user://Reports/settings.json"
    assert row["size"] == len(body) and row["size"] > 0, row
    import hashlib
    assert row["sha256"] == hashlib.sha256(body).hexdigest(), row

    retained = Path(r["retained_dir"])
    kept = (retained / row["path"]).read_bytes()
    assert kept == body, kept
    assert hashlib.sha256(kept).hexdigest() == row["sha256"]
    manifest = json.loads((retained / "manifest.json").read_text())
    assert manifest["ok"] is True and manifest["invocation_id"] == r["invocation_id"]
    assert manifest["declared"] == ["Reports/settings.json"]
    assert len(manifest["files"]) == 1
    assert manifest["files"][0]["sha256"] == row["sha256"]

    # The home is the invocation's disposable user directory: clean it up and
    # the retained evidence (bytes, hash, manifest) must survive unchanged.
    import shutil
    shutil.rmtree(home)
    assert not home.exists()
    assert (retained / row["path"]).read_bytes() == body
    assert hashlib.sha256((retained / row["path"]).read_bytes()).hexdigest() == row["sha256"]
    assert json.loads((retained / "manifest.json").read_text())["ok"] is True
    # Nothing from the excluded subtrees ever reached the destination, in any
    # case spelling, and the manifest lists exactly the one selected file.
    present = sorted(str(p.relative_to(retained)) for p in retained.rglob("*")
                     if p.is_file())
    assert present == ["Reports/settings.json", "manifest.json"], present
    joined = "\n".join(present)
    assert "saves" not in joined.lower() and "profile" not in joined.lower()


# ── declared-missing / refused retention is a truthful hard failure ─────────

def test_declared_missing_or_refused_retention_hard_fails_with_exact_statuses(
        monkeypatch, tmp_path):
    """An explicitly requested retention that cannot deliver what was declared
    is a hard failure: ok is False, the missing rel/pattern is named in
    `missing` and nothing else, the refused declaration is named in `refused`
    and nothing else, and no result is ever reported as ok=True for a
    declared-missing selector. The manifest records the same truthful
    statuses, and no excluded subtrees are opened even on failure."""
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / "Reports").mkdir(parents=True)
    (home / "Reports" / "present.json").write_text('{"pass": 1}')

    # A declared literal that exists nowhere in the user root is MISSING.
    r = gh._retain_copy(["Reports/absent.json"], [home], [], {},
                        pass_labels=[{"script": "a"}], requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["Reports/absent.json"], r
    assert r["refused"] == [] and r["files"] == [] and r["limit_hit"] is None, r
    manifest = json.loads((Path(r["retained_dir"]) / "manifest.json").read_text())
    assert manifest["ok"] is False and manifest["missing"] == ["Reports/absent.json"]
    assert manifest["refused"] == []

    # A refused declaration is REFUSED, never silently treated as missing.
    for bad in ("notes.txt", "saves/save_1.json", "Profiles/settings.json"):
        r = gh._retain_copy([bad], [home], [], {},
                            pass_labels=[{"script": "a"}], requested=True)
        assert r["ok"] is False, (bad, r)
        assert r["missing"] == [], (bad, r)
        assert len(r["refused"]) == 1, (bad, r)
        assert bad in r["refused"][0], (bad, r)

    # A pattern matched by no entry under the user root is MISSING too.
    r = gh._retain_copy([], [home], [], {}, patterns=["Reports/*.png"],
                        pass_labels=[{"script": "a"}], requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["Reports/*.png"], r
    assert r["refused"] == [], r

    # A present declared file alongside a missing one: the present file is
    # still taken, and the mixed declaration still hard-fails on the missing
    # one — partial success never papers over the gap.
    r = gh._retain_copy(["Reports/present.json", "Reports/absent.json"],
                        [home], [], {}, pass_labels=[{"script": "a"}],
                        requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["Reports/absent.json"], r
    assert [row["path"] for row in r["files"]] == ["Reports/present.json"], r


# ── a SELECTED pattern artifact that vanished before copy is a hard failure ──
#
# The independent source audit found this control-flow gap: the pattern walk
# matched a file and set `matched_anywhere`, then closed discovery; if the file
# was gone before `_retain_take` opened it, the `absent` outcome was IGNORED and
# the pattern still reported as a green "taken". A selected-but-undeliverable
# artifact must be named missing, exactly like a literal, with no retry, reopen
# or path fallback that could resurrect a stale name.

def test_pattern_match_that_vanishes_before_copy_is_missing_not_green(
        monkeypatch, tmp_path):
    """The walk SELECTS a regular artifact; if it is gone before the pinned
    copy opens it, the selected rel is named in `missing` and ok is False. It
    is never folded back into the "matched nothing" outcome, no file row is
    fabricated, and no reopen/fallback path can copy a stale name."""
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / "runs" / "a-1").mkdir(parents=True)
    target = home / "runs" / "a-1" / "report.json"
    target.write_text('{"run": 1}')
    real_walk = gh._retain_walk_pattern

    def walk_then_remove(root_fd, pattern, budget):
        matches, refused = real_walk(root_fd, pattern, budget)
        assert matches == ["runs/a-1/report.json"], matches
        target.unlink()  # selected by the walk, gone before the pinned copy
        return matches, refused

    monkeypatch.setattr(gh, "_retain_walk_pattern", walk_then_remove)
    r = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"],
                        requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["runs/a-1/report.json"], r
    assert r["files"] == [], r
    assert r["refused"] == [] and r["limit_hit"] is None, r
    manifest = json.loads((Path(r["retained_dir"]) / "manifest.json").read_text())
    assert manifest["ok"] is False
    assert manifest["missing"] == ["runs/a-1/report.json"]
    assert manifest["files"] == []
    # The destination is the invocation's own; the vanished source is gone and
    # nothing foreign was created or copied.
    assert not target.exists()


def test_pattern_matching_nothing_stays_the_zero_match_outcome(
        monkeypatch, tmp_path):
    """Control for the case above: when the walk selects NOTHING, the PATTERN
    itself is the missing entry (the historic, unchanged semantics) — distinct
    from a selected-then-lost rel, so the two outcomes stay truthful."""
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    (home / "other").mkdir(parents=True)
    (home / "other" / "report.json").write_text("{}")
    r = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"],
                        requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["runs/*/report.json"], r
    assert r["files"] == [] and r["refused"] == [], r


# ── an unallocatable destination never fabricates raw rows ───────────────────

def test_unallocatable_destination_reports_no_fabricated_raw_rows(
        monkeypatch, tmp_path):
    """If the owned destination cannot be allocated, the raw streams were
    copied NOWHERE: reporting file rows for them would be a fabricated green.
    The allocation failure alone makes ok False and the manifest empty."""
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "a.json").write_text("{}")

    def boom(base):
        raise OSError("AUTHOR destination unavailable")

    monkeypatch.setattr(gh, "_retain_new_invocation_dir", boom)
    r = gh._retain_copy([], [home],
                        [{"label": "t.gd", "stdout": "PASS\n", "stderr": ""}],
                        {}, requested=True)
    assert r["ok"] is False, r
    assert r["raw_logs"] == [], r
    assert r["files"] == [] and r["manifest"] == "", r
    assert any("AUTHOR destination unavailable" in x for x in r["refused"]), r


# ── an empty valid project + EXPLICIT retention is a truthful manifest ──────
#
# The second audit gap: `/script` discovery finding no admitted entry returned
# `passed: True` with no retention, even when the request explicitly asked for
# it. An explicit request must always get a bounded, truthful manifest; an
# omitted `retain` keeps the historic no-entry skip unchanged.

def _empty_project(tmp_path):
    (tmp_path / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="EmptyProject"\n')


def test_empty_project_explicit_raw_only_retention_writes_a_truthful_manifest(
        monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    _empty_project(tmp_path)
    r = gh.run_script(str(tmp_path), [], timeout=30, retain=[],
                      retain_requested=True)
    # The raw copy succeeded, but zero executable suites ran: retention
    # success is evidence, never a test PASS — the SCRIPT verdict fails.
    assert r["passed"] is False, r
    assert r["executed_suites"] == 0, r
    assert "FAILED" in r["summary"] and "zero authored tests" in r["summary"], r
    assert r["discovered"] == [] and r["results"] == [], r
    retention = r["retention"]
    assert retention["ok"] is True, retention
    assert retention["files"] == [] and retention["missing"] == [], retention
    assert retention["refused"] == [] and retention["limit_hit"] is None, retention
    manifest_path = Path(retention["manifest"])
    assert manifest_path.is_file()
    doc = json.loads(manifest_path.read_text())
    assert doc["ok"] is True and doc["declared"] == [] and doc["files"] == []
    assert doc["missing"] == [] and doc["refused"] == []


def test_empty_project_explicit_declaration_no_pass_can_satisfy_hard_fails(
        monkeypatch, tmp_path):
    """A zero-pass request that still DECLARES a literal no pass can produce is
    a truthful hard failure, not a green: the missing rel is named and the
    manifest records it."""
    gh = _load(tmp_path, monkeypatch)
    _empty_project(tmp_path)
    r = gh.run_script(str(tmp_path), [], timeout=30,
                      retain=["reports/import.json"])
    assert r["passed"] is False, r
    assert r["retention"]["missing"] == ["reports/import.json"], r["retention"]
    doc = json.loads(Path(r["retention"]["manifest"]).read_text())
    assert doc["ok"] is False and doc["missing"] == ["reports/import.json"]

    # The same zero-pass request with a bounded PATTERN is missing by pattern.
    r = gh.run_script(str(tmp_path), [], timeout=30, retain_patterns=["Reports/*.png"])
    assert r["passed"] is False, r
    assert r["retention"]["missing"] == ["Reports/*.png"], r["retention"]
    assert r["retention"]["files"] == [] and r["retention"]["refused"] == [], r

    # A declaration whose SHAPE is refused still hard-fails on the empty
    # project: refusal is named in `refused`, never silently treated as a
    # zero-pass success.
    r = gh.run_script(str(tmp_path), [], timeout=30, retain=["notes.txt"])
    assert r["passed"] is False, r
    assert r["retention"]["missing"] == [], r["retention"]
    assert len(r["retention"]["refused"]) == 1, r["retention"]
    assert "notes.txt" in r["retention"]["refused"][0], r["retention"]

def test_empty_project_without_retention_is_a_failed_verdict_and_no_retention(
        monkeypatch, tmp_path):
    """An OMITTED `retain` key is not a retention request: no `retention` key
    appears, but the zero-entry verdict is still a failed SCRIPT verdict — the
    historic headless green is gone, because zero authored tests executed."""
    gh = _load(tmp_path, monkeypatch)
    _empty_project(tmp_path)
    r = gh.run_script(str(tmp_path), [], timeout=30)
    assert r["passed"] is False and r["discovered"] == [], r
    assert r["executed_suites"] == 0 and "FAILED" in r["summary"], r
    assert "retention" not in r, r



def test_empty_project_render_with_explicit_retention_stays_a_hard_failure(
        monkeypatch, tmp_path):
    """Retention never rescues an opt-in render with nothing to render: the
    render stays a HARD failure while the explicit retention still records its
    bounded manifest."""
    gh = _load(tmp_path, monkeypatch)
    _empty_project(tmp_path)
    r = gh.run_script(str(tmp_path), [], timeout=30, render=True,
                      retain_requested=True)
    assert r["passed"] is False, r
    assert r["render_mode"] == "render" and r["render_requested"] is True, r
    assert "nothing to render" in r["summary"] and "FAILED" in r["summary"], r
    assert r["retention"]["ok"] is True, r["retention"]


def test_empty_project_retention_is_owned_immutable_and_leaves_foreign_state(
        monkeypatch, tmp_path):
    """The empty-project retention allocates a unique server-owned invocation
    directory under the declared evidence root, keeps prior invocations byte
    for byte, leaves a foreign sentinel untouched, leaks no descriptor and
    leaves no throwaway home behind."""
    import os
    gh = _load(tmp_path, monkeypatch)
    _empty_project(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "keep.txt"
    sentinel.write_text("FOREIGN-SENTINEL")
    before_fds = len(os.listdir("/proc/self/fd"))
    first = gh.run_script(str(tmp_path), [], timeout=30, retain_requested=True)
    first_manifest = Path(first["retention"]["manifest"])
    first_bytes = first_manifest.read_bytes()
    second = gh.run_script(str(tmp_path), [], timeout=30, retain_requested=True)
    assert len(os.listdir("/proc/self/fd")) == before_fds, "descriptor leak"
    assert (first["retention"]["retained_dir"]
            != second["retention"]["retained_dir"])
    assert (first["retention"]["invocation_id"]
            != second["retention"]["invocation_id"])
    # Prior invocation is immutable: its manifest bytes are unchanged.
    assert first_manifest.read_bytes() == first_bytes
    assert json.loads(first_manifest.read_text())["ok"] is True
    # Foreign state is untouched.
    assert sentinel.read_text() == "FOREIGN-SENTINEL"
    # The owned destination lives under the declared server evidence root.
    root = gh._retain_root() / "evidence"
    assert Path(first["retention"]["retained_dir"]).parent == root
    assert Path(second["retention"]["retained_dir"]).parent == root


# ── the real HTTP route: an empty project with an explicit request ──────────

def test_http_route_empty_project_explicit_retention_gets_a_manifest(
        monkeypatch, tmp_path):
    """Through the REAL _Handler, an empty-but-valid project with an explicit
    `{"retain": {}}` still reaches run_script, allocates the owned destination
    and returns 200 with a truthful raw-only manifest; the same project without
    a `retain` key keeps the historic no-retention skip (no `retention` key)."""
    import http.client
    import threading
    gh = _load(tmp_path, monkeypatch)
    _empty_project(tmp_path)
    runs = []
    real_run_script = gh.run_script

    def spy(project_dir, scripts, **kwargs):
        runs.append(kwargs)
        return real_run_script(project_dir, scripts, **kwargs)

    monkeypatch.setattr(gh, "run_script", spy)
    server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                          timeout=30)
        conn.request("POST", "/script",
                     json.dumps({"project_dir": str(tmp_path), "retain": {}}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert resp.status == 200, body
        assert runs and runs[-1]["retain_requested"] is True, runs
        assert body["passed"] is False, body
        assert body["retention"]["ok"] is True, body
        assert Path(body["retention"]["manifest"]).is_file(), body

        conn.request("POST", "/script",
                     json.dumps({"project_dir": str(tmp_path)}),
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        assert resp.status == 200 and "retention" not in body, body
        assert body["passed"] is False, body
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
