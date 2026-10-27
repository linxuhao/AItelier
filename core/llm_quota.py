"""Provider quota semantics — is this 429 a burst, or a spent window?

A 429 is two different failures wearing one exception class, and treating them
alike killed a live run on 2026-08-26:

  BURST   per-minute/per-second throttling. Clears in seconds; three backoffs
          with a 10s cap is exactly right.
  QUOTA   a usage window is spent ("You have exceeded the 5-hour usage quota.
          It will reset at 2026-08-26 09:18:28 +0800 CST"). NOTHING clears it
          but the clock. Retrying inside 20 seconds cannot succeed, and every
          wasted attempt still spends a step retry — so the run burned
          max_retries in 15 minutes and was marked `failed`, 18 minutes before
          the quota came back on its own.

Its own module, not part of the LLM gateway, because the scheduler asks these
questions on a path that must not import litellm to do it: the tick decides
whether to park BEFORE it claims a step, and pulling the whole gateway in for a
string test is a cost paid on every tick.
"""

import datetime
import re

# "It will reset at 2026-08-26 09:18:28 +0800 CST" — the offset is optional
# because not every provider prints one.
_QUOTA_RESET_RE = re.compile(
    r"reset(?:s)?\s+at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*([+-]\d{4})?")


# Observed Ollama wording: "You reached your Pro 5-hour limit". A named
# window alone says nothing about exhaustion: a burst response can also mention
# a daily allowance or say that a five-hour limit has NOT been reached.
_SPENT_WINDOW_RE = re.compile(
    r"\byou (?:have )?(?:reached|exceeded) your (?:\w+ )?"
    r"(?:\d+[- ]?hours?|hourly|daily|weekly|monthly)\s+(?:usage\s+)?limit\b")

# Observed Anthropic HTTP 400 wording (see `is_spend_limit_refusal`).
_SPEND_LIMIT_RE = re.compile(r"\byou have reached your specified api usage limits?\b")
_REGAIN_ACCESS_RE = re.compile(
    r"regain access on\s+(\d{4}-\d{2}-\d{2})\s+at\s+(\d{2}):(\d{2})\s*utc\b",
    re.IGNORECASE)



def is_quota_exhausted(err) -> bool:
    """Is this a spent usage window (vs. burst throttling)?

    Providers report both as RateLimitError; the prose distinguishes them.
    For window-limit wording, exceptions must also carry HTTP 429 provenance:
    the scheduler receives arbitrary application/tool errors as well. Plain
    strings support message inspection without importing the gateway/SDK.
    The observed Anthropic HTTP 400 spend-limit refusal is a spent window too,
    but only for an exception with that exact status/provider provenance.
    """
    if is_spend_limit_refusal(err):
        return True
    msg = str(err).lower()
    if "quota" in msg and ("exceed" in msg or "exhaust" in msg or "reset" in msg):
        return True
    if not isinstance(err, str) and getattr(err, "status_code", None) != 429:
        return False
    return _SPENT_WINDOW_RE.search(msg) is not None


def is_spend_limit_refusal(err) -> bool:
    """Observed Anthropic HTTP 400 API spend-limit refusal.

    Anthropic reports a spent organisation spend limit as `invalid_request_error`
    with status 400, not 429, and the prose names neither a quota nor a window:
    "You have reached your specified API usage limits. You will regain access on
    2026-11-01 at 00:00 UTC." LiteLLM types it as BadRequestError, so on the
    pre-fix baseline neither `is_quota_exhausted` nor a status remap recognised
    it as a present-tense spent window; `is_quota_exhausted` now delegates here.

    Provenance is part of the match: exception objects only (never a plain
    string or tool text), HTTP 400, and an Anthropic `llm_provider`. The wording
    alone is not trusted, because the same sentence can be quoted by arbitrary
    application prose.
    """
    if isinstance(err, str):
        return False
    if getattr(err, "status_code", None) != 400:
        return False
    if str(getattr(err, "llm_provider", "") or "").lower() != "anthropic":
        return False
    return _SPEND_LIMIT_RE.search(str(err).lower()) is not None


def quota_reset_at(err) -> "datetime.datetime | None":
    """UTC instant the provider says the window reopens, if it says so.

    Returns None when the message carries no parseable time — the caller is
    expected to fall back to a short fixed hold rather than guess.
    """
    from datetime import datetime, timedelta, timezone
    text = str(err)
    m = _QUOTA_RESET_RE.search(text)
    if not m:
        # Anthropic spend limit: "You will regain access on 2026-11-01 at
        # 00:00 UTC." Only an explicitly stated UTC instant is parsed.
        g = _REGAIN_ACCESS_RE.search(text)
        if not g:
            return None
        try:
            stamp = datetime.strptime(
                f"{g.group(1)} {g.group(2)}:{g.group(3)}:00",
                "%Y-%m-%d %H:%M:%S")

        except ValueError:
            return None
        return stamp.replace(tzinfo=timezone.utc)
    try:
        stamp = datetime.strptime(m.group(1).replace("T", " "),
                                  "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    off = m.group(2)
    if off:
        sign = 1 if off[0] == "+" else -1
        delta = timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
        return (stamp - sign * delta).replace(tzinfo=timezone.utc)
    # No offset given: assume the provider meant UTC. Being wrong here costs at
    # most a few hours of holding, which is still better than a dead run.
    return stamp.replace(tzinfo=timezone.utc)

