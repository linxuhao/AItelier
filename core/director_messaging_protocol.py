"""Transport-neutral constants and error envelope for director messaging v2."""
from __future__ import annotations

from copy import deepcopy


SCHEMA_ID = "aitelier.director-messaging.v2"
V3_SCHEMA_ID = "aitelier.director-messaging.v3"
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
V3_ERROR_CODES = ERROR_CODES | {"use_driver_inbox", "ack_quorum_unreachable"}


class DirectorMessageError(Exception):
    """Stable protocol error, represented by its closed JSON object."""

    def __init__(self, code: str, schema=SCHEMA_ID):
        if code not in V3_ERROR_CODES:
            raise ValueError(f"unknown director message error code: {code}")
        self.code = code
        self.envelope = {
            "schema": schema,
            "code": code,
            "detail": {"message": code},
        }
        super().__init__(code)

    def as_dict(self) -> dict:
        return deepcopy(self.envelope)
