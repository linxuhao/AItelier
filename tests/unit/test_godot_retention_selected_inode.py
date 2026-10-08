"""Retention keeps the SELECTED object, not whatever the name points at later.

SOURCE-only tests: no Godot, no engine, no network beyond the harness' own
stdlib HTTP server on 127.0.0.1. `_run`/`_run_probe` — the one seam that would
shell out to the engine — are stubbed; everything else (real temp filesystem,
real SHA256 of copied bytes, the pinned-descriptor copy path, the real HTTP
`_Handler`) runs for real.

THE DEFECT. A `retain.patterns` selector names a dynamic artifact by glob. The
discovery walk matched a regular file, then CLOSED its descriptor and handed the
relpath to `_retain_take`, which re-derived the artifact purely from its NAME
(a fresh `os.stat`/`os.open`). Between the walk's selection and the copy the
name could be unlinked and rewritten with a DIFFERENT inode (the observed
selection-to-copy identity defect: inode 65949532 replaced by 65949556 before
the copy). The old code then copied the replacement — a real product
counterexample where the retained bytes were never the object the walk selected.

THE CONTRACT. The walk keeps a regular descriptor for every object it SELECTS until
name validation and copying finish; the copy consumes exactly that live identity. A name now pointing at a different
inode is a selected-but-undeliverable artifact: a truthful hard failure naming
the selected rel in `missing`, never a silent copy, never a fresh-stat retry,
never a path fallback. The control cases stay distinct: a file SELECTED and then
REMOVED is the same hard failure; a file never touched is still retained; and a
sibling pass whose artifact is untouched keeps its own row.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path


_HARNESS = Path(__file__).resolve().parents[2] / "docker" / "godot" / "godot_harness.py"


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    monkeypatch.setenv("GODOT_DEPLOYMENT_LOCK", str(tmp_path / "deployment.lock"))
    monkeypatch.setenv("GODOT_RENDER_EFFECT_LOCK", str(tmp_path / "effect.lock"))
    monkeypatch.setenv("GODOT_EVIDENCE_ROOT", str(tmp_path / "evidence-root"))
    spec = importlib.util.spec_from_file_location("gh_selected_inode", _HARNESS)
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


def _open_path(fd):
    """The real filesystem path a pinned directory descriptor refers to."""
    return Path(os.readlink("/proc/self/fd/%d" % fd))


def _replace_with_new_inode(target, body):
    """Create while the old inode exists, then atomically replace its name."""
    replacement = target.with_name(target.name + ".replacement.tmp")
    replacement.write_text(body)
    os.replace(replacement, target)


# ── the direct mechanism: selection identity enforced through the copy ──────

def test_selected_inode_swapped_before_copy_is_missing_not_green(monkeypatch, tmp_path):
    """The walk SELECTS `runs/a-1/report.json`; the name is then replaced by a
    DIFFERENT inode holding different bytes before the pinned copy runs. The
    selected object is no longer deliverable, so the selected rel is a truthful
    hard failure and the replacement bytes are NEVER laundered into the
    invocation's evidence."""
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"runs/a-1/report.json": '{"selected": 1}'})
    real_walk = gh._retain_walk_pattern
    swapped = {}

    def walk_then_replace(root_fd, pattern, budget):
        matches, refused = real_walk(root_fd, pattern, budget)
        if matches:
            base = _open_path(root_fd)
            for rel in matches:
                target = base / rel
                old = target.stat()
                _replace_with_new_inode(target, '{"replacement": 2}')
                swapped[rel] = (old.st_ino, target.stat().st_ino)
        return matches, refused

    monkeypatch.setattr(gh, "_retain_walk_pattern", walk_then_replace)
    r = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"],
                        requested=True)
    assert swapped and swapped["runs/a-1/report.json"][0] != swapped["runs/a-1/report.json"][1]
    assert r["ok"] is False, r
    assert r["missing"] == ["runs/a-1/report.json"], r
    assert r["files"] == [], r
    # A swapped name is a hard failure, NOT a refusal: the walk found a regular
    # file, the copy simply refuses to substitute a different object for it.
    assert r["refused"] == [] and r["limit_hit"] is None, r
    manifest = json.loads((Path(r["retained_dir"]) / "manifest.json").read_text())
    assert manifest["ok"] is False
    assert manifest["missing"] == ["runs/a-1/report.json"]
    assert manifest["files"] == []
    # No raw row, no artifact row, and nothing under `runs/` reached the
    # destination: the replacement bytes were never copied.
    present = sorted(str(p.relative_to(Path(r["retained_dir"])))
                     for p in Path(r["retained_dir"]).rglob("*") if p.is_file())
    assert present == ["manifest.json"], present


