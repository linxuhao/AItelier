"""Transport-neutral constants and error envelope for director messaging v2."""
from __future__ import annotations

from copy import deepcopy


SCHEMA_ID = "aitelier.director-messaging.v2"
ACTIONS = (
    "send_director_message",
    "list_director_messages",
    "acknowledge_director_message",
    "resolve_director_message",
)
ERROR_CODES = frozenset({
    "invalid_request", "unauthorized", "unknown_project", "unknown_delivery",
    "idempotency_conflict", "invalid_transition", "version_conflict",
    "recipient_limit", "no_recipients",
})


class DirectorMessageError(Exception):
    """Stable protocol error, represented by its closed JSON object."""

    def __init__(self, code: str):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown director message error code: {code}")
        self.code = code
        self.envelope = {
            "schema": SCHEMA_ID,
            "code": code,
            "detail": {"message": code},
        }
        super().__init__(code)

    def as_dict(self) -> dict:
        return deepcopy(self.envelope)
