"""Run the SPA's own vitest suite from pytest and fail the gate when it is red.

What it runs, exactly::

    <node> /opt/aitelier-web/node_modules/vitest/vitest.mjs run \
        --no-file-parallelism --no-coverage --reporter=default

with ``cwd`` set to a temporary copy of the checked-out ``web/`` tree. ``run`` is the
non-watch command; ``--no-file-parallelism`` keeps the suite in one worker in one process
(the gate's container is 2 CPUs / 2 GiB and the suite is jsdom-heavy, so file parallelism
thrashes rather than helps); ``--no-coverage`` keeps it cheap; ``--reporter=default`` pins
the summary lines this row parses. Nothing is built: ``web/dist`` is irrelevant here.

Why: no other python row executes a Svelte/TypeScript assertion. The build step only
proves the SPA compiles, and the other pytest-side web rows inspect file lists and the
toolchain without running javascript. A red web test would therefore ship silently: the
repository gate stays green while the SPA's own suite is red. This row is the one that
runs the suite and makes a failing web test a failing gate - it replaces the earlier
``tests/integration/test_web_suite_in_image.py``, which measured the same suite but
``pytest.skip``-ped whenever the toolchain was absent, i.e. reported a verdict of
"skipped" instead of naming what was missing.

Where the toolchain comes from: the image bakes the frontend-build stage's
``web/node_modules`` at ``/opt/aitelier-web/node_modules`` (Dockerfile, right after the
``/srv/web_dist`` copy), and ``node`` is at ``/usr/local/bin/node``. Neither is installed
by this row and neither is taken from ``web/node_modules`` in a host checkout: the point
is to measure what the image ships. A gate that npm-installs at test time would test
whatever the network returned that minute.

Why a copy instead of running in ``web/``: Vite writes while it tests, so a run in the
repository tree would dirty the checkout. Measured in ``aitelier:latest``:

* ``@sveltejs/vite-plugin-svelte`` writes ``node_modules/.vite/_svelte_metadata.json`` in
  its ``buildStart`` hook (``web/vitest.config.js`` loads that plugin), and
* the config bundler writes a ``vitest.config.js.timestamp-*.mjs`` next to the config.

So the sources are copied into a pytest ``tmp_path`` that keeps the REPOSITORY layout
(``<stage>/web/...``, ``<stage>/tests/fixtures/...``): two of the suite's tests resolve
their subjects from their own location - ``Steward.test.ts`` reads ``../../App.svelte``
and ``StateProjectsQuestions.test.ts`` reads
``../../../../tests/fixtures/driver_questions_open_family.json``, which is only correct
from ``<stage>/web/src/__tests__/views`` - and bare imports only resolve if the importing
module really lives in the stage. Copied: ``web/src``, ``web/__tests__``,
``web/vitest.config.js``, ``web/package.json``, ``web/svelte.config.js`` (the svelte
plugin reads it: ``runes: true``), ``web/index.html`` and ``tests/fixtures``. Not copied:
``web/node_modules`` (taken from the image, see below) and ``web/dist`` (built by the
frontend stage; the suite never touches it). The checkout is only ever read: a run leaves
``git status --porcelain`` unchanged, and the row fails if the run creates
``web/node_modules`` or a ``vitest.config.js.timestamp-*.mjs`` bundle inside it.

The stage's ``node_modules`` is a real, writable directory whose every entry is a symlink
to the image's package plus a real ``.vite`` scratch directory, so the versions measured
are exactly the ones the image baked. It cannot be one plain symlink to
``/opt/aitelier-web/node_modules``: that tree is root-owned, and the svelte plugin's
metadata write then kills the run before a single test executes (measured: exit 1 after
2 s with ``EACCES ... node_modules/.vite/_svelte_metadata.json``). Do not "simplify" it
back to a bare symlink.

Never a skip: where ``node``, ``/opt/aitelier-web/node_modules`` or its vitest entry is
missing, the row FAILS naming the missing object and says the web suite is UNMEASURED
here, because a skip would look like a pass and witness nothing.

Two verdicts, no third: the suite is GREEN only when vitest exits 0 and its own summary
reports a non-zero test-file count, a non-zero test count, and no failed file or test.
Anything else - non-zero exit, an unparsable summary, a count gap, a timeout of
``TIMEOUT_S`` (600 s, against ~65 s measured) - FAILS with the argv, the copied tree, the
parsed counts and a bounded tail of vitest's own output.

Meaningless outside the shipped image (``aitelier:latest``).
"""

