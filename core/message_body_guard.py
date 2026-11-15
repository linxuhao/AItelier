"""The credential guard for the State writers that persist a free-text body.

core/message_secret_guard.py holds the single check (``refuse_credential_values``).
Two writers call it directly (send_driver_message, send_director_message). The
writers added here - the user message and user conversation writers, the P3 notice
writer and the notebook writers - all refuse through this one adapter, so every
one of them raises the same StateGraphError envelope as the existing call sites.
It runs before the writer's INSERT, so a refused body leaves no row.
"""
from __future__ import annotations

from core.message_secret_guard import refuse_credential_values
from core.state_graph import StateGraphError


def refuse_credential_body(body) -> None:
    """Raise ``StateGraphError`` if ``body`` embeds a configured credential value."""
    refuse_credential_values(body, lambda detail: StateGraphError("invalid_request: " + detail))
