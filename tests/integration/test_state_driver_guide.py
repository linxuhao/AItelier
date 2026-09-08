"""Real MCP prompt/resource discovery and tool fallback share one protocol."""
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
            write("report_external_attempt", {"attempt_id": attempt["attempt_id"],
                "observation_id": "final", "expected_version": 0,
                "context_hash": attempt["context_hash"], "status": "candidate",
                "report_ref": "test/report", "report_sha256": "b" * 64,
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