from __future__ import annotations

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
WEB = ROOT / "web"
#: Where the image bakes the frontend-build stage's node_modules tree.
VENDOR = Path("/opt/aitelier-web/node_modules")
VITEST_ENTRY = VENDOR / "vitest" / "vitest.mjs"
#: Repo-relative paths copied into the stage. The stage mirrors the repository layout
#: because the suite resolves subjects from its own location (see the docstring), and
#: because bare imports only resolve if the importing module really lives in the stage.
STAGED = (
    "web/src",
    "web/__tests__",
    "web/vitest.config.js",
    "web/package.json",
    "web/svelte.config.js",
    "web/index.html",
    "tests/fixtures",
)
VITEST_ARGS = ["run", "--no-file-parallelism", "--no-coverage", "--reporter=default"]
TIMEOUT_S = 600
TAIL_LINES = 80
# Variables a running DSH session sets; a vitest run must not inherit them.
DROPPED_ENV = ("AITELIER_DSH_EXECUTION", "DSH_PERMISSION_MODE", "AITELIER_TODO_CWD")
EXECUTOR = "the shipped image aitelier:latest (node at /usr/local/bin/node)"

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SUMMARY_LINE = re.compile(r"^\s*(Test Files|Tests)\s+(.+?)\s*$", re.MULTILINE)
COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|todo|pending)\b")
TOTAL = re.compile(r"\((\d+)\)\s*$")
#: A failed file is listed as `❯ <path>.test.ts (N tests | M failed)`; the marker also
#: appears in stack frames (`❯ <path>:5:47`), which the suffix filter and the dedup drop
#: for file names but which never hide a failure.
FAILED_FILE = re.compile(r"^\s*[❯>]\s+(\S+?\.(?:test|spec)\.(?:js|ts|jsx|tsx))\b", re.M)
#: The failure detail section repeats each failure as `FAIL <file> > <suite> > <test>`.
FAIL_LINE = re.compile(r"^\s*FAIL\s+(.+?)\s*$", re.MULTILINE)


def _summary(output: str) -> dict[str, dict[str, int]]:
    """Parse vitest's default-reporter summary into {files|tests: {state: count}}."""
    found: dict[str, dict[str, int]] = {}
    for label, rest in SUMMARY_LINE.findall(output):
        counts = dict.fromkeys(("passed", "failed", "skipped", "todo", "total"), 0)
        for number, state in COUNT.findall(rest):
            counts["skipped" if state == "pending" else state] += int(number)
        total = TOTAL.search(rest)
        counts["total"] = int(total.group(1)) if total else sum(
            counts[key] for key in ("passed", "failed", "skipped", "todo")
        )
        found["files" if label == "Test Files" else "tests"] = counts
    return found


def _failed_files(output: str) -> list[str]:
    names: list[str] = []
    for name in FAILED_FILE.findall(output):
        if name not in names:
            names.append(name)
    return names


def _fail_lines(output: str) -> list[str]:
    lines: list[str] = []
    for line in FAIL_LINE.findall(output):
        if line not in lines:
            lines.append(line)
    return lines


