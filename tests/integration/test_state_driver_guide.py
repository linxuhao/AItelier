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
