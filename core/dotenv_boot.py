# core/dotenv_boot.py
# The ONE parser for the repository-root `.env`, and the one function that
# applies it — called EXPLICITLY by a boot entry, never as an import side effect
# of importing the application.
#
# Why this exists as its own module: the loader used to be copy-pasted into
# three modules, and one of those copies ran at IMPORT time in api/main.py:
#
#     api/main.py lines 7-18  (removed 2026-11-08)
#         read <repo root>/.env -> os.environ, for every key not already set
#
# That copy ran BEFORE `from core import cf_access`, and core/cf_access.py reads
# AITELIER_CF_TEAM_DOMAIN / AITELIER_CF_AUD once, at import. So EVERY process
# that imported the app — notably every pytest run in a checkout whose root has
# an untracked `.env` — silently wore the deployment's write gate: two
# integration tests were refused `403 write_denied_not_authenticated` this cycle
# (iss-f909af237ae944af), and test_backend_shutdown.py had to PIN
# AITELIER_CF_AUD="" to hold its identity against it. Configuration belongs to
# the process you started, not to a file the importing process never named.
#
# What still needs `.env`, and why nothing needs it at api.main import time:
#
#   * the server CONTAINER gets its configuration from compose `env_file`
#     (docker-compose.yml:117-118) — it is already in os.environ before
#     api.main is imported, so the import-time read only ever re-applied the
#     same values, or injected a developer's `.env` when compose had not;
#   * the CLI host process needs it (AITELIER_PORT, AITELIER_ADMIN_TOKEN), so
#     `cli/server.py` and `cli/client.py` call load_repo_dotenv() at their own
#     import, before they read those variables — exactly the ordering they had
#     when they carried their own copy;
#   * there is no host-process fallback for the BACKEND (AGENTS.md: running
#     uvicorn on the host would make DPE git commits use the developer's
#     ~/.gitconfig identity), so "the app was started on a host that expects
#     .env to configure it" is not a path this repository supports.
#
# Behaviour matches the three inline loops it replaces, with two deliberate
# tolerances they did not have (see load_repo_dotenv):
#
#   * same file (<repo root>/.env), same grammar (KEY=VALUE, `#` comments,
#     optional `export `, surrounding quotes stripped), same setdefault rule —
#     an existing environment variable always wins, so a compose env_file, a
#     shell export or a test's explicit pin is never overwritten;
#   * a key that appears TWICE keeps its FIRST value — the replaced
#     `if _key not in os.environ` loops and `os.environ.setdefault` both did.
#     Measured 2026-11-08: a fixture `.env` with `DOTENV_DUP_KEY=first`, a
#     comment, then `DOTENV_DUP_KEY=second` gave "first" under the old
#     `import api.main` and "second" under the last-wins parser this corrects.

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

# The repository root, from this file's location: <root>/core/dotenv_boot.py.
REPO_ROOT = Path(__file__).resolve().parent.parent
DOTENV_PATH = REPO_ROOT / ".env"


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse `.env` text into a plain dict, FIRST assignment winning.

    A key assigned on several lines keeps the value from its FIRST line, the
    same as the inline loops this replaces (`if key not in os.environ` and
    `os.environ.setdefault`): a later duplicate is ignored, not applied.

    The deliberately dumb format the three inline copies implemented: no
    interpolation, no multi-line values, no escape sequences. A line without
    `=` is skipped, as is a blank or `#` line, and so is a line whose key is
    empty (the old loop raised OSError [Errno 22] on it from os.environ).

    Lines are split on "\\n" alone, NOT with str.splitlines(): splitlines also
    breaks on \\x0b, \\x0c, \\x1c-\\x1e, \\x85, \\u2028 and \\u2029, so a value
    containing one of them was cut into a bogus second assignment. The inline
    loops this replaces iterated the FILE (`for _line in _f`), which splits only
    on newline sequences — and, under universal newlines, hands back each line
    WITHOUT its trailing "\\r", which is why exactly one is stripped here.

    One residual difference: a LONE "\\r" (the old-Mac separator) also broke a
    line under universal newlines and does not break one here.
    """
    values: dict[str, str] = {}
    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip().removeprefix("export ")
        val = val.strip().strip("\"'")
        if key:
            values.setdefault(key, val)
    return values


def load_repo_dotenv(
    path: str | os.PathLike | None = None,
    environ: "Mapping[str, str] | None" = None,
) -> Path | None:
    """Merge `<repo root>/.env` into the process environment; existing keys win.

    Returns the file that was applied, or None when there is nothing to apply:
    the path does not exist, or reading it raised OSError — it is a directory,
    or a file this process may not read. The old inline loops raised in those
    cases (`open()` on a directory, or on a file without read permission); this
    returns None instead, so a missing or unreadable `.env` just applies
    nothing.

    OSError is the whole of that tolerance. A file that exists and opens but
    cannot be DECODED raises UnicodeDecodeError out of this function, i.e. at
    the boot entry that called it, and that process does not start. Within the
    file, a line whose key is empty is skipped, where the old loop raised
    OSError [Errno 22] from os.environ.

    Call this from a BOOT ENTRY — a CLI start, a script's own main — never from
    a module that something else imports to use the app.

    `path` / `environ` exist for tests that must not touch this process's real
    environment; both default to the real thing.
    """
    target = Path(path) if path is not None else DOTENV_PATH
    env = os.environ if environ is None else environ
    try:
        text = target.read_text()
    except OSError:
        return None
    for key, val in parse_dotenv(text).items():
        env.setdefault(key, val)
    return target
