"""Refuse message text that carries one of this service's own credentials.

Every State writer that persists free text calls ``refuse_credential_values``
(or the ``core/message_body_guard.py`` adapter over it) before it opens a
transaction, passing ALL of the fields that writer stores: a subject and a body,
a notebook entry's assertion and body. That text is persisted in State and
delivered to inboxes, so a credential value written into any field of it would
be copied to every recipient and kept for good. This module is the single
definition of the check.

What it refuses: text that contains the exact value of a configured
credential of this service. The set is read at call time from the same module
attributes the auth modules compare against (``api.authz.ADMIN_TOKEN`` and
``api.mcp_router._EXTERNAL_TOKEN``), so it can never drift from them, and a
configuration change is seen on the next call.

Coverage, measured (this paragraph states the set; it does not extend it).
Checked: the two credentials this server holds in plaintext, ``AITELIER_ADMIN_TOKEN``
(``api.authz.ADMIN_TOKEN``) and ``AITELIER_MCP_EXTERNAL_TOKEN``
(``api.mcp_router._EXTERNAL_TOKEN``).

NOT checked: each driver's own ``AITELIER_DRIVER_TOKEN``. The server stores only an
HMAC of it (``drivers.token_hash``), so it holds no plaintext value to compare
against, and the host token directory ``~/.aitelier-drivers/`` is not visible to
the backend process, so the guard cannot read the token from there either. A driver
token is therefore covered by nothing on the server side.

What a writer does instead: name where the token lives (the ``AITELIER_DRIVER_TOKEN``
setting, held in the host token directory ``~/.aitelier-drivers/``), never its value.
A hand-off or message that needs a token says where to find it.

What it deliberately does not do: scan for secret-shaped text, judge prose or
field names, or block a digest. Text that only NAMES a credential field (for
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


def refuse_credential_values(texts, refusal) -> None:
    """Raise ``refusal(detail)`` if any of ``texts`` embeds a configured credential value.

    ``texts`` is the write's whole free text: one string, or a collection of
    strings - one entry per field the writer persists (a subject and a body, a
    notebook entry's assertion and body). Every one of those fields is stored and
    shown to a reader, so the test is the same for each; passing them together is
    what keeps a writer at one guard call.

    ``refusal`` is the caller's own exception factory, so each write path keeps
    its error type and its closed envelope. ``detail`` names the field class and
    the remedy. It never includes the value, a prefix of it, or its length.
    """
    credentials = list(_configured_values())
    if not credentials:
        return
    for text in (texts if isinstance(texts, (list, tuple, set, frozenset)) else (texts,)):
        if not isinstance(text, str) or not text:
            continue
        for field, value in credentials:
            if value in text:
                raise refusal(
                    f"text: contains the configured value of {field}; message text is "
                    "stored and delivered, so credentials must not be written into it. "
                    "Remove the value and name the field instead.")
