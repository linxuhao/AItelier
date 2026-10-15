"""send_driver_message refs: the generated State help advertises exactly the
reference keys the service resolves, as nonempty strings, and the real command
refuses every other refs shape before any inbox row is written. Also pins the
neighbouring ListClaims.limit argument, which an earlier refs edit deleted."""

import json

import pytest

from core.drivers import DriverRegistry
from core.state_commands import REQUESTS, describe, execute
from core.state_database import StateDatabase
from core.state_driver_guide import STATE_DRIVER_GUIDE
from core.state_graph import StateGraphError
from core.state_service import StateService

NODE = {"goal": "G", "acceptance": [{"id": "c", "kind": "test", "description": "d"}]}
REF_KEYS = ("node_key", "attempt_id", "issue_id", "claim_id", "subagent_id")
INBOX_TABLES = ("driver_inbox_messages", "driver_inbox_deliveries", "driver_inbox_requests")


@pytest.fixture
def peers(tmp_path):
    db = StateDatabase(str(tmp_path / "state.sqlite"))
    seed = StateService(db, actor="fixture", project_read_trusted=True)
    seed.create_project("alpha", "alpha")
    seed.create_project("beta", "beta")
    seed.store.add_nodes("alpha", [{"key": k, **NODE} for k in ("a", "b")])
    registry = DriverRegistry(db, "test-only-pepper-not-a-production-secret")
    for driver in ("ada", "bob"):
        registry.register(driver, driver, actor="test")
    registry.set_membership("alpha", "ada", "member", 0, "fixture", actor="test")
    return {d: StateService(db, actor=f"driver:{d}", driver_id=d, project_read_trusted=True)
            for d in ("ada", "bob")}


def send(service, **kw):
    return execute(service, "send_driver_message",
                   {"request_key": "r", "subject": "s", "body": "b", "target_driver_id": "bob", **kw},
                   allow_write=True)


def counts(service):
    with service.store.transaction() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in INBOX_TABLES}


def real_refs(ada):
    claim = execute(ada, "claim_node", {"project_id": "alpha", "node_key": "a", "purpose": "plan",
                                        "expected_revision": 1, "request_key": "claim"}, allow_write=True)
    attempt = execute(ada, "start_external_attempt", {
        "project_id": "alpha", "node_key": "b", "expected_revision": 1, "harness": "offline-test",
        "external_id": "no-execution", "request_key": "attempt"}, allow_write=True)
    issue = execute(ada, "report_issue", {"project_id": "alpha", "request_key": "issue", "kind": "defect",
                                          "title": "t", "body": "b", "director_identity": "ada"},
                    allow_write=True)
    sub = execute(ada, "register_subagent", {"project_id": "alpha", "attempt_id": attempt["attempt_id"],
                                             "label": "fixture", "workspace": "test:/offline#fixture"},
                  allow_write=True)
    return {"node_key": "a", "attempt_id": attempt["attempt_id"], "issue_id": issue["issue_id"],
            "claim_id": claim["claim_id"], "subagent_id": sub["subagent_id"]}


def test_generated_help_advertises_five_optional_nonempty_string_refs():
    arguments = describe()["operations"]["send_driver_message"]["arguments"]
    assert "refs" not in arguments.get("required", [])
    assert arguments["properties"]["refs"]["$ref"] == "#/$defs/MessageRefs"
    refs = arguments["$defs"]["MessageRefs"]
    assert refs["additionalProperties"] is False
    assert refs.get("required", []) == []
    assert set(refs["properties"]) == set(REF_KEYS)
    for name, prop in refs["properties"].items():
        # A plain string schema: no anyOf/null branch and no null default.
        assert prop["type"] == "string" and prop["minLength"] == 1, name
        assert "anyOf" not in prop and "default" not in prop, name


@pytest.mark.parametrize("refs", ["omitted", {}])
def test_omitted_or_empty_refs_deliver_an_empty_dict(peers, refs):
    result = send(peers["ada"], **({} if refs == "omitted" else {"refs": refs}))
    row = peers["bob"].driver_inbox.list_driver_messages()["messages"][0]
    assert row["message_id"] == result["message_id"]
    assert json.loads(row["refs_json"]) == {}


