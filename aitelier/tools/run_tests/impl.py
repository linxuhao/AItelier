"""run_tests — execute the project's unit tests and write a test report.

Used as a tool STEP after the final verifier. It ALWAYS succeeds (so a failing
test never fails the run); the outcome is captured in ``test_report.json`` so the
verifier-review step can fold test failures into its change requests and loop
back to the planner (the goal-loop).

Runner resolution: prefer pytest in the current interpreter; if it is missing
(the Docker backend ships no test deps), provision a throwaway venv with
``--system-site-packages`` (so it inherits whatever IS installed) and install
the test toolchain — pytest + pytest-asyncio (REQUIRED by ``asyncio_mode=auto``
configs; without it every async test errors out) + pytest-timeout — plus the
project's declared dependencies (``requirements.txt``, or an editable install
that reads ``pyproject.toml``/``setup.py``, INCLUDING its declared test extras).
If the runner cannot be provisioned at all (e.g. no network), the gate records
``infrastructure_unavailable``. It remains distinct from a test failure and is
non-passing because missing evidence cannot release a tree.

Three outcomes, not two: pass, fail, and NO EVIDENCE. "pytest collected nothing"
(exit 5) is the third, and it is not a pass — see the returncode handling in
`run_tests` — unless the repo holds no Python at all, in which case the node /
compile sections are the real gate.
"""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess

from core import datadir, env_scrub
from aitelier import gate_admission
# Module-level, NOT function-local. This name is used on EVERY path out of
# `run_tests` (the return dict's `release_evidence`), so an import that lives
# inside the `fail_fast_gates` branch made it a LOCAL of the whole function:
# every ordinary invocation died with `UnboundLocalError: cannot access local
# variable 'release_disposition'` before writing a report at all. That is a
# step that never produced a verdict for a reason that has nothing to do with
# the code under test — the exact shape this card exists to remove.
from aitelier.gate_evidence import release_disposition
import sys
import tempfile
import time
from pathlib import Path