def test_selected_inode_removed_before_copy_is_missing_not_green(monkeypatch, tmp_path):
    """Control for the case above, kept on the SAME identity-enforced path: a
    file the walk SELECTED and which then vanished is the same truthful hard
    failure, named in `missing` with no fabricated row."""
    gh = _load(tmp_path, monkeypatch)
    home = _home_with(tmp_path, {"runs/a-1/report.json": '{"selected": 1}'})
    real_walk = gh._retain_walk_pattern

    def walk_then_remove(root_fd, pattern, budget):
        matches, refused = real_walk(root_fd, pattern, budget)
        if matches:
            base = _open_path(root_fd)
            for rel in matches:
                (base / rel).unlink()
        return matches, refused

    monkeypatch.setattr(gh, "_retain_walk_pattern", walk_then_remove)
    r = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"],
                        requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["runs/a-1/report.json"], r
    assert r["files"] == [] and r["refused"] == [] and r["limit_hit"] is None, r


def test_untouched_selected_artifact_is_still_retained_byte_for_byte(monkeypatch, tmp_path):
    """The positive control: with NO swap the identity enforcement is
    transparent — the SELECTED object is copied, its bytes hashed, and the row
    still records the real size/SHA256."""
    gh = _load(tmp_path, monkeypatch)
    body = b'{"selected": 1}'
    home = _home_with(tmp_path, {"runs/a-1/report.json": body})
    r = gh._retain_copy([], [home], [], {}, patterns=["runs/*/report.json"],
                        requested=True)
    assert r["ok"] is True and r["missing"] == [] and r["refused"] == [], r
    row = r["files"][0]
    assert row["user_path"] == "user://runs/a-1/report.json", row
    assert row["size"] == len(body)
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert (Path(r["retained_dir"]) / row["path"]).read_bytes() == body


def test_replaced_pass_is_missing_while_an_untouched_sibling_pass_survives(
        monkeypatch, tmp_path):
    """Two passes each write the same selected rel. Pass 0's name is swapped to
    a different inode, pass 1's is untouched: the swapped pass is named missing
    and the intact sibling pass keeps its own row — one bad pass never erases
    another pass's real evidence, and never substitutes the other pass's bytes."""
    gh = _load(tmp_path, monkeypatch)
    homes = []
    for i in range(2):
        h = _home_with(tmp_path, {"runs/a-1/report.json": '{"pass": %d}' % i},
                       name="home%d" % i)
        homes.append(h)
    real_walk = gh._retain_walk_pattern

    def replace_only_first_pass(root_fd, pattern, budget):
        matches, refused = real_walk(root_fd, pattern, budget)
        base = _open_path(root_fd)
        if matches and base.samefile(homes[0]):

            for rel in matches:
                target = base / rel
                _replace_with_new_inode(target, '{"replacement": 2}')
        return matches, refused

    monkeypatch.setattr(gh, "_retain_walk_pattern", replace_only_first_pass)
    r = gh._retain_copy([], homes, [], {}, patterns=["runs/*/report.json"],
                        pass_labels=[{"scenario": "s0"}, {"scenario": "s1"}],
                        requested=True)
    assert r["ok"] is False, r
    assert r["missing"] == ["runs/a-1/report.json"], r
    assert [row["pass"] for row in r["files"]] == [1], r["files"]
    kept = (Path(r["retained_dir"]) / r["files"][0]["path"]).read_text()
    assert kept == '{"pass": 1}', kept
    assert "replacement" not in kept


# ── the real HTTP routes: /script and /playtest ─────────────────────────────

def _stub_engine(gh, monkeypatch, artifact_rel, body='{"gate": 1}'):
    """Stub the one engine seam: a pass writes its artifact into the throwaway
    HOME it was handed, exactly as a real GDScript suite would."""
    import subprocess

    def engine(args, timeout, extra_env=None, render=False):
        if "--import" not in args and extra_env and "HOME" in extra_env:
            target = Path(extra_env["HOME"]) / artifact_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        return subprocess.CompletedProcess(args, 0, "PASS\n", "")

    monkeypatch.setattr(gh, "_run", engine)


def _stub_probe(gh, monkeypatch, artifact_rel, body='{"gate": 1}'):
    """Stub the playtest engine seam the same way, through the probe wrapper."""
    def probe(dst, state_path, frames, timeout, extra, scene="", capture_at=None,
              timing=None, render=True, raw=None, raw_label=None):
        if extra and "HOME" in extra:
            target = Path(extra["HOME"]) / artifact_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
        return {"frames": frames, "render_mode": "headless"}, [], False

    monkeypatch.setattr(gh, "_run_probe", probe)


def _project(tmp_path):
    (tmp_path / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="SelectedInodeGate"\n')
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "a.gd").write_text("extends SceneTree\n")


