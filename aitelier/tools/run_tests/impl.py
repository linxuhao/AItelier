"""run_tests — execute the project's unit tests and write a test report.

Used as a tool STEP after the final verifier. It ALWAYS succeeds (so a failing
test never fails the run); the outcome is captured in ``test_report.json`` so the
verifier-review step can fold test failures into its change requests and loop
back to the planner (the goal-loop).

Repository Python executes only through the trusted disposable CPU Docker
adapter. The reviewed test image supplies pytest, plugins and project dependencies;
there is no backend interpreter/venv fallback or live dependency installation.
Missing execution facility is explicit non-passing infrastructure evidence.

Three outcomes, not two: pass, fail, and NO EVIDENCE. "pytest collected nothing"
(exit 5) is the third, and it is not a pass — see the returncode handling in
`run_tests` — unless the repo holds no Python at all, in which case the node /
compile sections are the real gate.
"""

import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import struct
import subprocess

from core import datadir, env_scrub
from core import repository_executor
from aitelier import gate_admission
# Module-level, NOT function-local. This name is used on EVERY path out of
# `run_tests` (the return dict's `release_evidence`), so an import that lives
# inside the `fail_fast_gates` branch made it a LOCAL of the whole function:
# every ordinary invocation died with `UnboundLocalError: cannot access local
# variable 'release_disposition'` before writing a report at all. That is a
# step that never produced a verdict for a reason that has nothing to do with
# the code under test — the exact shape this card exists to remove.
from aitelier.gate_evidence import release_disposition, report_state
from aitelier.gate_coverage import validate_coverage
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import NamedTuple


def _lead_with_install_error(report: dict) -> str:
    """Put the install failure ABOVE the pytest output, when there was one.

    Ordering is the whole point. An unimportable package produces a
    `ModuleNotFoundError` whose real cause is the failed editable install; a
    reader handed only the symptom rewrites packaging metadata that was never
    wrong. That is exactly what happened, on every lap of two separate drives.
    """
    err = report.get("install_error")
    if not err or report.get("passed"):
        return report.get("summary", "")
    return ("The project could not be installed into the test environment, so its "
            "own package may be unimportable. Fix this FIRST — a "
            "ModuleNotFoundError below is most likely a consequence of it, not a "
            f"packaging-discovery problem:\n    {err}\n\n"
            + report.get("summary", ""))


def _lead_with_import_error(report: dict) -> str:
    """Put the failed `import <pkg>` ABOVE the pytest output, when there was one.

    A package that doesn't import makes every number below it meaningless — and
    pytest is a poor messenger for it: it exits 4 (usage error) before
    collection, so the report reads as a conftest problem several lines away
    from the actual defect.
    """
    err = report.get("import_error")
    if not err:
        return report.get("summary", "")
    # A failed install outranks it: the missing module is then a CONSEQUENCE, and
    # a reader handed only the symptom rewrites packaging metadata (see
    # `_lead_with_install_error`, written for exactly that loop).
    note = ("\n    NOTE: the project's install into the test environment also "
            "failed — see `install_error` below; that is the likelier root cause."
            if report.get("install_error") else "")
    return ("The delivered package does not import. Fix this FIRST — no test "
            "result below means anything until it does:\n    "
            f"{err}{note}\n\n" + report.get("summary", ""))


def _install_failure_reason(proc) -> str:
    """The ONE line worth showing from a failed pip run.

    A raw tail is traceback noise — the last four lines of a pip failure are
    usually caret markers and vendored frames. The line that matters is the
    exception, e.g. `BackendUnavailable: Cannot import
    'setuptools.backends._legacy'`, which names the exact cause. Prefer it;
    fall back to the tail only when nothing looks like an error.
    """
    text = (proc.stderr or "") + "\n" + (proc.stdout or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        # An exception line looks like `SomeError: message` at column 0-ish, and
        # is not a file/frame reference or a caret ruler.
        if (re.match(r"^[A-Za-z_][\w.]*(Error|Exception|Unavailable|NotFound)\b", ln)
                and not ln.startswith(("File ", "  File "))):
            return ln[:600]
    for ln in reversed(lines):
        if "error" in ln.lower() and not ln.startswith(("File ", "^")):
            return ln[:600]
    return " | ".join(lines[-3:])[:600] if lines else f"exit {proc.returncode}"


_TEST_EXTRA_NAMES = ("test", "tests", "dev", "testing")


def _pyproject(repo: Path) -> dict:
    """Parsed ``pyproject.toml``, or {} — never raises."""
    try:
        import tomllib
        return tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _test_extras(repo: Path) -> list[str]:
    """The project's DECLARED optional-dependency groups that carry test deps.

    Test-only deps live in ``[project.optional-dependencies]``, which
    ``pip install -e .`` does not touch — so every one of them was missing from
    the test environment, always. NL2Repo sweep 2026-08-18: 6 of 12 runs had a
    test-gate iteration blinded by exactly this (`asgi_lifespan`, `freezegun`,
    `sqlalchemy`, `pytest-benchmark`), and 2 were blind on every iteration. The
    gate then reported a `ModuleNotFoundError` in ``conftest.py`` — an
    ENVIRONMENT fault no DPE role can fix (none has a shell or an install tool)
    — while the real defect six lines below it was never reached.

    Only names the project actually declares are returned: pip errors out on an
    unknown extra, so guessing `[test]` blindly would break the install for
    every project that doesn't have one.
    """
    declared = (_pyproject(repo).get("project") or {}).get(
        "optional-dependencies") or {}
    if not isinstance(declared, dict):
        return []
    return [g for g in _TEST_EXTRA_NAMES if g in declared]


def _install_project_deps(venv_py: str, repo: Path) -> str:
    """Best-effort install of the project's declared deps. Returns "" or WHY it failed.

    Tries ``requirements.txt``; else an editable install of the project itself
    (reads ``pyproject.toml`` ``[project.dependencies]`` / ``setup.py``). Still
    never raises and never `check=True`s — the ``--system-site-packages`` base
    usually already satisfies imports, and a non-installable generated project
    (an app, not a package) must NOT fail the test gate.

    But the failure is RETURNED rather than discarded. It used to be swallowed
    twice over (`check=False` plus `except: pass`), and that hid the one fact
    that explained everything downstream. Observed: a generated project declared

        build-backend = "setuptools.backends._legacy:_Backend"   # does not exist

    so `pip install -e .` died with `BackendUnavailable: Cannot import
    'setuptools.backends._legacy'` — an error naming the exact cause. It was
    thrown away, pytest then reported `ModuleNotFoundError: No module named
    'word_frequency'`, and the maker — reading a symptom with the cause hidden —
    concluded "package discovery" and rewrote `[tool.setuptools.packages.find]`,
    which is irrelevant. Same loop, every lap.
    """
    try:
        extras: list[str] = []
        if (repo / "requirements.txt").exists():
            cmd = [venv_py, "-m", "pip", "install", "-q", "-r",
                   str(repo / "requirements.txt")]
        elif any((repo / f).exists()
                 for f in ("pyproject.toml", "setup.py", "setup.cfg")):
            # Test extras when the project declares them: `-e .[test,dev]`.
            extras = _test_extras(repo)
            target = f"{repo}[{','.join(extras)}]" if extras else str(repo)
            cmd = [venv_py, "-m", "pip", "install", "-q", "-e", target]
        else:
            return ""
        # `pip install -e` EXECUTES the target's build backend — setup.py or a
        # PEP 517 hook — i.e. LLM-authored code, which is the same property that
        # justified scrubbing `npm ci` one commit ago. And the output is
        # published: `_install_failure_reason` puts it in test_report.json (an
        # anonymously-readable step output) and in the reviewer's prompt (an
        # anonymously-readable trace). Third spawn, same rule.
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=300, check=False,
                              env=env_scrub.scrubbed_env())
        if proc.returncode != 0 and extras:
            # An extra that fails to resolve must not cost us the base install:
            # degrade to exactly what this did before extras existed.
            proc = subprocess.run(
                [venv_py, "-m", "pip", "install", "-q", "-e", str(repo)],
                env=env_scrub.scrubbed_env(),
                capture_output=True, text=True, timeout=300, check=False)
        if proc.returncode != 0:
            return _install_failure_reason(proc)
        return ""
    except Exception as e:
        return f"{type(e).__name__}: {e}"[:300]


def _closest(wanted: str, names: str) -> list[str]:
    """Nearest real names to a wrong one.

    A prefix or substring test is not enough: `decode_all` shares no substring
    with `decode`, and `InitializationCapabilities` differs from
    `InitializationOptions` only in its tail. Fuzzy matching catches both, which
    is the whole value — the agent needs the ONE name it meant, not a list to
    re-scan.
    """
    import difflib
    return difflib.get_close_matches(wanted, names.split(", "), n=3, cutoff=0.5)


_COLLECT_HEADER_RE = re.compile(r"^_+ ERROR collecting (\S+) _+$")


def _collection_errors(out: str) -> list[str]:
    """Every module pytest could not import, each with the exception that stopped it.

    Two things hid these. pytest without ``--continue-on-collection-errors``
    interrupts before running a single test, so one broken module reports as a
    suite with zero results — and the flag alone is not enough, because pytest's
    own short summary names the file and NOT the cause ("ERROR tests/test_x.py"),
    while the causes live in the per-module ERROR blocks, up in the part of the
    output an `out[-3000:]` tail drops. Pairing header to cause here is what turns
    a run into a list of every import defect, which is the point: the reviewer
    that reads this report can only open one fix-task per defect it can SEE, and
    the `5_review → 3` goal loop is capped at two laps.
    """
    errors: list[str] = []
    current: str | None = None
    for line in out.splitlines():
        header = _COLLECT_HEADER_RE.match(line.strip())
        if header:
            current = header.group(1)
            continue
        if current and line.startswith("E   "):
            errors.append(f"ERROR {current} - {line[4:].strip()}"[:400])
            current = None  # first E line is the exception; skip the rest of the block
    return errors


_ATTR_RE = re.compile(r"AttributeError: ['\"](\w+)['\"] object has no attribute ['\"](\w+)['\"]")
_FROM_IMPORT_RE = re.compile(r"from ([\w.]+) import ([\w, ]+)")

_IMPORT_NAME_RE = re.compile(
    r"cannot import name ['\"]([\w.]+)['\"] from ['\"]([\w.]+)['\"]")


def _explain_missing_names(py: str, summary: str) -> str:
    """Append what a module ACTUALLY exports when an import of a name fails.

    `ImportError: cannot import name 'InitializationCapabilities' from
    'mcp.server.models'` means the module imported fine — the answer is sitting
    in the interpreter that just raised. Without it the agent can only guess
    again: the read tools are closures over the project root / step staging /
    step output, so `site-packages` is out of their reach, and it guessed the
    same wrong symbol on three separate drives.

    Runs in the SAME interpreter pytest used, so the names are the real ones.
    Best-effort and silent on failure — this only ever adds information.
    """
    # No early return on "no import-name hits": the ATTRIBUTE pass below is a
    # separate failure mode, and short-circuiting here meant it never ran.
    hits = _IMPORT_NAME_RE.findall(summary or "")
    notes = []
    for wanted, module in dict.fromkeys(hits):
        try:
            proc = subprocess.run(
                [py, "-c",
                 "import importlib,sys;m=importlib.import_module(sys.argv[1]);"
                 "print(', '.join(sorted(n for n in dir(m) "
                 "if not n.startswith('_'))))", module],
                capture_output=True, text=True, timeout=30)
            names = (proc.stdout or "").strip()
        except Exception:
            names = ""
        if not names:
            continue
        close = _closest(wanted, names)
        notes.append(
            f"'{module}' has no '{wanted}'. It actually exports: {names[:800]}"
            + (f"\n  Closest by name: {', '.join(close)}" if close else ""))
    notes += _explain_missing_attrs(py, summary)
    if not notes:
        return summary
    return (summary + "\n\n[what those modules/objects really provide]\n"
            + "\n".join(notes))


def _explain_missing_attrs(py: str, summary: str) -> list[str]:
    """Same idea one layer deeper: `'X' object has no attribute 'y'`.

    The class is real and importable — it is named in a `from M import X` line in
    the same traceback — so its actual attributes are knowable. Observed after the
    import-level guess was fixed: the agent moved to `@server.tool()`, which is
    FastMCP's decorator and does not exist on the low-level `Server`. Without this
    it guesses again at the next layer, and the failure walks one attribute at a
    time through a fix budget.
    """
    hits = _ATTR_RE.findall(summary or "")
    if not hits:
        return []
    modules = {m for m, _ in _FROM_IMPORT_RE.findall(summary or "")}
    if not modules:
        return []
    notes = []
    for cls, attr in dict.fromkeys(hits):
        for mod in sorted(modules):
            try:
                proc = subprocess.run(
                    [py, "-c",
                     "import importlib,sys;m=importlib.import_module(sys.argv[1]);"
                     "c=getattr(m,sys.argv[2],None);print('' if c is None else "
                     "', '.join(sorted(n for n in dir(c) if not n.startswith('_'))))",
                     mod, cls],
                    capture_output=True, text=True, timeout=30)
                names = (proc.stdout or "").strip()
            except Exception:
                names = ""
            if names:
                close = _closest(attr, names)
                notes.append(
                    f"'{cls}' (from {mod}) has no '{attr}'. Its attributes are: "
                    f"{names[:800]}"
                    + (f"\n  Closest by name: {', '.join(close)}" if close else ""))
                break
    return notes


def _pythonpath_for(repo: Path) -> str:
    """PYTHONPATH for the pytest subprocess: the repo root, plus `src/`.

    Only the root used to be on the path, so a FLAT layout imported fine and the
    standard `src/` layout could not import its own package at all — every test
    module died on `ModuleNotFoundError`, on every attempt, unfixably.

    The project install that would otherwise cover this
    (`_install_project_deps` → `pip install -e .`) runs ONLY on the
    venv-provisioning path: `_resolve_pytest_python` returns the current
    interpreter immediately when pytest is already importable, which in the
    container it always is. So on the common path the project under test was
    never installed and nothing put `src` on the path.

    Adding it here rather than installing is deliberate: `pip install -e .` into
    the SERVER's own interpreter would mutate the container's site-packages with
    LLM-generated package metadata on every test run. This is the same thing
    pytest's own `pythonpath = ["src"]` ini option does, and it touches nothing.
    """
    roots = [str(repo)]
    for name in ("src",):
        d = repo / name
        if d.is_dir():
            roots.append(str(d))
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        roots.append(existing)
    return os.pathsep.join(roots)


_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox"}


def _package_module(repo: Path) -> str | None:
    """The delivered package's IMPORT name, or None if it can't be established.

    Derived from ``[project] name`` (distribution names normalise `-`/`.` to
    `_`) and then CONFIRMED against the tree: a name that doesn't correspond to
    a package/module actually in the repo is a name we'd only guess wrong with,
    so the smoke check is skipped rather than fabricating a failure.
    """
    name = (_pyproject(repo).get("project") or {}).get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    normalised = re.sub(r"[-_.]+", "_", name.strip())
    for mod in dict.fromkeys((normalised, normalised.lower())):
        for probe in (repo / mod / "__init__.py", repo / "src" / mod / "__init__.py",
                      repo / f"{mod}.py", repo / "src" / f"{mod}.py"):
            if probe.exists():
                return mod
    return None


def _import_smoke_error(py: str, repo: Path, env: dict) -> str:
    """`python -c "import <pkg>"` — the one line that says the code even loads.

    A package that does not import makes EVERY test meaningless, and the pytest
    output rarely says so plainly: NL2Repo `fastapi-users` shipped one wrong
    import (`SecurityBase` from `fastapi.security`, which lives in
    `fastapi.security.base`), `tests/conftest.py` became unimportable, pytest
    exited 4 before collection, and all 556 cases scored zero. A prior attempt
    on the same task died on a bare `NameError` in a strategy module. Both are
    this one line; neither was visible in the gate's report.

    Returns "" when the package imports (or can't be identified — see
    `_package_module`). Never raises: this only ever adds information.
    """
    mod = _package_module(repo)
    if not mod:
        return ""
    try:
        proc = subprocess.run([py, "-c", f"import {mod}"], capture_output=True,
                              text=True, cwd=str(repo), env=env, timeout=60)
        if proc.returncode == 0:
            return ""
        lines = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
        cause = lines[-1] if lines else f"exit {proc.returncode}"
        return f"import {mod} failed: {cause}"[:600]
    except Exception:
        return ""


def _has_python_sources(repo: Path) -> bool:
    """Does this repo contain Python code at all?

    Decides whether "pytest collected nothing" means MISSING EVIDENCE (a Python
    project that shipped no tests) or NOT APPLICABLE (a node/Godot project whose
    real gate is the node or compile section — failing those on an empty pytest
    run would spin the goal loop on something no task can fix).
    """
    for _root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]  # prune, don't just skip:
        if any(f.endswith(".py") for f in files):           # node_modules is huge
            return True
    return False


