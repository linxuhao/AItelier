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
If the runner cannot be provisioned at all (e.g. no network), the gate is
SKIPPED (passed=True) — a missing test runner must never masquerade as failing
tests, which would spin the goal-loop chasing a phantom failure.

Three outcomes, not two: pass, fail, and NO EVIDENCE. "pytest collected nothing"
(exit 5) is the third, and it is not a pass — see the returncode handling in
`run_tests` — unless the repo holds no Python at all, in which case the node /
compile sections are the real gate.
"""

import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess

from core import env_scrub
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
    step output, so `site-packages` is unreachable by design, and it guessed the
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

    # All attempts failed → a persistent outage. SKIP (a missing runner must
    # never masquerade as failing tests, which would spin the goal-loop).
    report.update(
        passed=True, skipped=True, returncode=0,
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


def _run_node_cmd(pkg_dir: Path, args: list[str], timeout: int) -> dict:
    """Run one npm command in its own process group; kill the tree on timeout."""
    proc = None
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(pkg_dir), start_new_session=True,
            # `npm ci` runs whatever `postinstall` the generated package.json
            # names. With no env= it inherited everything this process holds.
            env=env_scrub.scrubbed_env(),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
        out = ((stdout or "") + "\n" + (stderr or "")).strip()
        return {"passed": proc.returncode == 0,
                "returncode": proc.returncode, "output": out[-2000:]}
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        return {"passed": False, "returncode": -1,
                "output": f"timed out after {timeout}s: {' '.join(args)}"}
    except Exception as e:
        _kill_group(proc)
        return {"passed": False, "returncode": -1,
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


def run_tests(*, project_root: str = "", out_dir: str = "",
              workspace_root: str = "", **kwargs) -> dict:
    """Run pytest over the consolidated repo; write test_report.json to out_dir.

    Returns {written, passed}. The report holds {passed, returncode, summary,
    failures[], collection_errors[], skipped?} for the reviewer to read, plus a
    ``node`` section (npm install/build/test) when the repo contains a node
    project.
    """
    root = project_root or workspace_root
    report = {"passed": True, "returncode": 0, "summary": "", "failures": [],
              "collection_errors": []}

    if not root or not Path(root).is_absolute():
        # `Path("").resolve()` is the process CWD — for the hosted engine, the
        # AItelier checkout itself, which IS a git repo with its own test suite.
        # Nothing downstream refuses it (`repo.exists()` is true), so pytest ran
        # over the server's ~2000 tests and `test_report.json` handed the
        # reviewer a report about AItelier.
        #
        # Reachable because a run that declares no code repository has no
        # project root to give: the engine omits the argument (and older engines
        # forward ""), and `tool_creation` grants this tool to pipeline_forge's
        # `t_tool_impl`. Refusing is the only correct answer — there is no repo
        # to test — and it is written HERE, in the tool, rather than only in the
        # engine, because the engine that calls it may be any released version.
        report.update(
            passed=False,
            summary=f"run_tests: no project repository to test — project_root "
                    f"must be an absolute path (got {root!r}); refusing to "
                    f"resolve against the process CWD")
        repo = None
    else:
        repo = Path(root).resolve()

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
                     "--rootdir", str(repo), *_pytest_timeout_args(py)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    cwd=str(repo), env=env, start_new_session=True,
                )
                # Outer wall kept tight: this runs on the scheduler loop-thread
                # (under the per-project tick lock), so a long hang would stall
                # the whole run. A genuinely-passing suite finishes well under
                # this; an import/collection hang (not caught by pytest-timeout)
                # fails fast instead of blocking for minutes.
                stdout, stderr = proc.communicate(timeout=75)
                out = ((stdout or "") + "\n" + (stderr or "")).strip()
                report["returncode"] = proc.returncode
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
                report.update(passed=False, summary="pytest timed out after 75s")
            except Exception as e:  # never raise — the step must not fail
                _kill_group(proc)
                report.update(passed=False, summary=f"Error running pytest: {e}")
            finally:
                # Belt-and-suspenders: even on the success path pytest may leave
                # stray children — take the group down before cleaning up.
                if proc is not None:
                    _kill_group(proc)
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
            if not node["passed"]:
                report["passed"] = False
                for name, chk in node["checks"].items():
                    if not chk["passed"]:
                        report["failures"].append(
                            f"node:{name} failed (rc={chk['returncode']}): "
                            f"{chk['output'][-500:]}")

    # `out_dir` is the step's staging dir and is always supplied by the engine.
    # With no repo AND no out_dir there is nowhere to write — say so in the
    # return rather than defaulting to the CWD, which is the whole point above.
    if not out_dir and repo is None:
        return {"written": None, "passed": False, "error": report["summary"]}
    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "test_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "test_report.json", "passed": report["passed"]}
