"""Which callable a registered tool name dispatches to, and who supplies its identity.

AItelier builds the ONE ``ToolLoader`` the engine uses
(``api.dependencies.get_tool_loader``), and every SkillFlow dispatch of a
registered name resolves through ``load_fn``: the agent tool path, inline tool
steps, lifecycle hooks, step validation and ``{source: {tool: X}}`` context
sources. Only the agent path crosses the host override in
``core.skillflow_host``; the other four call the function directly. A route here
is therefore the one place a host decision reaches every dispatch of a name.

SkillFlow's native ``pytest`` tool runs repository tests in
the caller's own process with the caller's environment, so it is served by
``native_pytest`` below, which sends the same command to the owned disposable
CPU executor (``core.repository_executor``) used by ``run_tests`` and
``focused_check``. Native ``lint`` retains its engine dispatch but rejects an
empty result set rather than treating all([]) as a measured pass.

The route is applied in ``load_fn`` on every call, AFTER the base loader's
cache. That ordering is the point: ``ToolLoader.add_tools_dir`` (which
``register_tool`` and the MCP ``edit_tool`` reload call) clears the cache, so a
``register_dynamic_tool`` override would be erased by the next reload and the
native local pytest would silently return. A route applies whenever the loader
classifies the name as native (its first, engine-owned directory, or a dynamic
registration with no directory), so neither a cached callable nor a dynamic
re-registration of the same name can displace it.
"""
from __future__ import annotations

import hashlib
from functools import wraps
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

from skillflow.tool_loader import ToolLoader

#: Identity the host injects into every tool dispatch. SkillFlow fills these
#: with ``setdefault`` AFTER a graph's literal params, so a literal one in a
#: dispatch slot would displace the run's real identity.
HOST_IDENTITY_KEYWORDS: tuple[str, ...] = ("run_id", "step_id", "project_id",
                                           "operation_id")

#: Tools that hand ``run_id`` and a repository root to the owned executor. Their
#: caller-supplied identity and roots are replaced by the host's on the agent
#: path, and a literal root in a graph slot must be resolver syntax.
EXECUTOR_TOOLS: frozenset[str] = frozenset({"focused_check", "run_tests", "pytest"})
ROOT_KEYWORDS: tuple[str, ...] = ("project_root", "workspace_root")

# Keys of a validation spec the validator consumes itself; every other key is
# passed to the tool as a keyword argument.
_VALIDATION_RESERVED = frozenset({"files", "file", "tool", "on_failure", "max_retries"})
_RESOLVER_REFERENCE = re.compile(r"\$[A-Z][A-Z0-9_]*(?:/[^\0]*)?")

_NATIVE_PYTEST_TIMEOUT = 60
_FEEDBACK_LIMIT = 4000
_JUNIT_MAX_BYTES = 16 * 1024 * 1024


def _resolver_reference(value: Any) -> bool:
    if value == "":
        return True                         # "no opinion"; the engine drops it
    return (isinstance(value, str) and _RESOLVER_REFERENCE.fullmatch(value) is not None
            and ".." not in Path(value).parts)


def spoofed_slot_keys(tool_name: str, params: Any) -> list[str]:
    """The keys of ONE dispatch slot's literal params that claim host identity.

    Only the slot's top level is examined: that is what SkillFlow merges with
    its injected identity. Nested business payloads are not identity.
    """
    if not isinstance(params, dict):
        return []
    bad = [key for key in HOST_IDENTITY_KEYWORDS if key in params]
    if tool_name in EXECUTOR_TOOLS:
        bad += [key for key in ROOT_KEYWORDS
                if key in params and not _resolver_reference(params[key])]
    return bad


