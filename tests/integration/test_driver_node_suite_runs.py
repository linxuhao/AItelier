"""Run the driver's own node test suite from pytest and fail the gate if it is red.

What it runs: exactly ``node --test --test-concurrency=1`` (argv
``[<node>, "--test", "--test-concurrency=1"]``) with the working directory set to
``integrations/dsh-driver``. That is the driver suite's full ``node --test`` discovery,
and nothing else is run.

Why: nothing else executes this suite. The pytest-side guard
``tests/unit/test_driver_suite_lists_its_files.py`` reads the suite's file list and its
npm script, but it executes no javascript, so a red driver suite (146 tests when this row
was written) could stay red without the repository gate noticing. This row is the one
that runs the suite and makes any failure a failure of the gate.

Dependencies: the suite requires the pinned ``@deepseek-ai/dsh`` install at
``/opt/aitelier-dsh/node_modules/@deepseek-ai/dsh`` (checked via its package.json). That
install comes from the image, not from a ``node_modules`` tree in the repository, so no
npm install is needed. ``node`` is expected at ``/usr/local/bin/node`` in the image.

Runner shape: the suite measured 146/146 both with and without an init (``docker run
--init``) as PID 1. Without an init, an unreaped orphan is a zombie; the driver's
``alive()`` probe is zombie-aware (it reads ``/proc/<pid>/stat`` and treats state ``Z`` as
dead), so the verdict does not depend on the runner reaping. A real leak is still caught.

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
NODE_ARGS = ["--test", "--test-concurrency=1"]
TIMEOUT_S = 900
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
        pytest.fail(
            f"driver node suite TIMED OUT after {TIMEOUT_S} s and was killed: argv={argv!r} "
            f"cwd={DRIVER}. The suite is UNMEASURED.\n"
            f"--- last {TAIL_LINES} lines of output ---\n{tail}"
        )
    return proc.returncode, output or "", time.monotonic() - started


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
