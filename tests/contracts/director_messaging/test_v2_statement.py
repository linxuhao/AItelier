from pathlib import Path
import json

import jsonschema
import pytest

from contracts.director_messaging.v2.conformance import load_vectors, run_vectors
from contracts.director_messaging.v2.fake import (
    DirectorMessageError,
    InMemoryDirectorMessaging,
)


ROOT = Path(__file__).parents[3]
CONTRACT = ROOT / "contracts" / "director_messaging" / "v2"


class FakeHarness:
    def reset(self, project_ids):
        self.provider = InMemoryDirectorMessaging(project_ids, "bootstrap")

    def for_actor(self, actor):
        return self.provider.for_actor(actor)


def _send(fake, key, *, mode=None, subject="subject", body="body"):
    arguments = {
        "sender_project_id": "alpha",
        "director_identity": "director",
        "request_key": key,
        "subject": subject,
        "body": body,
        "target_project_id": "beta",
    }
    if mode is not None:
        arguments["delivery_mode"] = mode
    return fake.send_director_message(**arguments)


def test_v1_vectors_with_only_version_expectations_changed_pass_v2_fake():
    vectors = load_vectors()
    expected = sum(len(scenario["steps"]) for scenario in vectors["scenarios"])
    assert run_vectors(FakeHarness(), vectors) == expected
    assert expected >= 25


def test_v1_vector_scenarios_are_preserved_before_v2_delta_vectors():
    v1 = json.loads((ROOT / "contracts" / "director_messaging" / "v1" /
                     "vectors.json").read_text(encoding="utf-8"))
    v2 = load_vectors()
    transformed = json.loads(json.dumps(v1).replace(
        "aitelier.director-messaging.conformance.v1",
        "aitelier.director-messaging.conformance.v2").replace(
        "aitelier.director-messaging.v1", "aitelier.director-messaging.v2"))
    assert v2["project_ids"] == transformed["project_ids"]
    assert v2["scenarios"][:len(transformed["scenarios"])] == transformed["scenarios"]
    assert len(v2["scenarios"]) == len(transformed["scenarios"]) + 1


