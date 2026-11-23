"""Run the WeChat channel's node test suite from pytest, and fail the gate if it is red.

What it runs: one ``node --test`` invocation from the suite directory, and nothing else.

* ``integrations/dsh-channel-wechat`` -- argv ``[<node>, "--test", "index.test.mjs"]``,
  the suite's own npm-script argv, the file named explicitly because it sits at the suite
  root rather than under a ``test/`` directory. The floor is ``ℹ tests 39`` /
  ``ℹ pass 39`` / ``ℹ fail 0``, measured in the shipped image on 2026-11-23 at
  ``f7a325a1fb`` (node v24.22.3, 3.7 s on this row's own timer): 17 until the WeChat media
  interface tests landed, 33 until a group's medium was kept without a mention
  (``iss-68dac765c59c4dbe``; two group-event cases: a file stored through storeMedia with
  its room, and the refusals of a room this host does not serve). The recorded floor was 35,
  which the suite had already grown past without saying so (38 measured on this tree before
  the change below); 39 is the 38 plus the transient retry case (the real bridge, the real
  plugin and the real media library: a fetch that missed its deadline
  ``MEDIA_FETCH.attempts`` times ends with a stored ref and no refusal row in the library's
  index).

The counts are floors, not guesses: a run that prints fewer ``tests`` or fewer ``pass``
than the numbers above has silently dropped cases, and this row goes RED and says so.
Growth is allowed -- when the suite deliberately grows, raise the two numbers in the same
commit, because the counts node prints are the record.

Why: nothing else executes this suite, so a red WeChat channel suite could stay red
without the repository gate noticing.

The director suite (``integrations/dsh-director``) used to be a second row here. It was
removed because ``tests/integration/test_driver_node_suite_runs.py`` already runs every
one of its tests: the driver suite's ``plugin-boundary.test.mjs`` imports every
director test file (and its third test fails when the director's npm script names a
file it does not import), so the driver row's ``pass == tests`` check is red whenever a
director test is. Measured 2026-11-22 at ``5638facc0c`` (23 director test files, the
same 23 in the director's npm script and in ``plugin-boundary.test.mjs``'s imports): the
director suite alone ran 463 tests, and all 463 test names (with their nesting) appear
among the driver run's 773.

Dependencies: the suite resolves only the node standard library and files inside this
repository. It imports ``integrations/wechat-bridge/bridge.mjs``, whose HTTP layer takes
its puppeteer/puppet dependencies as arguments instead of requiring them. So it needs no
``node_modules`` tree of its own, no npm install, and not the pinned
``@deepseek-ai/dsh`` install under ``/opt/aitelier-dsh``. ``node`` is expected at
``/usr/local/bin/node`` in the image.

The other shipped channel suite, ``integrations/dsh-channel-slack``, is deliberately NOT included.
Its own witness test, ``the pinned Slack SDK resolves from the DSH runtime install and has
what this plugin uses``, fails with MODULE_NOT_FOUND because the image's
``/opt/aitelier-dsh/node_modules`` carries no ``@slack``. It joins this row once the image
installs the pinned ``@slack/socket-mode`` 3.1.1 and ``@slack/web-api`` 8.3.0, which makes
that witness green; until then running it would only measure a missing install.

Never a skip: where ``node`` is missing, where the suite directory or its test file
is missing, where the suite exits 0 having run nothing, or where node's own summary lines
cannot be parsed, the row FAILS, because the suite is then UNMEASURED and a skip would
look like a pass. The verdict is taken from the counts node itself prints (its ``ℹ``
summary lines), never from an assumption about them.

Meaningless outside the shipped image (``aitelier:latest``).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = Path("/usr/local/bin/node")
TIMEOUT_S = 900
TAIL_LINES = 80
# Variables a running DSH session sets; the suites must not inherit them.
DROPPED_ENV = ("AITELIER_DSH_EXECUTION", "DSH_PERMISSION_MODE", "AITELIER_TODO_CWD")
EXECUTOR = "the shipped image aitelier:latest (node at /usr/local/bin/node)"
FAIL_MARK = "\u2716"  # node's spec reporter marks a failed test with a heavy X
INFO_MARK = "\u2139"  # and prints its summary lines with an info sign


@dataclass(frozen=True)
class Suite:
    """One shipped node suite: where it lives, which files its runner names, its floor."""

    name: str
    # Files node is told to run, relative to the suite directory.
    files: tuple[str, ...]
    # The ``ℹ tests`` / ``ℹ pass`` counts this row measured when it was written. A run
    # below either is a run that dropped cases; growth is allowed but must be recorded
    # here in the same commit.
    expected_tests: int
    expected_pass: int

    @property
    def directory(self) -> Path:
        return ROOT / "integrations" / self.name


WECHAT = Suite(
    name="dsh-channel-wechat",
    files=("index.test.mjs",),
    expected_tests=39,
    expected_pass=39,
)


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


def _tail(output: str) -> str:
    return "\n".join(output.splitlines()[-TAIL_LINES:])


def _run(suite: Suite, argv: list[str], home: Path) -> tuple[int, str, float]:
    env = {key: value for key, value in os.environ.items() if key not in DROPPED_ENV}
    env["HOME"] = str(home)
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=suite.directory,
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
        pytest.fail(
            f"suite {suite.name} TIMED OUT after {TIMEOUT_S} s and was killed: "
            f"argv={argv!r} cwd={suite.directory}. The suite is UNMEASURED.\n"
            f"--- last {TAIL_LINES} lines of output ---\n{_tail(output or '')}"
        )
    return proc.returncode, output or "", time.monotonic() - started


def _assert_suite_green(suite: Suite, home: Path) -> None:
    if not NODE.is_file() or not os.access(NODE, os.X_OK):
        pytest.fail(
            f"missing capability: {NODE} is not an executable, so suite {suite.name} cannot "
            f"run. The suite is therefore UNMEASURED, not passing and not skipped. This row "
            f"expects {EXECUTOR}."
        )
    if not suite.directory.is_dir():
        pytest.fail(
            f"missing suite directory: {suite.directory} does not exist, so suite "
            f"{suite.name} cannot run. The suite is therefore UNMEASURED, not passing and "
            f"not skipped. This row expects {EXECUTOR}, where the suite ships with the "
            f"repository; a tree that drops the directory needs a decision, not a silent pass."
        )
    files = list(suite.files)
    missing = [name for name in files if not (suite.directory / name).is_file()]
    if missing:
        pytest.fail(
            f"missing test file(s) for suite {suite.name}: {missing} under "
            f"{suite.directory}. The suite is UNMEASURED, not passing and not skipped. "
            f"This row asserts the summary of {suite.expected_tests} tests; check the "
            f"suite's own runner and update the file list here if the suite legitimately moved."
        )
    argv = [str(NODE), "--test", *files]
    rc, output, wall = _run(suite, argv, home)
    counts = _summary(output)
    names = _failing_names(output)
    expected = (
        f"{INFO_MARK} tests {suite.expected_tests} / {INFO_MARK} pass {suite.expected_pass} "
        f"/ {INFO_MARK} fail 0"
    )
    context = (
        f"argv={argv!r} cwd={suite.directory} rc={rc} wall_clock={wall:.1f}s\n"
        f"asserted summary: {expected}\n"
        f"parsed: tests={counts.get('tests', '?')} pass={counts.get('pass', '?')} "
        f"fail={counts.get('fail', '?')}"
    )

    problems: list[str] = []
    if rc != 0:
        problems.append(f"non-zero exit rc={rc}")
    unparsed = [key for key in ("tests", "pass", "fail") if key not in counts]
    for key in unparsed:
        problems.append(f"summary line `{INFO_MARK} {key} N` not found in the output")
    # The counts are judged whenever node printed all three summary lines, even when the
    # suite also exited non-zero: a red suite whose run silently dropped cases must say
    # both things, not only the first one noticed.
    if not unparsed:
        if counts["fail"] != 0:
            problems.append(f"{counts['fail']} failing test(s)")
        if names:
            problems.append(f"{len(names)} `{FAIL_MARK}` line(s) in the output")
        if counts["tests"] < 1:
            problems.append("zero tests ran: discovery is broken or the suite is empty")
        if counts["tests"] < suite.expected_tests:
            problems.append(
                f"only {counts['tests']} tests ran, below the {suite.expected_tests} this "
                f"row asserts: cases were dropped"
            )
        if counts["pass"] < suite.expected_pass:
            problems.append(
                f"only {counts['pass']} tests passed, below the {suite.expected_pass} this "
                f"row asserts"
            )
        if counts["pass"] != counts["tests"]:
            problems.append(
                f"pass {counts['pass']} != tests {counts['tests']}: some tests were "
                f"skipped or cancelled, so the suite is not fully measured"
            )

    if problems:
        pytest.fail(
            f"suite {suite.name} is RED: " + "; ".join(problems) + "\n"
            f"{context}\n"
            f"failing tests ({FAIL_MARK} lines): {names or 'none named in the output'}\n"
            f"summary: {INFO_MARK} fail {counts.get('fail', '?')}\n"
            f"--- last {TAIL_LINES} lines of output ---\n{_tail(output)}"
        )

    # A passing row prints nothing under `pytest -q` (output is captured), so the measured
    # numbers go out as a warning: pytest prints the warnings summary on a pass too.
    # This is a report of the measurement, not a skip, and it never fails the row.
    warnings.warn(
        f"suite {suite.name} GREEN: tests={counts['tests']} pass={counts['pass']} "
        f"fail={counts['fail']} (asserted {expected})\n{context}",
        UserWarning,
        stacklevel=1,
    )


def test_channel_wechat_node_suite_passes_under_node_test(tmp_path: Path) -> None:
    _assert_suite_green(WECHAT, tmp_path)
