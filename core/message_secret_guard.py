"""Refuse a message body that carries one of this service's own credentials.

Both message write paths call ``refuse_credential_values`` before they open a
transaction: the project mailbox (``core/director_messaging.py``) and the
driver inbox (``core/driver_inbox.py``). A body is persisted in State and
delivered to inboxes, so a credential value written into one would be copied
to every recipient and kept for good. This module is the single definition of
the check.

What it refuses: a body that contains the exact value of a configured
credential of this service. The set is read at call time from the same module
attributes the auth modules compare against (``api.authz.ADMIN_TOKEN`` and
``api.mcp_router._EXTERNAL_TOKEN``), so it can never drift from them, and a
configuration change is seen on the next call.

What it deliberately does not do: scan for secret-shaped text, judge prose or
field names, or block a digest. A body that only NAMES a credential field (for
example ``AITELIER_ADMIN_TOKEN``) is ordinary text and is accepted.

The floor: a configured value shorter than ``MIN_SECRET_LEN`` characters is not
treated as a secret. An empty or one-character setting would otherwise match
almost every body and refuse ordinary text. Eight is a judgement, not a proof:
it is long enough that an ordinary word or phrase is very unlikely to contain
it verbatim, and short enough that a real short token is still guarded. A
configured value below it is simply not checked; it is not made safe.
"""
from __future__ import annotations

import importlib

MIN_SECRET_LEN = 8

# (field class, module, attribute). The field class is the only part of a
# configured value that may reach the caller. The value, its prefix and its
# length never do.
_CREDENTIALS = (
    ("AITELIER_ADMIN_TOKEN", "api.authz", "ADMIN_TOKEN"),
    ("AITELIER_MCP_EXTERNAL_TOKEN", "api.mcp_router", "_EXTERNAL_TOKEN"),
)


def _configured_values():
    """Yield (field class, value) for every configured credential of at least
    ``MIN_SECRET_LEN`` characters.

    Imports the auth module by name, so the value is the one the auth code
    compares against regardless of which modules this process happened to
    import. Reading ``sys.modules`` instead would skip a credential in any
    process that never loaded its auth module, which is a bypass, not a guard.
    A module that fails to import raises here, so a message write cannot
    proceed without its guard.
    """
    for field, module_name, attribute in _CREDENTIALS:
        module = importlib.import_module(module_name)
        value = getattr(module, attribute, "")
        if isinstance(value, str):
            value = value.strip()
            if len(value) >= MIN_SECRET_LEN:
                yield field, value


def refuse_credential_values(body: str, refusal) -> None:
    """Raise ``refusal(detail)`` if ``body`` embeds a configured credential value.

    ``refusal`` is the caller's own exception factory, so each write path keeps
    its error type and its closed envelope. ``detail`` names the field class and
    the remedy. It never includes the value, a prefix of it, or its length.
    """
    if not isinstance(body, str) or not body:
        return
    for field, value in _configured_values():
        if value in body:
            raise refusal(
                f"body: contains the configured value of {field}; a message body is "
                "stored and delivered, so credentials must not be written into it. "
                "Remove the value and name the field instead.")
