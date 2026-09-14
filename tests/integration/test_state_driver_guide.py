"""Real MCP prompt/resource discovery and tool fallback share one protocol."""
import hashlib
import json
from fastapi.testclient import TestClient
from api.state_only import create_app
from core.state_driver_guide import STATE_DRIVER_GUIDE


def test_mcp_driver_onboarding_surfaces_are_discoverable_and_consistent(tmp_path):
    app = create_app(str(tmp_path / "state.sqlite"), "x" * 40)
    with TestClient(app) as client:
        def rpc(method, params=None, authorized=True):
            headers = {"Accept": "application/json, text/event-stream"}
            if authorized:
                headers["Authorization"] = "Bearer " + "x" * 40
            return client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1,
                               "method": method, "params": params or {}}, headers=headers)
        for method, params in [("prompts/get", {"name": "state_graph_driver"}),
                               ("resources/read", {"uri": "aitelier://state/driver-guide"})]:
            assert rpc(method, params, authorized=False).status_code == 401
        prompts = rpc("prompts/list").json()["result"]["prompts"]
        assert any(p["name"] == "state_graph_driver" for p in prompts)
        prompt = rpc("prompts/get", {"name": "state_graph_driver"}).json()["result"]
        assert prompt["messages"][0]["content"]["text"] == STATE_DRIVER_GUIDE
        resources = rpc("resources/list").json()["result"]["resources"]
        assert any(r["uri"] == "aitelier://state/driver-guide" for r in resources)
        resource = rpc("resources/read", {"uri": "aitelier://state/driver-guide"}).json()["result"]
        assert resource["contents"][0]["text"] == STATE_DRIVER_GUIDE
        help_result = rpc("tools/call", {"name": "state_graph_help", "arguments": {}}).json()["result"]
        assert not help_result.get("isError")
        help_body = json.loads(help_result["content"][0]["text"])
        assert help_body["driver_guide"] == STATE_DRIVER_GUIDE
        assert help_body["driver_resource"] == "aitelier://state/driver-guide"
        search_schema = help_body["operations"]["search_driver_note_history"]
        assert search_schema["mutates"] is False
        assert search_schema["arguments"]["properties"]["excerpt_chars"]["maximum"] == 1000
        assert "search_driver_note_history" in STATE_DRIVER_GUIDE
        evidence_schema = help_body["operations"]["record_evidence"]
        artifact_help = evidence_schema["arguments"]["properties"]["artifact"]["description"]
        verdict_help = evidence_schema["arguments"]["properties"]["verdict"]["description"]
        assert "failed external attempt" in artifact_help
        assert "never promotes" in artifact_help
        assert "exactly once per criterion" in artifact_help
        assert "only pass or fail" in verdict_help
        assert "attempt artifact remains unset" in STATE_DRIVER_GUIDE
        assert "verify_node refuses" in STATE_DRIVER_GUIDE
        operation = client.get("/openapi.json", headers={
            "Authorization": "Bearer " + "x" * 40}).json()["paths"][
            "/api/state/projects/{project_id}/driver-note/history/search"]["get"]
        params = {item["name"]: item for item in operation["parameters"]}
        assert params["query"]["schema"]["maxLength"] == 500
        assert "Unicode case-insensitive" in params["query"]["description"]
        assert "permanent" in json.dumps(params["section"]["schema"])
        assert "temporary" in json.dumps(params["section"]["schema"])
        assert any(item.get("maxLength") == 320
                   for item in params["actor"]["schema"]["anyOf"])
        assert any(item.get("maxLength") == 320
                   for item in params["director_identity"]["schema"]["anyOf"])
        assert params["after_revision"]["schema"]["minimum"] == 0
        assert "Exclusive stable cursor" in params["after_revision"]["description"]
        assert any(item.get("minimum") == 1
                   for item in params["min_revision"]["schema"]["anyOf"])
        assert any(item.get("maximum") == 2**63 - 1
                   for item in params["max_revision"]["schema"]["anyOf"])
        assert "Exclusive timezone-aware" in params["created_after"]["description"]
        assert "Exclusive timezone-aware" in params["created_before"]["description"]
        assert any(item.get("maxLength") == 64
                   for item in params["created_after"]["schema"]["anyOf"])
        assert params["created_after"]["schema"]["format"] == "date-time"
        assert params["created_before"]["schema"]["format"] == "date-time"
        assert params["limit"]["schema"]["maximum"] == 100
        assert params["excerpt_chars"]["schema"]["minimum"] == 64
        assert params["excerpt_chars"]["schema"]["maximum"] == 1000
        assert "ordered by revision ascending" in operation["description"]
        assert "next_after_revision" in operation["description"]