def _kill_group(proc) -> None:
    """SIGKILL the process's whole session/group, then reap it.

    pytest spawns child processes (e.g. git subprocesses from the project's own
    test suite). subprocess timeout only kills the direct child, leaving the
    grandchildren orphaned → reparented to PID 1 → zombies. Launching pytest with
    start_new_session=True puts it in its own process group so we can take the
    whole tree down here.
    """
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


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
    # 1. pytest already importable in the running interpreter → use it directly.
    if importlib.util.find_spec("pytest") is not None:
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
        summary=(f"pytest unavailable and could not be provisioned after "
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


# How much of a command's output `_run_node_cmd` retains. Nothing that
# identifies a failure is read from this tail: a repository gate's failure
# identities come from the structured report directory it retains
# (`_report_dir_failure_cases`), so this bound may be any size.
OUTPUT_TAIL_CHARS = 2000


def _run_node_cmd(pkg_dir: Path, args: list[str], timeout: int,
                  env_overrides: dict | None = None) -> dict:
    """Run one npm command in its own process group; kill the tree on timeout.

    `output` is a bounded TAIL, and `output_truncated` says so. The one thing
    NOT read from that tail is a gate's own declaration record: the whole text
    is scanned BEFORE the bound is applied, because a real gate's log (a
    GDScript compile plus a play-through) runs to tens of kilobytes, and a
    declaration that is only readable while the run stays short is not
    readable at all. `timed_out` / `runner_error` are the framework OBSERVING
    that nothing was measured — never inferred from `returncode`.
    """
    proc = None
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(pkg_dir), start_new_session=True,
            # `npm ci` runs whatever `postinstall` the generated package.json
            # names. With no env= it inherited everything this process holds.
            env=env_scrub.scrubbed_env(**(env_overrides or {})),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        out = ((stdout or "") + "\n" + (stderr or "")).strip()
        return {"passed": proc.returncode == 0,
                "returncode": proc.returncode,
                "output": out[-OUTPUT_TAIL_CHARS:],
                "output_truncated": len(out) > OUTPUT_TAIL_CHARS,
                "unmeasured_declaration": _unmeasured_declaration(out)}
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return {"passed": False, "returncode": -1, "timed_out": True,
                "output": f"timed out after {timeout}s: {' '.join(args)}"}
    except Exception as e:
        _kill_group(proc)
        return {"passed": False, "returncode": -1, "runner_error": True,
                "output": f"{type(e).__name__}: {e}"}


def _run_node_checks(repo: Path) -> dict | None:
    """npm install/build/test gate for the repo's node project (if any).

    Mirrors the pytest gate's skip semantics: no node project → None (no
    section in the report); npm binary unavailable → skipped=True (a missing
    runner must never masquerade as failing tests). Otherwise install deps,
    then run the build and test scripts that package.json actually declares —
    the BUILD is what catches compile-level breakage (e.g. Svelte template
    errors) that unit tests alone never see.
    """
    pkg_dir = _find_node_project(repo)
    if pkg_dir is None:
        return None

    node: dict = {"passed": True, "dir": str(pkg_dir.relative_to(repo)) or ".",
                  "checks": {}}

    if shutil.which("npm") is None:
        node.update(passed=True, skipped=True,
                    summary="npm not available — node gate skipped "
                            "(install nodejs+npm in the backend image).")
        return node

    try:
        scripts = json.loads(
            (pkg_dir / "package.json").read_text(encoding="utf-8")
        ).get("scripts", {})
    except Exception:
        scripts = {}

    # npm ci needs a lockfile; fall back to install without one.
    install_cmd = ["npm", "ci"] if (pkg_dir / "package-lock.json").exists() \
        else ["npm", "install"]
    node["checks"]["install"] = _run_node_cmd(pkg_dir, install_cmd, timeout=600)
    if node["checks"]["install"]["passed"]:
        if "build" in scripts:
            node["checks"]["build"] = _run_node_cmd(
                pkg_dir, ["npm", "run", "build"], timeout=300)
        if "test" in scripts:
            node["checks"]["test"] = _run_node_cmd(
                pkg_dir, ["npm", "test"], timeout=300)

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


def _run_repo_gate(repo: Path) -> dict | None:
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
    report_dir = datadir.aitelier_home() / "gate-reports" / ticket
    # Every engine request the gate makes goes through the admission relay,
    # which queues render requests in the harness's render queue and records
    # the engine's own answer to each (aitelier/gate_admission.py).
    upstream = os.environ.get("GODOT_BUILDER_URL") or gate_admission.DEFAULT_BUILDER_URL
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        relay = gate_admission.AdmissionRelay(
            upstream, render_wait_sec=gate_admission.render_wait_seconds(),
            upstream_timeout=REPO_GATE_TIMEOUT)
        relay_url = relay.start()
    except (OSError, ValueError) as e:
        return {"passed": False, "returncode": -1, "runner_error": True,
                "script": REPO_GATE_SCRIPT, "ticket": ticket,
                "output": f"repository gate not started: admission relay "
                          f"unavailable ({type(e).__name__}: {e})",
                "measured": REPO_GATE_UNMEASURED}
    try:
        result = _run_node_cmd(
            repo, ["bash", str(script)], REPO_GATE_TIMEOUT,
            env_overrides={"GODOT_BUILDER_URL": relay_url,
                           "GATE_REPORT_DIR": str(report_dir)})
    finally:
        relay.stop()
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
    result["admission"] = admission
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

# ── UNMEASURED is DECLARED, never inferred ─────────────────────────────────
#
# A repository opts in to saying "I did not run" by emitting one whole line:
#
#   AITELIER_REPO_GATE_UNMEASURED={"state":"blocked","reason":"..."}
#
# `state` must be one of `_REPO_GATE_UNMEASURED_STATES`. Nothing else in a
# gate's output produces that reading, and in particular an EXIT CODE never
# can. `2` is NOT "the engine never ran": the game repo's own gate uses it for
# `incomplete`, which includes contract files the implementer wrote wrong
# ("no authored scenarios found ... an empty one is not a pass"), and those
# are MEASURED failures that have to stay visible with a non-empty failures[].
_REPO_GATE_UNMEASURED_PREFIX = "AITELIER_REPO_GATE_UNMEASURED="
_REPO_GATE_UNMEASURED_STATES = frozenset({"unmeasured", "not_run", "blocked"})

REPO_GATE_MEASURED_PASS = "measured_pass"
REPO_GATE_MEASURED_FAIL = "measured_fail"
REPO_GATE_UNMEASURED = "unmeasured"

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
    """`measured_pass`, `measured_fail` or `unmeasured`, for ONE gate run.

    UNMEASURED has three sources and all are the framework OBSERVING the
    absence itself: the gate's own declaration, a run this harness killed or
    could not start (`timed_out` / `runner_error`), and a gate that exited
    neither 0 nor 1 after the engine did not answer its last request — the
    admission relay's record of the engine's own reply (`admission.state` in
    `gate_admission.NOT_RUN_STATES`: refused admission, unreachable, or cut off
    before its answer was delivered). `returncode` alone is never a source: 0
    is `measured_pass`, 1 is `measured_fail` whatever the relay saw, and any
    other code with no engine refusal behind it (a contract error, a crash)
    stays `measured_fail`. A timeout is the framework's own observation,
    exactly like the pytest wall whose `skipped_because="pytest_timeout"` this
    aligns with.
    """
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


def _acquire_repo_gate(repo: Path, run_gate=None, sleep=None) -> dict | None:
    """Run the repo gate, RE-ACQUIRING the verdict while it is unmeasured.

    A gate that did not run has decided nothing, so it may not spend the
    loop's implement cycle and it may not end the run: the tool asks the gate
    again instead. Only the verdict it finally gets is folded in. `attempts`
    stays on the returned dict so a reviewer can see that the reading cost two
    runs rather than one.
    """
    run_gate = _run_repo_gate if run_gate is None else run_gate
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


def _report_dir_failure_cases(ticket_dir: Path) -> tuple[list[dict], str | None] | None:
    """Failure identities from the structured report a gate retained, or None.

    The gate writes its report under the ticket directory it was handed as
    GATE_REPORT_DIR: one `manifest.json` whose `stages` name every stage it
    ran, a `<stage>.json` per stage, and a `<stage>-findings.json` list for the
    stages it judges itself (tools/godot_gate.py in the game repository). Each
    finding is one failure. A stage with no findings file contributes the
    `errors[]` of a stage report that says `passed: false`.

    A finding's identity is `<stage>/<first 12 hex of its sha256>`, so the
    same finding keeps the same key on the next run. None means the gate
    retained no report here and the caller falls back to the output records.
    """
    if not ticket_dir.is_dir():
        return None
    manifests = [p for p in [ticket_dir / "manifest.json",
                             *sorted(ticket_dir.glob("*/manifest.json"))]
                 if p.is_file()]
    if not manifests:
        return None
    if len(manifests) > 1:
        return [], (f"repository gate retained {len(manifests)} reports under "
                    f"one ticket ({ticket_dir.name})")
    report_dir = manifests[0].parent

    def load(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except (OSError, ValueError) as e:
            return None, f"repository gate report {path.name} is unreadable: {e}"

    manifest, error = load(manifests[0])
    if error:
        return [], error
    stages = (manifest or {}).get("stages") if isinstance(manifest, dict) else None
    if not isinstance(stages, dict):
        return [], "repository gate manifest names no stages"
    records: list[dict] = []
    seen: set[str] = set()
    for stage, state in stages.items():
        findings_path = report_dir / f"{stage}-findings.json"
        if findings_path.is_file():
            items, error = load(findings_path)
            if error:
                return [], error
            if not isinstance(items, list):
                return [], f"repository gate {findings_path.name} is not a list"
        else:
            name = (state or {}).get("report") if isinstance(state, dict) else None
            stage_report, error = load(report_dir / name) if name else (None, None)
            if error:
                return [], error
            if not isinstance(stage_report, dict) or stage_report.get("passed") is not False:
                continue
            items = stage_report.get("errors") or [
                stage_report.get("summary") or f"{stage} reported passed=false"]
        for item in items:
            text = _finding_text(item)
            case_id = f"{stage}/{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"
            if case_id in seen:
                continue
            seen.add(case_id)
            records.append({"case_id": case_id, "status": "failed",
                            "detail": text[:1000]})
    if not records:
        return [], (f"repository gate report {report_dir.name} names no "
                    f"failure")
    return records, None


def _repo_gate_failure_cases(gate: dict) -> tuple[list[dict], str | None]:
    """Read trustworthy per-case identities from one failed repository gate.

    Gate prose is not an identity protocol.  A repository opts in by emitting
    one JSON record per failed case as::

        AITELIER_REPO_GATE_CASE={"case_id":"compile/autoload","status":"failed","detail":"..."}

    A gate that retains a structured report under its ticket
    (`gate["report_dir"]`, see `_report_dir_failure_cases`) is read from that
    report and nothing else, whatever the length of its output.

    Otherwise the retained command output is bounded, so any truncation makes
    the set incomplete and unusable.  One malformed or duplicate record
    likewise invalidates the whole set instead of mixing reliable and
    script-wide keys.
    """
    if gate.get("report_dir"):
        from_report = _report_dir_failure_cases(Path(gate["report_dir"]))
        if from_report is not None:
            return from_report
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
        name = case.get("name") or ""
        if not name:
            continue
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
            continue
        out.add("::".join(parts))
    return out


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
                    executed: "Executed | None" = None) -> None:
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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"failures": keep, "updated_at": time.time()}, indent=2),
            encoding="utf-8")
    except OSError as e:
        report["baseline_error"] = f"could not write {path}: {e}"


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


