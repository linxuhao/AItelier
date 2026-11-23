"""Run the WeChat bridge's own node suite from pytest, and fail the gate if it is red.

What it runs: one ``node --test`` invocation from the repository root, and nothing else.
Its argv is ``[<node>, "--test", "integrations/wechat-bridge/bridge.test.mjs"]`` -- the file
named explicitly, with the path the package's own npm script uses (``package.json``'s
``test`` script), so the suite is run exactly the way its author runs it. The floor is
``\u2139 tests 128`` / ``\u2139 pass 128`` / ``\u2139 fail 0``, measured in the shipped image on
2026-11-23 with the transient-media-retry case: one new test holds that a media whose fetch
missed its deadline ``MEDIA_FETCH.attempts`` times is still answered 503 and is served by the
next attempt, and that its event leaving the ring (404) is what ends it, not a failure count;
two earlier cases were rewritten to assert the same (a media that never answers, and a failed
download during a resumed login). The floor before it was 126, which the suite had then already
grown one case past without saying so (127 measured on this tree before the change above), and
it was the same suite with the sender-attribution witnesses -- five cases hold the member id a
group medium's raw content begins with (``<member>:\\n<XML>``): a medium whose talker is the room
records that id as its ``sender_ref`` and the probe line says ``senderSource=content-prefix``; a
group text message and a 1:1 message keep the sender they had; a medium with no such prefix
keeps the room and says ``senderSource=room``; and a token that is not id-shaped (a display
name, a space, 33 characters, a room id, no separator) is never taken for a member id. The
earlier floor, 121, was the same suite with the repaired group media sender probe and its nine
witnesses -- the newest three hold the probe's linear base64 validator against the regex it
replaced (a differential case over every string of length 0..6 and a boundary list), check that
content shaped ``<data>=<other>`` prints ``contentBytes=0``, and read a content accessor that
answers once exactly once when that one answer is ``undefined``. The one before that, 120, was
the same suite with the probe and its eight witnesses; the one before that, 118, was the same
suite with the probe and its six witnesses; the one before that,
116, was the same suite with the probe and its original four witnesses; the one before that, 112,
was before that probe; the one before that, 110, was before the send-refusal log lines and their
two witnesses were added; the one before that, 68, was before the bridge's media store and its
group media path grew it. (Measured on node v24.22.3 in the shipped image; this floor's runs
took 12-15 s of pytest wall clock, the 121 floor's 14 s.)

The counts are floors, not guesses: a run that prints fewer ``tests`` or fewer ``pass``
than the numbers above has silently dropped cases, and this row goes RED and says so.
Growth is allowed -- when the suite deliberately grows, raise the two numbers in the same
commit, because the counts node prints are the record.

Why: nothing else executes this suite. Its only reference in this repository is
``integrations/wechat-bridge/package.json``'s ``test`` script, which no pytest row, no CI
config and no other test invokes, so a red bridge suite could stay red without the
repository gate noticing.

Dependencies: the suite resolves only the node standard library and files inside this
repository. It imports ``integrations/wechat-bridge/bridge.mjs``, whose HTTP layer takes its
puppeteer/puppet dependencies as arguments instead of requiring them, so it needs no
``node_modules`` tree of its own, no npm install, and no package registry access. ``node``
is expected at ``/usr/local/bin/node`` in the image.

The other test file in that package, ``integrations/wechat-bridge/qr-svg.test.mjs``, is
deliberately NOT included even though the package's own npm script names both files. It
requires ``qrcode-terminal`` (through ``qr-svg.mjs``, which loads
``qrcode-terminal/vendor/QRCode/index.js``) and ``jsqr``, and the image installs no
``node_modules`` for this package: measured in the shipped image on 2026-11-23, it dies with
``Cannot find module 'qrcode-terminal/vendor/QRCode/index.js'``. Running it would only
measure a missing install. It joins this row once the image installs that package's
``qrcode-terminal`` 0.12.0 and the ``jsqr`` decoder it decodes with; until then naming it
here would make a permanently red row, not a measurement.

Never a skip: where ``node`` is missing, where the suite directory or its test file is
missing, where the suite exits 0 having run nothing, or where node's own summary lines
cannot be parsed, the row FAILS, because the suite is then UNMEASURED and a skip would look
like a pass. The verdict is taken from the counts node itself prints (its ``\u2139`` summary
lines), never from an assumption about them.

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
# Variables a running agent session sets; the suite must not inherit them.
DROPPED_ENV = ("AITELIER_DSH_EXECUTION", "DSH_PERMISSION_MODE", "AITELIER_TODO_CWD")
EXECUTOR = "the shipped image aitelier:latest (node at /usr/local/bin/node)"
FAIL_MARK = "\u2716"  # node's spec reporter marks a failed test with a heavy X
INFO_MARK = "\u2139"  # and prints its summary lines with an info sign


@dataclass(frozen=True)
class Suite:
    """One shipped node suite: where its package lives, the file its runner names, floors."""

    # The package under integrations/ that owns the suite, named as the package names it.
    name: str
    # The test file node is told to run, relative to the repository root: the same path
    # the package's own npm script passes.
    file: str
    # The ``\u2139 tests`` / ``\u2139 pass`` counts this row measured when it was written. A
    # run below either is a run that dropped cases; growth is allowed but must be recorded
    # here in the same commit.
    expected_tests: int
    expected_pass: int

    @property
    def path(self) -> Path:
        return ROOT / self.file

    @property
    def directory(self) -> Path:
        return self.path.parent


BRIDGE = Suite(
    name="wechat-bridge",
    file="integrations/wechat-bridge/bridge.test.mjs",
    expected_tests=128,
    expected_pass=128,
)


def _summary(output: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for key in ("tests", "pass", "fail"):
        hits = re.findall(rf"^[ \t]*{INFO_MARK} {key} (\d+)[ \t]*$", output, re.M)
        if hits:
            found[key] = int(hits[-1])
    return found


def _failing_names(output: str) -> list[str]:
    # A failing test is a `\u2716 <name> (<ms>ms)` line. node also prints a `\u2716 failing tests:`
    # section header and repeats failures as `\u2716 <name>` detail lines without a duration,
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
        cwd=ROOT,
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
            f"argv={argv!r} cwd={ROOT}. The suite is UNMEASURED.\n"
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
    if not suite.path.is_file():
        pytest.fail(
            f"missing test file for suite {suite.name}: {suite.path} does not exist. The "
            f"suite is UNMEASURED, not passing and not skipped. This row asserts the summary "
            f"of {suite.expected_tests} tests; check the package's own npm script and update "
            f"the file named here if the suite legitimately moved."
        )
    argv = [str(NODE), "--test", suite.file]
    rc, output, wall = _run(suite, argv, home)
    counts = _summary(output)
    names = _failing_names(output)
    expected = (
        f"{INFO_MARK} tests {suite.expected_tests} / {INFO_MARK} pass {suite.expected_pass} "
        f"/ {INFO_MARK} fail 0"
    )
    context = (
        f"argv={argv!r} cwd={ROOT} rc={rc} wall_clock={wall:.1f}s\n"
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


def test_wechat_bridge_node_suite_passes_under_node_test(tmp_path: Path) -> None:
    _assert_suite_green(BRIDGE, tmp_path)