def _pytest_timeout_args(py: str) -> list[str]:
    """Per-test timeout args, only if pytest-timeout is available for ``py``.

    Added unconditionally would make pytest error ("unrecognized arguments")
    on a host interpreter that lacks the plugin (e.g. the dev test interp).
    """
    try:
        if py == sys.executable:
            available = importlib.util.find_spec("pytest_timeout") is not None
        else:
            available = subprocess.run(
                [py, "-c", "import pytest_timeout"],
                capture_output=True, timeout=30).returncode == 0
        return ["--timeout=60", "--timeout-method=thread"] if available else []
    except Exception:
        return []


def _resolve_pytest_python(repo: Path, report: dict) -> tuple[str | None, str | None]:
    """Return (python_executable, venv_dir_to_cleanup).

    python_executable is an interpreter that can `-m pytest`; None means the
    runner is unavailable and the caller should SKIP (report is updated in place
    with the skip outcome).
    """
    # 1. Use the running interpreter only with the required async toolchain.
    if (importlib.util.find_spec("pytest") is not None
            and importlib.util.find_spec("pytest_asyncio") is not None):
        return sys.executable, None

    # 2. Provision a throwaway venv that inherits system site-packages (so we
    #    only have to add the test toolchain, not reinstall the whole dep set).
    #    The pip install reaches PyPI, so a transient network blip would skip the
    #    gate (tests never run → false pass). Retry the whole provisioning a few
    #    times with backoff so only a PERSISTENT outage skips; a momentary blip
    #    recovers on the next attempt.
    attempts = 3
    last_err: Exception | None = None
    for attempt in range(1, attempts + 1):
        venv_dir = tempfile.mkdtemp(prefix="aitelier_pytest_venv_")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", "--system-site-packages", venv_dir],
                capture_output=True, text=True, timeout=120, check=True,
            )
            venv_py = str(Path(venv_dir) / "bin" / "python")
            if not Path(venv_py).exists():  # windows / unusual layouts
                venv_py = str(Path(venv_dir) / "Scripts" / "python.exe")

            # Test toolchain: pytest + the plugins the project's pytest.ini
            # commonly requires. pytest-asyncio is mandatory for
            # `asyncio_mode = auto` (its absence makes every async test error);
            # pytest-timeout enables the per-test wall. pip already retries
            # individual downloads; the outer loop recovers from a blip that
            # exhausts pip's own retries.
            subprocess.run(
                [venv_py, "-m", "pip", "install", "-q",
                 "pytest", "pytest-asyncio", "pytest-timeout"],
                capture_output=True, text=True, timeout=300, check=True,
            )
            # Project's own deps — best-effort, must not skip the gate on failure.
            install_err = _install_project_deps(venv_py, repo)
            if install_err:
                # Surfaced, not fatal: the gate still runs, but the agent is told
                # WHY its package may be unimportable instead of being handed a
                # bare ModuleNotFoundError with the cause removed.
                report["install_error"] = install_err
            return venv_py, venv_dir
        except Exception as e:
            last_err = e
            shutil.rmtree(venv_dir, ignore_errors=True)
            if attempt < attempts:
                time.sleep(2 * attempt)  # 2s, then 4s, before retrying

    # All attempts failed → a persistent outage. Keep it distinct from a test
    # failure, but non-passing: a release cannot use a missing runner as proof.
    report.update(
        passed=False, skipped=True, infrastructure_unavailable=True,
        evidence_state="infrastructure_unavailable", returncode=0,
        summary=(f"pytest toolchain unavailable and could not be provisioned after "
                 f"{attempts} attempts ({type(last_err).__name__}: "
                 f"{str(last_err)[:200]}) — test gate skipped."),
    )
    return None, None


def _find_node_project(repo: Path) -> Path | None:
    """Locate the repo's node project: package.json at the root, else the
    first one exactly one level deep (e.g. ``web/package.json`` — AItelier's
    own layout; the root-only check is how two dogfood runs verified green
    with a frontend that didn't even compile)."""
    if (repo / "package.json").exists():
        return repo
    candidates = sorted(
        p.parent for p in repo.glob("*/package.json")
        if "node_modules" not in p.parts
    )
    return candidates[0] if candidates else None


# Keep routine output bounded. Diagnostics and explicit case records are read
# from the complete capture before this tail is applied; structured retained
# reports still take precedence for repository gate attribution.
OUTPUT_TAIL_CHARS = 2000


def _failure_context(out: str) -> tuple[str, bool]:
    """Readable diagnostics, never evidence of case identity or measurement."""
    lines = out.splitlines()
    selected = set()
    for i, line in enumerate(lines):
        if re.match(r"^\s*(?:SCRIPT ERROR:|ERROR:|push_error\b|FAILED\b|FAIL\b)", line):
            selected.update(range(max(0, i - 2), min(len(lines), i + 13)))
    text = "\n".join(lines[i] for i in sorted(selected))
    return text[:6000], len(text) > 6000


def _gate_feedback(gate: dict) -> str:
    context = gate.get("failure_context", "")
    note = "\n[diagnostic context truncated; read output_ref]" if gate.get("failure_context_truncated") else ""
    return (context + note) if context else str(gate.get("output", ""))[-1500:]


def _run_node_cmd(pkg_dir: Path, args: list[str], timeout: int,
                  env_overrides: dict | None = None, *, run_id: str = "",
                  node: dict | None = None) -> dict:
    """Run one repository command in the bounded CPU container; never locally.

    Two callers. The repository gate passes ``GATE_REPORT_DIR`` (its ticket)
    and its relay socket. The normal Node gate passes ``node`` — its profile,
    ticket, snapshot workdir and npm cache — and then ``pkg_dir`` is the run's
    OWNED repository, which binds the request to the run and is not mounted.
    Anything else is refused: there is no host-process fallback.

    `output` is a bounded tail. Diagnostics, case records and declarations
    are scanned from the complete capture; a repository gate also retains
    that capture under its existing ticket with a hash. `timed_out`,
    `runner_error` and `isolation_unavailable` are the framework OBSERVING
    that nothing was measured — never inferred from `returncode`.
    """
    try:
        if node is not None:
            done = repository_executor.execute(
                pkg_dir, args, timeout, writable_dirs=[Path(node["ticket"])],
                run_id=run_id, profile=node["profile"], workdir=str(node["workdir"]),
                npm_cache=str(node["npm_cache"]))
        elif (env_overrides or {}).get("GATE_REPORT_DIR"):
            done = repository_executor.execute(
                pkg_dir, args, timeout,
                writable_dirs=[Path(env_overrides["GATE_REPORT_DIR"])],
                relay_socket=env_overrides.get("AITELIER_GATE_RELAY_SOCKET", ""),
                report_dir=env_overrides["GATE_REPORT_DIR"], run_id=run_id)
        else:
            raise repository_executor.IsolationUnavailable(
                "no bounded execution profile for this command; local execution refused")
        stdout, stderr = done.stdout, done.stderr
        returncode = done.returncode
        out = ((stdout or "") + "\n" + (stderr or "")).strip()
        context, context_truncated = _failure_context(out)
        result = {"passed": returncode == 0,
                  "returncode": returncode,
                  "output": out[-OUTPUT_TAIL_CHARS:],
                  "output_truncated": len(out) > OUTPUT_TAIL_CHARS,
                  "failure_context": context,
                  "failure_context_truncated": context_truncated,
                  "unmeasured_declaration": _unmeasured_declaration(out)}
        # Scan the complete capture while it exists. Prose diagnostics never
        # become case identities; only the gate's explicit protocol does.
        cases, error = _repo_gate_failure_cases({"output": out})
        if len(cases) > 20:
            cases, error = [], "repository gate emitted more than 20 failed cases; read output_ref"
        result["captured_failure_cases"] = {"cases": cases, "error": error}
        directory = (env_overrides or {}).get("GATE_REPORT_DIR")
        if directory:
            try:
                raw = ((stdout or "") + "\n" + (stderr or "")).encode("utf-8")
                path = Path(directory) / "command-output.txt"
                with path.open("xb") as stream:
                    stream.write(raw)
                result["output_ref"] = {"path": str(path),
                                        "sha256": hashlib.sha256(raw).hexdigest(),
                                        "format": "stdout + newline + stderr"}
            except OSError as e:
                result["output_retention_error"] = str(e)
        return result
    except subprocess.TimeoutExpired:
        return {"passed": False, "returncode": -1, "timed_out": True,
                "output": f"timed out after {timeout}s: {' '.join(args)}"}
    except repository_executor.IsolationUnavailable as e:
        return {"passed": False, "returncode": -1, "runner_error": True,
                "isolation_unavailable": True, "output": f"{type(e).__name__}: {e}"}
    except Exception as e:
        return {"passed": False, "returncode": -1, "runner_error": True,
                "output": f"{type(e).__name__}: {e}"}


# The node gate's command budgets, unchanged from the local runner they replace.
NODE_INSTALL_TIMEOUT = 600
NODE_SCRIPT_TIMEOUT = 300
# The snapshot the node gate runs on. Over either cap the gate is unmeasured.
SNAPSHOT_MAX_ENTRIES = 50_000
SNAPSHOT_MAX_BYTES = 1 << 30
SNAPSHOT_EXCLUDED = (".git", "node_modules")
GIT_META_MAX_BYTES = 256 << 20

SNAPSHOT_CHUNK = 1 << 20     # the backend never holds more than this of one file


class SnapshotRefused(Exception):
    """The working tree cannot be snapshotted as a supported input."""


def _snapshot_tree(repo: Path, dest: Path) -> dict:
    """Copy ``repo``'s current working tree into ``dest`` without following links.

    Every directory and file is opened relative to its parent's fd with
    O_NOFOLLOW, so nothing the repository names can redirect a read outside
    it. ``.git`` and ``node_modules`` are not copied (no gitdir pointer or
    object store reaches a container; the install is clean). A link is copied
    as a link when it stays inside the repository and refused otherwise; a
    hard-linked file is refused; FIFOs/sockets/devices are skipped and
    counted. Returns the identity: report evidence only, never an
    authorization.
    """
    state = {"entries": 0, "bytes": 0, "skipped_special": 0}
    digest = hashlib.sha256()
    nofollow = os.O_RDONLY | os.O_NOFOLLOW

    def walk(fd: int, rel: str) -> None:
        with os.scandir(fd) as it:
            names = sorted(e.name for e in it)
        for name in names:
            if name in SNAPSHOT_EXCLUDED:
                continue
            path = f"{rel}/{name}" if rel else name
            state["entries"] += 1
            if state["entries"] > SNAPSHOT_MAX_ENTRIES:
                raise SnapshotRefused(f"more than {SNAPSHOT_MAX_ENTRIES} entries")
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            target = dest / path
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(name, dir_fd=fd)
                inside = os.path.normpath(os.path.join(os.path.dirname(path), link))
                if os.path.isabs(link) or inside == ".." or inside.startswith("../"):
                    raise SnapshotRefused(f"link {path} points outside the repository; "
                                          "unsupported input")
                os.symlink(link, target)
                digest.update(b"L\0" + path.encode() + b"\0" + link.encode() + b"\0")
            elif stat.S_ISDIR(info.st_mode):
                child = os.open(name, nofollow | os.O_DIRECTORY, dir_fd=fd)
                try:
                    target.mkdir(mode=0o700)
                    digest.update(b"D\0" + path.encode() + b"\0")
                    walk(child, path)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    raise SnapshotRefused(f"{path} is hard-linked; unsupported input")
                if state["bytes"] + info.st_size > SNAPSHOT_MAX_BYTES:
                    raise SnapshotRefused(f"more than {SNAPSHOT_MAX_BYTES} bytes")
                f = os.open(name, nofollow | os.O_NONBLOCK, dir_fd=fd)
                try:
                    if not stat.S_ISREG(os.fstat(f).st_mode):
                        raise SnapshotRefused(f"{path} changed type while copied")
                    out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                  0o700 if info.st_mode & 0o111 else 0o600)
                    content = hashlib.sha256()
                    try:
                        # Streamed: every chunk is charged as it is read, so a
                        # file that grows while it is copied is still bounded.
                        while chunk := os.read(f, SNAPSHOT_CHUNK):
                            state["bytes"] += len(chunk)
                            if state["bytes"] > SNAPSHOT_MAX_BYTES:
                                raise SnapshotRefused(f"more than {SNAPSHOT_MAX_BYTES} bytes")
                            content.update(chunk)
                            view = memoryview(chunk)
                            while view:
                                view = view[os.write(out, view):]
                    finally:
                        os.close(out)
                finally:
                    os.close(f)
                digest.update(b"F\0" + path.encode() + b"\0" + oct(info.st_mode & 0o777).encode()
                              + b"\0" + content.digest())
            else:
                state["skipped_special"] += 1

    dest.mkdir(mode=0o700)
    root = os.open(repo, nofollow | os.O_DIRECTORY)
    try:
        walk(root, "")
    finally:
        os.close(root)
    return {**state, "tree_sha256": digest.hexdigest(), "excluded": list(SNAPSHOT_EXCLUDED)}


def _snapshot_git(repo: Path, src: Path) -> dict:
    """Self-contained, shallow Git metadata of the source HEAD inside ``src``.

    Builds that stamp a version (`git rev-parse`, `describe --always`) keep
    working without any source `.git` pointer, common dir, hooks, config,
    remotes or credentials reaching a container: a fresh repository with no
    template, the one HEAD commit fetched by depth 1 and no tags, an index of
    that commit over the captured working tree, FETCH_HEAD removed. `dirty`
    is that index compared with the captured files, computed in the fresh
    repository (whose config is ours). Any failure leaves no metadata and says
    why; it never fails the gate.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(src.parent),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
           # A tree without its own .git must not borrow an enclosing repository's HEAD.
           "GIT_CEILING_DIRECTORIES": str(repo.parent)}

    def git(*args, cwd=src):
        return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=cwd, env=env,
                              capture_output=True, text=True, timeout=120)

    meta = src / ".git"
    try:
        head = git("rev-parse", "--verify", "-q", "HEAD^{commit}", cwd=repo)
        if head.returncode:
            return {"provided": False, "reason": "source has no HEAD commit"}
        sha = head.stdout.strip()
        for step in (("init", "-q", "--template=", "--initial-branch=snapshot", str(src)),
                     ("-c", "core.logAllRefUpdates=false", "fetch", "-q", "--no-tags",
                      "--depth=1", "--no-recurse-submodules", f"file://{repo}", sha),
                     ("-c", "core.logAllRefUpdates=false", "update-ref",
                      "refs/heads/snapshot", sha),
                     ("read-tree", sha)):
            done = git(*step)
            if done.returncode:
                raise RuntimeError(f"git {step[0] if step[0] != '-c' else step[2]} failed: "
                                   f"{done.stderr.strip()[-300:]}")
        (meta / "FETCH_HEAD").unlink(missing_ok=True)
        size = sum(p.stat().st_size for p in meta.rglob("*") if p.is_file() and not p.is_symlink())
        if size > GIT_META_MAX_BYTES:
            raise RuntimeError(f"shallow metadata is {size} bytes (> {GIT_META_MAX_BYTES})")
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=normal")
        if status.returncode:
            raise RuntimeError(f"git status failed: {status.stderr.strip()[-300:]}")
        changed = [e for e in status.stdout.split("\0") if e]
        return {"provided": True, "head": sha, "shallow": True, "tags": False,
                "dirty": bool(changed), "changed_entries": len(changed),
                "metadata_bytes": size}
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        if meta.is_dir() and not meta.is_symlink():
            shutil.rmtree(meta, ignore_errors=True)
        return {"provided": False, "reason": str(e)[:500]}


def _write_new(path: Path, data: bytes) -> None:
    """Create ``path`` (never through a link, never over an existing file)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def _remove_owned(ticket: Path) -> None:
    """Remove this gate's own ticket once its containers have exited.

    A run may leave directories without owner write/search permission; each
    real directory (lstat, never a link) is given 0700 before the removal.
    rmtree does not follow links.
    """
    for dirpath, dirnames, _ in os.walk(ticket):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            try:
                if stat.S_ISDIR(os.lstat(path).st_mode):
                    os.chmod(path, 0o700)
            except OSError:
                pass
    shutil.rmtree(ticket, ignore_errors=True)