def test_mcp_wait_returns_external_completion_and_remains_private(tmp_path):
    import concurrent.futures
    app = create_app(str(tmp_path / "wait.sqlite"), "x" * 40)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + "x" * 40,
                   "Accept": "application/json, text/event-stream"}
        def call(name, action, arguments, auth=True):
            return client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": name,
                "arguments": {"action": action, "arguments": arguments}}},
                headers=headers if auth else {"Accept": headers["Accept"]})
        def write(action, arguments):
            result = call("state_graph_write", action, arguments).json()["result"]
            assert not result.get("isError"), result
            return json.loads(result["content"][0]["text"])["result"]
        write("create_project", {"project_id": "p", "title": "P"})
        write("add_nodes", {"project_id": "p", "nodes": [{"key": "a", "goal": "A",
              "acceptance": [{"id": "c", "kind": "test", "description": "Check"}]}]})
        attempt = write("start_external_attempt", {"project_id": "p", "node_key": "a",
              "expected_revision": 1, "harness": "test", "external_id": "job", "request_key": "one"})
        service = app.state.state_service
        after = service.store.events("p")[-1]["seq"]
        args = {"project_id": "p", "after": after, "timeout_seconds": 2,
                "attempt_ids": [attempt["attempt_id"]]}
        assert call("state_graph_read", "wait_for_state_change", args, auth=False).status_code == 401
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(call, "state_graph_read", "wait_for_state_change", args)
            # Whether the report arrives before or during wait registration,
            # the durable cursor must make this exact completion observable.
            report=tmp_path/"wait-report.txt"
            report_body=json.dumps({"status":"candidate","settled":True,
                                    "usable":True}).encode()
            report.write_bytes(report_body)
            write("report_external_attempt", {"attempt_id": attempt["attempt_id"],
                "observation_id": "final", "expected_version": 0,
                "context_hash": attempt["context_hash"], "status": "candidate",
                "report_ref": str(report), "report_sha256": hashlib.sha256(report_body).hexdigest(),
                "artifact": "a" * 64, "artifact_kind": "sha256", "quiescent": True})
            result = future.result(timeout=5).json()["result"]
        assert not result.get("isError"), result
        waited = json.loads(result["content"][0]["text"])["result"]
        assert not waited["timed_out"]
        assert waited["events"][0]["payload"]["attempt_id"] == attempt["attempt_id"]
        assert service.store.get_node("p", "a")["status"] == "CANDIDATE"
        again = call("state_graph_read", "wait_for_state_change",
                     {**args, "after": waited["next_after"], "timeout_seconds": 0}).json()["result"]
        assert json.loads(again["content"][0]["text"])["result"]["timed_out"]
        invalid = call("state_graph_read", "wait_for_state_change",
                       {**args, "project_id": "missing", "timeout_seconds": 0}).json()["result"]
        assert invalid["isError"] is True


