"""Multi-driver P3 (slim, design §7.3 rule 9): ONE owner check at the entry of
every door that answers a checkpoint or rescues a failed run - the REST
approve/reject routes, the run-scoped delegates and the MCP answer_checkpoint
tool. A non-owner driver is refused (409 / error) before the engine is
touched; the owner reaches the engine; nothing is held open in between.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from core import drivers
from core.state_service import StateService
from tests.unit.test_state_p3_enforced_claims import NODE, claim, clock, events, write  # noqa: F401 - fixture
from tests.unit.test_state_p3_fix_round4 import INFO


def _bind_state_attempt(database, run_id="run-1"):
    """An enforced State project whose node b has a paused SkillFlow attempt
    bound to ``run_id`` and owned by codex (who holds the implement claim)."""
    owner = StateService(database, actor="driver:owner-cli", project_read_trusted=True, driver_id="owner-cli",
                         is_admin=True)
    owner.create_project("p", "P")
    owner.store.add_nodes("p", [{"key": "a", **NODE}, {"key": "b", **NODE}])
    write(owner, "set_claim_enforcement", project_id="p", claim_enforcement="on", expected_revision=0, reason="r")
    codex = StateService(database, actor="driver:codex", project_read_trusted=True, driver_id="codex")
    claim(codex, "b")
    with database.get_connection() as conn:
        contract = conn.execute("SELECT contract_hash FROM state_nodes WHERE project_id='p' AND node_key='b'").fetchone()[0]
        conn.execute("INSERT INTO state_attempts(attempt_id,project_id,node_key,node_revision,contract_hash,"
                     "dependency_snapshot,context_json,request_key,request_hash,workflow,execution_project_id,run_id,"
                     "status,created_at,updated_at,execution_kind,owner_driver_id,owner_fence,lease_expires_at,"
                     "last_heartbeat_at) VALUES('attempt-sf','p','b',1,?,'{}','{}','rk','rh','w','sg-x',?,'paused','t','t',"
                     "'skillflow','codex',1,'2999-01-01T00:00:00.000000+00:00','2026-01-01T00:00:00.000000+00:00')",
                     (contract, run_id))
        conn.commit()
    return codex


def _as(monkeypatch, driver):
    from api import state_graph_routers as routers
    monkeypatch.setattr(routers, "request_identity",
                        lambda request: drivers.Identity("driver", f"driver:{driver}", driver))
    monkeypatch.setattr(routers, "request_actor", lambda request: f"driver:{driver}")


def _rest(client, monkeypatch, project_id, status):
    import api.meta_routers as mr
    import core.run_driver as run_driver
    from api.dependencies import get_db_manager
    from api.main import app
    from tests.integration.test_meta_routers import _ensure_project
    _ensure_project(client, project_id)
    database = app.dependency_overrides[get_db_manager]()
    _bind_state_attempt(database)
    sf = MagicMock()
    sf.get_run.return_value = {"status": status}
    monkeypatch.setattr(mr, "get_skillflow", lambda: sf)
    monkeypatch.setattr(mr, "_get_checkpoint_info", lambda pid: INFO)
    monkeypatch.setattr(run_driver, "checkpoint_reject_target", lambda *a: None)
    return sf, database


def test_rest_checkpoint_routes_refuse_a_non_owner_at_entry(client, monkeypatch, clock):
    sf, database = _rest(client, monkeypatch, "cp-entry-proj", "paused")
    _as(monkeypatch, "grok")
    for verb, body in (("approve", {"checkpoint": "gather", "feedback": ""}),
                       ("reject", {"checkpoint": "gather", "feedback": "redo"})):
        resp = client.post(f"/api/meta/cp-entry-proj/checkpoint/{verb}", json=body)
        assert resp.status_code == 409 and "not_attempt_owner" in resp.text, resp.text
    sf.approve_checkpoint.assert_not_called()
    sf.reject_checkpoint.assert_not_called()
    _as(monkeypatch, "codex")
    assert client.post("/api/meta/cp-entry-proj/checkpoint/reject",
                       json={"checkpoint": "gather", "feedback": "redo"}).status_code == 200
    sf.reject_checkpoint.assert_called_once()
    with database.get_connection() as conn:
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='driver_checkpoint_decisions'").fetchone()


def test_failed_run_rescue_is_checked_once_at_entry_too(client, monkeypatch, clock):
    import api.run_routers as rr
    sf, _ = _rest(client, monkeypatch, "cp-rescue-proj", "failed")
    monkeypatch.setattr(rr, "_run_to_project_id", lambda run_id: "cp-rescue-proj")
    _as(monkeypatch, "grok")
    for path in ("/api/meta/cp-rescue-proj/checkpoint/approve", "/api/runs/run-1/checkpoint/approve"):
        resp = client.post(path, json={"checkpoint": "gather", "feedback": ""})
        assert resp.status_code == 409 and "not_attempt_owner" in resp.text, (path, resp.text)
    sf.reactivate_run.assert_not_called()
    sf.resume_run.assert_not_called()
    _as(monkeypatch, "owner-cli")                       # an admin passes, recorded as break glass
    from api import state_graph_routers as routers
    monkeypatch.setattr(routers, "authenticated_is_admin", lambda request: True)
    resp = client.post("/api/meta/cp-rescue-proj/checkpoint/approve", json={"checkpoint": "gather", "feedback": ""})
    assert resp.status_code == 200, resp.text
    sf.reactivate_run.assert_called_once_with("run-1")
    sf.resume_run.assert_called_once_with("run-1")


def test_mcp_answer_checkpoint_checks_the_owner_once(tmp_path, monkeypatch, clock):
    import api.dependencies as deps
    import api.meta_routers as mr
    import core.run_driver as run_driver
    from api import mcp_router
    from core import trace_reader
    from core.db_manager import DBManager
    monkeypatch.setenv("AITELIER_HOME", str(tmp_path / "home"))
    database = DBManager(str(tmp_path / "mcp.sqlite3"))
    _bind_state_attempt(database)
    sf = MagicMock()
    sf.get_run.return_value = {"status": "paused", "id": "run-1", "project_id": "p"}
    monkeypatch.setattr(deps, "get_skillflow", lambda: sf)
    monkeypatch.setattr(deps, "get_db_manager", lambda: database)
    monkeypatch.setattr(trace_reader, "resolve_run_ref",
                        lambda sf_, ref: ({"id": "run-1", "project_id": "p", "status": "paused",
                                           "graph_name": "meta_conversation"}, {}))
    monkeypatch.setattr(mr, "_get_checkpoint_info", lambda pid, rid=None: INFO)
    monkeypatch.setattr(run_driver, "checkpoint_reject_target", lambda *a: "redirect")
    captured = {}

    def tool(name, kind, description):
        def deco(fn):
            captured[name] = fn
            return fn
        return deco
    mcp_router._register_run_tools(tool)
    answer = captured["answer_checkpoint"]
    _as(monkeypatch, "grok")
    assert "not_attempt_owner" in answer("run-1", "reject", "redo it").get("error", "")
    sf.reject_checkpoint.assert_not_called()
    _as(monkeypatch, "codex")
    out = answer("run-1", "reject", "redo it")
    assert "error" not in out, out
    sf.reject_checkpoint.assert_called_once_with("run-1", "gather", "redo it", redirect_to="redirect")