def _run_node_checks(repo: Path, *, run_id: str = "") -> dict | None:
    """npm install/build/test gate for the repo's node project (if any).

    Mirrors the pytest gate's skip semantics: no node project → None (no
    section in the report); no measurement possible → skipped=True with the
    reason (an absence must never masquerade as passing or failing tests).
    Otherwise install deps, then run the build and test scripts that
    package.json actually declares — the BUILD is what catches compile-level
    breakage (e.g. Svelte template errors) that unit tests alone never see.

    Nothing of the repository executes in this process. Every npm command runs
    in the bounded CPU container on a snapshot under the gate's own ticket
    ``gate-reports/<sha256(run_id)>/node-*/`` (``src/`` + ``cache/``), so
    the candidate is never written (no node_modules, no lockfile) and a build
    script sees the ticket and nothing else:

    1. ``node_fetch`` (only when there are dependencies): the pinned npm, with
       the operator's network, in the snapshot itself — so that container
       holds the full private snapshot — and with no lifecycle script, git or
       URL dependency (`repository_executor.NODE_FETCH_FLAGS`), fills the
       cache. A fetch that fails is the install check's measured failure; a
       lockfile it writes is reported as ``generated_lock``.
    2. ``node_run`` with ``--network none``: the same ``ci``/``install`` as
       before plus ``--offline`` and every dependency script allowed (npm 9's
       behaviour), then build and test. Both phases read the gate's own npm
       user config (``install-links=true``, so a lock npm 9 wrote for a
       ``file:`` directory installs unchanged) and the ticket's npm cache: the
       backend HOME is not writable by the running uid (coding_impl f3560860,
       rc 243).
    """
    pkg_dir = _find_node_project(repo)
    if pkg_dir is None:
        return None
    rel = pkg_dir.relative_to(repo)
    node: dict = {"passed": True, "dir": str(rel) or ".", "checks": {}}

    def unmeasured(reason: str) -> dict:
        node.update(passed=True, skipped=True, unmeasured_reason=reason,
                    summary=f"node gate not measured: {reason}")
        return node

    root = repository_executor.node_report_root(datadir.aitelier_home(), run_id)
    try:
        root.mkdir(parents=True, exist_ok=True)
        ticket = Path(tempfile.mkdtemp(prefix=repository_executor.NODE_TICKET_PREFIX, dir=root))
    except OSError as e:
        return unmeasured(f"cannot create the run-owned node ticket under {root}: {e}")
    try:
        src, cache = ticket / "src", ticket / "cache"
        cache.mkdir(mode=0o700)
        try:
            identity = _snapshot_tree(repo, src)
        except (SnapshotRefused, OSError) as e:
            return unmeasured(f"repository snapshot refused: {e}")
        identity["git"] = _snapshot_git(repo, src)
        identity["source"] = str(repo)
        node["snapshot"] = identity
        workdir = src / rel
        rel_posix = rel.as_posix()               # "." reads as the snapshot root
        read = repository_executor.read_regular
        try:
            manifest = json.loads(read(src, f"{rel_posix}/package.json",
                                       repository_executor.NODE_MANIFEST_CAP) or b"{}")
            has_lock = any(read(src, f"{rel_posix}/{name}", repository_executor.NODE_LOCK_CAP)
                           is not None for name in repository_executor.NODE_LOCKFILES)
        except (repository_executor.NodeRefused, ValueError) as e:
            return unmeasured(f"package.json/lockfile not readable as plain files: {e}")
        _write_new(ticket / repository_executor.NODE_USERCONFIG,
                   repository_executor.NODE_USERCONFIG_TEXT.encode())
        scripts = manifest.get("scripts", {}) if isinstance(manifest, dict) else {}
        scripts = scripts if isinstance(scripts, dict) else {}
        install = "ci" if has_lock else "install"
        node["isolation"] = {"fetch": "skipped", "run_network": "none"}

        def run(label, args, timeout, profile, where):
            result = _run_node_cmd(repo, args, timeout, run_id=run_id,
                                   node={"ticket": ticket, "workdir": where,
                                         "npm_cache": cache, "profile": profile})
            result["phase"] = label
            return result

        # npm installs peers and workspace members' dependencies too.
        if has_lock or isinstance(manifest, dict) and any(manifest.get(f) for f in (
                "dependencies", "devDependencies", "optionalDependencies",
                "peerDependencies", "workspaces")):
            fetch = run("fetch", ["npm", install], NODE_INSTALL_TIMEOUT, "node_fetch", workdir)
            if fetch.get("isolation_unavailable"):
                node["isolation"]["fetch"] = "refused"
                node["checks"]["install"] = fetch
                return unmeasured(fetch["output"])
            node["isolation"]["fetch"] = "operator_network"
            if not fetch["passed"]:
                node["checks"]["install"] = fetch
                node["passed"] = False
                return node
            if not has_lock:
                # Written in place by the trusted fetch; the offline install uses it.
                data = read(workdir, "package-lock.json", repository_executor.NODE_LOCK_CAP)
                if data is not None:
                    node["generated_lock"] = {"path": "package-lock.json", "by": "node_fetch",
                                              "sha256": hashlib.sha256(data).hexdigest()}
        node["checks"]["install"] = run("install", ["npm", install, "--offline"],
                                        NODE_INSTALL_TIMEOUT, "node_run", workdir)
        if node["checks"]["install"].get("isolation_unavailable"):
            return unmeasured(node["checks"]["install"]["output"])
        if node["checks"]["install"]["passed"]:
            for name, args in (("build", ["npm", "run", "build"]), ("test", ["npm", "test"])):
                if name in scripts:
                    node["checks"][name] = run(name, args, NODE_SCRIPT_TIMEOUT, "node_run", workdir)
                    if node["checks"][name].get("isolation_unavailable"):
                        return unmeasured(node["checks"][name]["output"])
    except (OSError, repository_executor.NodeRefused) as e:
        return unmeasured(f"node ticket could not be prepared: {type(e).__name__}: {e}")
    finally:
        _remove_owned(ticket)

    node["passed"] = all(c["passed"] for c in node["checks"].values())
    return node


REPO_GATE_SCRIPT = "run_tests.sh"
# Measured 2026-09-10 on the wuxia baseline, one clean exclusive run:
# compile 11.5s + playtest 1995s = 2007s for 172 scenarios -- and that
# run HARD-failed early enough to skip the L0 control block entirely, so
# its 104 control runs (72,685 headless frames) were never paid. 2007s is
# therefore a FLOOR for a clean tree, not the figure. 900 was a
# placeholder and would have failed every gate as a timeout, which is the
# worst kind of red: one that has nothing to do with the code.
REPO_GATE_TIMEOUT = 5400


# inotify(7) constants (linux/inotify.h).
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_ISDIR = 0x40000000
_INOTIFY_EVENT = struct.Struct("iIII")


def _errno_text(call: str, number: int) -> str:
    return f"{call} failed (errno {number}: {os.strerror(number)})"


class _FirstEntry:
    """The first entry created directly inside one directory, in kernel order.

    This is how a gate run's report is told from every other report under
    its ticket. The ticket directory is new and empty when the gate starts.
    The gate protocol REQUIRES the gate to create its own report directory
    under GATE_REPORT_DIR before it creates anything else there
    (`docs/repo-gate-unmeasured-protocol.md`, "Which report is the gate's").
    The kernel reports creations in the order they happened (inotify), so
    this records which entry came first; `_retained_findings` decides from it
    whether the gate's report can be attributed at all.

    `read()` is None when the order could not be observed, and `failure` then
    says why (the failing call and its errno); otherwise
    `{"name": <entry or None>, "is_dir": bool}`.
    """

    def __init__(self, directory: Path):
        self._fd = None
        self.failure: str | None = None
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
            if fd < 0:
                self.failure = _errno_text("inotify_init1", ctypes.get_errno())
                return
            if libc.inotify_add_watch(fd, os.fsencode(str(directory)),
                                      _IN_CREATE | _IN_MOVED_TO) < 0:
                self.failure = _errno_text("inotify_add_watch",
                                           ctypes.get_errno())
                os.close(fd)
                return
            self._fd = fd
        except (OSError, AttributeError) as e:
            self._fd = None
            self.failure = f"inotify unavailable ({type(e).__name__}: {e})"

    def read(self) -> dict | None:
        if self._fd is None:
            return None
        data = b""
        try:
            while True:
                chunk = os.read(self._fd, 65536)
                if not chunk:
                    break
                data += chunk
        except BlockingIOError:
            pass
        except OSError as e:
            self.failure = f"reading the inotify events failed ({e})"
            return None
        offset = 0
        while offset + _INOTIFY_EVENT.size <= len(data):
            _wd, mask, _cookie, length = _INOTIFY_EVENT.unpack_from(data, offset)
            offset += _INOTIFY_EVENT.size
            name = data[offset:offset + length].split(b"\0", 1)[0]
            offset += length
            if name and mask & (_IN_CREATE | _IN_MOVED_TO):
                return {"name": os.fsdecode(name), "is_dir": bool(mask & _IN_ISDIR)}
        return {"name": None, "is_dir": False}

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def _run_repo_gate(repo: Path, *, run_id: str = "") -> dict | None:
    """The repo's OWN gate script, when it declares one.

    pytest is not every repo's gate. A Godot game, a Rust workspace, anything
    whose product is not Python can still carry a Python test suite — so this
    tool goes green while the product does not compile. Measured 2026-09-10 on
    the wuxia game: a branch that regressed the engine from "354 of 354 scripts
    parsed" to 9 parse errors (a dead AudioManager autoload, GameManager failing
    to load with it) reported ``1788 passed, 0 failed`` here, because nothing in
    this tool ever asked the engine anything. The Python suite it did run makes
    source-TEXT assertions about GDScript; text cannot see a parse error.

    A repo that ships an executable ``run_tests.sh`` at its root has DECLARED
    its gate. Run it and fold the verdict in, exactly as the node gate does.
    No script -> None, and behaviour is byte-for-byte what it was before.

    Reuses `_run_node_cmd`: the name says npm, the body is a generic
    "run this argv in that directory, scrubbed env, kill the process group on
    timeout" — which is what is wanted here too.
    """
    script = repo / REPO_GATE_SCRIPT
    if not script.is_file() or not os.access(script, os.X_OK):
        return None
    # Every run of the gate gets a TICKET: a directory of its own under
    # gate-reports/, handed to the gate as GATE_REPORT_DIR. A gate that retains
    # structured reports writes them there, and failure identities are read
    # from them (`_report_dir_failure_cases`), never from the bounded tail.
    ticket = "rt-%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                           os.urandom(4).hex())
    report_dir = (datadir.aitelier_home() / "gate-reports" /
                  hashlib.sha256(run_id.encode()).hexdigest() / ticket)
    # Every engine request the gate makes goes through the admission relay,
    # which queues render requests in the harness's render queue and records
    # the engine's own answer to each (aitelier/gate_admission.py).
    upstream = os.environ.get("GODOT_BUILDER_URL") or gate_admission.DEFAULT_BUILDER_URL
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        relay = gate_admission.AdmissionRelay(
            upstream, render_wait_sec=gate_admission.render_wait_seconds(),
            upstream_timeout=REPO_GATE_TIMEOUT)
        relay_path = gate_admission.relay_socket_path(datadir.aitelier_home(), run_id, ticket)
        gate_admission.relay_socket_namespace(datadir.aitelier_home(), create=True)
        relay_socket = str(relay_path)
        relay_url = relay.start(unix_socket=relay_socket)
    except (OSError, ValueError) as e:
        return {"passed": False, "returncode": -1, "runner_error": True,
                "script": REPO_GATE_SCRIPT, "ticket": ticket,
                "output": f"repository gate not started: admission relay "
                          f"unavailable ({type(e).__name__}: {e})",
                "measured": REPO_GATE_UNMEASURED}
    # Watched from before the gate starts: which entry the gate run created
    # first under its ticket (`_FirstEntry`).
    first_entry = _FirstEntry(report_dir)
    try:
        result = _run_node_cmd(
            repo, ["bash", str(script)], REPO_GATE_TIMEOUT,
            env_overrides={"GODOT_BUILDER_URL": relay_url,
                           "GATE_REPORT_DIR": str(report_dir),
                           "AITELIER_GATE_RELAY_SOCKET": relay_socket}, run_id=run_id)
    finally:
        relay.stop()  # removes only the socket inode this relay actually bound
        first = first_entry.read()
        first_entry.close()
    admission = gate_admission.admission_summary(relay.snapshot())
    admission.update(ticket=ticket, upstream=upstream)
    try:
        (report_dir / "admission.json").write_text(
            json.dumps(admission, indent=2), encoding="utf-8")
    except OSError:
        pass
    result["script"] = REPO_GATE_SCRIPT
    result["ticket"] = ticket
    result["report_dir"] = str(report_dir)
    result["repo"] = str(repo)
    result["admission"] = admission
    # Which report under the ticket is this gate run's (`_retained_findings`):
    # `state` is `own` when the gate's report was identified, and
    # `unattributable` otherwise, with `why`.
    retained = _retained_findings(report_dir, repo, first)
    attribution = {"state": retained.attribution,
                   "observed": first is not None}
    if first is not None:
        attribution.update(first_entry=first["name"],
                           first_is_dir=first["is_dir"])
    if retained.why:
        attribution["why"] = (f"{retained.why}: {first_entry.failure}"
                              if first is None and first_entry.failure
                              else retained.why)
    if retained.reports:
        attribution["reports"] = retained.reports
    result["report_attribution"] = attribution
    # Read coverage from the attributed structured report, never the bounded
    # log tail. A legacy/missing marker cannot silently become a full gate.
    coverage, coverage_error = _retained_coverage(retained)
    result["gate_coverage"] = coverage
    if coverage_error:
        result["gate_coverage_error"] = coverage_error
    # How many failures the gate's own retained report names (None when its
    # report was not identified). Read before the outcome: a finding makes
    # the run `measured_fail` whatever else it reports. A part of the ticket
    # that cannot be read, or a second report for this repository, is
    # `retained_error`; it never lowers the count.
    result["retained_findings"] = (len(retained.findings) if retained.owned
                                   else None)
    # Every red under a ticket whose gate report could not be attributed,
    # each with the file it was read from.
    if retained.attribution == ATTRIBUTION_UNATTRIBUTABLE and retained.unattributed:
        result["unattributed_reds"] = retained.unattributed
    if retained.error:
        result["retained_error"] = retained.error
    # ONE word for what this run of the gate is WORTH: `measured_pass`,
    # `measured_fail` or `unmeasured`. It is read by the fold in `run_tests`,
    # re-read by `_acquire_repo_gate`, and lands in every report a reviewer
    # sees. It is never the exit code alone — see `_repo_gate_outcome`.
    result["measured"] = _repo_gate_outcome(result)
    return result


BASELINE_FILE = "run_tests_baseline.json"

# What the baseline reading is WORTH on this run.
#
# `seeded` / `compared` are measurements: a baseline file was taken or read, so
# an empty `baseline_failures` means "this repo carried no standing red".
# `unavailable` / `unreadable` are the ABSENCE of a measurement, and an empty
# `baseline_failures` beside them means only "nobody looked". The two used to be
# the same three fields with the same values — which is how `state_dir` being
# unset deployment-wide (no config declared `capability: stateful` until
# 2026-09-20) produced reports that read like a clean baseline for months.
BASELINE_MEASURED = ("seeded", "compared")

_FAILED_RE = re.compile(r"^(?:.*\s)?FAILED\s+(\S+)")
_ERROR_RE = re.compile(r"^ERROR\s+(\S+)")

_GATE_RE = re.compile(r"^((?:node|repo_gate):\S+)")
_REPO_GATE_CASE_PREFIX = "AITELIER_REPO_GATE_CASE="
_REPO_GATE_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$")

# ── UNMEASURED: three sources, and a retained finding overrides all three ──
#
# Source 1 is the repository's own statement that it did not run, one whole
# line of its output:
#
#   AITELIER_REPO_GATE_UNMEASURED={"state":"blocked","reason":"..."}
#
# `state` must be one of `_REPO_GATE_UNMEASURED_STATES`. The other two
# sources are the harness killing or failing to start the gate, and the
# admission relay's record that the engine did not answer the gate's last
# request together with an exit code other than 0 and 1 (`_repo_gate_outcome`).
# An exit code alone is never a source. `2` is NOT "the engine never ran": the
# game repo's own gate uses it for `incomplete`, which includes contract files
# the implementer wrote wrong ("no authored scenarios found ... an empty one is
# not a pass"), and those are MEASURED failures that have to stay visible with
# a non-empty failures[]. And a gate whose retained report names any failure
# measured that failure, so none of the three sources applies to it.
_REPO_GATE_UNMEASURED_PREFIX = "AITELIER_REPO_GATE_UNMEASURED="
_REPO_GATE_UNMEASURED_STATES = frozenset({"unmeasured", "not_run", "blocked"})

REPO_GATE_MEASURED_PASS = "measured_pass"
REPO_GATE_MEASURED_FAIL = "measured_fail"
REPO_GATE_UNMEASURED = "unmeasured"
# The ticket holds a red and the gate's own report could not be identified,
# so nobody can say whose red it is (`_retained_findings`).
REPO_GATE_UNATTRIBUTABLE = "unattributable"

# What a report under the ticket is, as `_retained_findings` classifies it:
# the gate's own, provably another writer's, or neither provable.
ATTRIBUTION_OWN = "own"
ATTRIBUTION_NOT_OWN = "not_own"
ATTRIBUTION_UNATTRIBUTABLE = "unattributable"

# How many times ONE step invocation may run the repository gate.
#
# This is 1, and the number is a BUDGET rather than a tuning knob. Round 5
# bought the first contention's recovery by re-running the gate in-THIS-step
# (3 runs, REPO_GATE_RETRY_DELAY_SECONDS apart). Measured cost of that trade:
# the step's worst-case hold went 5400 s -> 3 x 5400 + 2 x 60 = 16320 s, and a
# single step held the scheduler for all of it. The repair is to stop paying
# for the wait HERE: an absence is parked by the SCHEDULER instead
# (`core/gate_deferral.py`), which lets the poller go on serving every other
# project while this run waits, and the episode's own ceiling is what ends it.
# So one step makes one gate call, and its worst case is the gate's own
# timeout (`REPO_GATE_TIMEOUT`), which is what the previous main line paid.
# Both numbers are asserted in
# tests/unit/test_gate_deferral_execution_points.py::test_one_step_holds_the_\
# scheduler_for_at_most_one_gate_run.
REPO_GATE_UNMEASURED_ATTEMPTS = 1
# Kept for the re-acquisition path that remains reachable (`attempts` still
# reports how many runs the reading cost, and the loop honors this constant
# when a deployment raises it deliberately). Not read on the default path.
REPO_GATE_RETRY_DELAY_SECONDS = 60



