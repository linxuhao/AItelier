import asyncio
import json

import pytest

from core.state_commands import (DRIVER_TOOL_DEFINITIONS, READ_REQUESTS, WRITE_REQUESTS,
                                 describe, execute)
from core.state_database import StateDatabase
from core.state_driver_notes import FREE_TEXT_CLOSED, NOTE_AFTER_REVISION_RETIRED
from core.state_graph import StateGraphError
from core.state_service import StateService
from tests.support.legacy_driver_note import seed_section


@pytest.fixture
def services(tmp_path):
    db_path = str(tmp_path / "state.sqlite")
    first = StateService(StateDatabase(db_path), actor="alice@example.test", project_read_trusted=True)
    first.create_project("aitelier", "AItelier")
    first.create_project("wuxia-myth", "Wuxia")
    return first, StateService(StateDatabase(db_path), actor="bob@example.test", project_read_trusted=True)


NOTE_KEYS = {"project_id", "revision", "index", "entry_count", "listed_count",
             "delisted_count", "superseded_count"}


def test_legacy_sections_stay_readable_as_project_scoped_history_only(services):
    alice, bob = services
    seed_section(alice, "aitelier", "permanent", "release rules", "aitelier-director")
    seed_section(bob, "wuxia-myth", "temporary", "combat batch", "wuxia-director")
    seed_section(alice, "aitelier", "temporary", "one", "aitelier-director", "append")
    seed_section(bob, "aitelier", "temporary", " + two", "relief-director", "append")
    history = alice.driver_notes.history("aitelier")
    assert [entry["revision"] for entry in history["entries"]] == [1, 2, 3]
    assert history["entries"][-1]["actor"] == "bob@example.test"
    assert history["entries"][-1]["temporary"] == "one + two"
    assert history["entries"][-1]["permanent"] == "release rules"
    assert alice.driver_notes.history("wuxia-myth")["entries"][0]["temporary"] == "combat batch"
    reopened = StateService(StateDatabase(alice.db.db_path), actor="handoff@example.test",
                            project_read_trusted=True)
    current = reopened.driver_notes.get("aitelier")
    assert set(current) == NOTE_KEYS and current["revision"] == 3


def test_get_driver_note_returns_the_index_and_never_section_text(services):
    """Goes red the moment any section text reappears in the get_driver_note payload."""
    service, _ = services
    seed_section(service, "aitelier", "permanent", "PERMANENT-CANARY-5f1", "lead")
    seed_section(service, "aitelier", "temporary", "TEMPORARY-CANARY-5f1", "lead")
    written = service.driver_notes.write_entry(
        "aitelier", "release r-17 needs two reviewers", "owner ruling", "lead")
    for note in (service.driver_notes.get("aitelier"),
                 execute(service, "get_driver_note", {"project_id": "aitelier"})):
        assert set(note) == NOTE_KEYS
        serialized = json.dumps(note)
        assert "CANARY-5f1" not in serialized
        assert note["revision"] == 2
        assert [item["address"] for item in note["index"]] == [written["address"]]
        assert note["index"][0]["index_line"].startswith("release r-17 needs two reviewers")
        assert (note["entry_count"], note["listed_count"], note["delisted_count"]) == (1, 1, 0)
    # The text is not gone: history still reads it.
    assert "PERMANENT-CANARY-5f1" in json.dumps(service.driver_notes.history("aitelier"))
    assert service.driver_notes.search("aitelier", "TEMPORARY-CANARY")["entries"]


@pytest.mark.parametrize("allow_write", [True, False])
def test_update_driver_note_is_refused_with_the_entry_instruction(services, allow_write):
    service, _ = services
    events = len(service.store.events("aitelier"))
    with pytest.raises(StateGraphError) as refused:
        execute(service, "update_driver_note", {
            "project_id": "aitelier", "section": "temporary", "content": "free text",
            "expected_revision": 0, "director_identity": "lead", "operation": "replace"},
            allow_write=allow_write)
    assert str(refused.value) == FREE_TEXT_CLOSED
    assert "write_driver_note_entry" in str(refused.value)
    # Refused, not silently accepted and not truncated: nothing was written.
    assert service.driver_notes.get("aitelier")["revision"] == 0
    assert service.driver_notes.history("aitelier")["entries"] == []
    assert len(service.store.events("aitelier")) == events
    # No surface advertises it and no in-process writer survives.
    assert "update_driver_note" not in WRITE_REQUESTS
    assert "update_driver_note" not in READ_REQUESTS
    assert "update_driver_note" not in describe()["operations"]
    enums = [tool["function"]["parameters"]["properties"]["action"]["enum"]
             for tool in DRIVER_TOOL_DEFINITIONS if tool["function"]["parameters"]["properties"]]
    assert all("update_driver_note" not in enum for enum in enums)
    assert not hasattr(service.driver_notes, "update")