def run_tests(*, project_root: str = "", out_dir: str = "",
              workspace_root: str = "", repo_gate: bool = True,
              state_dir: str = "", run_id: str = "",
              evidence_cycle_start: bool = False,
              evidence_cycle_from: str = "",
              fail_fast_gates: str = "",
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
    # Filled in as each leg runs; it is the ONLY licence to shrink the baseline.
    executed = Executed()

    if fail_fast_gates:
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
            target_dir.mkdir(parents=True, exist_ok=True)
            if run_id and evidence_cycle_from:
                stamp_report(report, run_id=run_id, out_dir=str(target_dir),
                             cycle_from=evidence_cycle_from)
            (target_dir / "test_report.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8")
            return {"written": "test_report.json", "passed": False,
                    "passed_relative": False, "new_failures": [],
                    "baseline_state": "unavailable", "baseline_known": 0,
                    "failure_count": len(report.get("failures") or []),
                    "release_evidence": release_disposition(report),
                    "skipped_because": report["skipped_because"],
                    "upstream_state": (blocker.get("upstream_state")
                                       or blocker.get("state"))}

    if not project_root or not Path(project_root).is_absolute():
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
        py, venv_dir = _resolve_pytest_python(repo, report)
        if py is None:
            pass  # runner unavailable → report already marked skipped/passed
        else:
            # Isolate: do NOT inherit PYTHONPATH from the host process — it may
            # point to AItelier's own source tree, causing pytest to discover
            # AItelier's tests instead of the project's.  Only the project root
            # belongs on the path.
            # Scrubbed, not raw. This runs LLM-authored pytest; passing the
            # server's whole environment handed it AITELIER_ADMIN_TOKEN and
            # every provider key, and whatever it printed went into
            # test_report.json (a public step output) and the reviewer's prompt.
            env = env_scrub.scrubbed_env(PYTHONPATH=_pythonpath_for(repo))
            # Cheapest possible check, and the one that would have caught two
            # failed drives on the same task: does the delivered package import?
            import_error = _import_smoke_error(py, repo, env)
            if import_error:
                report["import_error"] = import_error
            # start_new_session=True → pytest leads its own process group so we
            # can SIGKILL the whole tree (incl. git subprocesses it spawns) on
            # timeout or any error; otherwise those grandchildren leak as zombies.
            proc = None
            # Junit XML is how this gate learns which tests PASSED: `-q` prints
            # only failures, and "not in the failure list" is exactly the
            # not-run/not-collected confusion `Executed` exists to remove. It
            # is written OUTSIDE the repo so it can never be committed by a
            # later `repo_apply`.
            junit_dir = tempfile.mkdtemp(prefix="run_tests_junit_")
            junit_path = Path(junit_dir) / "junit.xml"
            try:
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
                proc = subprocess.Popen(
                    [py, "-m", "pytest", str(repo), "-q", "--tb=short",
                     "-p", "no:cacheprovider", "--continue-on-collection-errors",
                     "--rootdir", str(repo),
                     f"--junitxml={junit_path}", "-o", "junit_family=xunit1",
                     *_pytest_timeout_args(py)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    cwd=str(repo), env=env, start_new_session=True,
                )
                # Outer wall: this runs on the scheduler loop-thread (under the
                # per-project tick lock), so a true hang must not block forever.
                # It is a HANG detector, not a speed limit — see
                # PYTEST_WALL_SECONDS for what happened when it was both.
                stdout, stderr = proc.communicate(
                    timeout=PYTEST_WALL_SECONDS)
                out = ((stdout or "") + "\n" + (stderr or "")).strip()
                report["returncode"] = proc.returncode
                # 0 and 1 are the two outcomes in which pytest ran a session to
                # the end. 2 (usage/internal), 3 (interrupted), 4 (usage) and 5
                # (nothing collected) all mean the suite was not exercised, and
                # a baseline key must not be pruned off one of them.
                executed = Executed(
                    node_ids=_junit_node_ids(junit_path),
                    pytest_complete=proc.returncode in (0, 1))
                # pytest: 0=all passed, 1=failures, 5=NOTHING COLLECTED.
                # 5 is a third outcome — no evidence — and it used to be routed
                # forward as a pass: on the `autopep8` benchmark task the
                # implementer never delivered the test directory, the gate found
                # nothing, said `passed: true`, 5_review passed on that basis and
                # a 0.128-scoring repo shipped as `completed`. A gate that
                # checked nothing has not passed; it is only "not applicable"
                # when the repo holds no Python code at all (a node or Godot
                # project, gated by its own section below).
                report["passed"] = proc.returncode == 0
                if proc.returncode == 5:
                    report["no_tests_collected"] = True
                    report["passed"] = not _has_python_sources(repo)
                collection_errors = _collection_errors(out)[:50]
                report["collection_errors"] = collection_errors
                failed_lines = [ln.strip() for ln in out.splitlines()
                                if ln.startswith("FAILED") or " FAILED " in ln][:50]
                report["failures"] = collection_errors + failed_lines
                if proc.returncode == 5 and not report["passed"]:
                    report["failures"].append(
                        "No tests were collected — the gate verified NOTHING.")
                    report["summary"] = (
                        "No tests were collected, so nothing about this project "
                        "was verified. This is NOT a pass: the suite is missing "
                        "(or unreachable by pytest). Deliver tests under `tests/` "
                        "covering the MVP goals and re-run.")
                elif proc.returncode == 5:
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
                report["summary"] = _explain_missing_names(
                    py, _lead_with_install_error(report))
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                # A timeout is ABSENT evidence, not a red suite. `passed=False`
                # alone is indistinguishable from "ran and failed", and with an
                # empty failures[] plus a baseline diff of nothing it classified
                # as `known_failure` / `passed_relative: true` — a verdict about
                # tests that never ran. `skipped_because` routes it through
                # gate_evidence.report_state -> "skipped" -> "unresolved", which
                # is the state that means "go and verify", and the failures entry
                # keeps the reason readable to the agent that has to act on it.
                report.update(
                    passed=False, timed_out=True,
                    skipped_because="pytest_timeout",
                    pytest_wall_seconds=PYTEST_WALL_SECONDS,
                    summary=(
                        f"pytest did not finish within {PYTEST_WALL_SECONDS}s and was "
                        "killed, so NOTHING was measured. This is not a test failure: "
                        "no test result exists either way. Either the suite needs "
                        "longer than this harness allows, or it hangs."))
                report["failures"].append(
                    f"pytest:timed out after {PYTEST_WALL_SECONDS}s — no results "
                    "collected, the suite was killed mid-run")
            except Exception as e:  # never raise — the step must not fail
                _kill_group(proc)
                report.update(passed=False, summary=f"Error running pytest: {e}")
            finally:
                # Belt-and-suspenders: even on the success path pytest may leave
                # stray children — take the group down before cleaning up.
                if proc is not None:
                    _kill_group(proc)
                shutil.rmtree(junit_dir, ignore_errors=True)
                if venv_dir:
                    shutil.rmtree(venv_dir, ignore_errors=True)

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
        node = _run_node_checks(repo)
        if node is not None:
            report["node"] = node
            executed.node_gate_ran = not node.get("skipped")
            if node.get("skipped"):
                report.update(passed=False, skipped=True,
                              infrastructure_unavailable=True,
                              evidence_state="infrastructure_unavailable")
                report["failures"].append(
                    "node gate unavailable: " + node.get("summary", ""))
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
    if repo_gate and repo is not None and repo.exists():
        gate = _acquire_repo_gate(repo)
        if gate is not None:
            report["repo_gate"] = gate
            outcome = gate["measured"]
            # The engine's reply to the gate's last request, as the admission
            # relay recorded it (`answered`, `not_admitted`, `unreachable`, ...).
            report["repo_gate_admission"] = (gate.get("admission") or {}).get("state")
            if outcome == REPO_GATE_UNMEASURED:
                # Not run is not a product failure, so release routing may not
                # read it as one (`gate_evidence.report_state` maps `not_run`
                # to `skipped`, never `failed`). Only when nothing else in this
                # report is red: a measured red elsewhere stays a measured red.
                if not report["failures"]:
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
                # `repo_gate_unmeasured` because that one is also set for a
                # gate that never existed, and only a gate that RAN and
                # stayed silent is an absence to wait on.
                report["repo_gate_absent"] = True
                report["failures"].append(
                    f"repo_gate:{gate['script']} was NOT measured "
                    f"(engine admission: {report['repo_gate_admission']}; "
                    f"{gate.get('attempts', 1)} attempt(s)): "
                    f"{gate['output'][-1500:]}")
            elif outcome == REPO_GATE_MEASURED_PASS:
                # The gate ran and reported nothing failed: every case it
                # knows about passed, so a known-red case of its own may be
                # pruned. A gate that did not run at all (no run_tests.sh,
                # `repo_gate: false`) leaves this None and its keys stay
                # known-red.
                executed.repo_gate_cases = set()
            else:
                report["passed"] = False
                cases, identity_error = _repo_gate_failure_cases(gate)
                if identity_error is not None:
                    # A gate whose case identities cannot be read has not told
                    # us which cases ran; nothing of its may be pruned.
                    executed.repo_gate_cases = None
                    gate["failure_identity_error"] = identity_error
                    report["failure_identity_error"] = identity_error
                    report["failures"].append(
                        f"repo_gate:{gate['script']} failed "
                        f"(rc={gate['returncode']}): "
                        f"{gate['output'][-1500:]}")
                else:
                    gate["failure_cases"] = cases
                    executed.repo_gate_cases = {c["case_id"] for c in cases}
                    for case in cases:
                        detail = case["detail"] or "reported failed"
                        report["failures"].append(
                            f"repo_gate:{gate['script']}#{case['case_id']} "
                            f"failed: {detail}")


    # Known-red baseline: `new_failures[]` + `passed_relative` + the
    # `baseline_state` that says whether either is a measurement, on top of the
    # absolute `passed`, so a reader can tell this round's breakage from the
    # repo's standing red — and tell both from "no baseline was taken".
    _apply_baseline(report, _baseline_dir(state_dir, repo), executed)

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
    target_dir.mkdir(parents=True, exist_ok=True)
    if run_id and (evidence_cycle_start or evidence_cycle_from):
        from aitelier.gate_evidence import stamp_report
        stamp_report(report, run_id=run_id, out_dir=str(target_dir),
                     start_cycle=evidence_cycle_start,
                     cycle_from=evidence_cycle_from)
    (target_dir / "test_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "test_report.json", "passed": report["passed"],
            "release_evidence": release_disposition(report),
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
            "passed_relative": report["passed_relative"],
            # Carried in the RETURN, not only the report, because the terminal
            # failure reason is assembled from what the run left behind: a loop
            # that dies on `Cycle limit exceeded` has to be able to say which
            # reds it kept looping on and whether they were its own.
            "baseline_state": report["baseline_state"],
            "baseline_known": len(report["baseline_failures"]),
            "failure_count": len(report["failures"]),
            "new_failures": report["new_failures"]}
