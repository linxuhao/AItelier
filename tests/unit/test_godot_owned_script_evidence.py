"""The normal Godot HTTP harness: opt-in rendered /script runs and invocation-
owned generated evidence that survives the throwaway $HOME cleanup.

SOURCE-only tests: no Godot, no engine, no network service beyond the harness'
own stdlib HTTP server on 127.0.0.1. The harness is loaded the way the other
godot-harness tests load it (importlib from the real file), and `_run` — the one
seam that would shell out to the engine — is stubbed. Everything else, including
the real temporary filesystem and real SHA256 of copied bytes, runs for real.

Two behaviours are held to their contract here:

  * ROUTING. /script stays headless unless the request opts in with
    {"render": true}; an opt-in render names the SAME admitted `extends
    SceneTree` entries and never silently falls back to the pixel-blind path.
    An invalid flag/declaration is refused before any copy/import/admission, so
    a bad request changes nothing.

  * OWNED EVIDENCE. Only the DECLARED relative user:// artifacts this invocation
    generated are copied out before its $HOME is deleted, into a unique
    server-chosen directory, with the FULL raw stdout/stderr and a locatable
    manifest (path, size, SHA256, source, correlation). A missing/refused
    artifact or an exceeded limit is explicit, never a silent green; a foreign
    path, traversal, symlink or hardlink escape is refused and left untouched.
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
    """Load the real harness with its durable control state under tmp_path."""
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    monkeypatch.setenv("GODOT_DEPLOYMENT_LOCK", str(tmp_path / "deployment.lock"))
    monkeypatch.setenv("GODOT_RENDER_EFFECT_LOCK", str(tmp_path / "effect.lock"))
    monkeypatch.setenv("GODOT_EVIDENCE_ROOT", str(tmp_path / "evidence-root"))
    spec = importlib.util.spec_from_file_location("gh_owned_evidence", _HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RENDER_OWNER_WAIT_POLL_SEC = 0.02
    return module


# ── the declared-retention contract (pure, no I/O) ─────────────────────────

def test_retain_declared_accepts_files_and_refuses_unknown_keys(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    files, errors = gh._retain_declared({"retain": {"files": ["a.json", "f.png"]}})
    assert files == ["a.json", "f.png"] and errors == []
    files, errors = gh._retain_declared({})
    assert files == [] and errors == []
    files, errors = gh._retain_declared({"retain": {"files": ["a.json"], "dir": "/etc"}})
    assert files == [] and errors and "dir" in errors[0]
    files, errors = gh._retain_declared({"retain": "nope"})
    assert files == [] and errors
    files, errors = gh._retain_declared({"retain": {"files": "a.json"}})
    assert files == [] and errors


def test_relpath_validation_refuses_absolute_traversal_and_bad_suffix(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    good, refused = gh._retain_relpaths(
        ["user://reports/summary.json", "frames/f2.png", "sub\\win.png"])
    assert good == ["reports/summary.json", "frames/f2.png", "sub/win.png"]
    assert refused == []

    good, refused = gh._retain_relpaths(
        ["/etc/passwd.json", "../escape.json", "a/../../b.json", "notes.txt", ""])
    assert good == []
    assert len(refused) == 5
    assert any("relative" in r for r in refused)
    assert any("only" in r for r in refused)


# ── real-filesystem retention ──────────────────────────────────────────────

def _home_with(tmp_path, files):
    home = tmp_path / "home"
    for rel, body in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body if isinstance(body, bytes) else body.encode())
    return home


def test_declared_artifacts_are_copied_with_manifest_and_full_raw_logs(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"reports/summary.json": '{"ok": true}',
                                 "frames/f2.png": b"\x89PNG-not-really",
                                 "not_declared.json": "{}"})
    big_stdout = "PASS test_a\n" * 2000          # deliberately > 4000 chars
    big_stderr = "engine noise\n" * 2000
    raw = [{"label": "t_a.gd", "stdout": big_stdout, "stderr": big_stderr}]

    r = gh._retain_copy(["reports/summary.json", "frames/f2.png"], [home], raw,
                        {"project_id": "p", "run_id": "r", "operation_id": "op"})

    assert r["ok"] is True and r["refused"] == [] and r["missing"] == []
    retained = Path(r["retained_dir"])
    assert (retained / "reports/summary.json").read_text() == '{"ok": true}'
    assert (retained / "frames/f2.png").read_bytes() == b"\x89PNG-not-really"
    # Only declared artifacts are copied: the undeclared sibling stays behind.
    assert not (retained / "not_declared.json").exists()

    # Each copied byte is accounted for by size and SHA256, and the manifest is
    # readable at a locatable path that is not the response body.
    by_path = {row["path"]: row for row in r["files"]}
    assert set(by_path) == {"reports/summary.json", "frames/f2.png"}
    for rel, row in by_path.items():
        blob = (retained / rel).read_bytes()
        assert row["size"] == len(blob)
        assert row["sha256"] == hashlib.sha256(blob).hexdigest()
        assert row["source"] == "user://" and row["user_path"] == "user://" + rel

    # The FULL untruncated streams survive outside the response excerpt budget.
    assert any(row["source"] == "raw_stdout" for row in r["raw_logs"])
    raw_out = retained / "raw" / "0-t_a.gd.stdout.log"
    assert raw_out.read_text() == big_stdout and len(big_stdout) > 4000

    manifest = json.loads(Path(r["manifest"]).read_text())
    assert manifest["schema"] == "godot-invocation-evidence/1"
    assert manifest["project_id"] == "p" and manifest["run_id"] == "r"
    assert manifest["operation_id"] == "op"
    assert manifest["invocation_id"] == r["invocation_id"]


def test_a_declared_artifact_that_never_appeared_is_explicitly_missing(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"reports/summary.json": "{}"})
    r = gh._retain_copy(["reports/summary.json", "frames/never.png"], [home],
                        [], {"project_id": "p"})
    assert r["ok"] is False
    assert r["missing"] == ["frames/never.png"]
    assert "never.png" in json.loads(Path(r["manifest"]).read_text())["missing"][0]


def test_foreign_path_traversal_and_symlink_are_refused_and_untouched(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"ok.json": "{}"})
    outside = tmp_path / "FOREIGN.json"
    outside.write_text("secret")
    # A symlink inside user:// pointing at a foreign file must not be followed.
    (home / "link.json").symlink_to(outside)

    r = gh._retain_copy(["../FOREIGN.json", "/etc/hosts.json",
                         "link.json", "ok.json"], [home], [], {})

    assert r["ok"] is False
    refused = " ".join(r["refused"])
    assert "FOREIGN.json" in refused and "link.json" in refused
    assert outside.read_text() == "secret"          # never copied or altered
    assert (home / "link.json").is_symlink()        # left exactly as it was
    # The one legitimate artifact still got through.
    assert [row["path"] for row in r["files"]] == ["ok.json"]


def test_the_file_and_byte_limits_are_explicit(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(gh, "_RETAIN_MAX_FILES", 1)
    home = _home_with(tmp_path, {"a.json": "{}", "b.json": "{}"})
    r = gh._retain_copy(["a.json", "b.json"], [home], [], {})
    assert r["ok"] is False and r["limit_hit"] == "file_count"
    assert len(r["files"]) == 1

    monkeypatch.setattr(gh, "_RETAIN_MAX_FILES", 64)
    monkeypatch.setattr(gh, "_RETAIN_MAX_BYTES", 1)
    r = gh._retain_copy(["a.json"], [home], [], {})
    assert r["ok"] is False and r["limit_hit"] == "total_bytes"


def test_two_invocations_do_not_overwrite_each_other(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"a.json": "first"})
    one = gh._retain_copy(["a.json"], [home], [], {})
    (home / "a.json").write_text("second")
    two = gh._retain_copy(["a.json"], [home], [], {})
    assert one["retained_dir"] != two["retained_dir"]
    assert one["invocation_id"] != two["invocation_id"]
    assert Path(one["retained_dir"], "a.json").read_text() == "first"
    assert Path(two["retained_dir"], "a.json").read_text() == "second"


# ── run_script: routing, and retention that survives HOME cleanup ──────────

class _CP:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _script_project(tmp_path, entries=("t_a.gd",)):
    (tmp_path / "project.godot").write_text("[application]\n")
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    for name in entries:
        (tests / name).write_text("extends SceneTree\n")
    work = tmp_path / "work" / "proj"
    work.mkdir(parents=True)
    return work


def test_run_script_default_stays_headless(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    work = _script_project(tmp_path)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources", lambda dst, timeout=0: None)
    seen = []

    def fake_run(cmd, timeout, extra_env=None, render=False):
        seen.append(render)
        return _CP(0, "PASS all\n", "")

    monkeypatch.setattr(gh, "_run", fake_run)
    r = gh.run_script(str(tmp_path), [], timeout=30)
    assert seen == [False], "an omitted option must not render"
    assert r["render_mode"] == "headless" and r["render_requested"] is False
    assert r["passed"] is True


def test_run_script_render_true_runs_the_same_admitted_entries_in_render_mode(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    work = _script_project(tmp_path, entries=("t_a.gd", "t_b.gd"))
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources", lambda dst, timeout=0: None)
    seen = []

    def fake_run(cmd, timeout, extra_env=None, render=False):
        seen.append({"render": render, "script": cmd[-1], "home": extra_env["HOME"]})
        return _CP(0, "PASS all\n", "")

    monkeypatch.setattr(gh, "_run", fake_run)
    r = gh.run_script(str(tmp_path), [], timeout=30, render=True)
    assert [s["render"] for s in seen] == [True, True]
    assert [s["script"] for s in seen] == ["res://tests/t_a.gd", "res://tests/t_b.gd"]
    assert r["render_mode"] == "render" and r["render_requested"] is True
    assert r["discovered"] == ["res://tests/t_a.gd", "res://tests/t_b.gd"]
    assert r["passed"] is True


def test_run_script_render_with_no_admitted_entry_is_a_failure_not_a_green(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    (tmp_path / "project.godot").write_text("[application]\n")
    r = gh.run_script(str(tmp_path), [], timeout=30, render=True)
    assert r["passed"] is False
    assert r["render_mode"] == "render" and "nothing to render" in r["summary"]


def test_run_script_retention_copies_before_the_throwaway_home_is_removed(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    work = _script_project(tmp_path)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources",
                        lambda dst, timeout=0, raw_logs=None: None)
    homes = []

    def fake_run(cmd, timeout, extra_env=None, render=False):
        # Stand in for a suite that renders a PNG and a report into user://.
        home = Path(extra_env["HOME"])
        homes.append(home)
        (home / "reports").mkdir(parents=True, exist_ok=True)
        (home / "reports" / "summary.json").write_text('{"frames": 3}')
        (home / "frames").mkdir(exist_ok=True)
        (home / "frames" / "f2.png").write_bytes(b"PNG")
        return _CP(0, "PASS all\n", "")

    monkeypatch.setattr(gh, "_run", fake_run)
    r = gh.run_script(str(tmp_path), [], timeout=30,
                      retain=["reports/summary.json", "frames/f2.png"],
                      corr={"project_id": "demo", "run_id": "run-1",
                            "operation_id": "op-1"})
    assert r["passed"] is True
    retention = r["retention"]
    assert retention["ok"] is True
    assert Path(retention["retained_dir"], "reports/summary.json").is_file()
    assert Path(retention["retained_dir"], "frames/f2.png").read_bytes() == b"PNG"
    assert retention["files"][0]["sha256"] == hashlib.sha256(b'{"frames": 3}').hexdigest()
    # The evidence was copied out AND the throwaway home is gone afterwards.
    assert homes and all(not h.exists() for h in homes)


def test_run_script_missing_declared_artifact_does_not_pass_silently(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    work = _script_project(tmp_path)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources",
                        lambda dst, timeout=0, raw_logs=None: None)
    monkeypatch.setattr(gh, "_run", lambda *a, **k: _CP(0, "PASS all\n", ""))
    r = gh.run_script(str(tmp_path), [], timeout=30,
                      retain=["frames/never.png"], corr={"project_id": "p"})
    assert r["passed"] is False
    assert r["retention"]["missing"] == ["frames/never.png"]
    assert "NOT fully retained" in r["summary"]


def test_run_script_without_retention_keeps_the_existing_signature(monkeypatch, tmp_path):
    """No retention request => no `retention` key and the raw streams are not
    collected; the plain report stays exactly what every existing reader sees."""
    gh = _load(tmp_path, monkeypatch)
    work = _script_project(tmp_path)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources",
                        lambda dst, timeout=0, raw_logs=None: None)
    monkeypatch.setattr(gh, "_run", lambda *a, **k: _CP(0, "PASS all\n", ""))
    r = gh.run_script(str(tmp_path), [], timeout=30)
    assert "retention" not in r
    assert r["render_mode"] == "headless"


# ── the HTTP route: flag/declaration validated before any effect ───────────

class _Route:
    """The real harness _Handler on 127.0.0.1, with the render body stubbed."""

    def __init__(self, gh, monkeypatch):
        self.gh = gh
        self.calls = []

        def body(*_a, **_k):
            self.calls.append(_k)
            return {"passed": True, "results": [], "summary": "stub"}

        monkeypatch.setattr(gh, "run_script", body)
        monkeypatch.setattr(gh, "playtest_project", body)
        self.server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def test_the_http_route_refuses_a_non_boolean_render_before_any_effect(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    rig = _Route(gh, monkeypatch)
    try:
        code, body = rig.post("/script", {"project_dir": str(tmp_path), "render": "yes"})
        assert code == 400 and "render must be true or false" in body["error"]
        code, body = rig.post("/playtest", {"project_dir": str(tmp_path), "render": 1})
        assert code == 400
        assert rig.calls == [], "an invalid flag must not reach the render body"
    finally:
        rig.stop()


def test_the_http_route_refuses_a_malformed_retention_before_any_effect(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    rig = _Route(gh, monkeypatch)
    try:
        code, body = rig.post("/script", {"project_dir": str(tmp_path),
                                          "retain": {"files": ["a.json"], "to": "/tmp/x"}})
        assert code == 400 and "to" in body["error"]
        assert rig.calls == []
    finally:
        rig.stop()


def test_the_http_route_passes_render_and_retention_through(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    # The route validates an explicit selection against the project's real
    # admitted SceneTree entries, so this fixture needs one.
    (tmp_path / "project.godot").write_text('[application]\nconfig/name="P"\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.gd").write_text("extends SceneTree\n")
    rig = _Route(gh, monkeypatch)
    try:
        code, body = rig.post("/script", {
            "project_dir": str(tmp_path), "scripts": ["res://tests/a.gd"],
            "render": True, "retain": {"files": ["r.json"]},
            "project_id": "demo", "run_id": "run-1", "operation_id": "op-1"})
        assert code == 200 and body["passed"] is True
        assert rig.calls and rig.calls[0]["render"] is True
        assert rig.calls[0]["retain"] == ["r.json"]
        assert rig.calls[0]["corr"]["project_id"] == "demo"
    finally:
        rig.stop()


# ── regressions aligned with the independent routing/retention controls ────

def test_selection_validation_refuses_non_admitted_entries(monkeypatch, tmp_path):
    """A non-SceneTree, missing, traversal, absolute or non-string entry is
    refused BEFORE any copy/import/engine work; a rendered request with no
    real project is refused, and the headless default discovery is untouched."""
    gh = _load(tmp_path, monkeypatch)
    (tmp_path / "project.godot").write_text('[application]\nconfig/name="P"\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.gd").write_text("extends SceneTree\n")
    (tmp_path / "tests" / "node.gd").write_text("extends Node\n")
    errs = gh._validate_script_selection(str(tmp_path), ["res://tests/a.gd"], True)
    assert errs == []
    for bad in (["res://tests/node.gd"], ["res://tests/missing.gd"],
                ["../outside.gd"], ["/tmp/foreign.gd"], [7]):
        errs = gh._validate_script_selection(str(tmp_path), bad, True)
        assert errs, bad
    errs = gh._validate_script_selection(str(tmp_path / "nope"), [], True)
    assert errs
    errs = gh._validate_script_selection(str(tmp_path), [], False)
    assert errs == []


def test_the_http_route_refuses_an_invalid_selection_before_any_effect(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    (tmp_path / "project.godot").write_text('[application]\nconfig/name="P"\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.gd").write_text("extends SceneTree\n")
    (tmp_path / "tests" / "node.gd").write_text("extends Node\n")
    rig = _Route(gh, monkeypatch)
    try:
        for bad in (["res://tests/node.gd"], ["res://tests/missing.gd"],
                    ["../outside.gd"], ["/tmp/foreign.gd"], [7]):
            code, body = rig.post("/script", {"project_dir": str(tmp_path),
                                              "scripts": bad, "render": True})
            assert code == 400 and body["error"], bad
        code, body = rig.post("/script", {"project_dir": str(tmp_path / "nope"),
                                          "render": True})
        assert code == 400
        assert rig.calls == [], "an invalid selection must not reach the body"
    finally:
        rig.stop()


def test_retention_finds_the_real_godot_app_userdata_root(monkeypatch, tmp_path):
    """Declared artifacts a suite wrote into the REAL Godot user:// location
    (app_userdata/<config/name>) are found and retained before HOME cleanup."""
    gh = _load(tmp_path, monkeypatch)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="IndependentRetentionProject"\n')
    assert gh._project_user_dir_name(proj) == "IndependentRetentionProject"
    home = tmp_path / "home"
    user = home / ".local/share/godot/app_userdata/IndependentRetentionProject"
    (user / "reports").mkdir(parents=True)
    (user / "reports/summary.json").write_text('{"proof":"invocation"}')
    (user / "frames").mkdir()
    (user / "frames/f2.png").write_bytes(b"PNG")
    r = gh._retain_copy(["reports/summary.json", "frames/f2.png"], [home], [],
                        {}, user_dir_name="IndependentRetentionProject")
    assert r["ok"] and len(r["files"]) == 2
    root = Path(r["retained_dir"])
    assert (root / "reports/summary.json").read_text() == '{"proof":"invocation"}'


def test_multi_pass_same_declared_path_keeps_every_pass_with_identity(monkeypatch, tmp_path):
    """Two scripts that each write the same declared path both survive, under
    distinct paths, each labelled with its producing pass — the FIRST home
    must not collapse the later one."""
    gh = _load(tmp_path, monkeypatch)
    homes = []
    for i, name in enumerate(("a.gd", "b.gd")):
        h = tmp_path / ("home%d" % i)
        (h / "reports").mkdir(parents=True)
        (h / "reports/summary.json").write_text('{"entry": "%s"}' % name)
        homes.append(h)
    r = gh._retain_copy(["reports/summary.json"], homes, [], {},
                        pass_labels=[{"script": "res://tests/a.gd"},
                                     {"script": "res://tests/b.gd"}])
    assert r["ok"] and len(r["files"]) == 2
    assert [row.get("script") for row in r["files"]] == \
        ["res://tests/a.gd", "res://tests/b.gd"]
    root = Path(r["retained_dir"])
    assert "a.gd" in (root / r["files"][0]["path"]).read_text()
    assert "b.gd" in (root / r["files"][1]["path"]).read_text()


def test_retention_never_copies_hardlinks_or_symlink_aliases(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    foreign = tmp_path / "FOREIGN.json"
    foreign.write_text("foreign-private")
    os.link(foreign, home / "linked.json")
    r = gh._retain_copy(["linked.json"], [home], [], {})
    assert not r["ok"] and not r["files"]
    assert foreign.read_text() == "foreign-private"
    (home / "actual").mkdir()
    (home / "actual/a.json").write_text("{}")
    (home / "alias").symlink_to(home / "actual", target_is_directory=True)
    r = gh._retain_copy(["alias/a.json"], [home], [], {})
    assert not r["ok"] and not r["files"]


def test_symlinked_retention_destination_is_never_written_through(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "a.json").write_text("{}")
    foreign_dir = tmp_path / "FOREIGN"
    foreign_dir.mkdir()
    gh._retain_root().mkdir(parents=True)
    (gh._retain_root() / "evidence").symlink_to(foreign_dir, target_is_directory=True)
    try:
        r = gh._retain_copy(["a.json"], [home], [], {})
        assert not r["ok"] and not r["files"]
    except OSError:
        pass
    assert not [p for p in foreign_dir.rglob("*") if p.is_file()]


def test_invocation_collision_never_overwrites_previous_evidence(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "a.json").write_text("FIRST")
    one = gh._retain_copy(["a.json"], [home], [], {})
    before = (Path(one["retained_dir"]) / "a.json").read_bytes()
    (home / "a.json").write_text("SECOND")
    second = gh._retain_copy(["a.json"], [home], [], {})
    assert second["retained_dir"] != one["retained_dir"]
    assert (Path(one["retained_dir"]) / "a.json").read_bytes() == before
    assert (Path(second["retained_dir"]) / "a.json").read_text() == "SECOND"


def test_raw_logs_count_toward_the_declared_retention_budget(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "a.json").write_text("{}")
    monkeypatch.setattr(gh, "_RETAIN_MAX_BYTES", 8)
    r = gh._retain_copy(["a.json"], [home],
                        [{"stdout": "x" * 40, "stderr": "y" * 40}], {})
    assert not r["ok"] and r["limit_hit"] == "total_bytes"


def test_unexpected_script_error_still_writes_a_truthful_failure_manifest(monkeypatch, tmp_path):
    """An error nobody predicted must not erase the generated evidence: the
    failure manifest is written and every owned home is still cleaned."""
    gh = _load(tmp_path, monkeypatch)
    work = _script_project(tmp_path)
    monkeypatch.setattr(gh, "_copy_project", lambda proj: work)
    monkeypatch.setattr(gh, "_import_resources",
                        lambda dst, timeout=0, raw_logs=None: None)
    homes = []

    def boom(cmd, timeout, extra_env=None, render=False):
        home = Path(extra_env["HOME"])
        homes.append(home)
        (home / "reports").mkdir(parents=True, exist_ok=True)
        (home / "reports" / "summary.json").write_text('{"frames": 3}')
        raise RuntimeError("controlled-unexpected")

    monkeypatch.setattr(gh, "_run", boom)
    import pytest
    with pytest.raises(RuntimeError, match="controlled-unexpected"):
        gh.run_script(str(tmp_path), ["res://tests/t_a.gd"], timeout=30,
                      retain=["reports/summary.json"],
                      corr={"project_id": "p", "operation_id": "op-x"})
    assert homes and all(not h.exists() for h in homes)
    manifests = list(Path(gh._retain_root()).rglob("manifest.json"))
    assert manifests
    doc = json.loads(manifests[0].read_text())
    assert doc["ok"] is False and doc["operation_id"] == "op-x"
    assert any("unexpected" in r for r in doc["refused"])


def test_import_isolates_home_and_xdg_cleans_and_keeps_full_outcomes(monkeypatch, tmp_path):
    import subprocess
    import pytest
    for outcome in ("success", "timeout", "exception"):
        gh = _load(tmp_path / outcome, monkeypatch)
        homes = []
        raw = []
        marker = "IMPORT-RAW-" + "x" * 5000

        def fake(args, timeout, extra_env=None, render=False):
            assert "--import" in args and not render
            home = Path(extra_env["HOME"])
            homes.append(home)
            assert home.is_dir()
            for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
                assert Path(extra_env[key]).is_relative_to(home)
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(args, timeout, output=marker, stderr="RAW-ERR")
            if outcome == "exception":
                raise RuntimeError("IMPORT-FAIL")
            return _CP(0, marker, "RAW-ERR")

        monkeypatch.setattr(gh, "_run", fake)
        if outcome == "exception":
            with pytest.raises(RuntimeError, match="IMPORT-FAIL"):
                gh._import_resources(tmp_path, 20, raw_logs=raw)
            assert "IMPORT-FAIL" in raw[0]["stderr"]
        else:
            gh._import_resources(tmp_path, 20, raw_logs=raw)
            assert raw[0]["stdout"] == marker
            assert "RAW-ERR" in raw[0]["stderr"]
            assert raw[0]["timed_out"] == (outcome == "timeout")
        assert homes and not any(h.exists() for h in homes)


def test_legacy_playtest_retains_generated_owned_home_before_cleanup(monkeypatch, tmp_path):
    gh = _load(tmp_path, monkeypatch)
    work = _script_project(tmp_path)
    seen = []
    def probe(dst, state_path, frames, timeout, extra, **kwargs):
        home = Path(extra["HOME"])
        seen.append(home)
        assert Path(extra["XDG_DATA_HOME"]).is_relative_to(home)
        (home / "summary.json").write_text('{"smoke":true}')
        return {"frames": 2, "nodes": {}, "render_mode": "render"}, [], False
    monkeypatch.setattr(gh, "_run_probe", probe)
    result = gh._playtest_legacy(work, 2, "ui_accept", 20,
                                 retain=["summary.json"])
    assert result["passed"] and result["retention"]["ok"]
    assert len(result["retention"]["files"]) == 1
    assert not any(h.exists() for h in seen)


def test_declared_import_artifact_survives_normal_routes_and_smoke_all_outcomes(monkeypatch, tmp_path):
    import subprocess
    for mode in ("script", "spec", "legacy"):
        for outcome in ("success", "failure", "timeout", "unexpected"):
            case = tmp_path / (mode + "-" + outcome)
            case.mkdir()
            gh = _load(case, monkeypatch)
            _script_project(case)
            (case / "project.godot").write_text('[application]\nconfig/name="ImportOwned"\n')
            homes = []
            def fake(args, timeout, extra_env=None, render=False):
                home = Path(extra_env["HOME"])
                homes.append(home)
                if "--import" in args:
                    root = home / ".local/share/godot/app_userdata/ImportOwned/reports"
                    root.mkdir(parents=True)
                    (root / "import.json").write_text('{"import_owned":true}')
                    if outcome == "timeout":
                        raise subprocess.TimeoutExpired(args, timeout, output="IMPORT-OUT", stderr="IMPORT-ERR")
                    if outcome == "unexpected":
                        raise RuntimeError("ORIGINAL-IMPORT-ERROR")
                    return _CP(7 if outcome == "failure" else 0, "IMPORT-OUT", "IMPORT-ERR")
                if mode != "script":
                    Path(extra_env["AITELIER_PROBE_OUT"]).write_text(json.dumps({
                        "frames": 2, "nodes": {}, "asserts": [],
                        "timing": {"game_usec": 33333, "frames_stepped": 2}}))
                return _CP(0, "PASS", "")
            monkeypatch.setattr(gh, "_run", fake)
            server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            payload = {"project_dir": str(case), "retain": {"files": ["reports/import.json"]}}
            route = "/script" if mode == "script" else "/playtest"
            if mode == "script":
                payload["scripts"] = ["res://tests/t_a.gd"]
            else:
                payload.update(frames=2, captures=0)
                if mode == "spec":
                    payload["spec"] = {"scenarios": [{"name": "owned", "timeline": []}]}
            request = urllib.request.Request("http://127.0.0.1:%d%s" % (server.server_port, route),
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
            try:
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        code, body = response.status, json.loads(response.read())
                except urllib.error.HTTPError as exc:
                    code, body = exc.code, json.loads(exc.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            docs = [(p, json.loads(p.read_text())) for p in gh._retain_root().rglob("manifest.json")]
            assert len(docs) == 1, (mode, outcome, body)
            path, doc = docs[0]
            rows = [r for r in doc["files"] if r["user_path"] == "user://reports/import.json"]
            assert len(rows) == 1 and rows[0]["import"] == "resources", (mode, outcome, doc)
            assert (path.parent / rows[0]["path"]).read_text() == '{"import_owned":true}'
            assert homes and not any(h.exists() for h in homes), (mode, outcome)
            if outcome == "unexpected":
                assert code >= 400 and "ORIGINAL-IMPORT-ERROR" in body["error"]
                assert not doc["ok"]
            else:
                assert code == 200 and body["retention"]["ok"]


def test_import_home_is_registered_before_effect_and_deferred_to_caller(monkeypatch, tmp_path):
    import shutil
    gh = _load(tmp_path, monkeypatch)
    homes, labels = [], []
    raw = gh._InvocationLogs(homes, labels)
    def fake(args, timeout, extra_env=None, render=False):
        assert homes == [Path(extra_env["HOME"])] and homes[0].is_dir()
        assert labels == [{"import": "resources"}]
        return _CP(0, "FULL-IMPORT", "")
    monkeypatch.setattr(gh, "_run", fake)
    try:
        gh._import_resources(tmp_path, 20, raw_logs=raw)
        assert raw.homes is homes and raw.labels is labels
        assert homes[0].exists() and raw[0]["stdout"] == "FULL-IMPORT"
    finally:
        for home in homes:
            shutil.rmtree(home, ignore_errors=True)


def test_directory_fstat_failure_closes_new_and_previous_descriptors(monkeypatch, tmp_path):
    import errno
    import pytest
    gh = _load(tmp_path, monkeypatch)
    before = len(os.listdir("/proc/self/fd"))
    opened = []
    original_open, original_fstat = gh.os.open, gh.os.fstat
    def spy(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd
    def fault(fd):
        raise OSError(errno.EIO, "AUTHOR-directory-fstat")
    monkeypatch.setattr(gh.os, "open", spy)
    monkeypatch.setattr(gh.os, "fstat", fault)
    try:
        with pytest.raises(OSError, match="AUTHOR-directory-fstat"):
            gh._evidence_dir(tmp_path)
    finally:
        monkeypatch.setattr(gh.os, "open", original_open)
        monkeypatch.setattr(gh.os, "fstat", original_fstat)
    assert len(os.listdir("/proc/self/fd")) == before
    assert len(opened) >= 2
    for fd in opened:
        with pytest.raises(OSError):
            original_fstat(fd)


def test_retention_file_fstat_failure_closes_its_new_duplicate(monkeypatch, tmp_path):
    import errno
    import stat as stat_mod
    gh = _load(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "a.json").write_text('{"owned": true}')
    original_dup, original_fstat = gh.os.dup, gh.os.fstat
    owned, raised = {}, []
    def spy(fd):
        new = original_dup(fd)
        st = original_fstat(new)
        if stat_mod.S_ISREG(st.st_mode):
            owned[new] = (st.st_dev, st.st_ino)
        return new
    def fault(fd):
        if fd in owned and not raised:
            raised.append(fd)
            raise OSError(errno.EIO, "AUTHOR owned copy fstat EIO")
        return original_fstat(fd)
    before = len(os.listdir("/proc/self/fd"))
    monkeypatch.setattr(gh.os, "dup", spy)
    monkeypatch.setattr(gh.os, "fstat", fault)
    try:
        result = gh._retain_copy(["a.json"], [home], [], {})
        after = len(os.listdir("/proc/self/fd"))
        assert raised and not result["ok"]
        assert any("AUTHOR owned copy fstat EIO" in x for x in result["refused"])
        assert after == before, "copied-file duplicate leaked when its first fstat raised"
    finally:
        monkeypatch.setattr(gh.os, "dup", original_dup)
        monkeypatch.setattr(gh.os, "fstat", original_fstat)
        for fd, identity in owned.items():
            try:
                now = original_fstat(fd)
                if (now.st_dev, now.st_ino) == identity:
                    os.close(fd)
            except OSError:
                pass