def test_entry_writes_still_succeed_through_execute(services):
    service, _ = services
    written = execute(service, "write_driver_note_entry", {
        "project_id": "aitelier", "assertion": "release needs one reviewer",
        "body": "owner lead", "director_identity": "lead"}, allow_write=True)
    entry_id = written["address"].rsplit("/", 1)[-1]
    successor = execute(service, "supersede_driver_note_entry", {
        "project_id": "aitelier", "entry_id": entry_id,
        "assertion": "release needs two reviewers", "body": "owner lead; ruling r-2",
        "reason": "owner raised the bar", "director_identity": "lead",
        "force": "in_force"}, allow_write=True)
    # The superseded tombstone can no longer change a decision, so it may be delisted.
    delisted = execute(service, "delist_driver_note_entry", {
        "project_id": "aitelier", "entry_id": entry_id, "reason": "successor carries it",
        "director_identity": "lead"}, allow_write=True)
    assert delisted["listing"] == "delisted"
    note = execute(service, "get_driver_note", {"project_id": "aitelier"})
    assert [item["address"] for item in note["index"]] == [successor["successor"]["address"]]
    assert "[in force]" in note["index"][0]["index_line"]
    assert (note["entry_count"], note["listed_count"], note["delisted_count"],
            note["superseded_count"]) == (2, 1, 1, 1)


def test_search_is_project_scoped_filterable_redacted_and_stably_paginated(services, monkeypatch):
    alice, bob = services
    timestamps = iter([
        "2026-09-12T10:00:00.000000+00:00",
        "2026-09-12T11:00:00.000000+00:00",
        "2026-09-12T12:00:00.000000+00:00",
        "2026-09-12T13:00:00.000000+00:00",
    ])
    monkeypatch.setattr("core.state_driver_notes.now", lambda: next(timestamps))
    seed_section(alice, "aitelier", "permanent", "release alpha", "lead")
    seed_section(bob, "aitelier", "temporary",
                 "handoff target password=synthetic-password " + "x" * 120, "relief")
    seed_section(alice, "aitelier", "temporary", " target beta", "lead", "append")
    seed_section(bob, "wuxia-myth", "temporary", "target beta foreign", "relief")

    filtered = alice.driver_notes.search(
        "aitelier", "beta", section="temporary", actor="alice@example.test",
        director_identity="lead", min_revision=3, max_revision=3,
        created_after="2026-09-12T11:30:00Z",
        created_before="2026-09-12T12:30:00+00:00", excerpt_chars=64)
    assert [entry["revision"] for entry in filtered["entries"]] == [3]
    assert len(filtered["entries"][0]["excerpt"]) <= 64
    assert set(filtered["entries"][0]) == {
        "revision", "section", "operation", "actor", "director_identity",
        "created_at", "excerpt"}

    redacted = alice.driver_notes.search("aitelier", "handoff", excerpt_chars=1000)
    assert len(redacted["entries"]) == 2  # replace and later append snapshots
    assert all("synthetic-password" not in row["excerpt"] for row in redacted["entries"])
    assert all("password=[REDACTED]" in row["excerpt"] for row in redacted["entries"])
    assert alice.driver_notes.search("aitelier", "foreign")["entries"] == []

    first = alice.driver_notes.search("aitelier", "", limit=1)
    second = alice.driver_notes.search(
        "aitelier", "", after_revision=first["next_after_revision"], limit=1)
    third = alice.driver_notes.search(
        "aitelier", "", after_revision=second["next_after_revision"], limit=1)
    assert [first["entries"][0]["revision"], second["entries"][0]["revision"],
            third["entries"][0]["revision"]] == [1, 2, 3]
    assert first["truncated"] and second["truncated"] and not third["truncated"]


