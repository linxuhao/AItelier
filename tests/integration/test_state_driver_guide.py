"""Real MCP prompt/resource discovery and tool fallback share one protocol."""
import hashlib
import json
from fastapi.testclient import TestClient
from api.state_only import create_app
from core.state_driver_guide import (GUIDE_SECTIONS, STATE_DRIVER_GUIDE,
                                     STATE_DRIVER_GUIDE_INDEX)


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
        # The injected field is the INDEX; the full guide is one fetch away.
        assert help_body["driver_guide"] == STATE_DRIVER_GUIDE_INDEX
        assert help_body["driver_guide_full_chars"] == len(STATE_DRIVER_GUIDE)
        assert len(help_body["driver_guide"]) < len(STATE_DRIVER_GUIDE) / 4
        addresses = help_body["driver_guide_sections"]
        assert addresses == [f"guide://{slug}" for slug in GUIDE_SECTIONS]
        for address in addresses:
            fetched = rpc("tools/call", {"name": "state_graph_read", "arguments": {
                "action": "get_driver_guide_section",
                "arguments": {"address": address}}}).json()["result"]
            assert not fetched.get("isError"), fetched
            section = json.loads(fetched["content"][0]["text"])["result"]
            assert section["text"] in STATE_DRIVER_GUIDE
        missing = rpc("tools/call", {"name": "state_graph_read", "arguments": {
            "action": "get_driver_guide_section",
            "arguments": {"address": "guide://not-a-section"}}}).json()["result"]
        assert missing["isError"] is True
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
        report_help = evidence_schema["arguments"]["properties"]["report_ref"]["description"]
        assert "status=completed" in report_help
        assert "status=candidate" in report_help
        assert "criterion_id, artifact and verdict" in report_help
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


def test_mcp_driver_notes_refuse_free_text_and_serve_entries_and_history(tmp_path):
    from core.state_driver_index import INFORMATIONAL_CLOSED
    from core.state_driver_notes import FREE_TEXT_CLOSED
    from tests.support.legacy_driver_note import seed_section

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
        # The retired free-text write is refused loudly on MCP and REST alike.
        refused = rpc("state_graph_write", "update_driver_note", write).json()["result"]
        assert refused["isError"] is True
        assert refused["content"][0]["text"] == (
            "Error executing tool state_graph_write: " + FREE_TEXT_CLOSED)
        rest_refused = client.post("/api/state/commands/update_driver_note", json=write,
                                   headers=headers)
        assert rest_refused.status_code == 422
        assert rest_refused.json()["detail"] == FREE_TEXT_CLOSED
        # An informational entry is refused verbatim on MCP and REST alike.
        informational = {"project_id": "aitelier", "assertion": "release handoff in flight",
                         "body": "owner aitelier-director",
                         "director_identity": "aitelier-director", "force": "informational"}
        refused = rpc("state_graph_write", "write_driver_note_entry", informational).json()["result"]
        assert refused["isError"] is True
        assert refused["content"][0]["text"] == (
            "Error executing tool state_graph_write: " + INFORMATIONAL_CLOSED)
        rest_refused = client.post("/api/state/commands/write_driver_note_entry",
                                   json=informational, headers=headers)
        assert rest_refused.status_code == 422
        assert rest_refused.json()["detail"] == INFORMATIONAL_CLOSED
        # Rules are the write path.
        entry = result(rpc("state_graph_write", "write_driver_note_entry", {
            **informational, "assertion": "release handoff needs two reviewers",
            "force": "in_force"}))
        note = result(rpc("state_graph_read", "get_driver_note", {"project_id": "aitelier"}))
        assert [item["address"] for item in note["index"]] == [entry["address"]]
        rest_supersede = client.post("/api/state/commands/supersede_driver_note_entry", json={
            **informational, "entry_id": entry["entry_id"], "reason": "x"}, headers=headers)
        assert rest_supersede.status_code == 422
        assert rest_supersede.json()["detail"] == INFORMATIONAL_CLOSED
        assert "permanent" not in note and "temporary" not in note
        assert result(rpc("state_graph_read", "get_driver_note",
                          {"project_id": "wuxia-myth"}))["index"] == []

        # Retired section text from before the closure stays searchable history.
        service = app.state.state_service
        seed_section(service, "aitelier", "temporary", "release handoff", "aitelier-director")
        history = result(rpc("state_graph_read", "driver_note_history",
                             {"project_id": "aitelier"}))
        assert history["entries"][0]["director_identity"] == "aitelier-director"
        seed_section(service, "aitelier", "temporary", " release access_token=synthetic-secret",
                     "aitelier-director", "append")
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
        assert "update_driver_note" not in schema["operations"]
        rest_schema = schema["operations"]["search_driver_note_history"]
        assert rest_schema["mutates"] is False
        assert rest_schema["arguments"]["properties"]["excerpt_chars"]["maximum"] == 1000
        rest = client.get("/api/state/projects/aitelier/driver-note", headers=headers)
        assert rest.status_code == 200
        assert "release access_token" not in rest.text and "temporary" not in rest.json()
        assert rest.json()["revision"] == 2 and rest.json()["entry_count"] == 1
        foreign = client.get("/api/state/projects/wuxia-myth/driver-note", headers=headers)
        assert foreign.status_code == 200 and foreign.json()["revision"] == 0

        # Entry search and the superseded filter, on MCP and REST alike.
        successor = result(rpc("state_graph_write", "supersede_driver_note_entry", {
            "project_id": "aitelier", "entry_id": entry["entry_id"],
            "assertion": "release handoff needs three reviewers", "body": "ruling",
            "reason": "raised", "director_identity": "aitelier-director"}))["successor"]
        search = {"project_id": "aitelier", "query": "REVIEWERS"}
        mcp_hits = result(rpc("state_graph_read", "search_driver_note_entries", search))
        assert [hit["address"] for hit in mcp_hits["entries"]] == [successor["address"]]
        rest_hits = client.post("/api/state/query/search_driver_note_entries", json=search,
                                headers=headers)
        assert rest_hits.status_code == 200 and rest_hits.json() == mcp_hits
        wide = result(rpc("state_graph_read", "search_driver_note_entries",
                          {**search, "include_superseded": True}))
        assert [hit["address"] for hit in wide["entries"]] == [
            entry["address"], successor["address"]]
        current = client.get("/api/state/projects/aitelier/driver-note", headers=headers).json()
        assert [item["address"] for item in current["index"]] == [successor["address"]]
        assert current["superseded_count"] == 1
        widened = client.get("/api/state/projects/aitelier/driver-note",
                             params={"include_superseded": "true"}, headers=headers).json()
        assert [item["address"] for item in widened["index"]] == [
            entry["address"], successor["address"]]
        assert widened["index"][0]["superseded_by"] == successor["address"]
        assert schema["operations"]["search_driver_note_entries"]["mutates"] is False