def test_v2_schema_is_closed_and_declares_delta_fields():
    schema = json.loads((CONTRACT / "schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    assert schema["$defs"]["schemaTag"] == {"const": "aitelier.director-messaging.v2"}
    assert schema["$defs"]["message"]["properties"]["delivery_mode"]["enum"] == [
        "transient", "standing"]
    assert schema["$defs"]["listSuccess"]["properties"]["result"]["required"] == [
        "project_id", "items", "matched_total", "has_more", "next_after"]


def test_statement_declares_safe_legacy_migration_without_binding_it():
    readme = (CONTRACT / "README.md").read_text(encoding="utf-8")
    assert "delivery_mode TEXT NOT NULL DEFAULT 'transient'" in readme
    assert "Existing rows become transient" in readme
    assert "retaining every existing ID, content" in readme
    assert "real SQLite migration" in readme
    assert "separate proof attempt" in readme


def test_default_explicit_and_reply_noninheritance_modes():
    fake = InMemoryDirectorMessaging(["alpha", "beta"], "actor")
    transient = _send(fake, "transient")
    standing = _send(fake, "standing", mode="standing")
    cited = standing["result"]["deliveries"][0]["delivery_id"]
    reply = fake.for_actor("actor-b").send_director_message(
        "beta", "director-b", "reply", "reply", "body",
        reply_to_delivery_id=cited)
    assert transient["result"]["message"]["delivery_mode"] == "transient"
    assert standing["result"]["message"]["delivery_mode"] == "standing"
    assert reply["result"]["message"]["delivery_mode"] == "transient"
    with pytest.raises(DirectorMessageError, match="invalid_request"):
        _send(fake, "bad", mode="durable")


def test_filtered_high_water_pagination_sparse_empty_and_final_pages():
    fake = InMemoryDirectorMessaging(["alpha", "beta"], "actor")
    one = _send(fake, "one", mode="standing")
    _send(fake, "two")
    three = _send(fake, "three", mode="standing")
    fake.acknowledge_director_message(
        "beta", one["result"]["deliveries"][0]["delivery_id"], 1, "ack")

    first = fake.list_director_messages(
        "beta", limit=1, delivery_mode="standing",
        statuses=["unread", "acknowledged"])["result"]
    assert [item["delivery"]["delivery_seq"] for item in first["items"]] == [1]
    assert first == {**first, "matched_total": 2, "has_more": True, "next_after": 1}

    final = fake.list_director_messages(
        "beta", after=first["next_after"], limit=1, delivery_mode="standing",
        statuses=["unread", "acknowledged"])["result"]
    assert final["items"][0]["delivery"]["delivery_id"] == \
        three["result"]["deliveries"][0]["delivery_id"]
    assert (final["matched_total"], final["has_more"], final["next_after"]) == (1, False, 3)

    empty = fake.list_director_messages(
        "beta", after=1, delivery_mode="transient", statuses=["resolved"])["result"]
    assert empty["items"] == []
    assert (empty["matched_total"], empty["has_more"], empty["next_after"]) == (0, False, 3)


@pytest.mark.parametrize("statuses", [[], ["unread", "unread"], ["missing"], "unread"])
def test_status_filter_is_nonempty_duplicate_free_known_list(statuses):
    fake = InMemoryDirectorMessaging(["alpha", "beta"], "actor")
    with pytest.raises(DirectorMessageError, match="invalid_request"):
        fake.list_director_messages("beta", statuses=statuses)


def test_standing_survives_fake_restart_and_resolution_removes_only_recovery_projection():
    fake = InMemoryDirectorMessaging(["alpha", "beta"], "actor")
    sent = _send(fake, "standing", mode="standing")
    delivery_id = sent["result"]["deliveries"][0]["delivery_id"]
    restarted = fake.restart()
    assert restarted.list_director_messages(
        "beta", delivery_mode="standing", statuses=["unread", "acknowledged"]
    )["result"]["matched_total"] == 1
    restarted.acknowledge_director_message("beta", delivery_id, 1, "ack")
    restarted.resolve_director_message("beta", delivery_id, 2, "resolve")
    assert restarted.project_active_standing("beta") == ""
    audited = restarted.list_director_messages(
        "beta", delivery_mode="standing", statuses=["resolved"])["result"]
    assert audited["matched_total"] == 1


def test_projection_redacts_independently_is_bounded_and_never_mutates():
    fake = InMemoryDirectorMessaging(["alpha", "beta"], "actor")
    for index in range(10):
        _send(fake, f"standing-{index}", mode="standing",
              subject=f"standing-{index}", body=f"body-{index}")
    _send(fake, "transient", body="must-never-project")
    before_events = list(fake._memory.events["beta"])
    before = fake.list_director_messages("beta")["result"]["items"]
    projection = fake.project_active_standing("beta")
    lines = projection.splitlines()
    assert len(projection) <= 3000
    assert lines[-1] == "[omitted_active_standing=2]"
    payloads = [json.loads(line) for line in lines[:-1]]
    assert len(payloads) == 8
    assert [payload["delivery_seq"] for payload in payloads] == list(range(1, 9))
    assert "must-never-project" not in projection
    assert fake.list_director_messages("beta")["result"]["items"] == before
    assert fake._memory.events["beta"] == before_events


def test_projection_replaces_whole_lines_until_marker_fits():
    fake = InMemoryDirectorMessaging(["alpha", "beta"], "actor", redactor=lambda value: value)
    for index in range(8):
        _send(fake, f"large-{index}", mode="standing", subject="S" * 200,
              body="B" * 8000)
    projection = fake.project_active_standing("beta")
    assert len(projection) <= 3000
    assert projection.splitlines()[-1].startswith("[omitted_active_standing=")
    for line in projection.splitlines()[:-1]:
        json.loads(line)


def test_projection_redacts_subject_and_body_independently_then_limits_body():
    fake = InMemoryDirectorMessaging(["alpha", "beta"], "actor")
    _send(fake, "secret", mode="standing",
          subject="api_key=sk_abcdefghijklmnopqrstuvwxyz",
          body="secret=abcdefghijklmnopqrstuvwxyz " + "界" * 500)
    payload = json.loads(fake.project_active_standing("beta"))
    assert "abcdefghijklmnopqrstuvwxyz" not in payload["subject"]
    assert "abcdefghijklmnopqrstuvwxyz" not in payload["body_excerpt"]
    assert len(payload["body_excerpt"]) <= 320


def test_projection_and_listing_are_project_isolated():
    fake = InMemoryDirectorMessaging(["alpha", "beta", "gamma"], "actor")
    _send(fake, "beta", mode="standing")
    fake.send_director_message(
        "alpha", "director", "gamma", "gamma-only", "body", "gamma",
        delivery_mode="standing")
    assert "gamma-only" not in fake.project_active_standing("beta")
    assert "gamma-only" in fake.project_active_standing("gamma")


def test_list_and_transitions_create_no_receipt_replay():
    fake = InMemoryDirectorMessaging(["alpha", "beta"], "actor")
    sent = _send(fake, "one", mode="standing")
    delivery_id = sent["result"]["deliveries"][0]["delivery_id"]
    assert len(fake._memory.events["beta"]) == 1
    fake.list_director_messages("beta")
    fake.project_active_standing("beta")
    fake.acknowledge_director_message("beta", delivery_id, 1, "ack")
    fake.resolve_director_message("beta", delivery_id, 2, "resolve")
    assert len(fake._memory.events["beta"]) == 1


def test_statement_is_transport_neutral_and_contains_no_real_binding():
    sources = "\n".join(path.read_text(encoding="utf-8") for path in CONTRACT.glob("*.py"))
    assert "sqlite3" not in sources
    assert "FastAPI" not in sources
    assert "register_state_tools" not in sources
    assert "PostCompact" not in sources