def _unmeasured_declaration(text: str) -> dict | None:
    """The gate's OWN statement that it did not measure, or None.

    A declaration is a whole line that STARTS with the prefix — a log echo of
    the prefix in the middle of a line is not a record — carrying a JSON
    object whose `state` is one of `_REPO_GATE_UNMEASURED_STATES`.
    `state: "failed"` is not one of them: a gate that failed measured its
    subject, and a measured failure is not an absence.
    """
    for line in str(text).splitlines():
        if not line.startswith(_REPO_GATE_UNMEASURED_PREFIX):
            continue
        raw = line[len(_REPO_GATE_UNMEASURED_PREFIX):]
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        state = str(value.get("state") or "").strip().lower()
        if state not in _REPO_GATE_UNMEASURED_STATES:
            continue
        return {"state": state,
                "reason": str(value.get("reason") or "")[:500]}
    return None


def _repo_gate_declares_unmeasured(gate: dict) -> bool:
    """Did this run of the repo gate declare that it did not measure?"""
    if gate.get("unmeasured_declaration") is not None:
        return True
    if gate.get("output_truncated"):
        # Only a fragment was retained and it was not scanned whole, so
        # nothing in it is a record: the declaration could have been cut out of
        # the middle, and half a line still looks like a line.
        return False
    return _unmeasured_declaration(str(gate.get("output", ""))) is not None


def _repo_gate_outcome(gate: dict) -> str:
    """`measured_pass`, `measured_fail`, `unmeasured` or `unattributable`,
    for ONE gate run.

    A ticket whose gate report could not be attributed and that holds any red
    (`unattributed_reds`) is `unattributable`, whatever else the run says.
    With no red there, the rules below apply as they do to any gate.

    The gate's retained report is read next: when it names any failure
    (`retained_findings` > 0, counted by `_run_repo_gate` from the report the
    gate kept under its ticket), the run is `measured_fail`, whatever the exit
    code, the relay record or a declaration say. A gate that measured a red
    and was then refused on a later request still measured that red.

    Otherwise UNMEASURED has three sources: the gate's own declaration, a run
    this harness killed or could not start (`timed_out` / `runner_error`), and
    a gate that exited neither 0 nor 1 after the engine did not answer its last
    request — the admission relay's record of the engine's own reply
    (`admission.state` in `gate_admission.NOT_RUN_STATES`: refused admission,
    unreachable, or cut off before its answer was delivered). `returncode`
    alone is never a source: 0 is `measured_pass`, 1 is `measured_fail`
    whatever the relay saw, and any other code with no engine refusal behind
    it (a contract error, a crash) stays `measured_fail`. A timeout is the
    framework's own observation, exactly like the pytest wall whose
    `skipped_because="pytest_timeout"` this aligns with.
    """
    if gate.get("unattributed_reds"):
        return REPO_GATE_UNATTRIBUTABLE
    if gate.get("retained_findings"):
        return REPO_GATE_MEASURED_FAIL
    if gate.get("gate_coverage_error"):
        return REPO_GATE_UNMEASURED
    if _repo_gate_declares_unmeasured(gate):
        return REPO_GATE_UNMEASURED
    if gate.get("timed_out") is True or gate.get("runner_error") is True:
        return REPO_GATE_UNMEASURED
    returncode = gate.get("returncode")
    admission = gate.get("admission") or {}
    if (returncode not in (0, 1)
            and admission.get("state") in gate_admission.NOT_RUN_STATES):
        return REPO_GATE_UNMEASURED
    return (REPO_GATE_MEASURED_PASS if returncode == 0
            else REPO_GATE_MEASURED_FAIL)


def _retry_delay_seconds() -> float:
    """Seconds between re-acquisitions of an unmeasured gate.

    The env override exists so a drill can exercise the re-acquisition path
    without waiting out the production pause; it is read at CALL time, so the
    value is the one the process holds when the gate is actually re-run.
    """
    raw = os.environ.get("AITELIER_REPO_GATE_RETRY_DELAY_SECONDS")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return REPO_GATE_RETRY_DELAY_SECONDS


def _acquire_repo_gate(repo: Path, run_gate=None, sleep=None, *, run_id: str = "") -> dict | None:
    """Run the repo gate, RE-ACQUIRING the verdict while it is unmeasured.

    A gate that did not run has decided nothing, so it may not spend the
    loop's implement cycle and it may not end the run: the tool asks the gate
    again instead. Only the verdict it finally gets is folded in. `attempts`
    stays on the returned dict so a reviewer can see that the reading cost two
    runs rather than one.
    """
    run_gate = (lambda root: _run_repo_gate(root, run_id=run_id)) if run_gate is None else run_gate
    sleep = time.sleep if sleep is None else sleep
    gate = run_gate(repo)
    attempts = 1
    while (gate is not None
           and gate.get("measured") == REPO_GATE_UNMEASURED
           and attempts < REPO_GATE_UNMEASURED_ATTEMPTS):
        sleep(_retry_delay_seconds())
        attempts += 1
        gate = run_gate(repo)
    if gate is not None:
        gate["attempts"] = attempts
    return gate

def _finding_text(item) -> str:
    """One finding as text: a string as it is, a located error as file:line."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and item.get("file"):
        return f"{item.get('file')}:{item.get('line')}: {item.get('msg', '')}"
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as e:
        return None, f"repository gate report {path.name} is unreadable: {e}"


class _Retained(NamedTuple):
    """What a ticket holds for one repository (see `_retained_findings`)."""
    report_dir: Path
    findings: list
    error: str | None
    owned: bool
    attribution: str
    why: str | None
    unattributed: list
    reports: dict


def _retained_coverage(retained: _Retained) -> tuple[dict | None, str | None]:
    if not retained.owned:
        return None, "coverage unavailable: gate report was not attributed"
    manifest, error = _load_json(retained.report_dir / "manifest.json")
    if error or not isinstance(manifest, dict):
        return None, error or "coverage unavailable: manifest is not an object"
    coverage = manifest.get("gate_coverage")
    if manifest.get("purpose") == "provisional_round_feedback":
        if isinstance(coverage, dict):
            coverage = dict(coverage, purpose="provisional_round_feedback")
        return coverage, "provisional feedback is not full test evidence"
    if isinstance(coverage, dict) and coverage.get("purpose") == "provisional_round_feedback":
        return coverage, "provisional feedback is not full test evidence"
    error = validate_coverage(coverage)
    return coverage if isinstance(coverage, dict) else None, error


def _stage_findings(manifest_path: Path, manifest: dict, ticket_dir: Path
                    ) -> tuple[list, list[str]]:
    """Every red one manifest's stages name, and the parts that could not be read.

    Each stage contributes every entry of its `<stage>-findings.json`, and,
    when that list is empty or missing, the `errors[]` (else the `summary`) of
    its stage report when that report says `passed: false`. Each finding is
    `(stage, item, stage_report_path_or_None)`.
    """
    report_dir = manifest_path.parent
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        return [], [f"repository gate manifest "
                    f"{manifest_path.relative_to(ticket_dir)} names no stages"]
    findings: list = []
    errors: list[str] = []
    for stage, state in stages.items():
        name = (state or {}).get("report") if isinstance(state, dict) else None
        stage_path = report_dir / name if isinstance(name, str) and name else None
        items: list = []
        findings_path = report_dir / f"{stage}-findings.json"
        if findings_path.is_file():
            listed, error = _load_json(findings_path)
            if error:
                errors.append(error)
            elif not isinstance(listed, list):
                errors.append(f"repository gate {findings_path.name} "
                              f"is not a list")
            else:
                items = listed
        if not items and stage_path is not None:
            verdict, error = _stage_verdict(stage_path)
            if error:
                errors.append(error)
            elif verdict is not None and verdict.get("passed") is False:
                items = verdict.get("errors") or [
                    verdict.get("summary")
                    or f"{stage} reported passed=false"]
        findings.extend((stage, item, stage_path) for item in items)
    # Authored-contract validation can fail before the first stage and before
    # coverage exists. Its explicit manifest red is still a retained finding;
    # attribution is decided by the caller, exactly as for stage findings.
    if (not findings and manifest.get("status") == "failed"
            and manifest.get("outcome") == REPO_GATE_MEASURED_FAIL):
        findings.append(("manifest", "repository gate reported measured_fail "
                         "in its failed manifest", manifest_path))
    return findings, errors


def _retained_findings(ticket_dir: Path, repo: Path, first: dict | None
                       ) -> _Retained:
    """Every failure the gate's retained report for `repo` names, unjudged.

    The gate writes its report under the ticket directory it was handed as
    GATE_REPORT_DIR: one `manifest.json` whose `stages` name every stage it
    ran, a `<stage>.json` per stage, and a `<stage>-findings.json` list for the
    stages it judges itself (tools/godot_gate.py in the game repository).

    WHICH report is the gate's is decided by this gate run's own observation,
    never by what a report says about itself. `first` is the first entry the
    run created under its ticket (`_FirstEntry`; None when the order could not
    be observed). Every report under the ticket is one of three things:

      * `own`: the ticket's first entry is a directory, and a `manifest.json`
        inside it names `repo`. That is the gate's report.
      * `not_own`: the gate's report was identified, and this report is not
        it.
      * `unattributable`: everything else. The order was not observed, the
        first entry is not a directory, nothing was created, or the first
        entry holds no manifest that names `repo`. Then no report under the
        ticket is known to be the gate's, and none is known not to be.

    With the gate's report identified, reds only accumulate: each readable
    part of it contributes its own (`_stage_findings`). A part that cannot be
    read, a second report that names `repo`, or a manifest inside the gate's
    entry that names another repository adds to `error` and takes away
    nothing already read.

    With no report identified, `findings` is empty and `unattributed` lists
    every red anywhere under the ticket (every readable manifest's stages,
    and every non-empty `*-findings.json` no readable manifest covers), each
    as `"<path under the ticket>: <finding>"`. A red is neither dropped nor
    counted as the gate's.
    """
    reports: dict = {}
    if not ticket_dir.is_dir():
        return _Retained(ticket_dir, [], None, False,
                         ATTRIBUTION_UNATTRIBUTABLE,
                         "the ticket directory does not exist", [], reports)

    def names_repo(manifest) -> bool:
        claimed = manifest.get("repo") if isinstance(manifest, dict) else None
        if not isinstance(claimed, str) or not claimed:
            return False
        try:
            return Path(claimed).resolve() == Path(repo).resolve()
        except (OSError, RuntimeError):
            return False

    errors: list[str] = []
    manifests: list = []
    for path in sorted(p for p in ticket_dir.rglob("manifest.json") if p.is_file()):
        manifest, error = _load_json(path)
        if error:
            errors.append(error)
            continue
        manifests.append((path, manifest))
    naming_repo = [path for path, manifest in manifests if names_repo(manifest)]

    own_dir, why = None, None
    if first is None:
        why = "the order of creation under the ticket was not observed"
    elif first.get("name") is None:
        why = "the gate run created nothing under its ticket"
    elif not first.get("is_dir"):
        why = (f"the first entry under the ticket, {first['name']!r}, "
               f"is not a directory")
    else:
        candidate = ticket_dir / first["name"]
        if any(candidate in path.parents for path in naming_repo):
            own_dir = candidate
        else:
            why = (f"the first entry under the ticket, {first['name']!r}, "
                   f"holds no manifest.json that names {repo}")

    if own_dir is None:
        unattributed: list[str] = []
        covered = set()
        for path, manifest in manifests:
            reports[str(path.relative_to(ticket_dir))] = ATTRIBUTION_UNATTRIBUTABLE
            covered.add(path.parent)
            found, part_errors = _stage_findings(path, manifest, ticket_dir)
            errors += part_errors
            unattributed += [
                f"{(stage_path or path).relative_to(ticket_dir)} "
                f"[{stage}]: {_finding_text(item)}"
                for stage, item, stage_path in found]
        for path in sorted(p for p in ticket_dir.rglob("*-findings.json")
                           if p.is_file() and p.parent not in covered):
            listed, error = _load_json(path)
            if error:
                errors.append(error)
            elif isinstance(listed, list):
                unattributed += [f"{path.relative_to(ticket_dir)}: "
                                 f"{_finding_text(item)}" for item in listed]
        return _Retained(ticket_dir, [], "; ".join(errors) or None, False,
                         ATTRIBUTION_UNATTRIBUTABLE, why, unattributed, reports)

    owned: list = []
    for path, manifest in manifests:
        key = str(path.relative_to(ticket_dir))
        if own_dir in path.parents:
            if names_repo(manifest):
                owned.append((path, manifest))
                reports[key] = ATTRIBUTION_OWN
            else:
                reports[key] = ATTRIBUTION_NOT_OWN
                errors.append(f"the gate's own report entry {own_dir.name} "
                              f"holds {key}, which names "
                              f"{manifest.get('repo')!r}, not {repo}")
        else:
            reports[key] = ATTRIBUTION_NOT_OWN
    if len(naming_repo) > 1:
        errors.append(f"repository gate retained {len(naming_repo)} reports "
                      f"for {repo} under one ticket ({ticket_dir.name}); only "
                      f"those in its first entry {own_dir.name} are the gate's")

    findings: list = []
    for manifest_path, manifest in owned:
        found, part_errors = _stage_findings(manifest_path, manifest, ticket_dir)
        findings += found
        errors += part_errors
    return _Retained(owned[0][0].parent, findings, "; ".join(errors) or None,
                     True, ATTRIBUTION_OWN, None, [], reports)


# A stage report up to this size is parsed whole. A play-test report carries
# every capture the sidecar took (measured 2026-09-14: 442 MB, 441 MB of it
# `captures`, 2.1 GB resident to parse), so above this only its trailing
# top-level members are parsed (`_stage_report_fields`), and only when they
# fit in STAGE_REPORT_TAIL_MAX (the same report's `behavior` was 630 KB).
STAGE_REPORT_FULL_PARSE_MAX = 64 * 1024 * 1024
STAGE_REPORT_TAIL_MAX = 64 * 1024 * 1024


def _stage_report_fields(path: Path) -> dict | None:
    """A stage report's fields, or None when they cannot be read.

    Above `STAGE_REPORT_FULL_PARSE_MAX` the report is parsed from its LAST
    `"behavior":` member to its end, wrapped in `{`. That text parses as a JSON
    object only when the member sits at the report's top level: inside a string
    the quote is escaped, and inside a nested object the suffix carries closing
    brackets the `{` does not open. So a parse that succeeds read the report's
    own `behavior` (and whatever top-level members follow it, `summary`
    included).
    """
    import mmap
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size <= STAGE_REPORT_FULL_PARSE_MAX:
        data, _error = _load_json(path)
        return data if isinstance(data, dict) else None
    try:
        with open(path, "rb") as fh, mmap.mmap(fh.fileno(), 0,
                                               access=mmap.ACCESS_READ) as m:
            at = m.rfind(b'"behavior":')
            if at <= 0 or size - at > STAGE_REPORT_TAIL_MAX:
                return None
            tail = m[at:]
        data = json.loads(b"{" + tail)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _leading_member(path: Path) -> tuple[str | None, object]:
    """The first top-level member of a JSON object file: `(key, value)`.

    Only the head of the file is read, so it costs the same for a 442 MB
    play-test report as for a small one. `(None, None)` when the file does not
    open with an object whose first member fits in that head.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return None, None
    decoder = json.JSONDecoder()
    at = len(head) - len(head.lstrip())
    if not head.startswith("{", at):
        return None, None
    try:
        at += 1
        at += len(head[at:]) - len(head[at:].lstrip())
        key, at = decoder.raw_decode(head, at)
        at += len(head[at:]) - len(head[at:].lstrip())
        if not head.startswith(":", at):
            return None, None
        at += 1
        at += len(head[at:]) - len(head[at:].lstrip())
        value, _end = decoder.raw_decode(head, at)
    except ValueError:
        return None, None
    return (key, value) if isinstance(key, str) else (None, None)


def _stage_verdict(path: Path) -> tuple[dict | None, str | None]:
    """A stage report's own `passed` (with `errors`/`summary` when it failed).

    Returns `(fields, error)`; `fields` is None when the report is not an
    object. A report above `STAGE_REPORT_FULL_PARSE_MAX` is not parsed whole:
    its `passed` is read from its leading member, or from its trailing members
    (`_stage_report_fields`), and a report whose `passed` is in neither place
    is an error, not a pass.
    """
    try:
        size = path.stat().st_size
    except OSError as e:
        return None, f"repository gate report {path.name} is unreadable: {e}"
    if size <= STAGE_REPORT_FULL_PARSE_MAX:
        data, error = _load_json(path)
        if error:
            return None, error
        return (data if isinstance(data, dict) else None), None
    key, value = _leading_member(path)
    if key == "passed" and value is not False:
        return {"passed": value}, None
    tail = _stage_report_fields(path) or {}
    if key == "passed":
        return {**tail, "passed": value}, None
    if "passed" in tail:
        return tail, None
    return None, (f"repository gate report {path.name} ({size} bytes) states "
                  f"no readable `passed`")