def test_search_compares_time_instants_and_unicode_casefold(services, monkeypatch):
    service, _ = services
    timestamps = iter([
        "2026-09-12T12:00:00.000000+00:00",
        "2026-09-12T12:00:00.000001+00:00",
    ])
    monkeypatch.setattr("core.state_driver_notes.now", lambda: next(timestamps))
    seed_section(service, "aitelier", "permanent", "Decision ÄPFEL", "lead")
    seed_section(service, "aitelier", "temporary", "one microsecond later", "lead")

    assert [row["revision"] for row in service.driver_notes.search(
        "aitelier", "äpfel")["entries"]] == [1]
    assert [row["revision"] for row in service.driver_notes.search(
        "aitelier", "", created_after="2026-09-12T14:00:00+02:00")["entries"]] == [2]
    assert service.driver_notes.search(
        "aitelier", "", created_before="2026-09-12T12:00:00Z")["entries"] == []
    assert [row["revision"] for row in service.driver_notes.search(
        "aitelier", "", created_after="2026-09-12T12:00:00.000000Z")["entries"]] == [2]
    assert service.driver_notes.search(
        "aitelier", "", created_after="2026-09-12T12:00:00.000001Z")["entries"] == []
    assert [row["revision"] for row in service.driver_notes.search(
        "aitelier", "", created_before="2026-09-12T12:00:00.000001Z")["entries"]] == [1]


def test_search_redacts_slack_tokens_and_identity_metadata(services):
    service, _ = services
    secret_actor = "sk_abcdefghijklmnop1234"
    writer = StateService(StateDatabase(service.db.db_path), actor=secret_actor,
                          project_read_trusted=True)
    slack_token = "".join(("xo", "xb-1234567890-abcdefghijklmnop"))
    seed_section(writer, "aitelier", "temporary",
                 f"handoff {slack_token} password=synthetic-password",
                 "api_key=director-secret")
    result = writer.driver_notes.search("aitelier", "handoff", excerpt_chars=1000)
    serialized = json.dumps(result)
    assert secret_actor not in serialized
    assert "director-secret" not in serialized
    assert slack_token not in serialized
    assert "synthetic-password" not in serialized
    assert result["entries"][0]["actor"] == "[REDACTED]"
    assert result["entries"][0]["director_identity"] == "api_key=[REDACTED]"


@pytest.mark.asyncio
async def test_any_filter_wakes_from_any_selected_source(services):
    a, _ = services
    a.store.add_nodes("aitelier", [
        {"key": "selected", "goal": "Selected", "acceptance": [
            {"id": "check", "kind": "test", "description": "check"}]},
        {"key": "other", "goal": "Other", "acceptance": [
            {"id": "check", "kind": "test", "description": "check"}]},
    ])
    attempt = a.external.register(
        "aitelier", "other", 1, "test", "worker", "request")
    cursor = a.store.events("aitelier")[-1]["seq"]
    a.external.observe(attempt["attempt_id"], "paused", 0, attempt["context_hash"],
        "paused", "private/report", "a" * 64)
    any_result = await a.wait_for_state_change(
        "aitelier", after=cursor, node_keys=["selected"],
        attempt_ids=[attempt["attempt_id"]], filter_mode="any", timeout_seconds=0)
    assert any_result["events"][0]["payload"]["attempt_id"] == attempt["attempt_id"]
    consumed = a.store.events("aitelier")[-1]["seq"]
    snapshot = await a.wait_for_state_change(
        "aitelier", after=consumed, attempt_ids=[attempt["attempt_id"]], filter_mode="any",
        return_when_idle=True, timeout_seconds=0)
    assert snapshot["reason"] == "action_required"
    assert snapshot["attempts"][0]["attempt_id"] == attempt["attempt_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revision", [0, 1])
async def test_note_after_revision_is_refused_loudly_and_plain_idle_stays_fast(services, revision):
    service, _ = services
    cursor = service.store.events("aitelier")[-1]["seq"]
    idle = await service.wait_for_state_change(
        "aitelier", after=cursor, return_when_idle=True, timeout_seconds=900)
    assert idle["reason"] == "nothing_to_wait" and not idle["timed_out"]
    # A wait that could never wake is refused at once, never left to hang until
    # its timeout. asyncio.wait_for turns a regression into a fast failure.
    with pytest.raises(StateGraphError) as direct:
        await asyncio.wait_for(service.wait_for_state_change(
            "aitelier", after=cursor, note_after_revision=revision,
            return_when_idle=True, timeout_seconds=900), 2)
    assert str(direct.value) == NOTE_AFTER_REVISION_RETIRED
    with pytest.raises(StateGraphError) as typed:
        await asyncio.wait_for(execute(service, "wait_for_state_change", {
            "project_id": "aitelier", "after": cursor, "note_after_revision": revision,
            "return_when_idle": True, "timeout_seconds": 900}), 2)
    assert str(typed.value) == NOTE_AFTER_REVISION_RETIRED
    assert "no longer advances" in NOTE_AFTER_REVISION_RETIRED
