"""One definition of "which environment variables must not reach a subprocess".

The rule existed already, as a private attribute on MetaAgent, and it was applied
to exactly one of the three places that spawn a child process with the server's
environment. The coding-mode `bash` tool — the one everybody thinks of as the
dangerous one, because a model chooses its argument — was scrubbed. The pipeline
test runner, which runs LLM-authored `pytest` and `npm ci` (and therefore any
`postinstall` script the generated `package.json` names), passed `os.environ`
straight through. The hardened path was the safe one and the unattended path was
not, which is the wrong way round.

So the regex lives here, with both consumers importing it, rather than in the
class that happened to need it first.

**What this is not.** It is a denylist on variable NAMES. It cannot stop a child
that reads `/run/secrets/<NAME>` directly — the container runs as the uid that
owns those files. Scrubbing the environment narrows accidental capture (a token
landing in a test log, an error message, a trace row); it is not a sandbox, and
nothing here should be read as one.
"""
from __future__ import annotations

import os
import re

# Anchored to the END of the name. Secret conventions are suffixes
# (DEEPSEEK_API_KEY, GITHUB_TOKEN, MY_PASSWORD); an unanchored `_KEY` also
# matched GIT_CONFIG_KEY_0 — the compose-wired credential helper — while leaving
# GIT_CONFIG_COUNT behind, which broke every git command in the container.
ENV_SECRET_RE = re.compile(
    r"(_KEY|_TOKEN|_SECRET|_SECRETS|PASSWORD|_CREDENTIAL|_CREDENTIALS)$", re.I)


def scrubbed_env(base: dict[str, str] | None = None, **overrides: str) -> dict[str, str]:
    """`os.environ` (or `base`) minus anything whose NAME looks like a secret.

    `overrides` are applied AFTER the filter, so a caller can set a variable the
    pattern would otherwise strip — and so an override cannot be silently undone
    by an inherited value of the same name.
    """
    src = os.environ if base is None else base
    env = {k: v for k, v in src.items() if not ENV_SECRET_RE.search(k)}
    env.update(overrides)
    return env
