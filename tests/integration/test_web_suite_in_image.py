"""Run the SPA's own vitest suite from pytest and fail the gate when it is red.

The gap this closes: no Python test reads ``web/src``. The build step only proves the
SPA *compiles* (``npm run build``); it never executes a single assertion, so a red
Svelte/TypeScript test shipped silently. The pytest-side guards around the driver's
node suite (``tests/unit/test_driver_suite_lists_its_files.py``) likewise inspect file
lists without running javascript. This row is the one that runs ``vitest`` and turns a
failing web test into a failing gate.

What it runs, exactly::

    <node> /opt/aitelier-web/node_modules/vitest/vitest.mjs run \
        --root <stage> --no-file-parallelism --no-coverage --reporter=default

``run`` is the non-watch command, ``--no-file-parallelism`` keeps it to one worker in
one process, ``--no-coverage`` keeps it cheap, and ``--reporter=default`` pins the
summary format this row parses. Nothing is built: ``web/dist`` is irrelevant here.

Where the toolchain comes from: the image bakes the frontend-build stage's
``web/node_modules`` at ``/opt/aitelier-web/node_modules`` (Dockerfile, right after the
``/srv/web_dist`` copy). It is deliberately NOT installed by this row and NOT installed
by the final image stage - a gate that npm-installs at test time would test whatever the
network returned that minute, not what the image ships.

Why a stage instead of running in ``web/`` in place: the vendored tree is root-owned and
read-only for the uid the gate runs as (1000), and Vite writes ``node_modules/.vite``
(the svelte plugin stores ``_svelte_metadata.json`` there) and a bundled
``vitest.config.js.timestamp-*.mjs`` next to the config. So the sources are copied into a
pytest ``tmp_path`` and ``node_modules`` inside the stage is a writable directory of
symlinks to the image's packages plus a real ``.vite`` directory. The stage keeps the
REPOSITORY layout (``<stage>/web/...``, ``<stage>/tests/fixtures/...``) because the suite
resolves its subjects from ``import.meta.dirname``/``import.meta.url`` - ``Steward.test.ts``
reads ``../../App.svelte`` and ``StateProjectsQuestions.test.ts`` reads
``../../../../tests/fixtures/driver_questions_open_family.json`` - and because bare
imports only resolve if the importing module really lives in the stage. Copied: ``web/src``,
``web/__tests__``, ``web/*.js``/``*.json``/``index.html`` and ``tests/fixtures`` (~2.4 MB).
Not copied: ``web/node_modules`` (symlinked from the image) and ``web/dist`` (built by the
frontend stage; the suite never touches it). The checkout is never written to, and the run
measures the checked-out sources.

Two verdicts, no third:
  * ``/opt/aitelier-web/node_modules`` absent -> ``pytest.skip`` naming the missing path
    and saying the web suite is UNMEASURED here. A missing toolchain must never look
    like a pass, so the reason says so in those words. (This is what happens to this row
    when it is run in an image built before the toolchain was baked, e.g. the deployed
    ``aitelier:latest`` of 2026-11-18.)
  * toolchain present -> the suite runs and any of {non-zero rc, unparsable summary,
    a failed file/test, an unexplained count gap, a timeout} FAILS the row with the
    parsed counts, the failing file names and the output tail attached.
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
#: Where the image bakes the frontend-build stage's node_modules tree.
VENDOR = Path("/opt/aitelier-web/node_modules")
VITEST_ENTRY = VENDOR / "vitest" / "vitest.mjs"
#: Repo-relative paths the run needs, copied into the stage. The stage mirrors the
#: repository layout (``<stage>/web/...``, ``<stage>/tests/fixtures/...``) because the
#: suite locates its subjects from ``import.meta.dirname``/``import.meta.url``:
#: ``Steward.test.ts`` reads ``../../App.svelte`` and ``StateProjectsQuestions.test.ts``
#: reads ``../../../../tests/fixtures/driver_questions_open_family.json``. Bare imports
#: must also resolve against the stage's ``node_modules``, which only happens if the
#: importer really lives in the stage. ``web/node_modules`` (symlinked from the image)
#: and ``web/dist`` (built by the frontend stage, irrelevant to the suite) are not copied.
STAGED = (
    "web/src",
    "web/__tests__",
    "web/package.json",
    "web/vitest.config.js",
    "web/svelte.config.js",
    "web/index.html",
    "tests/fixtures",
)
VITEST_ARGS = ["run", "--no-file-parallelism", "--no-coverage", "--reporter=default"]
TIMEOUT_S = 900
TAIL_LINES = 60
#: Populated by a node test runner; a vitest run must not inherit a DSH session's.
DROPPED_ENV = ("AITELIER_DSH_EXECUTION", "DSH_PERMISSION_MODE", "AITELIER_TODO_CWD")

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SUMMARY_LINE = re.compile(r"^\s*(Test Files|Tests)\s+(.+?)\s*$", re.MULTILINE)
COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|todo|pending)\b")
TOTAL = re.compile(r"\((\d+)\)\s*$")
FAILED_FILE = re.compile(r"^\s*(?:FAIL|❯)\s+(\S+?\.(?:test|spec)\.(?:js|ts|jsx|tsx))\b", re.MULTILINE)


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


def _stage(tmp_path: Path) -> Path:
    """Shadow the suite's sources into a writable, repository-shaped stage.

    Returns the vitest root (``<stage>/web``). Nothing under the checkout is modified.
    """
    stage = tmp_path / "repo"
    for name in STAGED:
        source = ROOT / name
        if not source.exists():
            pytest.fail(f"missing source: {source} does not exist, so the stage is incomplete.")
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)

    # A real, writable node_modules whose entries point at the image's packages: Vite
    # and @sveltejs/vite-plugin-svelte write ``node_modules/.vite``, and the vendored
    # tree is read-only for uid 1000. Symlinks are resolved to the real files, so the
    # versions measured are exactly the ones the image baked.
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
    return web_root


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
            f"web suite TIMED OUT after {TIMEOUT_S} s and was killed: argv={argv!r} "
            f"cwd={cwd}. The web suite is UNMEASURED.\n"
            f"--- last {TAIL_LINES} lines of output ---\n{tail}"
        )
    # vitest colourises the summary labels themselves ("\x1b[2m Test Files \x1b[22m"),
    # which defeats a line-anchored parse and makes a failure message unreadable.
    return proc.returncode, ANSI.sub("", output or ""), time.monotonic() - started


def test_web_suite_passes_under_vitest_in_the_image(tmp_path: Path) -> None:
    if not VENDOR.is_dir():
        pytest.skip(
            f"missing toolchain: {VENDOR} does not exist, so the SPA's vitest suite cannot "
            "run here and the web suite is UNMEASURED here - this is a refusal to report a "
            "verdict, not a pass. The path is baked by the Dockerfile from the "
            "frontend-build stage (web/node_modules: vitest, jsdom, "
            "@testing-library/svelte); an image built before that copy existed (for "
            "example the deployed aitelier:latest of 2026-11-18) has node at "
            "/usr/local/bin/node but no JS test dependencies."
        )
    if not VITEST_ENTRY.is_file():
        pytest.fail(
            f"broken toolchain: {VENDOR} exists but {VITEST_ENTRY} is absent, so the SPA's "
            "vitest suite is UNMEASURED: there is no vitest to run. Rebuild the image from a "
            "Dockerfile that copies the frontend-build stage's web/node_modules there."
        )
    node = shutil.which("node")
    if node is None:
        pytest.fail(
            f"missing capability: `node` is not on PATH ({os.environ.get('PATH', '')!r}), so the "
            "SPA's vitest suite is UNMEASURED. The image copies node from its dsh-runtime stage "
            "to /usr/local/bin/node; run this row in that image."
        )

    stage = _stage(tmp_path)
    argv = [node, str(VITEST_ENTRY), *VITEST_ARGS, "--root", str(stage)]
    rc, output, wall = _run(argv, cwd=stage, home=tmp_path)
    counts = _summary(output)
    failures = _failed_files(output)
    # The exact summary lines, kept verbatim: they are the measurement, and a passing
    # row prints them nowhere else (`pytest -q` captures output).
    summary_lines = [
        line.strip()
        for line in output.splitlines()
        if SUMMARY_LINE.match(line)
    ]
    tail = "\n".join(output.splitlines()[-TAIL_LINES:])
    tests = counts.get("tests", {})
    files = counts.get("files", {})
    context = (
        f"argv={argv!r} cwd={stage} rc={rc} wall_clock={wall:.1f}s node={node}\n"
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
    if not problems:
        if files["failed"] or tests["failed"]:
            problems.append(
                f"{files['failed']} failing file(s), {tests['failed']} failing test(s)"
            )
        if tests["total"] < 1:
            problems.append("zero tests ran: discovery is broken or the suite is empty")
        if tests["passed"] < 1:
            problems.append("no test passed")
        unexplained = tests["total"] - (
            tests["passed"] + tests["failed"] + tests["skipped"] + tests["todo"]
        )
        if unexplained != 0:
            problems.append(
                f"summary counts do not add up: total={tests['total']} != passed+failed+"
                f"skipped+todo (gap {unexplained})"
            )

    if problems:
        pytest.fail(
            "SPA vitest suite is RED: " + "; ".join(problems) + "\n"
            f"{context}\n"
            f"failing files named in the output: {failures or 'none named in the output'}\n"
            f"--- last {TAIL_LINES} lines of output ---\n{tail}"
        )

    # A passing row prints nothing under `pytest -q` (output is captured), so the measured
    # numbers go out as a warning: pytest shows the warnings summary on a pass too. This
    # reports the measurement; it never changes the verdict.
    warnings.warn(
        f"SPA vitest suite GREEN: files={files['total']} "
        f"tests={tests['total']} passed={tests['passed']} failed={tests['failed']} "
        f"skipped={tests['skipped']}\n{context}",
        UserWarning,
        stacklevel=1,
    )
