"""Standalone in-memory fake for the director messaging v1 contract."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from threading import RLock
import unicodedata
from uuid import UUID, uuid4

from core.state_driver_notes import _redact
from core.director_messaging_protocol import (
    ACTIONS, DirectorMessageError, ERROR_CODES, SCHEMA_ID,
)

__all__ = [
    "ACTIONS", "DirectorMessageError", "ERROR_CODES", "InMemoryDirectorMessaging",
]


def _invalid() -> None:
    raise DirectorMessageError("invalid_request")


def _db_id(value) -> str:
    if not isinstance(value, str) or not 1 <= len(unicodedata.normalize("NFC", value)) <= 320:
        _invalid()
    return value


def _nfc_text(value, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        _invalid()
    value = unicodedata.normalize("NFC", value)
    if not minimum <= len(value) <= maximum:
        _invalid()
    return value


def _integer(value, minimum: int, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        _invalid()
    return value


def _canonical(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _Memory:
    def __init__(self, project_ids):
        projects = [_db_id(project_id) for project_id in project_ids]
        if len(set(projects)) != len(projects):
            raise ValueError("project_ids must be unique")
        self.projects = set(projects)
        self.messages: dict[str, dict] = {}
        self.deliveries: dict[str, dict] = {}
        self.inboxes: dict[str, list[str]] = {project_id: [] for project_id in projects}
        self.events: dict[str, list[dict]] = {project_id: [] for project_id in projects}
        self.dedupe: dict[tuple[str, str, str, str], tuple[str, dict]] = {}
        self.event_seq = 0
        self.lock = RLock()


class InMemoryDirectorMessaging:
    """Actor-bound fake with the exact four v1 service actions.

    ``for_actor`` and ``fresh`` are test-harness lifecycle helpers, not protocol
    actions. Actor authentication and authorization belong to the transport.
    """

    def __init__(self, project_ids, actor: str, *, _memory: _Memory | None = None,
                 clock=None, id_factory=None, redactor=_redact):
        if not isinstance(actor, str) or not actor:
            raise ValueError("actor must be authenticated nonempty text")
        self.actor = actor
        self._initial_projects = tuple(project_ids)
        self._memory = _memory or _Memory(self._initial_projects)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._redactor = redactor

    def for_actor(self, actor: str) -> "InMemoryDirectorMessaging":
        return type(self)(self._initial_projects, actor, _memory=self._memory,
                          clock=self._clock, id_factory=self._id_factory,
                          redactor=self._redactor)

    def fresh(self) -> "InMemoryDirectorMessaging":
        return type(self)(self._initial_projects, self.actor, clock=self._clock,
                          id_factory=self._id_factory, redactor=self._redactor)

    @staticmethod
    def _success(result: dict) -> dict:
        return {"schema": SCHEMA_ID, "result": result}

    def _project(self, project_id: str) -> None:
        if project_id not in self._memory.projects:
            raise DirectorMessageError("unknown_project")

    def _dedupe(self, scope: str, operation: str, request_key: str,
                payload: dict) -> dict | None:
        found = self._memory.dedupe.get((self.actor, scope, operation, request_key))
        if found is None:
            return None
        canonical, result = found
        if canonical != _canonical(payload):
            raise DirectorMessageError("idempotency_conflict")
        replay = deepcopy(result)
        replay["replayed"] = True
        return self._success(replay)

    def _record(self, scope: str, operation: str, request_key: str,
                payload: dict, result: dict) -> dict:
        self._memory.dedupe[(self.actor, scope, operation, request_key)] = (
            _canonical(payload), deepcopy(result))
        return self._success(deepcopy(result))

    def _new_id(self) -> str:
        value = self._id_factory()
        try:
            parsed = UUID(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "id_factory must return a lowercase canonical UUIDv4 string") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise RuntimeError("id_factory must return a lowercase canonical UUIDv4 string")
        return value

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def send_director_message(self, sender_project_id, director_identity, request_key,
                              subject, body, target_project_id=None, broadcast=False,
                              reply_to_delivery_id=None):
        sender_project_id = _db_id(sender_project_id)
        director_identity = _nfc_text(director_identity, 1, 320)
        request_key = _nfc_text(request_key, 1, 320)
        subject = _nfc_text(subject, 1, 200)
        body = _nfc_text(body, 0, 8000)
        if type(broadcast) is not bool:
            _invalid()
        if target_project_id is not None:
            target_project_id = _db_id(target_project_id)
        if reply_to_delivery_id is not None:
            reply_to_delivery_id = _db_id(reply_to_delivery_id)
        modes = (
            not broadcast and target_project_id is not None and reply_to_delivery_id is None,
            broadcast and target_project_id is None and reply_to_delivery_id is None,
            not broadcast and target_project_id is None and reply_to_delivery_id is not None,
        )
        if sum(modes) != 1:
            _invalid()

        payload = {
            "body": body, "broadcast": broadcast, "director_identity": director_identity,
            "reply_to_delivery_id": reply_to_delivery_id, "request_key": request_key,
            "sender_project_id": sender_project_id, "subject": subject,
            "target_project_id": target_project_id,
        }
        operation = "send_director_message"
        with self._memory.lock:
            replay = self._dedupe(sender_project_id, operation, request_key, payload)
            if replay is not None:
                return replay
            self._project(sender_project_id)

            thread_id = None
            if modes[0]:
                if target_project_id == sender_project_id:
                    _invalid()
                self._project(target_project_id)
                targets = [target_project_id]
            elif modes[1]:
                targets = sorted(self._memory.projects - {sender_project_id})
                if not targets:
                    raise DirectorMessageError("no_recipients")
                if len(targets) > 200:
                    raise DirectorMessageError("recipient_limit")
            else:
                delivery = self._memory.deliveries.get(reply_to_delivery_id)
                if delivery is None or delivery["target_project_id"] != sender_project_id:
                    raise DirectorMessageError("unknown_delivery")
                original = self._memory.messages[delivery["message_id"]]
                target = original["sender_project_id"]
                self._project(target)
                targets = [target]
                thread_id = original["thread_id"]

            message_id = self._new_id()
            if thread_id is None:
                thread_id = message_id
            message = {
                "message_id": message_id,
                "thread_id": thread_id,
                "sender_project_id": sender_project_id,
                "director_identity": director_identity,
                "actor": self.actor,
                "subject": subject,
                "body": body,
                "created_at": self._now(),
                "reply_to_delivery_id": reply_to_delivery_id,
            }
            summary = self._redactor(subject + "\n" + body)[:320]
            prepared = []
            for target in targets:
                delivery_id = self._new_id()
                delivery = {
                    "delivery_id": delivery_id,
                    "message_id": message_id,
                    "target_project_id": target,
                    "delivery_seq": len(self._memory.inboxes[target]) + 1,
                    "status": "unread",
                    "version": 1,
                }
                prepared.append((target, delivery, {
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "delivery_id": delivery_id,
                    "summary": summary,
                }))

            self._memory.messages[message_id] = deepcopy(message)
            for target, delivery, event_payload in prepared:
                self._memory.deliveries[delivery["delivery_id"]] = deepcopy(delivery)
                self._memory.inboxes[target].append(delivery["delivery_id"])
                self._memory.event_seq += 1
                self._memory.events[target].append({
                    "seq": self._memory.event_seq,
                    "event_type": "director_message_received",
                    "node_key": None,
                    "actionable": True,
                    "payload": event_payload,
                })
            result = {
                "message": deepcopy(message),
                "deliveries": [deepcopy(item[1]) for item in prepared],
                "replayed": False,
            }
            return self._record(sender_project_id, operation, request_key, payload, result)

    def list_director_messages(self, project_id, after=0, limit=100):
        project_id = _db_id(project_id)
        after = _integer(after, 0)
        limit = _integer(limit, 1, 100)
        with self._memory.lock:
            self._project(project_id)
            deliveries = [self._memory.deliveries[delivery_id]
                          for delivery_id in self._memory.inboxes[project_id]]
            deliveries = [item for item in deliveries if item["delivery_seq"] > after][:limit]
            items = [{
                "message": deepcopy(self._memory.messages[item["message_id"]]),
                "delivery": deepcopy(item),
            } for item in deliveries]
            next_after = items[-1]["delivery"]["delivery_seq"] if items else after
            return self._success({"project_id": project_id, "items": items,
                                  "next_after": next_after})

    def acknowledge_director_message(self, project_id, delivery_id, expected_version,
                                     request_key):
        return self._transition("acknowledge_director_message", project_id, delivery_id,
                                expected_version, request_key, "acknowledged")

    def resolve_director_message(self, project_id, delivery_id, expected_version,
                                 request_key):
        return self._transition("resolve_director_message", project_id, delivery_id,
                                expected_version, request_key, "resolved")

    def _transition(self, operation, project_id, delivery_id, expected_version,
                    request_key, target_status):
        project_id = _db_id(project_id)
        delivery_id = _db_id(delivery_id)
        expected_version = _integer(expected_version, 1)
        request_key = _nfc_text(request_key, 1, 320)
        payload = {
            "delivery_id": delivery_id,
            "expected_version": expected_version,
            "project_id": project_id,
            "request_key": request_key,
        }
        with self._memory.lock:
            replay = self._dedupe(project_id, operation, request_key, payload)
            if replay is not None:
                return replay
            self._project(project_id)
            delivery = self._memory.deliveries.get(delivery_id)
            if delivery is None or delivery["target_project_id"] != project_id:
                raise DirectorMessageError("unknown_delivery")
            if delivery["status"] == target_status:
                result = {"delivery": deepcopy(delivery), "replayed": False}
                return self._record(project_id, operation, request_key, payload, result)
            legal_source = "unread" if target_status == "acknowledged" else "acknowledged"
            if delivery["status"] != legal_source:
                raise DirectorMessageError("invalid_transition")
            if delivery["version"] != expected_version:
                raise DirectorMessageError("version_conflict")
            delivery["status"] = target_status
            delivery["version"] += 1
            result = {"delivery": deepcopy(delivery), "replayed": False}
            return self._record(project_id, operation, request_key, payload, result)