def test_project_scoped_entry_supersede_race_has_one_winner_and_the_loser_reloads(tmp_path):
    """Two isolated directors race to supersede one entry; the loser reloads and retries."""
    from concurrent.futures import ThreadPoolExecutor
    from core.state_database import StateDatabase
    from core.state_service import StateService
    from core.state_graph import StateConflict

    db = StateDatabase(str(tmp_path / "handoff.sqlite"))
    first = StateService(db, actor="director-first", project_read_trusted=True)
    second = StateService(db, actor="director-second", project_read_trusted=True)
    first.create_project("project-a", "Project A")
    first.create_project("project-b", "Project B")
    shared = first.driver_notes.write_entry(
        "project-a", "release needs one reviewer", "owner first", "first")
    shared_id = shared["address"].rsplit("/", 1)[1]

    def submit(service, director_identity, assertion):
        try:
            return ("won", service.driver_notes.supersede_entry(
                "project-a", shared_id, assertion, "ruling body", "ruling changed",
                director_identity))
        except StateConflict as exc:
            return ("lost", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(
            lambda args: submit(*args),
            ((first, "first", "release needs two reviewers"),
             (second, "second", "release needs three reviewers")),
        ))
    assert sorted(kind for kind, _ in outcomes) == ["lost", "won"]
    winner = next(value for kind, value in outcomes if kind == "won")
    refusal = next(value for kind, value in outcomes if kind == "lost")
    assert winner["successor"]["address"] in refusal
    # The loser reloads the index and supersedes the successor it names.
    current = second.driver_notes.get("project-a")
    assert [item["address"] for item in current["index"]] == [winner["successor"]["address"]]
    assert current["superseded_count"] == 1
    retried = second.driver_notes.supersede_entry(
        "project-a", winner["successor"]["entry_id"], "release needs four reviewers", "body",
        "reloaded", "second")
    assert retried["entry_count"] == 3 and retried["superseded"]["lifecycle"] == "superseded"
    assert first.driver_notes.get("project-b")["entry_count"] == 0


def test_documented_criterion_envelope_matches_real_validators():
    """Exercise the guide's JSON, including the external/evidence status distinction."""
    import pytest
    from core.state_graph import StateConflict
    from core.state_report_integrity import (
        _structured_terminal, validate_evidence_semantics, validate_external_semantics)

    section = GUIDE_SECTIONS["handle-changes-without-inventing-acceptance"]["text"]
    report = section.split("```json\n", 1)[1].split("\n```", 1)[0].encode()
    envelope = _structured_terminal(report)
    criterion, artifact, verdict = (
        envelope["criterion_id"], envelope["artifact"], envelope["verdict"])
    validate_evidence_semantics(report, criterion, verdict, artifact)
    validate_external_semantics(report, "candidate", artifact)

    candidate = json.dumps({**envelope, "status": "candidate"}).encode()
    _structured_terminal(candidate)
    with pytest.raises(StateConflict, match="evidence report status is not completed"):
        validate_evidence_semantics(candidate, criterion, verdict, artifact)
    validate_external_semantics(candidate, "candidate", artifact)