def graph_identity_violations(graph) -> list[str]:
    """Every dispatch slot in ``graph`` whose literal params claim host identity."""
    problems: list[str] = []

    def check(where: str, tool: Any, params: Any) -> None:
        bad = spoofed_slot_keys(str(tool or ""), params)
        if bad:
            problems.append(
                f"{where} (tool {tool!r}) sets host-owned {', '.join(bad)}; the "
                f"engine injects the run's own identity, and a literal here would "
                f"displace it. Roots of {sorted(EXECUTOR_TOOLS)} must be resolver "
                f"references such as $PROJECT_ROOT.")

    def check_validation(where: str, specs: Any) -> None:
        for index, spec in enumerate(specs if isinstance(specs, list) else []):
            if isinstance(spec, dict):
                check(f"{where}[{index}]", spec.get("tool"),
                      {k: v for k, v in spec.items() if k not in _VALIDATION_RESERVED})

    for step in getattr(graph, "steps", None) or []:
        sid = getattr(step, "id", "?")
        if getattr(step, "tool_name", None):
            check(f"step {sid!r} tool_params", step.tool_name,
                  getattr(step, "tool_params", None) or {})
        check_validation(f"step {sid!r} validation", getattr(step, "validation", None))
        lifecycle = getattr(step, "lifecycle", None) or {}
        for hook_name, spec in (lifecycle.items() if isinstance(lifecycle, dict) else []):
            where = f"step {sid!r} lifecycle {hook_name!r}"
            if isinstance(spec, list) and hook_name == "after_deliver":
                check_validation(where, spec)
            elif isinstance(spec, list):
                for index, item in enumerate(spec):
                    if isinstance(item, dict):
                        check(f"{where}[{index}]", item.get("tool"), item.get("params"))
            elif isinstance(spec, dict):
                check(where, spec.get("tool"), spec.get("params"))
    return problems


# ── native pytest, served by the owned executor ─────────────────────────────

def _result(verdict: str, feedback: str, **extra) -> dict:
    """The native tool's ``verdict``/``feedback`` shape, plus an explicit
    ``passed``. Both readers that interpret a tool result (the validator's
    ``_add_issues`` and the lifecycle-hook path) look at ``passed`` first, and
    the hook path counts a result with neither ``passed`` nor ``error`` as a
    PASS — so the native ``{"verdict": "failed"}`` passed every hook."""
    out = {"verdict": verdict, "passed": verdict == "passed",
           "feedback": feedback[-_FEEDBACK_LIMIT:]}
    out.update(extra)
    if verdict != "passed":
        out.setdefault("error", out["feedback"])
    return out


def _junit_counts(path: Path) -> dict | None:
    """Summed testsuite counts from pytest's JUnit file, or None if unusable."""
    try:
        if not path.is_file() or path.stat().st_size > _JUNIT_MAX_BYTES:
            return None
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return None
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    try:
        for suite in suites:
            for key in counts:
                counts[key] += int(suite.attrib.get(key, 0) or 0)
    except ValueError:
        return None
    counts["passed"] = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return counts