def test_mcp_driver_notes_are_authorized_project_scoped_and_cas_protected(tmp_path):
    app = create_app(str(tmp_path / "notes.sqlite"), "x" * 40)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer " + "x" * 40,
                   "Accept": "application/json, text/event-stream"}

        def rpc(name, action, arguments, authorized=True):
            return client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": name,
                "arguments": {"action": action, "arguments": arguments}}},
                headers=headers if authorized else {"Accept": headers["Accept"]})

        def result(response):
            body = response.json()["result"]
            assert not body.get("isError"), body
            return json.loads(body["content"][0]["text"])["result"]

        for project in ("aitelier", "wuxia-myth"):
            result(rpc("state_graph_write", "create_project",
                       {"project_id": project, "title": project}))
        write = {"project_id": "aitelier", "section": "temporary",
                 "content": "release handoff", "expected_revision": 0,
                 "director_identity": "aitelier-director", "operation": "replace"}
        assert rpc("state_graph_write", "update_driver_note", write,
                   authorized=False).status_code == 401
        note = result(rpc("state_graph_write", "update_driver_note", write))
        assert note["revision"] == 1 and note["temporary"] == "release handoff"
        assert result(rpc("state_graph_read", "get_driver_note",
                          {"project_id": "wuxia-myth"}))["revision"] == 0
        conflict = rpc("state_graph_write", "update_driver_note", write).json()["result"]
        assert conflict["isError"] is True
        assert "current 1" in conflict["content"][0]["text"]
        history = result(rpc("state_graph_read", "driver_note_history",
                             {"project_id": "aitelier"}))
        assert history["entries"][0]["director_identity"] == "aitelier-director"
        result(rpc("state_graph_write", "update_driver_note", {
            **write, "content": " release access_token=synthetic-secret", "expected_revision": 1,
            "operation": "append"}))
        search_args = {"project_id": "aitelier", "query": "release",
                       "section": "temporary", "actor": history["entries"][0]["actor"],
                       "director_identity": "aitelier-director", "after_revision": 0,
                       "min_revision": 1, "max_revision": 2,
                       "created_after": "2000-01-01T00:00:00Z",
                       "created_before": "2100-01-01T00:00:00+00:00",
                       "limit": 1, "excerpt_chars": 64}
        mcp_search = result(rpc("state_graph_read", "search_driver_note_history", search_args))
        assert mcp_search["entries"][0]["revision"] == 1
        assert mcp_search["truncated"] is True
        rest_search = client.get(
            "/api/state/projects/aitelier/driver-note/history/search",
            params=search_args, headers=headers)
        assert rest_search.status_code == 200
        assert rest_search.json() == mcp_search
        assert "synthetic-secret" not in json.dumps(mcp_search)
        empty = result(rpc("state_graph_read", "search_driver_note_history", {
            "project_id": "aitelier", "query": "does-not-exist"}))
        assert empty["entries"] == [] and empty["next_after_revision"] == 0
        schema = client.get("/api/state/schema", headers=headers).json()
        rest_schema = schema["operations"]["search_driver_note_history"]
        assert rest_schema["mutates"] is False
        assert rest_schema["arguments"]["properties"]["excerpt_chars"]["maximum"] == 1000
        rest = client.get("/api/state/projects/aitelier/driver-note", headers=headers)
        assert rest.status_code == 200
        assert rest.json()["temporary"].startswith("release handoff release")
        foreign = client.get("/api/state/projects/wuxia-myth/driver-note", headers=headers)
        assert foreign.status_code == 200 and foreign.json()["revision"] == 0


def test_project_scoped_note_cas_race_reloads_before_submit_and_redacts_identity(tmp_path):
    """Two isolated directors must lose/reload/submit against one revision."""
    from concurrent.futures import ThreadPoolExecutor
    from core.state_database import StateDatabase
    from core.state_service import StateService
    from core.state_graph import StateConflict

    db = StateDatabase(str(tmp_path / "handoff.sqlite"))
    first = StateService(db, actor="Authorization: Bearer synthetic-first")
    second = StateService(db, actor="Authorization: Bearer synthetic-second")
    first.create_project("project-a", "Project A")
    first.create_project("project-b", "Project B")
    for service in (first, second):
        # Construction on the same isolated DB is intentional: this models
        # two successors sharing State while project facts remain scoped.
        assert service.driver_notes.get("project-a")["revision"] == 0
    payload = {
        "section": "temporary",
        "expected_revision": 0,
        "operation": "replace",
    }

    def submit(service, director_identity, content):
        try:
            return ("won", service.driver_notes.update(
                "project-a", content=content, director_identity=director_identity, **payload))
        except StateConflict:
            current = service.driver_notes.get("project-a")
            return ("lost", current)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda args: submit(*args),
            ((first, "director_secret=synthetic-first", "first handoff"),
             (second, "director_secret=synthetic-second", "second handoff")),
        ))
    assert {outcome[0] for outcome in outcomes} == {"won", "lost"}
    winner = next(value for kind, value in outcomes if kind == "won")
    loser_read = next(value for kind, value in outcomes if kind == "lost")
    assert loser_read["revision"] == winner["revision"] == 1
    deliberate = second if outcomes[1][0] == "lost" else first
    committed = deliberate.driver_notes.update(
        "project-a", section="temporary", content="reloaded handoff",
        expected_revision=loser_read["revision"],
        director_identity="director_secret=synthetic-retry", operation="append")
    assert committed["revision"] == 2

    assert first.driver_notes.get("project-b")["revision"] == 0
    search = first.driver_notes.search(
        "project-a", query="handoff", excerpt_chars=64)
    assert search["entries"]
    serialized = json.dumps(search, ensure_ascii=False)
    assert "synthetic-first" not in serialized
    assert "secret-first" not in serialized
    assert all(entry["actor"] == "Authorization: Bearer [REDACTED]" for entry in search["entries"])
    assert all("synthetic-" not in entry["director_identity"] for entry in search["entries"])
    assert first.driver_notes.search("project-b", query="handoff")["entries"] == []
