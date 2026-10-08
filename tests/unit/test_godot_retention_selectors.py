"""Normal-gate retention: dynamic-name selectors, raw-only requests, sizing.

SOURCE-only tests: no Godot, no engine, no network beyond the harness' own
stdlib HTTP server on 127.0.0.1. `_run` — the one seam that would shell out to
the engine — is stubbed; everything else (real temp filesystem, real SHA256 of
copied bytes, the pinned-descriptor copy path) runs for real.

These hold the whole-gate contract added on top of the literal `files` retain:

  * SELECTORS. `retain.patterns` names dynamic artifacts (a ticks-suffixed
    report, an own PNG) with an anchored literal first-directory prefix, no
    recursive/root-wide wildcard, no saves/profile subtree and a fixed depth; a
    pattern matching nothing is reported missing, never a silent green.

  * RAW-ONLY. `{"retain": {"files": []}}` is an explicit request that keeps the
    full streams and no artifacts; an omitted `retain` key changes nothing.

  * WHOLE-GATE SIZING. A 69-script gate emits >= 140 raw streams (69 entry
    points + the import pass, each stdout+stderr) and a 221-scenario playtest
    >= 442, all inside the server-configured finite quota.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path


_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    monkeypatch.setenv("GODOT_DEPLOYMENT_LOCK", str(tmp_path / "deployment.lock"))
    monkeypatch.setenv("GODOT_RENDER_EFFECT_LOCK", str(tmp_path / "effect.lock"))
    monkeypatch.setenv("GODOT_EVIDENCE_ROOT", str(tmp_path / "evidence-root"))
    spec = importlib.util.spec_from_file_location("gh_retention_selectors", _HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RENDER_OWNER_WAIT_POLL_SEC = 0.02
    return module


def _home_with(tmp_path, files, name="home"):
    home = tmp_path / name
    for rel, body in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode())
    return home


# ── selector syntax (pure, no I/O) ──────────────────────────────────────────

def test_selector_normalisation_is_shape_checked(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    pats, errors = gh._retain_selectors({"retain": {"patterns": ["a-*/b.json"]}})
    assert pats == ["a-*/b.json"] and errors == []
    assert gh._retain_selectors({}) == ([], [])
    pats, errors = gh._retain_selectors({"retain": {"patterns": "a.json"}})
    assert pats == [] and errors
    pats, errors = gh._retain_selectors({"retain": {"patterns": [7]}})
    assert pats == [] and errors


def test_pattern_syntax_refuses_recursive_rootwide_and_unsafe_shapes(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    good, refused = gh._retain_patterns(
        ["monthly-journey-actual-*/runtime.json",
         "user://frames/f?.png",
         "runs/2026-*/*.json"])
    assert good == ["monthly-journey-actual-*/runtime.json",
                    "frames/f?.png", "runs/2026-*/*.json"]
    assert refused == []

    good, refused = gh._retain_patterns(
        ["**/runtime.json",          # recursive wildcard
         "*/x.json",                 # root-wide, no literal first dir
         "/etc/*.json",              # absolute
         "../*/x.json",              # traversal
         "saves/*.json",             # excluded subtree
         "profile/*.png",            # excluded subtree
         "dir/only",                 # no allowed suffix
         "just-one-part.json",       # no directory component
         ""])
    assert good == []
    assert len(refused) == 9
    assert any("recursive" in r for r in refused)
    assert any("literal prefix" in r for r in refused)
    assert any("saves/profile" in r for r in refused)
    assert any("only" in r for r in refused)


def test_pattern_count_is_bounded(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(gh, "_RETAIN_MAX_PATTERNS", 1)
    good, refused = gh._retain_patterns(["a-*/r.json", "b-*/r.json"])
    assert good == ["a-*/r.json"]
    assert refused and "more than 1" in refused[0]


# ── selector resolution on the real filesystem ─────────────────────────────

def test_pattern_retains_every_dynamic_match_sorted_and_deduplicated(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {
        "monthly-journey-actual-1700000000/runtime.json": '{"run": 1}',
        "monthly-journey-actual-1700000123/runtime.json": '{"run": 2}',
        "monthly-journey-actual-1700000123/other.json": "{}",   # not selected
    })
    r = gh._retain_copy([], [home], [], {},
                        patterns=["monthly-journey-actual-*/runtime.json"],
                        requested=True)
    assert r["ok"] and r["refused"] == [] and r["missing"] == []
    retained = Path(r["retained_dir"])
    kept = sorted(row["user_path"] for row in r["files"])
    assert kept == ["user://monthly-journey-actual-1700000000/runtime.json",
                    "user://monthly-journey-actual-1700000123/runtime.json"]
    for row in r["files"]:
        rel = row["user_path"][len("user://"):]
        blob = (retained / row["path"]).read_bytes()
        assert row["size"] == len(blob)
        assert row["sha256"] == hashlib.sha256(blob).hexdigest()
    assert json.loads(Path(r["manifest"]).read_text())["patterns"] == \
        ["monthly-journey-actual-*/runtime.json"]


def test_pattern_that_matches_nothing_is_missing_not_green(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"other/runtime.json": "{}"})
    r = gh._retain_copy([], [home], [], {},
                        patterns=["monthly-journey-actual-*/runtime.json"],
                        requested=True)
    assert r["ok"] is False and r["files"] == []
    assert r["missing"] == ["monthly-journey-actual-*/runtime.json"]


def test_pattern_never_descends_into_saves_or_follows_symlinks(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {
        "runs/a-1/report.json": "{}",
        "runs/saves/a-2/report.json": "{}",     # nested excluded name
    })
    outside = tmp_path / "FOREIGN.json"
    outside.write_text("secret")
    (home / "runs" / "a-3").mkdir()
    (home / "runs" / "a-3" / "report.json").symlink_to(outside)
    r = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"],
                        requested=True)
    kept = sorted(row["user_path"] for row in r["files"])
    assert kept == ["user://runs/a-1/report.json"]
    assert outside.read_text() == "secret"
    assert r["ok"] is False and r["refused"]      # the symlink is refused, not copied
    assert (home / "runs" / "a-3" / "report.json").is_symlink()


def test_pattern_search_budget_is_finite(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(gh, "_RETAIN_MAX_SEARCH_ENTRIES", 1)
    home = _home_with(tmp_path, {"a-1/r.json": "{}", "a-2/r.json": "{}"})
    r = gh._retain_copy([], [home], [], {}, patterns=["a-*/r.json"],
                        requested=True)
    assert r["ok"] is False
    assert any("search exceeded" in x for x in r["refused"])


def test_pattern_refuses_fifo_and_non_regular_matches(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"a-1/r.json": "{}"})
    # A FIFO whose NAME matches the pattern's final component must be refused
    # without blocking: the walk opens it O_NONBLOCK and rejects a non-regular.
    os.mkfifo(home / "a-1" / "blocked.json")
    r = gh._retain_copy([], [home], [], {}, patterns=["a-1/*.json"],
                        requested=True)
    kept = sorted(row["user_path"] for row in r["files"])
    assert kept == ["user://a-1/r.json"]
    assert r["ok"] is False
    assert any("not a regular" in x for x in r["refused"])


# ── raw-only request ────────────────────────────────────────────────────────

def test_raw_only_request_keeps_streams_and_no_artifacts(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    assert gh._retain_requested({"retain": {"files": []}}) is True
    assert gh._retain_requested({"retain": {"patterns": []}}) is True
    assert gh._retain_requested({}) is False
    assert gh._retain_requested({"retain": "nope"}) is False

    home = _home_with(tmp_path, {"unrelated.json": "{}"})
    r = gh._retain_copy([], [home], [{"label": "t.gd", "stdout": "PASS\n" * 900,
                                      "stderr": ""}], {}, requested=True)
    assert r["ok"] is True and r["files"] == [] and r["missing"] == []
    retained = Path(r["retained_dir"])
    assert (retained / "raw" / "0-t.gd.stdout.log").read_text() == "PASS\n" * 900
    manifest = json.loads(Path(r["manifest"]).read_text())
    assert manifest["files"] == [] and manifest["raw_logs"]


def test_route_refuses_malformed_patterns_before_any_effect(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    (tmp_path / "project.godot").write_text('[application]\nconfig/name="P"\n')
    calls = []

    def body(*_a, **_k):
        calls.append(_k)
        return {"passed": True, "results": [], "summary": "stub"}

    monkeypatch.setattr(gh, "run_script", body)
    monkeypatch.setattr(gh, "playtest_project", body)
    server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/script" % server.server_port,
            data=json.dumps({"project_dir": str(tmp_path),
                             "retain": {"files": [], "patterns": ["**/x.json"]}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                code, body_doc = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            code, body_doc = exc.code, json.loads(exc.read())
    finally:
        server.shutdown()
        server.server_close()
    assert code == 400 and "recursive" in body_doc["error"]
    assert calls == [], "a malformed pattern must not reach the run body"


# ── whole-gate sizing ────────────────────────────────────────────────────────

def test_whole_script_gate_raw_streams_exceed_the_default_quota(monkeypatch, tmp_path):
    """69 entry points + the import pass = 140 raw streams; the default file
    quota (64) is a truthful HARD failure, and a server-configured quota holds
    every stream."""
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {})
    raw = [{"label": "import", "stdout": "i", "stderr": ""}]
    raw += [{"label": "t%d.gd" % i, "stdout": "PASS %d\n" % i, "stderr": ""}
            for i in range(69)]
    assert len(raw) * 2 == 140

    r = gh._retain_copy([], [home], raw, {}, requested=True)
    assert r["ok"] is False and r["limit_hit"] == "file_count"
    assert len(r["raw_logs"]) == 64

    monkeypatch.setattr(gh, "_RETAIN_MAX_FILES", 512)
    r = gh._retain_copy([], [home], raw, {}, requested=True)
    assert r["ok"] is True and r["limit_hit"] is None and len(r["raw_logs"]) == 140


def test_whole_playtest_gate_raw_streams_sized_for_221_scenarios(monkeypatch, tmp_path):
    """221 scenarios + the import pass = 444 raw streams (>= 442); a configured
    finite quota retains them all."""
    gh = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(gh, "_RETAIN_MAX_FILES", 1024)
    home = _home_with(tmp_path, {})
    raw = [{"label": "import", "stdout": "i", "stderr": ""}]
    raw += [{"label": "s%d" % i, "stdout": "x", "stderr": "y"} for i in range(221)]
    assert len(raw) * 2 == 444
    r = gh._retain_copy([], [home], raw, {}, requested=True)
    assert r["ok"] is True and len(r["raw_logs"]) == 444