def _id_part(value) -> str:
    """One component of a case id: no whitespace, no `/`, same text -> same part."""
    return urllib.parse.quote(str(value), safe="._-")


def _assert_identities(stage: str, report: dict) -> dict:
    """`{finding prefix: [case id, ...]}` for every failing assertion row.

    A failing row is written by the gate as
    `"  <scenario> / <name>: <expr> -> actual <actual>; observed <observed>"`;
    everything after `-> actual ` is the value the run observed, so the prefix
    before it, built here from the row's own fields, is what a finding is
    matched on. The id is scenario, assertion name and frame, all three read
    from the row: the same assertion evaluated at two frames is two ids, and
    the observed value is in none of them. Rows are listed in report order,
    which is the order the gate wrote their findings in.
    """
    out: dict = {}
    behavior = report.get("behavior") if isinstance(report, dict) else None
    for scenario in (behavior or {}).get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        for row in scenario.get("asserts") or []:
            if not isinstance(row, dict):
                continue
            if row.get("passed") is True and not row.get("error"):
                continue
            prefix = "  %s / %s: %s -> actual " % (
                scenario.get("name"), row.get("name"), row.get("expr"))
            case_id = "%s/%s/%s@%s" % (stage, _id_part(scenario.get("name")),
                                       _id_part(row.get("name")),
                                       _id_part(row.get("frame")))
            out.setdefault(prefix, []).append(case_id)
    return out


def _repeatability_identity(stage: str, text: str) -> str | None:
    """`repeatability <scenario>: <row> != <row>` -> scenario, name and frame.

    The two rows are the JSON the gate embedded; the first one's `name` and
    `frame` identify the assertion, and neither observed value is used.
    """
    head = "repeatability "
    if not text.startswith(head):
        return None
    split = text.find(": {", len(head))
    if split < 0:
        return None
    try:
        row, end = json.JSONDecoder().raw_decode(text, split + 2)
    except ValueError:
        return None
    if not isinstance(row, dict) or not text.startswith(" != ", end):
        return None
    return "%s/repeatability/%s/%s@%s" % (
        stage, _id_part(text[len(head):split]), _id_part(row.get("name")),
        _id_part(row.get("frame")))


def _finding_identities(findings) -> list[dict]:
    """One failure record per finding, with an id that does not change between runs.

    In order of preference, a finding's id is built from:
      * the failing assertion row it reports (`_assert_identities`),
      * the assertion row a repeatability mismatch embeds,
      * its text with the stage report's own `summary` cut off the end,
      * its whole text.
    The last two are sha256 prefixes. Two findings that end up with the same
    id are both kept, the second as `<id>~2`, so no finding is dropped.
    """
    reports: dict = {}
    pending: dict = {}
    records: list[dict] = []
    counts: dict = {}
    for stage, item, stage_path in findings:
        text = _finding_text(item)
        case_id = None
        if isinstance(item, str) and stage_path is not None:
            if stage_path not in reports:
                reports[stage_path] = _stage_report_fields(stage_path) or {}
                pending[stage_path] = _assert_identities(stage, reports[stage_path])
            if " -> actual " in text:
                prefix = text[:text.index(" -> actual ") + len(" -> actual ")]
                queue = pending[stage_path].get(prefix)
                if queue:
                    case_id = queue.pop(0)
            if case_id is None:
                case_id = _repeatability_identity(stage, text)
            summary = reports[stage_path].get("summary")
            if (case_id is None and isinstance(summary, str) and summary
                    and len(text) > len(summary) and text.endswith(summary)):
                stem = text[:-len(summary)]
                case_id = (f"{stage}/summary/"
                           f"{hashlib.sha256(stem.encode('utf-8')).hexdigest()[:12]}")
        if case_id is None:
            case_id = f"{stage}/{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"
        counts[case_id] = counts.get(case_id, 0) + 1
        if counts[case_id] > 1:
            case_id = f"{case_id}~{counts[case_id]}"
        records.append({"case_id": case_id, "status": "failed",
                        "detail": text[:1000]})
    return records


def _report_dir_failure_cases(ticket_dir: Path, repo: Path,
                              first: dict | None
                              ) -> tuple[list[dict], str | None] | None:
    """Failure identities from the structured report a gate retained, or None.

    Reads the findings of `_retained_findings` and names each one with
    `_finding_identities`. None means the gate's own report was not
    identified under the ticket and the caller falls back to the output
    records. An error comes back BESIDE the identities that were read, never
    instead of them.
    """
    found = _retained_findings(ticket_dir, repo, first)
    if not found.owned:
        return None
    cases = _finding_identities(found.findings) if found.findings else []
    error = found.error
    if found.owned and not found.findings and error is None:
        error = (f"repository gate report {found.report_dir.name} names no "
                 f"failure")
    return cases, error


def _gate_first_entry(gate: dict) -> dict | None:
    """The creation order `_run_repo_gate` observed, as `_retained_findings` takes it."""
    attribution = gate.get("report_attribution") or {}
    if not attribution.get("observed"):
        return None
    return {"name": attribution.get("first_entry"),
            "is_dir": bool(attribution.get("first_is_dir"))}


def _repo_gate_failure_cases(gate: dict) -> tuple[list[dict], str | None]:
    """Read trustworthy per-case identities from one failed repository gate.

    Gate prose is not an identity protocol.  A repository opts in by emitting
    one JSON record per failed case as::

        AITELIER_REPO_GATE_CASE={"case_id":"compile/autoload","status":"failed","detail":"..."}

    A gate that retains a structured report for its repository under its
    ticket (`gate["report_dir"]`, `gate["repo"]`, see
    `_report_dir_failure_cases`) is read from that report and nothing else,
    whatever the length of its output.

    Otherwise use explicit case records scanned from the complete capture.
    Legacy tail-only results are unusable when truncated. One malformed or
    duplicate record invalidates the set; diagnostic prose never supplies ids.
    """
    if gate.get("report_dir") and gate.get("repo"):
        from_report = _report_dir_failure_cases(Path(gate["report_dir"]),
                                                Path(gate["repo"]),
                                                _gate_first_entry(gate))
        if from_report is not None:
            return from_report
    captured = gate.get("captured_failure_cases")
    if captured is not None:
        return captured["cases"], captured["error"]
    if gate.get("output_truncated"):
        return [], "repository gate output was truncated"
    records: list[dict] = []
    seen: set[str] = set()
    for line in str(gate.get("output", "")).splitlines():
        if not line.startswith(_REPO_GATE_CASE_PREFIX):
            continue
        raw = line[len(_REPO_GATE_CASE_PREFIX):]
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return [], "repository gate emitted malformed case JSON"
        if not isinstance(value, dict):
            return [], "repository gate case record must be an object"
        case_id = value.get("case_id")
        alias = value.get("id")
        if alias is not None and case_id is not None and alias != case_id:
            return [], "repository gate case identity is ambiguous"
        if case_id is None:
            case_id = alias
        if (not isinstance(case_id, str)
                or not _REPO_GATE_CASE_ID_RE.fullmatch(case_id)):
            return [], "repository gate case identity is missing or malformed"
        if case_id in seen:
            return [], f"repository gate case identity is duplicated: {case_id}"
        if value.get("status") != "failed":
            return [], f"repository gate case status is malformed: {case_id}"
        detail = value.get("detail", "")
        if not isinstance(detail, str):
            return [], f"repository gate case detail is malformed: {case_id}"
        seen.add(case_id)
        records.append({"case_id": case_id, "status": "failed",
                        "detail": detail[:1000]})
    if not records:
        return [], "repository gate did not emit per-case identities"
    return records, None


def _failure_key(line: str) -> str:
    """Identity of a failure, stripped of everything that varies run to run.

    A baseline can only be compared against if the two sides are the same
    string, and the raw `failures[]` entries are not: a FAILED line carries the
    assertion message, a collection ERROR carries the exception text, and the
    node / repo_gate entries embed a returncode plus the last 500-1500 bytes of
    the command's output. Keyed on the test nodeid / module / gate name instead,
    a test that keeps failing for a slightly different reason still counts as
    the SAME known-red failure — which is the point: the baseline exists to
    answer "did THIS round break something", not "is the wording identical".
    """
    line = line.strip()
    for rx in (_ERROR_RE, _GATE_RE, _FAILED_RE):
        m = rx.match(line)
        if m:
            return m.group(1)
    return line[:200]


class Executed:
    """What this run can PROVE it executed — the licence to SHRINK the baseline.

    Pruning a key means "that known-red test is green now, so a future red is
    the round's fault". The only evidence that licenses it is that the test RAN
    this time and did not fail. The rule this replaces inferred it from absence:
    `keep = known & set(keys)` kept a key only while it kept FAILING, so a
    known-red test that was never collected — a deleted module, a renamed
    nodeid, a collection error upstream of it, a `-k` narrowed run, a gate that
    did not run at all — was dropped from the baseline exactly as if it had been
    fixed. The next run then reported it as a NEW failure of the round.

    `node_ids` comes from pytest's own junit XML (every test it executed,
    passes included — the `-q` text only names failures). `repo_gate_cases` is
    None when the repo gate did not run, and the set of case ids it reported
    when it did. Everything unproven keeps its key: over-keeping costs one
    forgiven red, under-keeping blames the round for someone else's.
    """

    def __init__(self, node_ids=(), pytest_complete: bool = False,
                 repo_gate_cases=None, node_gate_ran: bool = False):
        self.node_ids = set(node_ids)
        self.pytest_complete = bool(pytest_complete)
        self.repo_gate_cases = repo_gate_cases
        self.node_gate_ran = bool(node_gate_ran)

    def ran(self, key: str) -> bool:
        """Did the subject named by this baseline key actually run this time?"""
        if key.startswith("repo_gate:"):
            return self.repo_gate_cases is not None
        if key.startswith("node:"):
            return self.node_gate_ran
        if "::" in key:
            return key in self.node_ids
        # A collection-error key names a MODULE, not a test: it ran when the
        # module imported and pytest got through the session.
        return self.pytest_complete and any(
            n.startswith(key + "::") for n in self.node_ids)


def _junit_case_id(case) -> str:
    """The pytest node id of one junit `testcase` element ('' when unknowable)."""
    name = case.get("name") or ""
    if not name:
        return ""
    file_attr = case.get("file") or ""
    classname = case.get("classname") or ""
    if file_attr:
        module = file_attr[:-3] if file_attr.endswith(".py") else file_attr
        dotted = module.replace("/", ".")
        inner = (classname[len(dotted) + 1:]
                 if classname.startswith(dotted + ".") else "")
        parts = [file_attr] + ([inner] if inner else []) + [name]
    elif classname:
        parts = ["/".join(classname.split(".")) + ".py", name]
    else:
        return ""
    return "::".join(parts)


