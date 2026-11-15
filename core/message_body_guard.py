"""The credential guard for the State writers that persist free text.

core/message_secret_guard.py holds the single check (``refuse_credential_values``).
The two message write paths that were guarded first (send_driver_message,
send_director_message) call that check directly with every text field they
persist. The writers added here - the user message and user conversation writers,
the P3 notice writer and the notebook writers - refuse through this one adapter,
so every one of them raises the same StateGraphError envelope as the existing
call sites.

A caller passes ALL of its writer's persisted text at once (``subject, body,
segments``), never one field at a time: a field added to the row later is one more
argument here, not a second call site. Nested blocks - a message's segments, a
reply's answers, or the validated models the write surface hands over - are
flattened to their string values, because what lands in a ``*_json`` column is
stored and read back like any other field.

It runs before the writer's INSERT, so a refused write leaves no row.
"""
from __future__ import annotations

from collections.abc import Mapping

from core.message_secret_guard import refuse_credential_values
from core.state_graph import StateGraphError


def refuse_credential_body(*texts) -> None:
    """Raise ``StateGraphError`` if any persisted text field embeds a credential value."""
    refuse_credential_values(list(_free_text(texts)),
                             lambda detail: StateGraphError("invalid_request: " + detail))


def _free_text(value):
    """Yield every string inside ``value``: fields, nested blocks and models alike."""
    if isinstance(value, str):
        if value:
            yield value
        return
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _free_text(item)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _free_text(item)
        return
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        yield from _free_text(dump())