def _stage(tmp_path: Path) -> tuple[Path, list[tuple[str, int]]]:
    """Copy the suite's sources into a writable, repository-shaped stage.

    Returns the vitest root (``<stage>/web``) and, for the report, every copied path with
    the number of files it brought. Nothing under the checkout is written.
    """
    stage = tmp_path / "repo"
    staged: list[tuple[str, int]] = []
    for name in STAGED:
        source = ROOT / name
        if not source.exists():
            pytest.fail(
                f"missing source: {source} does not exist, so the stage would be incomplete "
                "and the SPA suite UNMEASURED. Run this row against a full checkout."
            )
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
            count = sum(1 for path in target.rglob("*") if path.is_file())
        else:
            shutil.copy2(source, target)
            count = 1
        staged.append((name, count))

    # A real, writable node_modules whose entries point at the image's packages: the
    # svelte plugin writes ``node_modules/.vite/_svelte_metadata.json`` at startup and the
    # baked tree is root-owned (a plain symlink to VENDOR dies there with EACCES before
    # any test runs). Symlinks resolve to the real files, so what is measured is exactly
    # what the image baked - never a host checkout's node_modules.
    web_root = stage / "web"
    modules = web_root / "node_modules"
    modules.mkdir()
    (modules / ".vite").mkdir()
    for entry in sorted(VENDOR.iterdir()):
        if entry.name in (".bin", ".vite", ".cache"):
            # .bin: this row invokes vitest.mjs directly, npm shims are not needed.
            # .vite/.cache: the stage's own writable scratch directories, not packages.
            continue
        target = modules / entry.name
        if entry.name.startswith("@") and entry.is_dir():
            target.mkdir()
            for member in sorted(entry.iterdir()):
                os.symlink(member, target / member.name)
        else:
            os.symlink(entry, target)
    return web_root, staged


def _run(argv: list[str], cwd: Path, home: Path) -> tuple[int, str, float]:
    env = {key: value for key, value in os.environ.items() if key not in DROPPED_ENV}
    env["HOME"] = str(home)
    env["CI"] = "1"  # non-interactive reporter, never a watch prompt
    env["NO_COLOR"] = "1"  # and no escape codes in the summary this row parses
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
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
        # Kill the whole group: vitest may still own worker children.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        output, _ = proc.communicate()
        tail = "\n".join(ANSI.sub("", output or "").splitlines()[-TAIL_LINES:])
        pytest.fail(
            f"SPA vitest suite TIMED OUT after {TIMEOUT_S} s and was killed: argv={argv!r} "
            f"cwd={cwd}. The SPA suite is UNMEASURED.\n"
            f"--- last {TAIL_LINES} lines of output ---\n{tail}"
        )
    # vitest colourises the summary labels themselves ("\x1b[2m Test Files \x1b[22m"),
    # which defeats a line-anchored parse and makes a failure message unreadable.
    return proc.returncode, ANSI.sub("", output or ""), time.monotonic() - started