def _swap_after_walk(gh, monkeypatch):
    real_walk = gh._retain_walk_pattern

    def wrapper(root_fd, pattern, budget):
        matches, refused = real_walk(root_fd, pattern, budget)
        if matches:
            base = _open_path(root_fd)
            for rel in matches:
                target = base / rel
                _replace_with_new_inode(target, '{"replacement": 2}')
        return matches, refused

    monkeypatch.setattr(gh, "_retain_walk_pattern", wrapper)


def _post(gh, route, payload):
    import threading
    import urllib.error
    import urllib.request
    server = gh.ThreadingHTTPServer(("127.0.0.1", 0), gh._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (server.server_port, route),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_script_route_selected_inode_replaced_is_a_hard_failure(monkeypatch, tmp_path):
    """Through the REAL `/script` handler a deterministic replacement between
    selection and copy makes the whole gate fail: passed is False, the selected
    rel is missing, and the replacement bytes are never retained."""
    gh = _load(tmp_path, monkeypatch)
    _project(tmp_path)
    _stub_engine(gh, monkeypatch, "runs/a-1/report.json")
    _swap_after_walk(gh, monkeypatch)
    status, body = _post(gh, "/script", {
        "project_dir": str(tmp_path), "scripts": ["res://tests/a.gd"],
        "retain": {"patterns": ["runs/*/report.json"]}})
    assert status == 200, body
    assert body["passed"] is False, body
    retention = body["retention"]
    assert retention["ok"] is False, retention
    assert retention["missing"] == ["runs/a-1/report.json"], retention
    assert retention["files"] == [], retention
    manifest = json.loads(Path(retention["manifest"]).read_text())
    assert manifest["ok"] is False
    assert manifest["missing"] == ["runs/a-1/report.json"]
    assert manifest["files"] == []


def test_script_route_untouched_selected_artifact_passes_and_is_retained(
        monkeypatch, tmp_path):
    """The matched positive: NO replacement, so the same `/script` request
    passes and the SELECTED artifact is retained with its real bytes/hash."""
    gh = _load(tmp_path, monkeypatch)
    _project(tmp_path)
    _stub_engine(gh, monkeypatch, "runs/a-1/report.json", body='{"gate": 1}')
    status, body = _post(gh, "/script", {
        "project_dir": str(tmp_path), "scripts": ["res://tests/a.gd"],
        "retain": {"patterns": ["runs/*/report.json"]}})
    assert status == 200, body
    assert body["passed"] is True, body
    retention = body["retention"]
    assert retention["ok"] is True, retention
    assert retention["missing"] == [] and retention["refused"] == [], retention
    row = retention["files"][0]
    kept = (Path(retention["retained_dir"]) / row["path"]).read_bytes()
    assert kept == b'{"gate": 1}'
    assert row["sha256"] == hashlib.sha256(kept).hexdigest()
    assert row["user_path"] == "user://runs/a-1/report.json"


def test_playtest_route_selected_inode_replaced_is_a_hard_failure(monkeypatch, tmp_path):
    """The previously-UNRUN `/playtest` route gets the same deterministic
    replacement negative: selection-to-copy identity is enforced there too."""
    gh = _load(tmp_path, monkeypatch)
    _project(tmp_path)
    _stub_engine(gh, monkeypatch, "unused.json")
    _stub_probe(gh, monkeypatch, "runs/a-1/report.json")
    _swap_after_walk(gh, monkeypatch)
    status, body = _post(gh, "/playtest", {
        "project_dir": str(tmp_path),
        "retain": {"patterns": ["runs/*/report.json"]}})
    assert status == 200, body
    assert body["passed"] is False, body
    retention = body["retention"]
    assert retention["ok"] is False, retention
    assert retention["missing"] == ["runs/a-1/report.json"], retention
    assert retention["files"] == [], retention


def test_playtest_route_untouched_selected_artifact_passes_and_is_retained(
        monkeypatch, tmp_path):
    """The matched `/playtest` positive: with no replacement the selected
    artifact is retained and the run is a truthful pass."""
    gh = _load(tmp_path, monkeypatch)
    _project(tmp_path)
    _stub_engine(gh, monkeypatch, "unused.json")
    _stub_probe(gh, monkeypatch, "runs/a-1/report.json", body='{"gate": 7}')
    status, body = _post(gh, "/playtest", {
        "project_dir": str(tmp_path),
        "retain": {"patterns": ["runs/*/report.json"]}})
    assert status == 200, body
    assert body["passed"] is True, body
    retention = body["retention"]
    assert retention["ok"] is True, retention
    row = retention["files"][0]
    kept = (Path(retention["retained_dir"]) / row["path"]).read_bytes()
    assert kept == b'{"gate": 7}'
    assert row["sha256"] == hashlib.sha256(kept).hexdigest()
