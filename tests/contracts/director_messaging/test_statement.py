from concurrent.futures import ThreadPoolExecutor
import inspect
import json
from pathlib import Path

import jsonschema
import pytest

from contracts.director_messaging.v1.conformance import load_vectors, run_vectors
from contracts.director_messaging.v1.fake import (
    ACTIONS,
    DirectorMessageError,
    InMemoryDirectorMessaging,
)


ROOT = Path(__file__).parents[3]
CONTRACT = ROOT / "contracts" / "director_messaging" / "v1"


class FakeHarness:
    def reset(self, project_ids):
        self.provider = InMemoryDirectorMessaging(project_ids, "bootstrap")

    def for_actor(self, actor):
        return self.provider.for_actor(actor)


def test_literal_vectors_pass_unchanged_against_standalone_fake():
    vectors = load_vectors()
    expected = sum(len(scenario["steps"]) for scenario in vectors["scenarios"])
    assert run_vectors(FakeHarness(), vectors) == expected
    assert expected >= 25


def test_protocol_schema_is_closed_and_rejects_extra_envelope_fields():
    schema = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    error = {
        "schema": "aitelier.director-messaging.v1",
        "code": "unknown_project",
        "detail": {"message": "unknown_project"},
    }
    jsonschema.validate(error, schema)
    error["extra"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(error, schema)


def test_exact_database_id_uses_normalized_length_without_normalizing_value():
    decomposed = "e\u0301" * 200
    normalized = "é" * 200
    fake = InMemoryDirectorMessaging(["sender", decomposed], "actor")
    sent = fake.send_director_message(
        "sender", "director", "unicode-id", "subject", "", decomposed)
    assert sent["result"]["deliveries"][0]["target_project_id"] == decomposed
    with pytest.raises(DirectorMessageError) as caught:
        fake.list_director_messages(normalized)
    assert caught.value.code == "unknown_project"
    schema = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(sent, schema)


def test_fake_exposes_the_four_declared_actions_without_sqlite_or_transport():
    assert ACTIONS == (
        "send_director_message",
        "list_director_messages",
        "acknowledge_director_message",
        "resolve_director_message",
    )
    source = inspect.getsource(InMemoryDirectorMessaging)
    assert "sqlite" not in source.lower()
    assert "mcp" not in source.lower()
    assert "wait_for_state_change" not in source


def test_event_summary_uses_shared_redactor_and_has_closed_payload():
    fake = InMemoryDirectorMessaging(["a", "b"], "actor")
    sent = fake.send_director_message(
        sender_project_id="a",
        director_identity="director",
        request_key="secret-event",
        subject="Token",
        body="api_key=sk_abcdefghijklmnopqrstuvwxyz",
        target_project_id="b",
    )
    event = fake._memory.events["b"][0]
    assert event["event_type"] == "director_message_received"
    assert event["node_key"] is None
    assert event["actionable"] is True
    assert set(event["payload"]) == {"message_id", "thread_id", "delivery_id", "summary"}
    assert event["payload"]["delivery_id"] == sent["result"]["deliveries"][0]["delivery_id"]
    assert "abcdefghijklmnopqrstuvwxyz" not in event["payload"]["summary"]
    assert len(event["payload"]["summary"]) <= 320


def test_concurrent_sends_allocate_unique_contiguous_inbox_sequences():
    fake = InMemoryDirectorMessaging(["a", "b"], "actor")

    def send(index):
        return fake.send_director_message(
            sender_project_id="a",
            director_identity="director",
            request_key=f"request-{index}",
            subject=f"subject-{index}",
            body="",
            target_project_id="b",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(send, range(20)))
    deliveries = [response["result"]["deliveries"][0] for response in responses]
    assert len({item["delivery_id"] for item in deliveries}) == 20
    inbox = fake.list_director_messages("b")["result"]["items"]
    assert [item["delivery"]["delivery_seq"] for item in inbox] == list(range(1, 21))


def test_failed_mutation_does_not_reserve_request_key():
    fake = InMemoryDirectorMessaging(["a", "b"], "actor")
    with pytest.raises(DirectorMessageError) as caught:
        fake.send_director_message("a", "director", "same", "subject", "", "missing")
    assert caught.value.code == "unknown_project"
    result = fake.send_director_message("a", "director", "same", "subject", "", "b")
    assert result["result"]["replayed"] is False


def test_contract_requires_agent_neutral_future_guidance():
    readme = (CONTRACT / "README.md").read_text(encoding="utf-8")
    assert "agent-neutral" in readme
    assert "Codex, Claude, a workflow, and an external harness" in readme
    assert "only as transport examples" in readme