@pytest.mark.slow
def test_web_suite_runs_under_vitest_in_the_image(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            "missing capability: the web suite is UNMEASURED here - `node` is not on PATH "
            f"({os.environ.get('PATH', '')!r}), so the SPA's vitest suite cannot run, and a "
            "missing binary must never look like a pass or a skip. The image copies node to "
            f"/usr/local/bin/node; this row expects {EXECUTOR}."
        )
    if not VENDOR.is_dir():
        pytest.fail(
            f"missing toolchain: the web suite is UNMEASURED here - {VENDOR} is missing, so "
            "there is no vitest to run, and a missing toolchain must never look like a pass "
            "or a skip. The path is baked by the Dockerfile from the frontend-build stage "
            "(vitest, jsdom, @testing-library/svelte); this row expects "
            f"{EXECUTOR}."
        )
    if not VITEST_ENTRY.is_file():
        pytest.fail(
            f"missing toolchain: the web suite is UNMEASURED here - {VENDOR} exists but its "
            f"entry point {VITEST_ENTRY} is missing, so there is no vitest to run, and a "
            "broken toolchain must never look like a pass or a skip."
        )

    # The checkout must be read-only for this row: remember whether anything the run
    # is known to want to write already existed, so a leak into the repository is caught.
    repo_modules_before = (WEB / "node_modules").exists()
    repo_stray_before = sorted(WEB.glob("*.timestamp-*.mjs"))

    web_root, staged = _stage(tmp_path)
    argv = [node, str(VITEST_ENTRY), *VITEST_ARGS]
    rc, output, wall = _run(argv, cwd=web_root, home=tmp_path)

    counts = _summary(output)
    failed_files = _failed_files(output)
    fail_lines = _fail_lines(output)
    summary_lines = [
        line.strip() for line in output.splitlines() if SUMMARY_LINE.match(line)
    ]
    tail = "\n".join(output.splitlines()[-TAIL_LINES:])
    tests = counts.get("tests", {})
    files = counts.get("files", {})
    copied = ", ".join(f"{name} ({count} file{'s' if count != 1 else ''})" for name, count in staged)
    context = (
        f"argv={argv!r} cwd={web_root} rc={rc} wall_clock={wall:.1f}s node={node}\n"
        f"copied into the stage (root={tmp_path}): {copied}\n"
        f"node_modules: writable directory of symlinks into {VENDOR}\n"
        f"parsed: files passed={files.get('passed', '?')} failed={files.get('failed', '?')} "
        f"total={files.get('total', '?')} | tests passed={tests.get('passed', '?')} "
        f"failed={tests.get('failed', '?')} skipped={tests.get('skipped', '?')} "
        f"todo={tests.get('todo', '?')} total={tests.get('total', '?')}\n"
        f"vitest summary: {' | '.join(summary_lines) or 'not printed'}"
    )

    problems: list[str] = []
    if rc != 0:
        problems.append(f"non-zero exit rc={rc}")
    for key, label in (("files", "Test Files"), ("tests", "Tests")):
        if key not in counts:
            problems.append(f"summary line `{label} ...` not found in the output")
    # The row may not write into the repository tree; the stage is the only writable place.
    if (WEB / "node_modules").exists() and not repo_modules_before:
        problems.append(f"the run created {WEB / 'node_modules'} inside the repository")
    if sorted(WEB.glob("*.timestamp-*.mjs")) != repo_stray_before:
        problems.append(f"the run left a vitest config bundle next to {WEB / 'vitest.config.js'}")
    if not problems:
        if files["failed"] or tests["failed"]:
            problems.append(
                f"{files['failed']} failing file(s), {tests['failed']} failing test(s)"
            )
        if files["total"] < 1:
            problems.append("zero test files ran: discovery is broken or the suite is empty")
        if tests["total"] < 1:
            problems.append("zero tests ran: discovery is broken or the suite is empty")
        if tests["passed"] < 1:
            problems.append("no test passed")
        gap = tests["total"] - (
            tests["passed"] + tests["failed"] + tests["skipped"] + tests["todo"]
        )
        if gap != 0:
            problems.append(
                f"summary counts do not add up: total={tests['total']} != passed+failed+"
                f"skipped+todo (gap {gap})"
            )

    if problems:
        pytest.fail(
            "SPA vitest suite is RED: " + "; ".join(problems) + "\n"
            f"{context}\n"
            f"failing files ({failed_files or 'none named in the output'})\n"
            f"failing tests ({fail_lines or 'none named in the output'})\n"
            f"--- last {TAIL_LINES} lines of output ---\n{tail}"
        )

    # A passing row prints nothing under `pytest -q` (output is captured), so the measured
    # numbers go out as a warning: pytest prints the warnings summary on a pass too.
    # This is a report of the measurement, not a skip, and it never fails the row.
    warnings.warn(
        f"SPA vitest suite GREEN: files={files['total']} tests={tests['total']} "
        f"passed={tests['passed']} failed={tests['failed']} skipped={tests['skipped']} "
        f"wall_clock={wall:.1f}s\n{context}",
        UserWarning,
        stacklevel=1,
    )