def native_pytest(file: str, *, workspace_root: str = "", project_root: str = "",
                  run_id: str = "") -> dict:
    """SkillFlow's ``pytest`` tool, measured in the owned disposable executor.

    Same parameters and ``verdict``/``feedback`` result as the native tool, plus
    the run identity the executor binds against and ``project_root``. The
    executor's launcher binds a run to the repository it OWNS, so the root is
    the code root the engine decided for the run (``project_root``, which the
    engine injects on the agent, tool-step and hook routes); ``workspace_root``
    is used only where the engine supplies no code root. On the agent route the
    two are the same value. Deliberately NOT the native
    semantics where those let nothing pass for something:

    * no run identity, a missing/escaping file or a non-Python target is a
      refusal, never ``passed`` (the native tool passed any non-``.py`` file);
    * exit 5 (nothing collected) is unmeasured, not a pass;
    * exit 0 passes only when the JUnit record shows at least one test that
      ran and was not skipped: an all-skipped session measured nothing;
    * an unavailable executor or a timeout is unmeasured, with no exit code
      invented and no local fallback.

    Cancellation propagates; the executor's own cleanup removes its container.
    """
    from core import datadir, repository_executor

    if not isinstance(run_id, str) or not run_id.strip():
        return _result("failed", "pytest: no run identity reached this dispatch, so "
                       "the owned executor cannot bind it to a repository; nothing "
                       "was measured", measured=False, unmeasured_reason="no_run_identity")
    base = project_root or workspace_root
    if not isinstance(base, str) or not Path(base).is_absolute():
        return _result("failed", f"pytest: no absolute repository root reached this "
                       f"dispatch (got {base!r}); nothing was measured", measured=False,
                       unmeasured_reason="no_root")
    if not isinstance(file, str) or not file or file.startswith("-"):
        return _result("failed", f"pytest: {file!r} is not a test file path",
                       measured=False, unmeasured_reason="bad_target")
    root = Path(base).resolve()
    target = (root / file).resolve()
    if target != root and root not in target.parents:
        return _result("failed", f"pytest: {file!r} escapes {root}", measured=False,
                       unmeasured_reason="bad_target")
    if not target.is_file():
        return _result("failed", f"File not found: {file}", measured=False,
                       unmeasured_reason="bad_target")
    if target.suffix != ".py":
        return _result("failed", f"pytest: {file} is not a Python test file; nothing "
                       f"was measured", measured=False, unmeasured_reason="not_python")

    tickets = datadir.aitelier_home() / "gate-reports" / hashlib.sha256(run_id.encode()).hexdigest()
    tickets.mkdir(parents=True, exist_ok=True)
    ticket = Path(tempfile.mkdtemp(prefix="native_pytest_", dir=tickets))
    junit = ticket / "junit.xml"
    relative = str(target.relative_to(root))
    command = ["python3", "-m", "pytest", relative, "-q", "--tb=short",
               "-p", "no:cacheprovider", "--rootdir", str(root), f"--junitxml={junit}"]
    try:
        done = repository_executor.execute(root, command, _NATIVE_PYTEST_TIMEOUT,
                                           writable_dirs=[ticket], run_id=run_id)
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or b""
        partial = partial.decode(errors="replace") if isinstance(partial, bytes) else partial
        return _result("failed", f"pytest timed out after {_NATIVE_PYTEST_TIMEOUT}s; "
                       f"nothing was measured\n{partial}", measured=False,
                       unmeasured_reason="timeout", timed_out=True, returncode=None)
    except (repository_executor.IsolationUnavailable, OSError) as exc:
        return _result("failed", f"pytest: isolated CPU executor unavailable: {exc}; "
                       f"nothing was measured", measured=False,
                       unmeasured_reason="executor_unavailable",
                       infrastructure_unavailable=True, returncode=None)
    output = ((done.stdout or "") + "\n" + (done.stderr or "")).strip()
    counts = _junit_counts(junit)
    extra = {"returncode": done.returncode, "counts": counts, "junit_path": str(junit)}
    if done.returncode == 5:
        return _result("failed", "pytest collected no tests; nothing was measured\n"
                       + output, measured=False, unmeasured_reason="no_tests", **extra)
    if done.returncode not in (0, 1):
        return _result("failed", f"pytest did not complete a session (exit "
                       f"{done.returncode})\n{output}", measured=False,
                       unmeasured_reason="session_incomplete", **extra)
    if counts is None:
        return _result("failed", "pytest left no readable JUnit record, so what ran "
                       "is unknown\n" + output, measured=False,
                       unmeasured_reason="no_record", **extra)
    if done.returncode == 1 or counts["failures"] or counts["errors"]:
        return _result("failed", output or "pytest reported failures", measured=True, **extra)
    if counts["passed"] <= 0:
        return _result("failed", "every collected test was skipped; nothing was "
                       "measured\n" + output, measured=False,
                       unmeasured_reason="all_skipped", **extra)
    return _result("passed", "", measured=True, **extra)


#: name -> host callable that serves it when the name resolves to the engine's
#: native tools directory. The one table; a later host route extends it.
NATIVE_ROUTES: dict[str, Callable[..., dict]] = {"pytest": native_pytest}


class AItelierToolLoader(ToolLoader):
    """The engine's loader, with host routes applied on every ``load_fn``."""

    def load_fn(self, name: str) -> Callable:
        fn = super().load_fn(name)
        route = NATIVE_ROUTES.get(name)
        if route is not None and self.is_native(name):
            return route
        if name == "lint" and self.is_native(name):
            @wraps(fn)
            def measured_lint(*args, **kwargs):
                result = fn(*args, **kwargs)
                if not result.get("results"):
                    return {**result, "all_passed": False, "unmeasured": True,
                            "error": "lint matched no files; no lint measurement was made"}
                return result
            return measured_lint
        return fn
