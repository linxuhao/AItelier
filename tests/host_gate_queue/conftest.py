"""Supply the two interpreter roles this suite declares, from the environment it runs in.

`owned_support.Harness` runs real child processes under two interpreters:

* ``GATE_QUEUE_TEST_VENV_PYTHON``   — must import ``httpx`` (the owned CLI surface);
* ``GATE_QUEUE_TEST_ORDINARY_PYTHON`` — must be a working interpreter that cannot
  import ``httpx`` (the stdlib-only surface).

Neither role is asserted here. Each is *observed*: a candidate is executed with
``-c PROBE`` and the role is filled only from that result. When a role
cannot be observed, neither role is filled, so the suite keeps its documented
behaviour: ``Harness`` skips with ``VENV_REQUIREMENT``, naming the variable. A
half-supplied pair would make the fixture raise instead of skipping, which is
why the pair is filled atomically.

What is deliberately NOT done:

* a variable the caller already set is never touched — including one that is
  present but empty, which therefore still produces the named skip;
* nothing is installed, no interpreter is re-exec'd, no file is written, no
  venv is assumed to exist at any path; the candidate paths below are only
  hypotheses, and the decision always comes from running them;
* the probes run with ``PYTHONPATH`` removed, because ``Harness`` strips it from
  every child environment it builds — a candidate that only imports ``httpx``
  through ``PYTHONPATH`` could not serve the role, so it must not look like it can.

Loaded by both shapes of this suite: the gate's ``pytest <repo> --rootdir <repo>``
(collection reaches this file) and the review shape
``pytest -c tests/host_gate_queue/pytest.ini --confcutdir=tests/host_gate_queue``.
A hook in ``tests/conftest.py`` would not be loaded by the second shape, which is
why the supply lives in the suite's own directory.
"""
from __future__ import annotations

import os
import subprocess
import sys

CLIENT_ROLE = "GATE_QUEUE_TEST_VENV_PYTHON"
ORDINARY_ROLE = "GATE_QUEUE_TEST_ORDINARY_PYTHON"

# Exit 0 only when the client module resolves to a real file. A bare `import httpx`
# also succeeds on an empty directory named httpx (a namespace package, whose
# __file__ is None), so that alone must not be taken as a usable client.
PROBE = ('import httpx, pathlib, sys; sys.exit(0 if getattr(httpx, "__file__", None) '
         'and pathlib.Path(httpx.__file__).is_file() else 1)')
PROBE_TIMEOUT = 20

# Hypotheses only. The running interpreter comes first (the gate's pytest leg runs
# the image's own python3), then the image's other client-capable interpreters.
_CLIENT_CANDIDATES = (sys.executable, "/usr/local/bin/python", "/usr/local/bin/python3")
# The stdlib-only role: the image's system interpreter, which has no client, and
# the interpreter running this suite when it happens to lack one.
_ORDINARY_CANDIDATES = (sys.executable, "/usr/bin/python3",
                        "/usr/bin/python3.13", "/usr/bin/python3.12")

# realpath -> True (runs, imports httpx) | False (runs, cannot import httpx)
#             | None (cannot be executed at all). Shared by both roles so no
# candidate is ever probed twice, and remembered for the whole session.
_OBSERVED = {}


def _probe_environment():
    return {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}


def _observe(candidate):
    """Observe one candidate interpreter. Never raises; returns True/False/None."""
    if not candidate:
        return None
    try:
        key = os.path.realpath(candidate)
    except (OSError, ValueError):
        key = candidate
    if key in _OBSERVED:
        return _OBSERVED[key]
    try:
        done = subprocess.run([candidate, "-c", PROBE], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=PROBE_TIMEOUT,
                              env=_probe_environment())
        verdict = done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        verdict = None  # not an executable interpreter: not observable
    _OBSERVED[key] = verdict
    return verdict


def _first(candidates, wanted):
    for candidate in candidates:
        if _observe(candidate) is wanted:
            return candidate
    return None


def supply(environ=None):
    """Fill absent roles from observation. Returns {name: value} for what was set."""
    env = os.environ if environ is None else environ
    if CLIENT_ROLE in env and ORDINARY_ROLE in env:
        return {}  # the caller supplied the pair: observe nothing, touch nothing
    client = env[CLIENT_ROLE] if CLIENT_ROLE in env else _first(_CLIENT_CANDIDATES, True)
    ordinary = env[ORDINARY_ROLE] if ORDINARY_ROLE in env else _first(_ORDINARY_CANDIDATES, False)
    if not client or not ordinary:
        # One role absent from this environment: supply neither, so the owned
        # suite keeps skipping with its named requirement instead of erroring.
        return {}
    supplied = {}
    for name, value in ((CLIENT_ROLE, client), (ORDINARY_ROLE, ordinary)):
        if name not in env:
            env[name] = value
            supplied[name] = value
    return supplied


SUPPLIED = supply()