def _junit_node_ids(path: Path) -> set:
    """Node ids pytest reports as EXECUTED, read from its junit XML.

    `-o junit_family=xunit1` is what carries the `file` attribute; without it
    only the dotted `classname` survives and a module path cannot be
    reconstructed from it unambiguously. The classname fallback is kept for the
    day that family is dropped — a wrong reconstruction there costs a missed
    prune, never a wrong one.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(str(path)).getroot()
    except Exception:
        return set()
    out = set()
    for case in root.iter("testcase"):
        node = _junit_case_id(case)
        if node:
            out.add(node)
    return out


def _junit_completed_reds(path: Path, limit: int = 50, detail_chars: int = 2000) -> list:
    """Failures/errors pytest itself recorded for tests that COMPLETED, from its junit XML.

    Read only when a later storage refusal ended the session early: pytest's
    `Exit` path prints no assertion detail or short summary, while the JUnit
    file this same invocation already wrote does carry the completed tests'
    identity and failure text. Bounded (`limit` cases, `detail_chars` each);
    each record says whether its detail was cut. These are retained first
    evidence only -- never attribution, baseline keys or new_failures.
    """
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(str(path)).getroot()
    except Exception:
        return []
    out = []
    for case in root.iter("testcase"):
        node = _junit_case_id(case)
        if not node:
            continue
        for kind in ("failure", "error"):
            element = case.find(kind)
            if element is None:
                continue
            message = (element.get("message") or "").strip()
            detail = (element.text or "").strip()
            out.append({"node_id": node, "kind": kind,
                        "message": message[:500],
                        "detail": detail[:detail_chars],
                        "detail_complete": len(detail) <= detail_chars})
            break
        if len(out) >= limit:
            break
    return out


def _retain_completed_reds(report: dict, reds: list) -> None:
    """Put completed reds from JUnit on the report's existing failures/summary.

    Only identities pytest's own terminal output did not already list are
    added, each marked as completed before the storage refusal. The run stays
    unmeasured (`storage_unmeasured`), so `_apply_baseline` neither keys nor
    attributes them; they are diagnostics, not this round's regressions.
    """
    if not reds:
        return
    listed = {token for f in (report.get("failures") or []) for token in str(f).split()}
    added = []
    for red in reds:
        if red["node_id"] in listed:
            continue
        first = red["message"].splitlines()[0] if red["message"] else red["kind"]
        entry = (f"FAILED {red['node_id']} - {first} (completed before the storage "
                 f"refusal; recorded in pytest's JUnit, not attributed)")
        report.setdefault("failures", []).append(entry)
        added.append(red)
    if not added:
        return
    block = ["Completed test failures/errors recorded in this session's JUnit before the "
             "storage refusal (first evidence; not attributed, not a baseline):"]
    for red in added:
        cut = "" if red["detail_complete"] else "\n    [...detail truncated...]"
        block.append(f"{red['node_id']}\n    "
                     + (red["detail"] or red["message"]).replace("\n", "\n    ") + cut)
    text = "\n".join(block)
    report["summary"] = (report.get("summary") or "") + "\n\n" + text[:8000]


def _baseline_dir(state_dir: str, repo) -> str:
    """The per-REPOSITORY corner of the per-CONFIG `state_dir`.

    `stateful` keys its directory by config name, and one config runs against
    many repositories: `coding_impl` takes an `against_project`, `dpe_default`
    is every project's pipeline. One baseline file directly under `state_dir`
    would carry the AItelier checkout's known-red into the wuxia game's run and
    forgive it there — a shared-state bug of exactly the kind the capability
    exists to prevent, so the scoping belongs here and not in the graph.

    Still RELATIVE to the injected directory: the tool never computes a home-
    relative path of its own, which is the rule `stateful` states in its own
    briefing.
    """
    if not state_dir or repo is None:
        return ""
    key = hashlib.sha256(str(repo).encode("utf-8")).hexdigest()[:16]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(repo).name)[:40] or "repo"
    return str(Path(state_dir) / "repos" / f"{name}-{key}")


def _apply_baseline(report: dict, state_dir: str,
                    executed: "Executed | None" = None,
                    defer_write: bool = False) -> "dict | None":

    """Add `new_failures[]` + `passed_relative` by diffing against known-red.

    `passed` stays absolute (whatever the suite actually reported) — callers and
    graphs already route on it. What it cannot say is whether the failures are
    the ROUND's: a pipeline whose test gate routes on `passed` sends every lap
    back to replanning while a single pre-existing red sits in the repo, and the
    round burns its whole loop budget on a defect it did not introduce.

    The baseline is SEEDED on the first run that finds none (so the pre-existing
    red of the repo as it stands becomes the known set) and afterwards only ever
    SHRINKS: a key proven to have run and not failed is dropped, so a test that
    was fixed and then broken again is reported as new. Nothing is ever added
    after the seed — an added key would let this round's own regression enter
    the baseline and be forgiven by the next lap.

    Scope: the snapshot is per worktree (`_baseline_dir`) and is FIRST OBSERVED
    by the first run that finds none. `baseline_state: seeded` records that and
    nothing more: the report has no timestamp or ordering against the round's
    edits, so a seed alone proves neither "before" nor "after" and supports no
    causal attribution. (`configs/coding_impl.yaml` runs a `baseline` step with
    `prechange_capture: true` BEFORE `implement`, and its `test` step reads it
    with `prechange_from: baseline`; that machine protocol is separate and is
    unchanged.) Only a later `compared` run measures change SINCE the observed
    snapshot. The human diagnostic (`scheduler._attribution_line`) states this
    per state.

    `baseline_state` says which of four things happened, because the fields
    alone cannot: `seeded`, `compared`, `unavailable` (no `state_dir` — the
    `stateful` capability is what injects one, and the step must declare it) and
    `unreadable` (a corrupt baseline, or failure identities this run could not
    key). An UNMEASURED baseline can never produce `passed_relative: True`: that
    field is a claim that the red was already there, and with nothing to compare
    against there is no such claim to make. The claim used to be produced
    anyway — a red run whose failures[] the parser could not key (a pytest
    usage error, returncode 2) came out as `passed_relative: True` over an empty
    baseline, which `gate_evidence.report_state` read as `known_failure`, which
    routes a game run PAST the hold and into `5_vision`.
    """
    failures = [str(f) for f in report.get("failures", [])]
    keys = [_failure_key(f) for f in failures]

    path = Path(state_dir) / BASELINE_FILE if state_dir else None
    baseline: list[str] = []
    state = "unavailable" if path is None else "compared"
    if path is not None and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            baseline = [str(k) for k in (data or {}).get("failures", [])]
        except (ValueError, OSError) as e:
            report["baseline_error"] = f"unreadable baseline {path}: {e}"
            state = "unreadable"

    identity_error = report.get("failure_identity_error")
    if report.get("storage_unmeasured") or report.get("measurement_not_taken"):
        # A PROVED storage inability (`diagnose_storage`: errno from a trusted
        # syscall) means this session measured nothing trustworthy. Whatever
        # pytest printed stays in failures[]/stdout as retained evidence, but it
        # is not a set of this round's regressions: nothing is new, nothing may
        # seed, prune or rewrite a baseline, and no relative pass can exist.
        # This precedes the identity-error branch: an unkeyable failure list on
        # an unmeasured run is still unmeasured, and the identity error stays
        # visible as a diagnostic instead of becoming `new_failures`.
        if identity_error:
            prior = report.get("baseline_error")
            report["baseline_error"] = (f"{prior}; {identity_error}" if prior
                                        else str(identity_error))
        report["new_failures"] = []
        report["passed_relative"] = False
        report["baseline_failures"] = []
        report["baseline_state"] = "unmeasured"
        return

    if identity_error:
        prior = report.get("baseline_error")
        report["baseline_error"] = (f"{prior}; {identity_error}" if prior
                                    else str(identity_error))
        report["new_failures"] = failures
        report["passed_relative"] = False
        report["baseline_failures"] = []
        # Not "compared": this run's own failure identities are unreliable, so
        # the diff it would produce is not a measurement of anything.
        report["baseline_state"] = "unreadable"
        return

    if (not failures and not report.get("passed")
            and report.get("returncode") not in (0, None)):
        # A non-zero pytest result with NO usable failure identity is not a
        # "known red", whatever the storage looked like: an empty case list is
        # not a measured zero-failure session, and no evidence that the red
        # was already there. No label is invented for it. It stays visibly
        # refused (never seeds, never relative-passes) and the raw output
        # stays in the report.
        diagnosed = (report.get("storage_diagnosis") or {}).get("state") or "unknown"
        report["baseline_error"] = (
            f"non-zero result (rc={report.get('returncode')}) without usable failure "
            f"identities (storage observation: {diagnosed}); nothing can be attributed")
        report["new_failures"] = []
        report["passed_relative"] = False
        report["baseline_failures"] = []
        report["baseline_state"] = "unmeasured"
        return


    known = set(baseline)
    # A declared ABSENCE is not a baseline, in either direction:
    #
    #   * it may not SEED one. The seed is taken from this run's `failures[]`,
    #     and an absence's `failures[]` entry is "repo_gate:run_tests.sh was NOT
    #     measured" — writing that into the baseline would record a gate that
    #     never spoke as this repo's standing known-red, and a later real red
    #     from that same gate would then be forgiven by it.
    #   * it may not report a relative pass. `passed_relative: true` is the
    #     claim "this red was already there"; with no verdict there is no such
    #     claim to make, and this is the ONE field a run must never reach from
    #     an absence. Measured on the r5 candidate: `passed_relative: true` +
    #     `baseline_state: seeded` and a baseline file written on every pole.
    #
    # So an absence with no baseline behind it gets its own state, `unmeasured`,
    # which is in neither BASELINE_MEASURED nor the write path below. An absence
    # that HAS a baseline still runs the diff, because that is what keeps a
    # known-red case from being pruned by a gate that said it did not run — the
    # `Executed` guard below — but it can no more pass relatively than seed.
    absent = bool(report.get("repo_gate_absent")
                  or report.get("repo_gate_unmeasured"))
    if (path is not None and not path.is_file() and state == "compared"
            and not absent):
        # Seed: this repo's current red IS the known red. Nothing is new
        # relative to a baseline that was just taken from it.
        known = set(keys)
        state = "seeded"
        report["new_failures"] = []
        report["baseline_seeded"] = True
    else:
        report["new_failures"] = [f for f, k in zip(failures, keys)
                                  if k not in known]
        if absent and (path is None or not path.is_file()):
            state = "unmeasured"
    report["baseline_state"] = state
    report["passed_relative"] = (state in BASELINE_MEASURED
                                 and not absent
                                 and not report["new_failures"])
    # Only a measured baseline may report a known-red SET. `[]` beside
    # `unavailable` means nobody looked, and saying so in one field that a
    # reader already routes on beats a sentence in `summary` nobody parses.

    report["baseline_failures"] = (sorted(known) if state in BASELINE_MEASURED
                                   else [])

    # No baseline file is written for an absence that found none: there was
    # nothing to compare against and nothing measured to record.
    if path is None or state in ("unreadable", "unmeasured"):
        return

    # Persist: the seed, or the pruned set. A key is dropped only when this run
    # can prove the thing it names RAN and did not fail — see `Executed`.
    if report.get("baseline_seeded"):
        keep = sorted(known)
        blocked: list[str] = []
    else:
        still_failing = set(keys)
        blocked = sorted(k for k in known
                         if k not in still_failing
                         and not (executed is not None and executed.ran(k)))
        keep = sorted((known & still_failing) | set(blocked))
    if blocked:
        # Readable in the report, so "it stayed known-red" is never mistaken
        # for "it was measured green and kept anyway".
        report["baseline_kept_unproven"] = blocked[:50]
    if keep == sorted(baseline) and path.is_file():
        return
    if defer_write:
        # The caller stages the measured report before committing this decision,
        # then publishes it. A later publication failure discloses any baseline
        # already stored alongside the raw observations and non-delivery result.
        return {"path": path, "keep": keep}
    return _commit_baseline(report, {"path": path, "keep": keep})


def _write_replacing(path: Path, text: str) -> None:
    """Write `text` to `path` through a sibling temp file and `os.replace`.

    A failed write (ENOSPC mid-file, say) leaves whatever `path` held before
    intact instead of truncated; the temp file is this call's own name and is
    removed on failure. Not a durability or cross-writer guarantee.
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _commit_baseline(report: dict, pending: dict) -> bool:
    """Write a baseline decided by `_apply_baseline`; True only if it was stored.

    On failure the decision is WITHDRAWN from the report: a seed or prune that
    is not on disk cannot back `baseline_state: seeded|compared`, a known-red
    set or a relative pass (`gate_evidence.report_state` reads those as an
    attributable known red). The previous baseline file, if any, is left as it
    was (`_write_replacing`); the absolute `passed`, the raw output and
    `failures[]` are untouched, and nothing is invented.
    """
    path = pending["path"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_replacing(path, json.dumps(
            {"failures": pending["keep"], "updated_at": time.time()}, indent=2))
    except OSError as e:
        number = e.errno
        prior = report.get("baseline_error")
        sentence = (f"could not write {path}: {e} (errno {number} "
                    f"{errno.errorcode.get(number, '')}); the "
                    f"{'seed' if report.get('baseline_seeded') else 'baseline update'} "
                    "was NOT stored, so no baseline attribution or relative pass is claimed")
        report["baseline_error"] = f"{prior}; {sentence}" if prior else sentence
        report["baseline_persisted"] = False
        report["baseline_write_errno"] = number
        report.pop("baseline_seeded", None)
        report["new_failures"] = []
        report["passed_relative"] = False
        report["baseline_failures"] = []
        report["baseline_state"] = "unmeasured"
        if "evidence_state" in report:
            report["evidence_state"] = report_state(report)
        if "release_evidence" in report:
            report["release_evidence"] = release_disposition(report)
        return False
    report["baseline_persisted"] = True
    return True



# How long the pytest leg may run before it is killed.
#
# This was 75 seconds, and the comment at the call site justified it as keeping
# the scheduler loop-thread free: "a genuinely-passing suite finishes well under
# this". Measured 2026-09-18 on the wuxia game repo: 2222 passed, 6 skipped,
# **83 seconds**. Eight seconds over the wall, every single time.
#
# What that cost, on run 1a60bf3d: four implement->test laps, ~4h, ending in
# `Cycle limit exceeded`, on a deliverable whose own 335 lines of new tests all
# pass. The round was never shown a test result; it was handed `passed: false`
# with an empty failures list and asked to fix it, four times.
#
# The loop-thread argument does not survive contact with this pipeline anyway:
# the GDScript repo_gate in the SAME step takes about an hour. A wall that is
# 1.2% of its neighbour is not protecting the scheduler from anything, it is
# just the smallest number in the file.
PYTEST_WALL_SECONDS = 1800
MAX_PYTEST_WALL_SECONDS = 7200  # Existing CPU executor request ceiling.


# --- Storage exhaustion is not a test regression -------------------------------
#
# pytest's tmp_path (`make_numbered_dir`) swallows the errno of a failed mkdir and
# reports only "could not create numbered dir ... after 10 tries" -- the 41 errors
# of iss-8403bf9711304e5c, sent back to an implementer whose code was fine. That
# sentence is the REPOSITORY's text and is deliberately not read here: it is also
# what a read-only directory or a permission error produces. The only evidence
# accepted is the errno of a syscall made by the trusted executor entry
# (`core/repository_executor_entry.py`, `storage-observation/1`) -- or by this
# module itself, on the backend's own output root -- answering ENOSPC/EDQUOT. No
# free-space threshold is consulted: `statvfs` numbers are RETAINED beside the
# errno (they say bytes vs inodes) but never decide anything.
STORAGE_SCHEMA = "storage-observation/1"
_STORAGE_FULL = (errno.ENOSPC, errno.EDQUOT)
_STATVFS_FIELDS = ("f_bsize", "f_frsize", "f_blocks", "f_bfree", "f_bavail",
                   "f_files", "f_ffree", "f_favail")
STORAGE_EXHAUSTED_STATES = ("exhausted_preflight", "exhausted_during_run")


class _HostPrepError(Exception):
    """The backend's own output root could not be prepared (errno kept)."""

    def __init__(self, what: str, path, exc: OSError):
        super().__init__(f"{what} {path}: {exc}")
        self.what, self.path, self.exc = what, str(path), exc


def _host_statvfs(path) -> dict | None:
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        info = os.statvfs(str(probe))
    except OSError:
        return None
    return {name: getattr(info, name) for name in _STATVFS_FIELDS}


def _full_resource(vfs) -> list[str]:
    """Which resource the retained statvfs says is at zero (evidence, not proof)."""
    out = []
    if isinstance(vfs, dict):
        if vfs.get("f_files") and vfs.get("f_favail") == 0:
            out.append("inodes")
        if vfs.get("f_blocks") and vfs.get("f_bavail") == 0:
            out.append("bytes")
    return out or ["unspecified"]


def diagnose_storage(storage, returncode) -> dict:
    """Classify the trusted storage observation of ONE pytest leg. Pure.

    States:
      exhausted_preflight   output-root probe got ENOSPC/EDQUOT before pytest started.
      exhausted_during_run  a required-temp probe during the session, or a probe
                            after a non-clean session, got ENOSPC/EDQUOT.
      ambiguous             a probe failed with some other errno (EACCES, EROFS, ...)
                            or with no errno: NOT disk exhaustion, nothing waived.
      clean                 every probe succeeded: the failures are the code's.
      unknown               no trusted observation (older entry, other transport).
    """
    if not isinstance(storage, dict) or storage.get("schema") != STORAGE_SCHEMA:
        return {"state": "unknown", "reason": "no trusted storage observation"}
    findings = []
    witnessed = 0          # authoritative probes that actually succeeded (mkdir AND write)
    for phase in ("before", "after"):
        observations = storage.get(phase)
        for obs in observations if isinstance(observations, list) else ():
            if not isinstance(obs, dict):
                continue
            # Only an explicit False demotes a root to evidence: the entry sets it
            # when the pytest temp namespace could not be resolved from pytest.
            authoritative = obs.get("authoritative") is not False
            mkdir, write = obs.get("mkdir"), obs.get("write")
            if (authoritative and isinstance(mkdir, dict) and mkdir.get("ok") is True
                    and isinstance(write, dict) and write.get("ok") is True):
                witnessed += 1
            for op in ("mkdir", "write"):
                attempt = obs.get(op)
                if not isinstance(attempt, dict) or attempt.get("ok") is not False:
                    continue
                if attempt.get("skipped"):
                    continue
                number = attempt.get("errno")
                number = number if type(number) is int else None
                full = number in _STORAGE_FULL
                findings.append({
                    "phase": phase, "path": obs.get("path"), "op": op, "errno": number,
                    "errname": errno.errorcode.get(number, "") if number else "",
                    "exhausted": full and authoritative,
                    "authoritative": authoritative,
                    "resource": (_full_resource(obs.get("statvfs"))
                                 if full and authoritative else []),
                    "mount": obs.get("mount"), "statvfs": obs.get("statvfs")})
    exhausted = [f for f in findings if f["exhausted"]]
    resolution = storage.get("tmp_resolution")
    unresolved = isinstance(resolution, dict) and resolution.get("resolved") is False
    if any(f["phase"] == "before" for f in exhausted):
        state = ("exhausted_during_run" if storage.get("refused_at") == "temp_factory_use"
                 else "exhausted_preflight")
    elif exhausted and returncode != 0:
        state = "exhausted_during_run"
    elif findings:
        state = "ambiguous"
    elif witnessed == 0 or unresolved:
        # No successful syscall witness (schema-only / partial / copied data), or
        # the namespace pytest really uses could not be established: clean is a
        # claim this record cannot support.
        state = "unknown"
    else:
        state = "clean"
    return {"state": state, "findings": findings,
            "proof": [f for f in exhausted if state in STORAGE_EXHAUSTED_STATES][:4],
            "preflight_refused": storage.get("preflight_refused") is True,
            "tmp_resolution": resolution if isinstance(resolution, dict) else None,
            "observation": storage}



def _mark_storage_unmeasured(report: dict, diagnosis: dict, unmeasured_entries: list) -> None:
    """Make a PROVED storage inability an infrastructure not-run.

    It is the same outcome `cpu_executor_unavailable` already is (skipped +
    infrastructure_unavailable + evidence_state), so gate_evidence.report_state
    reads it as `infrastructure_unavailable` and release routing as `unresolved`;
    `storage_unmeasured` is what `_apply_baseline` reads to refuse seeding,
    pruning and any relative pass. The raw returncode/stdout/stderr stay as the
    executor returned them; `failures[]` keeps whatever pytest really listed.
    """
    proof = (diagnosis.get("proof") or [{}])[0]
    where = (f"{proof.get('op')} in {proof.get('path')} -> {proof.get('errname')} "
             f"({'/'.join(proof.get('resource') or [])})")
    ran = diagnosis["state"] == "exhausted_during_run"
    at_factory = (diagnosis.get("observation") or {}).get("refused_at") == "temp_factory_use"
    when = (" during pytest's normal temp initialization; earlier configuration, collection "
            "or tests may already have run" if at_factory else " after pytest ran" if ran else
            " before any repository code ran")
    sentence = (f"Storage exhausted: the trusted executor's own {where} was refused by the "
                f"filesystem" + when + ". The test measurement is "
                + ("INCOMPLETE and its results cannot be trusted either way"
                   if ran else "NOT TAKEN") + ". This is not a test failure and is never a "
                "baseline; re-run once capacity is restored.")
    report.update(passed=False, skipped=True, infrastructure_unavailable=True,
                  skipped_because="storage_exhausted", evidence_state="infrastructure_unavailable",
                  storage_unmeasured=True)
    report["summary"] = sentence + ("\n\n" + report["summary"] if report.get("summary") else "")
    entry = f"pytest:storage exhausted ({where}) — no trustworthy measurement"
    report["failures"].append(entry)
    unmeasured_entries.append(entry)


def _apply_storage_diagnosis(report: dict, storage, returncode,
                             unmeasured_entries: list) -> dict:
    diagnosis = diagnose_storage(storage, returncode)
    report["storage_diagnosis"] = diagnosis
    if diagnosis["state"] in STORAGE_EXHAUSTED_STATES:
        if diagnosis["state"] == "exhausted_preflight":
            diagnosis["pytest_ran"] = False
        _mark_storage_unmeasured(report, diagnosis, unmeasured_entries)
    return diagnosis


def _host_storage_failure(report: dict, err: _HostPrepError, unmeasured_entries: list) -> None:
    """The backend's own junit/output root refused a syscall: nothing was run."""
    number = err.exc.errno
    exhausted = number in _STORAGE_FULL
    finding = {"phase": "before", "path": err.path, "op": err.what, "errno": number,
               "errname": errno.errorcode.get(number, ""), "exhausted": exhausted,
               "error": str(err.exc)[:200], "statvfs": _host_statvfs(err.path)}
    finding["resource"] = _full_resource(finding["statvfs"]) if exhausted else []
    state = "exhausted_preflight" if exhausted else "ambiguous"
    report["storage_diagnosis"] = {"state": state, "scope": "backend_output_root",
                                   "findings": [finding],
                                   "proof": [finding] if exhausted else [],
                                   "pytest_ran": False}
    # pytest was never started: there is no session return code to report, and
    # the default 0 would read as a clean run.
    report["returncode"] = None
    if exhausted:
        _mark_storage_unmeasured(report, report["storage_diagnosis"], unmeasured_entries)
    else:
        # Not proof of exhaustion, but pytest was never started either: say that,
        # as an unavailable facility, and claim nothing about the disk.
        report.update(passed=False, returncode=-1, skipped=True, infrastructure_unavailable=True,
                      skipped_because="test_workspace_unavailable", summary=str(err),
                      measurement_not_taken=True)
        entry = f"pytest:could not prepare its result directory ({err}) — no measurement"
        report["failures"].append(entry)
        unmeasured_entries.append(entry)


def _report_write_failure(report: dict, target_dir: Path, what: str, exc: OSError) -> dict:
    """The report itself could not be stored: say so, and never claim success.

    `written: None` is what routes a run to `test_evidence_missing` (a report
    that was not written is not evidence). `passed` is False whatever the suite
    did -- a green result that could not be recorded is not a delivered verdict.
    The errno is kept; ENOSPC/EDQUOT here is the filesystem's own answer for the
    backend's output root.
    """
    number = exc.errno
    exhausted = number in _STORAGE_FULL
    # The raw observations were made before the store failed and are the only
    # copy once the JUnit directory is cleaned up: keep them in the same return,
    # beside -- never instead of -- the failure itself. This is a BOUNDED tail,
    # not complete raw preservation: `raw_bounds` says, per stream, whether it
    # is complete and gives the original length and sha256 of the full text.
    bounds = {}

    def tail(name, text, limit=60_000):
        text = text if isinstance(text, str) else ""
        bounds[name] = {"chars": len(text), "complete": len(text) <= limit,
                        "sha256": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()}
        return text if len(text) <= limit else "[...truncated head...]" + text[-limit:]

    diagnosis = report.get("storage_diagnosis")
    retained = {"stdout": tail("stdout", report.get("stdout")),
                "stderr": tail("stderr", report.get("stderr")),
                "failures": [str(f)[:2000] for f in (report.get("failures") or [])][:100],
                "collection_errors": [str(f)[:2000] for f in
                                      (report.get("collection_errors") or [])][:100],
                "summary": tail("summary", report.get("summary"), 8_000)}
    retained["raw_bounds"] = bounds
    if report.get("baseline_error"):
        retained["baseline_error"] = report["baseline_error"]
    if report.get("completed_before_storage_refusal"):
        # Completed reds read from the (since removed) JUnit before a storage
        # refusal: already bounded by `_junit_completed_reds`.
        retained["completed_before_storage_refusal"] = report["completed_before_storage_refusal"]
    if isinstance(diagnosis, dict):
        retained["storage_diagnosis"] = diagnosis
        retained["storage_state"] = diagnosis.get("state")
    return {"written": None, "passed": False, "_error": True,
            "error": (f"run_tests: could not {what} the test report under {target_dir}: "
                      f"{exc} (errno {number} {errno.errorcode.get(number, '')})"),
            "report_write_failed": True, "storage_exhausted": exhausted,
            "infrastructure_unavailable": True,
            "evidence_state": "infrastructure_unavailable",
            "release_evidence": "unresolved", "passed_relative": False,
            "new_failures": [], "baseline_state": "unmeasured",
            # A baseline can already be stored when final publication fails;
            # an attempted but failed seed/update is explicitly False.
            "baseline_persisted": report.get("baseline_persisted") is True,
            "storage_unmeasured": bool(report.get("storage_unmeasured")),
            "returncode_observed": report.get("returncode"),
            "raw_retained": retained, **retained,
            "host_statvfs": _host_statvfs(target_dir)}



def run_tests(*, project_root: str = "", out_dir: str = "",
              workspace_root: str = "", repo_gate: bool = True,
              state_dir: str = "", run_id: str = "",
              pytest_wall_seconds: int = PYTEST_WALL_SECONDS,
              evidence_cycle_start: bool = False,
              evidence_cycle_from: str = "",
              fail_fast_gates: str = "",
              prechange_capture: bool = False, prechange_from: str = "",
              **kwargs) -> dict:
    """Run pytest over the consolidated repo; write test_report.json to out_dir.

    Returns {written, passed, passed_relative, baseline_state, baseline_known,
    failure_count, new_failures}. The report holds {passed, returncode, summary,
    failures[], collection_errors[], skipped?} for the reviewer to read, plus a
    ``node`` section (npm install/build/test) when the repo contains a node
    project, and the baseline fields {new_failures[], passed_relative,
    baseline_failures[], baseline_state} — see `_apply_baseline`. `state_dir`
    is injected by the `stateful` capability; a step that does not declare it
    gets `baseline_state: "unavailable"` and no relative pass at all.
    """
    report = {"passed": True, "returncode": 0, "summary": "", "failures": [],
              "collection_errors": []}
    wall_valid = (type(pytest_wall_seconds) is int
                  and 1 <= pytest_wall_seconds <= MAX_PYTEST_WALL_SECONDS)
    if wall_valid:
        report["pytest_wall_seconds"] = pytest_wall_seconds
    else:
        reason = ("run_tests: pytest_wall_seconds must be an integer from 1 to "
                  f"{MAX_PYTEST_WALL_SECONDS}; no tests were executed")
        report.update(passed=False, returncode=None, skipped=True,
                      measurement_not_taken=True,
                      skipped_because="invalid_pytest_wall_seconds", summary=reason)
        report["failures"].append(reason)
    # Filled in as each leg runs; it is the ONLY licence to shrink the baseline.
    executed = Executed()
    # The entries of `failures[]` that record a leg which measured NOTHING (a
    # pytest killed at its wall, a node runner that is not installed). Every
    # other entry is a measured failure.
    unmeasured_entries: list[str] = []
    # Set when a trusted probe proved the test namespace cannot be written
    # BEFORE repository code ran: the node and repo gates are then not started
    # either (they would only spend their budget on the same refusal).
    storage_preflight_refused = False

    prechange_before = prechange_cases = prechange_profile = None
    retained_red_candidate = False
    prechange_enabled = wall_valid and bool(prechange_capture or prechange_from)
    if prechange_enabled:
        from core import coding_impl_outcome as coding
        try:
            report["run_binding"] = coding.current_binding(run_id)
            if prechange_capture:
                # Inline native tool nodes do not resolve required context in
                # the pinned engine. Reuse the existing published-seed guard
                # before paying for this upfront measurement.
                from api.dependencies import get_skillflow
                from core.seed_publication import seed_dir, seed_is_published
                try:
                    ready, why = seed_is_published(seed_dir(
                        get_skillflow(), report["run_binding"]["project_id"], "coding_impl"), "plan.md")
                except Exception as exc:
                    ready, why = False, "published seed unreadable: " + type(exc).__name__
                if not ready:
                    return {"written": None, "passed": False, "_error": True,
                            "error": "coding_impl baseline requires published plan.md: " + why}
            prechange_before = coding.owned_snapshot(project_root, run_id, original=prechange_capture)
        except Exception as exc:
            report["prechange_unavailable"] = str(exc)[:300]

    if fail_fast_gates and wall_valid:
        if not out_dir or not Path(out_dir).is_absolute():
            return {"written": None, "passed": False,
                    "error": ("run_tests fail-fast requires an absolute "
                              "out_dir=$STEP_DIR")}
        from aitelier.gate_evidence import (
            first_upstream_blocker,
            stamp_report,
            upstream_failed_report,
        )
        target_dir = Path(out_dir)
        blocker = first_upstream_blocker(target_dir.parent, run_id,
                                         fail_fast_gates)
        if blocker:
            report.update(upstream_failed_report(blocker, "Test gate"))
            # No baseline was consulted, and `unavailable` is how the report
            # says that rather than showing an empty known-red set as if it had
            # been measured.
            report.update(passed_relative=False, new_failures=[],
                          baseline_failures=[], baseline_state="unavailable")
            stage = "mkdir"
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                if run_id and evidence_cycle_from:
                    stage = "stamp the evidence cycle of"
                    stamp_report(report, run_id=run_id, out_dir=str(target_dir),
                                 cycle_from=evidence_cycle_from)
                stage = "write"
                (target_dir / "test_report.json").write_text(
                    json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError as exc:
                # Same non-delivery answer as the main path: never an escaped
                # exception, never a claim that the report was written.
                return _report_write_failure(report, target_dir, stage, exc)

            return {"written": "test_report.json", "passed": False,
                    "passed_relative": False, "new_failures": [],
                    "baseline_state": "unavailable", "baseline_known": 0,
                    "failure_count": len(report.get("failures") or []),
                    "release_evidence": release_disposition(report),
                    "skipped_because": report["skipped_because"],
                    "upstream_state": (blocker.get("upstream_state")
                                       or blocker.get("state"))}

    if not wall_valid:
        repo = None  # Invalid policy must not start pytest, Node or repository gates.
    elif not project_root or not Path(project_root).is_absolute():
        # The check is on `project_root` — the code repository — and on nothing
        # else, because that is the only argument that names one.
        #
        # The line this replaces was `Path(project_root or workspace_root)`, and
        # it has two ways to go wrong that nothing downstream would catch —
        # `repo.exists()` is true for both:
        #
        #   * with both roots empty, `Path("").resolve()` is the process CWD (in
        #     the container `/app`, the AItelier checkout — itself a git repo
        #     with ~2000 tests);
        #   * with `project_root` missing and `workspace_root` supplied, the
        #     fallback picks the DPS WORKSPACE, so pytest would run over the
        #     run's own step-output tree.
        #
        # Neither has happened. Until this round `get_project_code_path` always
        # returned a path, so `project_root` was always truthy and the fallback
        # was dead code. What makes it live is the new answer this round adds: a
        # run that declares no code repository, for which the engine supplies no
        # project root at all (skillflow >=1.5.52 omits the argument; earlier
        # releases, including the 1.5.46 the container installs from PyPI,
        # forward "") while still supplying an absolute `workspace_root`. That
        # shape is reachable — `tool_creation` grants this tool to
        # pipeline_forge's `t_tool_impl`, and pipeline_forge declares
        # `repo_mode: none`.
        #
        # So the check is on `project_root` alone, because that is the only
        # argument that names a code repository, and refusing is the only correct
        # answer when there is none. Written HERE rather than only in the engine,
        # because the engine that calls it may be any released version.
        report.update(
            passed=False,
            summary=f"run_tests: no project repository to test — project_root "
                    f"must be an absolute path (got {project_root!r}); refusing "
                    f"to resolve against the process CWD or the DPS workspace")
        repo = None
    else:
        repo = Path(project_root).resolve()

    if repo is None:
        pass                      # refused above — there is nothing to run
    elif not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    else:
        py = "python3"
        # Junit XML is how this gate learns which tests PASSED: `-q` prints
        # only failures, and "not in the failure list" is exactly the
        # not-run/not-collected confusion `Executed` exists to remove. It
        # is written OUTSIDE the repo so it can never be committed by a
        # later `repo_apply`.
        junit_root = (datadir.aitelier_home() / "gate-reports" /
                      hashlib.sha256(run_id.encode()).hexdigest())
        junit_dir = None
        try:
            # The backend's own output root. These two calls used to sit outside
            # the `try`, so a full filesystem here raised out of run_tests with
            # no report at all; the errno is kept now and classified.
            try:
                junit_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise _HostPrepError("mkdir", junit_root, exc) from exc
            try:
                junit_dir = tempfile.mkdtemp(prefix="run_tests_junit_", dir=junit_root)
            except OSError as exc:
                raise _HostPrepError("mkdtemp", junit_root, exc) from exc
            junit_path = Path(junit_dir) / "junit.xml"
            # --rootdir forces pytest root to the project repo so it doesn't
            # walk up and find AItelier's pytest.ini (whose testpaths=tests
            # would cause discovery of AItelier's own test suite).
            # --continue-on-collection-errors: without it a single
            # unimportable module INTERRUPTS the session, so the report is
            # one traceback and zero test results even when the other 1600
            # tests would have run and passed. With it, every module is
            # imported (all import defects reported in one pass) and the
            # tests that do collect still run, so the reviewer sees the
            # whole picture instead of the first thing that broke.
            done = repository_executor.execute(
                repo, [py, "-m", "pytest", str(repo), "-q", "--tb=short",
                       "-p", "no:cacheprovider", "--continue-on-collection-errors",
                       "--rootdir", str(repo), f"--junitxml={junit_path}",
                       "-o", "junit_family=xunit1"],
                pytest_wall_seconds, writable_dirs=[Path(junit_dir)],
                import_module=_package_module(repo) or "", pytest_timeout=True, run_id=run_id)
            stdout, stderr = done.stdout, done.stderr
            if prechange_enabled:
                identity = getattr(done, "execution_identity", None)
                report["execution_identity"] = identity
                try:
                    if prechange_before is None:
                        raise ValueError("original owned bytes unavailable")
                    prechange_cases = coding.junit_observations(junit_path, repo, prechange_before["files"])
                    report["pytest_observations"] = prechange_cases
                    prechange_profile = coding.execution_profile(identity, prechange_before, junit_dir)
                except Exception as exc:
                    report["prechange_unavailable"] = str(exc)[:300]
            if done.import_error:
                report["import_error"] = done.import_error
            if getattr(done, "git", None) is not None:
                # What Git metadata the container had (`private_git`).
                report["git_metadata"] = done.git
            out = ((stdout or "") + "\n" + (stderr or "")).strip()
            report["stdout"], report["stderr"] = stdout or "", stderr or ""
            report["returncode"] = done.returncode
            # 0 and 1 are the two outcomes in which pytest ran a session to
            # the end. 2 (usage/internal), 3 (interrupted), 4 (usage) and 5
            # (nothing collected) all mean the suite was not exercised, and
            # a baseline key must not be pruned off one of them.
            executed = Executed(
                node_ids=_junit_node_ids(junit_path),
                pytest_complete=done.returncode in (0, 1))
            # pytest: 0=all passed, 1=failures, 5=NOTHING COLLECTED.
            # 5 is a third outcome — no evidence — and it used to be routed
            # forward as a pass: on the `autopep8` benchmark task the
            # implementer never delivered the test directory, the gate found
            # nothing, said `passed: true`, 5_review passed on that basis and
            # a 0.128-scoring repo shipped as `completed`. A gate that
            # checked nothing has not passed; it is only "not applicable"
            # when the repo holds no Python code at all (a node or Godot
            # project, gated by its own section below).
            report["passed"] = done.returncode == 0
            if done.returncode == 5:
                report["no_tests_collected"] = True
                report["passed"] = not _has_python_sources(repo)
            collection_errors = _collection_errors(out)[:50]
            report["collection_errors"] = collection_errors
            failed_lines = [ln.strip() for ln in out.splitlines()
                            if ln.startswith("FAILED") or " FAILED " in ln][:50]
            report["failures"] = collection_errors + failed_lines
            if done.returncode == 5 and not report["passed"]:
                report["failures"].append(
                    "No tests were collected — the gate verified NOTHING.")
                report["summary"] = (
                    "No tests were collected, so nothing about this project "
                    "was verified. This is NOT a pass: the suite is missing "
                    "(or unreachable by pytest). Deliver tests under `tests/` "
                    "covering the MVP goals and re-run.")
            elif done.returncode == 5:
                report["summary"] = ("No tests were collected (no Python "
                                     "sources — pytest not applicable).")
            else:
                report["summary"] = out[-3000:]
            # Collection errors go ABOVE the tail, and in full. They are the
            # defects that make every OTHER result meaningless (an
            # unimportable module contributes zero passing tests), and they
            # are printed first — so the tail slice is exactly what drops
            # them. Kept together so one fix pass can address all of them
            # instead of one per goal-loop lap.
            if collection_errors:
                report["summary"] = (
                    f"{len(collection_errors)} module(s) could not be imported. "
                    "Every test in them counts as failed; fix ALL of these, not "
                    "just the first:\n  "
                    + "\n  ".join(collection_errors)
                    + "\n\n" + report["summary"])
            # Lead with the install failure when there was one. An
            # unimportable package produces a ModuleNotFoundError whose real
            # cause is the failed editable install, and a reader given only
            # the symptom rewrites packaging metadata that was never wrong.
            report["summary"] = _lead_with_install_error(report)
            # The executor entry's own filesystem observation decides whether
            # this session measured anything (see `diagnose_storage`); the
            # repository's output text is never consulted for that.
            diagnosis = _apply_storage_diagnosis(
                report, getattr(done, "storage", None), done.returncode, unmeasured_entries)
            if diagnosis["state"] == "exhausted_preflight":
                storage_preflight_refused = True
            if diagnosis["state"] in STORAGE_EXHAUSTED_STATES:
                executed = Executed()  # nothing proven to have run: no prune licence
            if diagnosis["state"] == "exhausted_during_run":
                # pytest's Exit path prints no assertion detail for tests that
                # already completed; this same invocation's JUnit (read before
                # its directory is removed below) does. Retain it as first
                # evidence on the existing failures/summary surface, bounded,
                # with the run still unmeasured (no baseline, no new_failures).
                reds = _junit_completed_reds(junit_path)
                if reds:
                    report["completed_before_storage_refusal"] = reds
                    _retain_completed_reds(report, reds)
        except _HostPrepError as e:
            _host_storage_failure(report, e, unmeasured_entries)
            storage_preflight_refused = bool(report["storage_diagnosis"]["proof"])
        except repository_executor.IsolationUnavailable as e:
            report.update(passed=False, returncode=-1, skipped=True, infrastructure_unavailable=True,
                          measurement_not_taken=True, evidence_state="infrastructure_unavailable",
                          skipped_because="cpu_executor_unavailable", summary=str(e))
            report["failures"].append("pytest:isolated CPU executor unavailable — no measurement")
            unmeasured_entries.append(report["failures"][-1])
        except subprocess.TimeoutExpired as e:
            # A timeout is ABSENT evidence, not a red suite. `passed=False`
            # alone is indistinguishable from "ran and failed", and with an
            # empty failures[] plus a baseline diff of nothing it classified
            # as `known_failure` / `passed_relative: true` — a verdict about
            # tests that never ran. `skipped_because` routes it through
            # gate_evidence.report_state -> "skipped" -> "unresolved", which
            # is the state that means "go and verify", and the failures entry
            # keeps the reason readable to the agent that has to act on it.
            report.update(
                passed=False, returncode=None, timed_out=True, measurement_not_taken=True,
                stdout=e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                stderr=e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
                skipped_because="pytest_timeout",
                pytest_wall_seconds=pytest_wall_seconds,
                summary=(
                    f"pytest did not finish within {pytest_wall_seconds}s and was "
                    "killed, so NOTHING was measured. This is not a test failure: "
                    "no test result exists either way. Either the suite needs "
                    "longer than this harness allows, or it hangs."))
            report["failures"].append(
                f"pytest:timed out after {pytest_wall_seconds}s — no results "
                "collected, the suite was killed mid-run")
            unmeasured_entries.append(report["failures"][-1])
        except Exception as e:  # never raise — the step must not fail
            report.update(passed=False, summary=f"Error running pytest: {e}")
        finally:
            if junit_dir:
                shutil.rmtree(junit_dir, ignore_errors=True)

        # A package that doesn't import fails the gate whatever pytest said
        # (it can still exit 0 — e.g. when the broken module is only reached
        # by a conftest that pytest never got to).
        if report.get("import_error"):
            report["passed"] = False
            report["failures"].insert(0, report["import_error"])
            report["summary"] = _lead_with_import_error(report)

    # Node gate (npm install/build/test) — folded into the same report so
    # 5_review loops frontend breakage back through the goal-loop exactly
    # like pytest failures.
    if repo is not None and repo.exists():
        node = None if storage_preflight_refused else _run_node_checks(repo, run_id=run_id)
        if node is not None:
            report["node"] = node
            executed.node_gate_ran = not node.get("skipped")
            if node.get("skipped"):
                report.update(passed=False, skipped=True,
                              infrastructure_unavailable=True,
                              evidence_state="infrastructure_unavailable")
                report["failures"].append(
                    "node gate unavailable: " + node.get("summary", ""))
                unmeasured_entries.append(report["failures"][-1])
            elif not node["passed"]:
                report["passed"] = False
                for name, chk in node["checks"].items():
                    if not chk["passed"]:
                        report["failures"].append(
                            f"node:{name} failed (rc={chk['returncode']}): "
                            f"{chk['output'][-500:]}")

    # The repo's own declared gate (run_tests.sh) — folded in like the node
    # gate. A green pytest run over a product pytest cannot compile is a
    # pass-on-absence, and it shipped a non-compiling branch on 2026-09-10.
    # `repo_gate: false` belongs ONLY to a pipeline that already owns an engine
    # gate of its own (dpe_game's 5_compile). Everywhere else the default
    # stands: a green pytest over a product pytest cannot compile is not a pass.
    if storage_preflight_refused and repo_gate:
        report["repo_gate_skipped_because"] = "storage_exhausted_preflight"
    if repo_gate and repo is not None and repo.exists() and not storage_preflight_refused:
        gate = _acquire_repo_gate(repo, run_id=run_id)
        if gate is not None:
            report["repo_gate"] = gate
            report["gate_coverage"] = gate.get("gate_coverage")
            if isinstance(report["gate_coverage"], dict) and report["gate_coverage"].get("purpose") == "provisional_round_feedback":
                report["purpose"] = "provisional_round_feedback"
                report["full_test_passed"] = False
            if gate.get("gate_coverage_error"):
                report["gate_coverage_error"] = gate["gate_coverage_error"]
            outcome = gate["measured"]
            # The engine's reply to the gate's last request, as the admission
            # relay recorded it (`answered`, `not_admitted`, `unreachable`, ...).
            report["repo_gate_admission"] = (gate.get("admission") or {}).get("state")
            if outcome == REPO_GATE_UNATTRIBUTABLE:
                # Reds under the ticket, and no report there is provably the
                # gate's: they are neither this code's reds nor an absence.
                # `repo_gate_unattributable` ends the run on its own terminal
                # (`configs/coding_impl.yaml`), except beside a missing runner,
                # which ends it at `test_evidence_missing` as always.
                reds = gate["unattributed_reds"]
                why = (gate.get("report_attribution") or {}).get("why") or ""
                executed.repo_gate_cases = None
                report["passed"] = False
                report["repo_gate_absent"] = False
                report["repo_gate_unattributable"] = not report.get(
                    "infrastructure_unavailable")
                if report["repo_gate_unattributable"]:
                    report["evidence_state"] = "unattributable"
                sentence = (f"repository gate report could not be attributed "
                            f"({why}): {len(reds)} red(s) under ticket "
                            f"{gate.get('ticket')}")
                # The reds' identities belong to no known report, so they may
                # neither seed nor prune a baseline (`_apply_baseline`).
                gate["failure_identity_error"] = sentence
                report["failure_identity_error"] = sentence
                listed = "; ".join(reds[:20])
                more = f"; and {len(reds) - 20} more" if len(reds) > 20 else ""
                report["failures"].append(
                    f"repo_gate:{gate['script']} {sentence}: {listed}{more}")
            elif outcome == REPO_GATE_UNMEASURED:
                # The run is an ABSENCE only when nothing else in this report
                # was MEASURED red and no runner is missing:
                #   * a measured red (a pytest red, a collection error, an
                #     import error, a node check red) goes back to the
                #     implementer like any other red;
                #   * a pytest killed at its wall measured nothing, so it is
                #     no reason to charge the implementer, and the run waits
                #     for the gate (`unmeasured_entries`);
                #   * a missing runner (`infrastructure_unavailable`) is not
                #     something waiting for the gate can bring back, so the
                #     report goes on to `test_evidence`, whose schema ends the
                #     run at `test_evidence_missing`.
                # Which entries count is `unmeasured_entries`, never how many
                # entries `failures[]` holds.
                measured_elsewhere = [f for f in report["failures"]
                                      if f not in unmeasured_entries]
                absent = (not measured_elsewhere
                          and not report.get("infrastructure_unavailable"))
                # Not run is not a product failure, so release routing may not
                # read it as one (`gate_evidence.report_state` maps `not_run`
                # to `skipped`, never `failed`).
                if absent:
                    report["evidence_state"] = "not_run"
                # The gate said it did not run and re-acquiring the verdict
                # did not get one either. Nothing was measured: there is no
                # case list to prune from and no failure of the implementer's
                # to read out of it. Say exactly that; never invent a red, and
                # never let one be forgiven by a gate that stayed silent.
                executed.repo_gate_cases = None
                report["passed"] = False
                report["repo_gate_unmeasured"] = True
                # The scheduler reads THIS flag to tell an absence from a
                # red: a gate that produced no verdict decided nothing, so
                # the run may not be sent back to the implementer for it
                # (see core/gate_deferral.py). It is a separate key from
                # `repo_gate_unmeasured`, which says only that this gate
                # measured nothing. When it is an absence is `absent` above.
                report["repo_gate_absent"] = absent
                report["failures"].append(
                    f"repo_gate:{gate['script']} was NOT measured "
                    f"(engine admission: {report['repo_gate_admission']}; "
                    f"{gate.get('attempts', 1)} attempt(s)): "
                    f"{_gate_feedback(gate)}")
            elif outcome == REPO_GATE_MEASURED_PASS:
                # The gate ran and reported nothing failed: every case it
                # knows about passed, so a known-red case of its own may be
                # pruned. A gate that did not run at all (no run_tests.sh,
                # `repo_gate: false`) leaves this None and its keys stay
                # known-red.
                coverage = gate.get("gate_coverage")
                # The baseline currently has gate-level execution authority,
                # not scenario-level authority. Keep every old gate red after
                # a subset or unknown scope; absence is not a repair.
                executed.repo_gate_cases = (set() if isinstance(coverage, dict)
                    and not validate_coverage(coverage)
                    and coverage["coverage"] == "full"
                    and coverage.get("purpose") != "provisional_round_feedback" else None)
            else:
                report["passed"] = False
                cases, identity_error = _repo_gate_failure_cases(gate)
                # Every case that was read is listed, with or without an
                # identity error beside it: an unreadable part of the report
                # adds that error and removes no red.
                if cases:
                    gate["failure_cases"] = cases
                for case in cases:
                    detail = case["detail"] or "reported failed"
                    report["failures"].append(
                        f"repo_gate:{gate['script']}#{case['case_id']} "
                        f"failed: {detail}")
                if identity_error is not None:
                    # A gate whose case identities cannot all be read has not
                    # told us which cases ran; nothing of its may be pruned.
                    executed.repo_gate_cases = None
                    gate["failure_identity_error"] = identity_error
                    report["failure_identity_error"] = identity_error
                    if not cases:
                        report["failures"].append(
                            f"repo_gate:{gate['script']} failed "
                            f"(rc={gate['returncode']}): "
                            f"{_gate_feedback(gate)}")
                else:
                    coverage = gate.get("gate_coverage")
                    executed.repo_gate_cases = ({c["case_id"] for c in cases}
                        if isinstance(coverage, dict) and not validate_coverage(coverage)
                        and coverage["coverage"] == "full"
                        and coverage.get("purpose") != "provisional_round_feedback" else None)


    # Known-red baseline: `new_failures[]` + `passed_relative` + the
    # `baseline_state` that says whether either is a measurement, on top of the
    # absolute `passed`, so a reader can tell this round's breakage from the
    # repo's standing red — and tell both from "no baseline was taken".
    # The decision is taken now; persistence waits for a successful report stage
    # (see the delivery order below), with any later side effect disclosed.
    pending_baseline = _apply_baseline(
        report, _baseline_dir(state_dir, repo), executed, defer_write=True)


    # With no repo AND no out_dir there is nowhere to write — say so in the
    # return rather than defaulting to the CWD, which is the whole point above.
    #
    # `out_dir` is normally the step's staging dir, but it is NOT guaranteed:
    # `SkillFlow.execute_tool` setdefaults workspace_root, project_root,
    # config_name, step_id, run_id, step_tmp_dir and step_dir — not out_dir —
    # and a tool NODE gets whatever its `tool_params` name. So this branch is
    # reachable, not defensive padding.
    # ABSOLUTE, for the same reason the root above must be. Checking only for an
    # EMPTY out_dir left the hole this guard exists to close: `out_dir` is an
    # agent-visible parameter (tool.yaml, required: false), and `run_tests` is
    # granted to a repo-less step by the `tool_creation` capability — so
    # `out_dir="reports"` on a run with no repo made `Path("reports").mkdir()`
    # resolve against the process CWD, which in the container is /app, the
    # bind-mounted AItelier checkout. One directory and one fixed filename, but
    # written outside the run's jail all the same.
    if out_dir and not Path(out_dir).is_absolute():
        return {"written": None, "passed": False,
                "error": f"run_tests: out_dir must be an absolute path (got "
                         f"{out_dir!r}) — refusing to resolve against the "
                         f"process CWD. {report['summary']}"}
    if not out_dir and repo is None:
        return {"written": None, "passed": False, "error": report["summary"]}
    target_dir = Path(out_dir) if out_dir else repo
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _report_write_failure(report, target_dir, "mkdir", exc)
    if run_id and (evidence_cycle_start or evidence_cycle_from):
        from aitelier.gate_evidence import stamp_report
        try:
            stamp_report(report, run_id=run_id, out_dir=str(target_dir),
                         start_cycle=evidence_cycle_start,
                         cycle_from=evidence_cycle_from)
        except OSError as exc:
            # The evidence-cycle manifest is part of delivering this report: a
            # failed store is a non-delivery that keeps the raw observations and
            # persists no baseline, like a failed initial report stage.
            return _report_write_failure(report, target_dir, "stamp the evidence cycle of", exc)

    if prechange_enabled:
        try:
            after = coding.owned_snapshot(repo, run_id, original=prechange_capture)
            retained_red_candidate = coding.finish_measurement(
                report, target_dir, prechange_before, after, prechange_cases, prechange_profile,
                capture=prechange_capture, source=prechange_from)
        except Exception as exc:
            report["prechange_unavailable"] = str(exc)[:300]
    # Delivery, in an order whose every outcome is described truthfully:
    #   1. the report is STAGED (a sibling temp name; nothing published yet) --
    #      a failure here persists neither report nor baseline;
    #   2. the baseline decision is committed; if that write fails the claim it
    #      backed is withdrawn from the report (`_commit_baseline`) and the
    #      staged report is re-staged to say so;
    #   3. the staged report is published by `os.replace`, so an earlier valid
    #      report is never truncated by a failed write.
    # A failure at 1-3 is the existing non-delivery answer (`_report_write_failure`),
    # never `written: test_report.json`. A failure at final publication can leave
    # the already stored baseline; that measured side effect is disclosed by
    # baseline_persisted, alongside the retained raw and non-delivery verdict.
    report_path = target_dir / "test_report.json"
    staged = report_path.with_name(
        f".test_report.json.tmp-{os.getpid()}-{time.monotonic_ns()}")

    def undeliverable(what: str, exc: OSError) -> dict:
        try:
            staged.unlink()
        except OSError:
            pass
        return _report_write_failure(report, target_dir, what, exc)

    def stage() -> None:
        staged.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        stage()
    except OSError as exc:
        # Initial staging failed before the pending baseline was committed.
        return undeliverable("write", exc)
    if pending_baseline and not _commit_baseline(report, pending_baseline):
        try:
            stage()
        except OSError as exc:
            return undeliverable("write", exc)
    try:
        os.replace(staged, report_path)
    except OSError as exc:
        return undeliverable("publish", exc)

    return {"written": "test_report.json", "passed": report["passed"],
            # What the baseline write actually did (None: nothing to write).
            "baseline_persisted": report.get("baseline_persisted"),
            "baseline_error": report.get("baseline_error"),
            # Read by the same consumers as the file's own fields: a proved
            # storage inability is infrastructure evidence, never a red.
            "evidence_state": report.get("evidence_state"),
            "infrastructure_unavailable": bool(report.get("infrastructure_unavailable")),
            "storage_unmeasured": bool(report.get("storage_unmeasured")),
            "storage_state": (report.get("storage_diagnosis") or {}).get("state"),
            # Completed reds this session's JUnit recorded before a storage
            # refusal ended it: first evidence, never new_failures/baseline.
            "completed_before_storage_refusal": report.get("completed_before_storage_refusal") or [],
            "retained_red_candidate": retained_red_candidate,
            "release_evidence": release_disposition(report),
            "purpose": report.get("purpose"),
            "full_test_passed": False if report.get("purpose") == "provisional_round_feedback" else report.get("full_test_passed"),
            "gate_coverage": report.get("gate_coverage"),
            "gate_coverage_error": report.get("gate_coverage_error"),
            # The routing flag ITSELF, on the RETURN and not only in the file.
            # A `from_file` match must be evaluated by a step that owns the file
            # AND whose content parses to an OBJECT: measured, a report parsing
            # to a list raises AttributeError inside the engine's `_flags_match`
            # (`'list' object has no attribute 'get'`), which takes the
            # transition resolver down instead of routing anywhere. The return
            # dict is merged into the step's flags, so `{field:
            # repo_gate_absent, value: true}` matches with NO file reader at all
            # — no dependence on which step owns the artifact, no crash on a
            # malformed one. `configs/coding_impl.yaml` routes on this flag;
            # core/gate_deferral.py reads the FILE for the reason.
            "repo_gate_absent": bool(report.get("repo_gate_absent")),
            # The same, for a ticket whose reds belong to no identified report.
            "repo_gate_unattributable": bool(
                report.get("repo_gate_unattributable")),
            "passed_relative": report["passed_relative"],
            # Carried in the RETURN, not only the report, because the terminal
            # failure reason is assembled from what the run left behind: a loop
            # that dies on `Cycle limit exceeded` has to be able to say which
            # reds it kept looping on and whether they were its own.
            "baseline_state": report["baseline_state"],
            "baseline_known": len(report["baseline_failures"]),
            "failure_count": len(report["failures"]),
            "new_failures": report["new_failures"]}
