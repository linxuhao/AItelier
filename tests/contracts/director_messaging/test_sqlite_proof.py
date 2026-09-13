from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from contracts.director_messaging.v1.conformance import load_vectors, run_vectors
from contracts.director_messaging.v1.fake import DirectorMessageError
from core.director_messaging import SQLiteDirectorMessaging
from core.state_database import StateDatabase
from core.state_graph import StateGraphStore
from core.state_service import StateService


class SQLiteHarness:
    def __init__(self, root: Path):
        self.root = root
        self.index = 0

    def reset(self, project_ids):
        self.index += 1
        db = StateDatabase(str(self.root / f"vectors-{self.index}.sqlite"))
        service = StateService(db, actor="bootstrap")
        for project_id in project_ids:
            service.create_project(project_id, project_id)
        self.provider = service.director_messages

    def for_actor(self, actor):
        return self.provider.for_actor(actor)


def _service(path: Path, actor="transport-a"):
    return StateService(StateDatabase(str(path)), actor=actor)


def _projects(service, *project_ids):
    for project_id in project_ids:
        service.create_project(project_id, project_id)


def _send(provider, key="send", target="beta", **overrides):
    arguments = {
        "sender_project_id": "alpha",
        "director_identity": "director",
        "request_key": key,
        "subject": "subject",
        "body": "body",
        "target_project_id": target,
    }
    arguments.update(overrides)
    return provider.send_director_message(**arguments)


def test_literal_vectors_pass_unchanged_against_sqlite(tmp_path):
    vectors = load_vectors()
    expected = sum(len(scenario["steps"]) for scenario in vectors["scenarios"])
    assert run_vectors(SQLiteHarness(tmp_path), vectors) == expected


def test_additive_schema_migrates_existing_state_database_and_survives_restart(tmp_path):
    path = tmp_path / "existing.sqlite"
    database = StateDatabase(str(path))
    store = StateGraphStore(database)
    store.create_project("alpha", "alpha")
    store.create_project("beta", "beta")
    with database.get_connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'state_director_%'").fetchone()[0] == 0
    first = StateService(database, actor="transport-a")
    sent = _send(first.director_messages)
    delivery_id = sent["result"]["deliveries"][0]["delivery_id"]

    reopened = _service(path)
    listed = reopened.director_messages.list_director_messages("beta")
    assert listed["result"]["items"][0]["delivery"]["delivery_id"] == delivery_id
    replay = reopened.director_messages.for_actor("transport-a").send_director_message(
        "alpha", "director", "send", "subject", "body", "beta")
    assert replay["result"]["replayed"] is True
    with reopened.db.get_connection() as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'state_director_%'")}
    assert names == {"state_director_messages", "state_director_deliveries",
                     "state_director_inbox_sequences", "state_director_idempotency"}


def test_duplicate_send_and_version_races_are_atomic(tmp_path):
    path = tmp_path / "races.sqlite"
    service = _service(path)
    _projects(service, "alpha", "beta")

    def duplicate():
        return _service(path).director_messages.for_actor("transport-a").send_director_message(
            "alpha", "director", "same", "subject", "body", "beta")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: duplicate(), range(2)))
    assert sorted(item["result"]["replayed"] for item in results) == [False, True]
    delivery_id = results[0]["result"]["deliveries"][0]["delivery_id"]
    assert len(service.director_messages.list_director_messages("beta")["result"]["items"]) == 1

    def ack(key):
        provider = _service(path).director_messages.for_actor("transport-a")
        try:
            return provider.acknowledge_director_message("beta", delivery_id, 1, key)
        except DirectorMessageError as exc:
            return exc.as_dict()

    with ThreadPoolExecutor(max_workers=2) as pool:
        raced = list(pool.map(ack, ("ack-a", "ack-b")))
    assert [item.get("code", "success") for item in raced] == ["success", "success"]
    assert {item["result"]["delivery"]["version"] for item in raced} == {2}
    with pytest.raises(DirectorMessageError, match="version_conflict"):
        service.director_messages.resolve_director_message("beta", delivery_id, 1, "stale-resolve")


def test_broadcast_snapshot_excludes_project_created_after_snapshot(tmp_path):
    path = tmp_path / "snapshot.sqlite"
    service = _service(path)
    _projects(service, "alpha", "beta")
    entered = threading.Event()
    release = threading.Event()

    def blocking_redactor(value):
        entered.set()
        assert release.wait(2)
        return value

    provider = SQLiteDirectorMessaging(service.store, "transport-a", redactor=blocking_redactor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(provider.send_director_message,
            "alpha", "director", "broadcast", "subject", "body", None, True, None)
        assert entered.wait(1)
        def create_later():
            return _service(path).create_project("gamma", "gamma")
        creating = pool.submit(create_later)
        release.set()
        result = sending.result(timeout=3)
        creating.result(timeout=3)
    assert [d["target_project_id"] for d in result["result"]["deliveries"]] == ["beta"]
    assert service.director_messages.list_director_messages("gamma")["result"]["items"] == []


def test_concurrent_duplicate_broadcast_and_reply_create_each_delivery_once(tmp_path):
    path = tmp_path / "duplicate-broadcast-reply.sqlite"
    service = _service(path)
    _projects(service, "alpha", "beta", "gamma")

    def broadcast():
        return _service(path).director_messages.for_actor("transport-a").send_director_message(
            "alpha", "director", "broadcast-same", "subject", "body",
            broadcast=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        broadcasts = list(pool.map(lambda _: broadcast(), range(2)))
    assert sorted(item["result"]["replayed"] for item in broadcasts) == [False, True]
    assert len(service.director_messages.list_director_messages("beta")["result"]["items"]) == 1
    assert len(service.director_messages.list_director_messages("gamma")["result"]["items"]) == 1
    cited = broadcasts[0]["result"]["deliveries"][0]["delivery_id"]

    def reply():
        return _service(path).director_messages.for_actor("transport-b").send_director_message(
            "beta", "director-b", "reply-same", "reply", "body",
            reply_to_delivery_id=cited)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: reply(), range(2)))
    assert sorted(item["result"]["replayed"] for item in replies) == [False, True]
    assert len(service.director_messages.list_director_messages("alpha")["result"]["items"]) == 1