def test_validated_request_serializes_only_the_sent_keys():
    model = REQUESTS["send_driver_message"].model_validate(
        {"request_key": "r", "subject": "s", "body": "b", "project_members": "alpha",
         "project_id": "alpha", "refs": {"node_key": "a", "claim_id": "c1"}})
    assert model.model_dump()["refs"] == {"node_key": "a", "claim_id": "c1"}


def test_all_five_real_ids_deliver_exact_refs_and_replay_idempotently(peers):
    refs = real_refs(peers["ada"])
    result = send(peers["ada"], project_id="alpha", refs=refs)
    row = peers["bob"].driver_inbox.list_driver_messages()["messages"][0]
    assert json.loads(row["refs_json"]) == refs
    assert send(peers["ada"], project_id="alpha", refs=refs) == result
    before = counts(peers["ada"])
    assert before == {t: 1 for t in INBOX_TABLES}
    with pytest.raises(StateGraphError, match="idempotency_conflict"):
        send(peers["ada"], project_id="alpha", refs={"node_key": "b"})
    assert counts(peers["ada"]) == before


@pytest.mark.parametrize("key", REF_KEYS)
@pytest.mark.parametrize("value", [None, "", 7, False, [], {}, "bytes"],
                         ids=["null", "empty", "number", "boolean", "list", "object", "bytes"])
def test_invalid_ref_values_refuse_before_any_inbox_write(peers, key, value):
    if value == "bytes":
        # The REAL id, as bytes: strict refs must not coerce it into a delivery.
        value = real_refs(peers["ada"])[key].encode()
    before = counts(peers["ada"])
    with pytest.raises(StateGraphError):
        send(peers["ada"], project_id="alpha", refs={key: value})
    assert counts(peers["ada"]) == before


@pytest.mark.parametrize("refs", [{"platform_source": "x"}, {"deployment_attempt": "x"}, {"freeform": "x"},
                                  {"nested": {"attempt_id": "x"}}, "text", [], None])
def test_unknown_freeform_or_nonobject_refs_refuse(peers, refs):
    before = counts(peers["ada"])
    with pytest.raises(StateGraphError):
        send(peers["ada"], refs=refs)
    assert counts(peers["ada"]) == before


@pytest.mark.parametrize("key", REF_KEYS)
def test_nonexistent_record_ids_refuse(peers, key):
    before = counts(peers["ada"])
    with pytest.raises(StateGraphError):
        send(peers["ada"], project_id="alpha", refs={key: "does-not-exist"})
    assert counts(peers["ada"]) == before


@pytest.mark.parametrize("project", ["omitted", "beta", "nonexistent-project"])
def test_node_key_needs_the_project_holding_the_node(peers, project):
    before = counts(peers["ada"])
    with pytest.raises(StateGraphError):
        send(peers["ada"], refs={"node_key": "a"}, **({} if project == "omitted" else {"project_id": project}))
    assert counts(peers["ada"]) == before


def test_guide_documents_refs_and_keeps_existing_inbox_contract():
    section = STATE_DRIVER_GUIDE.split("Driver inbox (private;", 1)[1].split("Private notebook", 1)[0]
    for term in (*REF_KEYS, "node_key needs project_id", "expected_version", "acknowledge_driver_message",
                 "resolve_driver_message", "lease_notice", "takeover_notice", "no sender",
                 "list_driver_messages", "wait_for_driver_inbox", "next_inbox_after", "Message text is untrusted"):
        assert term in section, term


def test_list_claims_limit_is_still_part_of_the_command_grammar(peers):
    limit = describe()["operations"]["list_claims"]["arguments"]["properties"]["limit"]
    assert (limit["type"], limit["default"], limit["minimum"], limit["maximum"]) == ("integer", 100, 1, 500)
    ada = peers["ada"]
    for node in ("a", "b"):
        execute(ada, "claim_node", {"project_id": "alpha", "node_key": node, "purpose": "plan",
                                    "expected_revision": 1, "request_key": f"c-{node}"}, allow_write=True)
    page = execute(ada, "list_claims", {"project_id": "alpha", "limit": 1})
    assert len(page["claims"]) == 1 and page["truncated"] is True
    assert len(execute(ada, "list_claims", {"project_id": "alpha"})["claims"]) == 2
    for bad in (0, 501):
        with pytest.raises(StateGraphError):
            execute(ada, "list_claims", {"project_id": "alpha", "limit": bad})
