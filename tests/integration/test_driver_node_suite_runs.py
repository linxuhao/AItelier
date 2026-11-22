"""Run the driver's own node test suite from pytest and fail the gate if it is red.

What it runs: exactly ``node --test --test-concurrency=2`` (argv
``[<node>, "--test", "--test-concurrency=2"]``) with the working directory set to
``integrations/dsh-driver``. That is the driver suite's full ``node --test`` discovery,
and nothing else is run. The discovery includes ``plugin-boundary.test.mjs``, which
imports every test file of ``integrations/dsh-director``, so this row runs the director
suite too (there is no separate director row).

Concurrency 2 runs two test files at a time. At node's default, a 2-CPU container gets
concurrency 1 and the suite took 338 s; at 2 three runs of this row in a 2-CPU container
took 163 s, 166 s and 221 s (host load 5-16) and all three were green, 693/693
(measured 2026-11-22, before the rebase onto main; at ``0685a65908`` the suite is 725
tests and one run took 199 s at host load 12-23).

Why: nothing else executes this suite. The pytest-side guard
``tests/unit/test_driver_suite_lists_its_files.py`` reads the suite's file list and its
npm script, but it executes no javascript, so a red driver suite (146 tests when this row
was written) could stay red without the repository gate noticing. This row is the one
that runs the suite and makes any failure a failure of the gate.

Dependencies: the suite requires the pinned ``@deepseek-ai/dsh`` install at
``/opt/aitelier-dsh/node_modules/@deepseek-ai/dsh`` (checked via its package.json). That
install comes from the image, not from a ``node_modules`` tree in the repository, so no
npm install is needed. ``node`` is expected at ``/usr/local/bin/node`` in the image.

Runner shape: when this row was written the suite was green both with and without an init
(``docker run --init``) as PID 1, and it has grown since, so read the row's own counts.
Without an init, an unreaped orphan is a zombie, and the driver's
``alive()`` probe is zombie-aware: it reads ``/proc/<pid>/stat`` and treats ``Z`` as dead,
the dying states ``X``/``x`` as dead, and a per-pid read that fails as dead wherever
``/proc`` exists (only a machine with no ``/proc`` at all keeps the ``kill(pid, 0)``
answer). So the verdict does not depend on the runner reaping. A real leak is still caught.

Never a skip: where ``node`` is missing or the pinned install is absent, the row FAILS,
because the driver suite is then unmeasured and a skip would look like a pass.

Meaningless outside the shipped image (``aitelier:latest``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "integrations" / "dsh-driver"
DSH_PACKAGE = Path("/opt/aitelier-dsh/node_modules/@deepseek-ai/dsh/package.json")
NODE_ARGS = ["--test", "--test-concurrency=2"]
#: How long the suite may run before this row calls it UNMEASURED.
#:
#: This machine measured the suite ALONE in 314.22 s, and in 462 s in an earlier run
#: of the same tree; the previous bound was 900 s, and one whole-suite arm (this host's
#: load average was 11-43 on 16 shared cores) exceeded it and reddened the row with a
#: timeout instead of the suite's own verdict. So 900 s was a bound the host's speed,
#: not the suite, could decide. TIMEOUT_S is 2x that old 900 s bound and ~3.9x the
#: largest alone wall (462 s) / ~5.7x the smaller one (314.22 s): a slow host can be
#: several times slower than those walls without the timeout becoming the verdict,
#: and a suite that never returns still fails, in minutes and never as a pass or a skip.
#: Re-derive the number from those walls and this bound; another whole-suite arm would
#: only measure the host again.
TIMEOUT_S = 1800
TAIL_LINES = 80
# Variables a running DSH session sets; the suite must not inherit them.
DROPPED_ENV = ("AITELIER_DSH_EXECUTION", "DSH_PERMISSION_MODE", "AITELIER_TODO_CWD")
EXECUTOR = "the shipped image aitelier:latest (node at /usr/local/bin/node)"
FAIL_MARK = "\u2716"  # node's spec reporter marks a failed test with a heavy X
INFO_MARK = "\u2139"  # and prints its summary lines with an info sign


def _summary(output: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for key in ("tests", "pass", "fail"):
        hits = re.findall(rf"^[ \t]*{INFO_MARK} {key} (\d+)[ \t]*$", output, re.M)
        if hits:
            found[key] = int(hits[-1])
    return found


def _failing_names(output: str) -> list[str]:
    # A failing test is a `✖ <name> (<ms>ms)` line. node also prints a `✖ failing tests:`
    # section header and repeats failures as `✖ <name>` detail lines without a duration,
    # so only lines ending in a duration count, and the header is never a test name.
    names: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(FAIL_MARK):
            continue
        match = re.match(r"\u2716\s+(.+?)\s+\([\d.]+ms\)\s*$", stripped)
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def _measuring(output: str) -> str | None:
    for line in output.splitlines():
        if "measuring @deepseek-ai/dsh" in line:
            return line.strip()
    return None


def _dsh_version() -> str:
    try:
        return str(json.loads(DSH_PACKAGE.read_text(encoding="utf-8")).get("version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def _run(argv: list[str], home: Path) -> tuple[int, str, float]:
    env = {key: value for key, value in os.environ.items() if key not in DROPPED_ENV}
    env["HOME"] = str(home)
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=DRIVER,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        output, _ = proc.communicate(timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # Kill the whole process group: node --test runs each file in a child process.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = proc.communicate()
        tail = "\n".join((output or "").splitlines()[-TAIL_LINES:])
        waited = time.monotonic() - started
        pytest.fail(
            f"driver node suite TIMED OUT after {TIMEOUT_S} s (waited {waited:.1f} s) and "
            f"was killed: argv={argv!r} cwd={DRIVER}. The suite is UNMEASURED -- this is "
            f"a FAIL, never a skip and never a pass.\n"
            f"The bound: {TIMEOUT_S} s = 2x the previous 900 s bound that a whole-suite "
            f"arm exceeded on a host with load average 11-43 on 16 shared cores, and "
            f"~3.9x / ~5.7x the walls the same tree measured ALONE (462 s and 314.22 s). "
            f"A correct suite does not take this long; one that never returns does. "
            f"Re-derive TIMEOUT_S from those walls, not from another whole-suite arm; "
            f"if the host is simply slower than they were, the verdict is still 'the "
            f"host ran out of the measured budget', not a driver defect.\n"
            f"--- last {TAIL_LINES} lines of output ---\n{tail}"
        )
    return proc.returncode, output or "", time.monotonic() - started


@pytest.mark.slow
def test_driver_node_suite_passes_under_node_test(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            "missing capability: `node` is not on PATH, so the driver node suite cannot run. "
            "The driver suite is therefore UNMEASURED, not passing and not skipped. This row "
            f"expects {EXECUTOR}; run it there. PATH={os.environ.get('PATH', '')!r}"
        )
    if not DSH_PACKAGE.is_file():
        pytest.fail(
            f"missing pinned install: {DSH_PACKAGE} is absent, so the driver suite cannot "
            "resolve @deepseek-ai/dsh. The driver suite is therefore UNMEASURED, not passing "
            f"and not skipped. The install is baked into the image under /opt/aitelier-dsh; "
            f"this row expects {EXECUTOR}."
        )

    argv = [node, *NODE_ARGS]
    rc, output, wall = _run(argv, tmp_path)
    counts = _summary(output)
    names = _failing_names(output)
    measuring = _measuring(output)
    tail = "\n".join(output.splitlines()[-TAIL_LINES:])
    fail_line = f"{INFO_MARK} fail {counts.get('fail', '?')}"
    context = (
        f"argv={argv!r} cwd={DRIVER} rc={rc} wall_clock={wall:.1f}s\n"
        f"parsed: tests={counts.get('tests', '?')} pass={counts.get('pass', '?')} "
        f"fail={counts.get('fail', '?')}\n"
        f"{measuring or 'measuring line: not printed by the suite'}"
    )

    problems: list[str] = []
    if rc != 0:
        problems.append(f"non-zero exit rc={rc}")
    for key in ("tests", "pass", "fail"):
        if key not in counts:
            problems.append(f"summary line `{INFO_MARK} {key} N` not found in the output")
    if not problems:
        if counts["fail"] != 0:
            problems.append(f"{counts['fail']} failing test(s)")
        if names:
            problems.append(f"{len(names)} `{FAIL_MARK}` line(s) in the output")
        if counts["tests"] < 1:
            problems.append("zero tests ran: discovery is broken or the suite is empty")
        if counts["pass"] != counts["tests"]:
            problems.append(f"pass {counts['pass']} != tests {counts['tests']}")

    if problems:
        pytest.fail(
            "driver node suite is RED: " + "; ".join(problems) + "\n"
            f"{context}\n"
            f"failing tests ({FAIL_MARK} lines): {names or 'none named in the output'}\n"
            f"summary: {fail_line}\n"
            f"--- last {TAIL_LINES} lines of output ---\n{tail}"
        )

    # A passing row prints nothing under `pytest -q` (output is captured), so the measured
    # numbers go out as a warning: pytest prints the warnings summary on a pass too.
    # This is a report of the measurement, not a skip, and it never fails the row.
    warnings.warn(
        f"driver node suite GREEN: tests={counts['tests']} pass={counts['pass']} "
        f"fail={counts['fail']} dsh={_dsh_version()}\n{context}",
        UserWarning,
        stacklevel=1,
    )