@pytest.mark.asyncio
async def test_director_event_is_universal_wait_match_and_cursor_is_durable(tmp_path):
    path = tmp_path / "wait.sqlite"
    service = _service(path)
    _projects(service, "alpha", "beta")
    cursor = service.store.events("beta")[-1]["seq"]
    sent = _send(service.director_messages)
    result = await service.wait_for_state_change(
        "beta", after=cursor, node_keys=["not-present"], attempt_ids=["not-present"],
        filter_mode="all", actionable_only=True, timeout_seconds=0)
    assert [event["event_type"] for event in result["events"]] == ["director_message_received"]
    event = result["events"][0]
    assert event["node_key"] is None
    assert set(event["payload"]) == {"message_id", "thread_id", "delivery_id", "summary"}
    assert event["payload"]["delivery_id"] == sent["result"]["deliveries"][0]["delivery_id"]
    restarted = _service(path)
    empty = await restarted.wait_for_state_change(
        "beta", after=result["next_after"], node_keys=["x"], attempt_ids=["y"],
        filter_mode="any", actionable_only=False, timeout_seconds=0)
    assert empty == {"events": [], "next_after": result["next_after"], "timed_out": True}


@pytest.mark.asyncio
async def test_reply_wakes_direct_sender_only_and_reads_transitions_do_not_loop(tmp_path):
    path = tmp_path / "reply-wait.sqlite"
    service = _service(path)
    _projects(service, "alpha", "beta", "gamma")
    alpha_after = service.store.events("alpha")[-1]["seq"]
    gamma_after = service.store.events("gamma")[-1]["seq"]
    first = _send(service.director_messages)
    cited = first["result"]["deliveries"][0]["delivery_id"]
    reply = service.director_messages.for_actor("transport-b").send_director_message(
        "beta", "director-b", "reply", "reply", "body",
        reply_to_delivery_id=cited)
    reply_delivery = reply["result"]["deliveries"][0]
    assert reply_delivery["target_project_id"] == "alpha"
    woke = await service.wait_for_state_change(
        "alpha", after=alpha_after, node_keys=["unrelated"], attempt_ids=["unrelated"],
        timeout_seconds=0)
    assert [event["payload"]["delivery_id"] for event in woke["events"]] == [
        reply_delivery["delivery_id"]]
    assert (await service.wait_for_state_change(
        "gamma", after=gamma_after, timeout_seconds=0))["timed_out"] is True

    before = len([event for event in service.store.events("alpha")
                  if event["event_type"] == "director_message_received"])
    service.director_messages.list_director_messages("alpha")
    service.director_messages.acknowledge_director_message(
        "alpha", reply_delivery["delivery_id"], 1, "ack-reply")
    service.director_messages.resolve_director_message(
        "alpha", reply_delivery["delivery_id"], 2, "resolve-reply")
    after = len([event for event in service.store.events("alpha")
                 if event["event_type"] == "director_message_received"])
    assert after == before


def test_broadcast_refusal_rolls_back_all_rows_and_sequences(tmp_path):
    path = tmp_path / "rollback.sqlite"
    service = _service(path)
    _projects(service, "only")
    with pytest.raises(DirectorMessageError, match="no_recipients"):
        service.director_messages.send_director_message(
            "only", "director", "empty", "subject", "", broadcast=True)
    with service.db.get_connection() as conn:
        for table in ("state_director_messages", "state_director_deliveries",
                      "state_director_inbox_sequences", "state_director_idempotency"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_mid_broadcast_failure_rolls_back_message_deliveries_events_and_sequences(
        tmp_path, monkeypatch):
    path = tmp_path / "mid-rollback.sqlite"
    service = _service(path)
    _projects(service, "alpha", "beta", "gamma")
    original = service.store._event
    calls = 0

    def fail_second(conn, project_id, node_key, event_type, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected event failure")
        return original(conn, project_id, node_key, event_type, payload)

    monkeypatch.setattr(service.store, "_event", fail_second)
    with pytest.raises(RuntimeError, match="injected event failure"):
        service.director_messages.send_director_message(
            "alpha", "director", "broken", "subject", "body", broadcast=True)
    with service.db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM state_director_messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM state_director_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM state_director_inbox_sequences").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM state_director_idempotency").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM state_events WHERE event_type='director_message_received'").fetchone()[0] == 0

    monkeypatch.setattr(service.store, "_event", original)
    sent = service.director_messages.send_director_message(
        "alpha", "director", "working", "subject", "body", broadcast=True)
    assert [item["delivery_seq"] for item in sent["result"]["deliveries"]] == [1, 1]
