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

import re

# "It will reset at 2026-08-26 09:18:28 +0800 CST" — the offset is optional
# because not every provider prints one.
_QUOTA_RESET_RE = re.compile(
    r"reset(?:s)?\s+at\s+(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\s*([+-]\d{4})?")


def is_quota_exhausted(err) -> bool:
    """Is this a spent usage window (vs. burst throttling)?

    Keyed on the message, not the class: providers report both as
    RateLimitError, and only the prose distinguishes them.
    """
    msg = str(err).lower()
    return "quota" in msg and ("exceed" in msg or "exhaust" in msg
                              or "reset" in msg)


def quota_reset_at(err) -> "datetime.datetime | None":
    """UTC instant the provider says the window reopens, if it says so.

    Returns None when the message carries no parseable time — the caller is
    expected to fall back to a short fixed hold rather than guess.
    """
    from datetime import datetime, timedelta, timezone
    m = _QUOTA_RESET_RE.search(str(err))
    if not m:
        return None
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
