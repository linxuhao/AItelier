from __future__ import annotations

import importlib.util
import json
import subprocess
import sys

import pytest


def _harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GODOT_LIFECYCLE_DB", str(tmp_path / "owners.sqlite3"))
    spec = importlib.util.spec_from_file_location(
        "godot_harness_lifecycle", "docker/godot/godot_harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_owner_is_durable_single_owner_and_explicitly_reconciled(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    first = harness.acquire_render_owner("project-a", "run-a", "op-a")
    assert first["generation"] == 1
    try:
        harness.acquire_render_owner("project-b", "run-b", "op-b")
    except RuntimeError as exc:
        assert "render owner exists" in str(exc)
    else:
        raise AssertionError("second render owner was admitted")
    lost = harness.mark_render_owner_lost(first["owner_id"], first["generation"], "worker exited")
    assert lost["status"] == "owner_lost"
    reconciled = harness.reconcile_render_owner(
        first["owner_id"], first["generation"], "operator", "verified process exited")
    assert reconciled["status"] == "reconciled"
    second = harness.acquire_render_owner("project-b", "run-b", "op-b")
    harness.release_render_owner(second["owner_id"], second["generation"])
    assert all(row["status"] in {"reconciled", "released"}
               for row in harness.render_owner_snapshot())


def test_reconciliation_refuses_while_real_render_lock_is_held(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    owner = harness.acquire_render_owner("project-a", "run-a", "op-a")
    harness.mark_render_owner_lost(owner["owner_id"], owner["generation"], "worker lost")
    harness._Handler._RENDER_LOCK.acquire()
    try:
        with pytest.raises(RuntimeError, match="effect|lock|settled"):
            harness.reconcile_render_owner(
                owner["owner_id"], owner["generation"], "operator", "checked")
        with pytest.raises(RuntimeError, match="render owner exists"):
            harness.acquire_render_owner("project-b", "run-b", "op-b")
    finally:
        harness._Handler._RENDER_LOCK.release()
    reconciled = harness.reconcile_render_owner(
        owner["owner_id"], owner["generation"], "operator", "lock released")
    assert reconciled["status"] == "reconciled"


def test_reconciliation_requires_process_exit_and_durable_effect_lock_release(tmp_path, monkeypatch):
    harness = _harness(tmp_path, monkeypatch)
    owner = harness.acquire_render_owner("project-a", "run-a", "op-a")
    lock_path = str(tmp_path / "render-effect.lock")
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,sys; f=open(sys.argv[1],'a+b'); "
         "fcntl.flock(f.fileno(),fcntl.LOCK_EX); print('locked',flush=True); sys.stdin.read()",
         lock_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with harness._lifecycle_connection() as conn:
            conn.execute("UPDATE render_owners SET actor=? WHERE owner_id=?",
                         (f"godot-harness:{child.pid}", owner["owner_id"]))
            conn.commit()
        harness.mark_render_owner_lost(owner["owner_id"], owner["generation"], "worker lost")
        with pytest.raises(RuntimeError, match="still alive"):
            harness.reconcile_render_owner(
                owner["owner_id"], owner["generation"], "operator", "checked")
        assert harness.render_owner_snapshot()[0]["status"] == "owner_lost"
    finally:
        if child.stdin:
            child.stdin.close()
        child.wait(timeout=5)
    reconciled = harness.reconcile_render_owner(
        owner["owner_id"], owner["generation"], "operator", "process and lock settled")
    assert reconciled["status"] == "reconciled"


def test_supported_playtest_calls_preserve_exact_owner_identities(tmp_path, monkeypatch):
    from aitelier.tools.godot_compile import impl as compile_tool
    from aitelier.tools.godot_playtest import impl as playtest
    from aitelier.tools.godot_playtest_scenario import impl as scenario

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "project.godot").write_text("[application]\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(playtest, "read_spec", lambda _p: (None, {"source": "", "errors": [], "notes": []}))
    monkeypatch.setattr(playtest, "post_playtest", lambda payload, **_kw: captured.append(payload) or {"passed": True})
    playtest.godot_playtest(project_root=str(repo), out_dir=str(tmp_path / "out"),
                            project_id="project-real", run_id="run-real",
                            operation_id="op-real")
    assert {k: captured[-1][k] for k in ("project_id", "run_id", "operation_id")} == {
        "project_id": "project-real", "run_id": "run-real", "operation_id": "op-real"}

    spec = {"scenarios": [{"name": "one", "timeline": []}]}
    monkeypatch.setattr(playtest, "read_spec", lambda _p: (spec, {"source": "fixture", "errors": [], "notes": []}))
    monkeypatch.setattr(playtest, "post_playtest", lambda payload, **_kw: captured.append(payload) or {
        "passed": True, "behavior": {"scenarios": [{"name": "one", "passed": True, "asserts": []}]}})
    scenario.godot_playtest_scenario(
        scenario="one", project_root=str(repo), project_id="project-real",
        run_id="run-real", operation_id="op-scenario")
    assert {k: captured[-1][k] for k in ("project_id", "run_id", "operation_id")} == {
        "project_id": "project-real", "run_id": "run-real", "operation_id": "op-scenario"}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self):
            return json.dumps({"passed": False, "errors": ["fixture"]}).encode()

    def fake_urlopen(request, **_kwargs):
        captured.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(compile_tool.urllib.request, "urlopen", fake_urlopen)
    compile_tool.godot_compile(
        project_root=str(repo), out_dir=str(tmp_path / "compile-out"),
        project_id="project-real", run_id="run-real", operation_id="op-compile")
    assert {k: captured[-1][k] for k in ("project_id", "run_id", "operation_id")} == {
        "project_id": "project-real", "run_id": "run-real", "operation_id": "op-compile"}


def test_host_injects_real_project_identity_into_agent_tool_call(monkeypatch):
    from skillflow import SkillFlow

    from core.skillflow_host import AItelierSkillFlow

    from api.dependencies import get_tool_loader

    observed = {}
    def base(_self, name, params, **kwargs):
        observed.update(params)
        return {"passed": True}
    monkeypatch.setattr(SkillFlow, "_execute_tool_impl", base)
    host = object.__new__(AItelierSkillFlow)
    # The REAL loader, so this asserts against godot_playtest's own signature and
    # its own tool.yaml rather than against a stub that can agree with anything.
    # Both surfaces have to say yes before the host binds the identity.
    host._tool_loader = get_tool_loader()
    host._get_project_id = lambda _run: "project-from-run"
    host._execute_tool_impl("godot_playtest", {}, run_id="run-real",
                            step_id="test", project_root="/tmp/repo")
    assert observed["project_id"] == "project-from-run"
