"""Two bounded implementation probes, with no general command surface.

``coding_impl`` needs feedback before it commits its step, but giving its model a
shell would also give it every executable visible to the backend.  This tool
therefore exposes only pytest node IDs and one existing Godot scenario.  The
host owns ``project_root``; ``PipelineEngine`` strips an agent-supplied root
before SkillFlow injects the isolated run worktree.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from core import env_scrub
from aitelier.tools.godot_playtest_scenario.impl import (
    godot_playtest_scenario as _run_godot_scenario,
)


_OUTPUT_LIMIT_BYTES = 12_000
_MAX_TIMEOUT_SECONDS = 300
_MAX_TARGETS = 8
_MAX_TARGET_CHARS = 300
_SCENARIO = re.compile(r"^[A-Za-z0-9_. -]{1,120}$")
_FORBIDDEN_ARGS = {"command", "cwd", "env", "executable", "host", "shell", "container"}
_PROCESS_START_METHOD = "subprocess"

_GODOT_WORKER = r"""
import json, os, sys
from core import env_scrub
from aitelier.tools.godot_playtest_scenario.impl import godot_playtest_scenario
kwargs = json.loads(sys.stdin.read())
safe = env_scrub.scrubbed_env()
os.environ.clear(); os.environ.update(safe)
result = godot_playtest_scenario(**kwargs)
raw = json.dumps(result, ensure_ascii=False, sort_keys=True).encode('utf-8')
limit = 12000
print(json.dumps({
    'hard_passed': bool(result.get('hard_passed')),
    'all_passed': bool(result.get('all_passed')),
    'timed_out': bool(result.get('timed_out')),
    'output': raw[:limit].decode('utf-8', errors='ignore'),
    'output_bytes': len(raw),
    'output_truncated': len(raw) > limit,
}, ensure_ascii=False))
"""


def _root(project_root: str) -> Path:
    if not project_root or not Path(project_root).is_absolute():
        raise ValueError("focused_check requires an injected absolute project_root")
    root = Path(project_root).resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ValueError("focused_check project_root is not an owned Git worktree")
    return root


def _timeout(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("timeout_seconds must be an integer")
    if not 1 <= value <= _MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must be between 1 and {_MAX_TIMEOUT_SECONDS}"
        )
    return value


def _pytest_targets(root: Path, targets: list[str] | None) -> list[str]:
    if not isinstance(targets, list) or not 1 <= len(targets) <= _MAX_TARGETS:
        raise ValueError(f"pytest requires 1-{_MAX_TARGETS} focused targets")
    checked: list[str] = []
    for target in targets:
        if not isinstance(target, str) or not target or len(target) > _MAX_TARGET_CHARS:
            raise ValueError("pytest targets must be short non-empty strings")
        if target.startswith("-"):
            raise ValueError("pytest target options are unsupported")
        file_part = target.split("::", 1)[0]
        path = Path(file_part)
        if path.is_absolute():
            raise ValueError("pytest targets must be relative to the attempt worktree")
        candidate = (root / path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError("pytest target escapes the attempt worktree")
        if not candidate.exists():
            raise ValueError("pytest target does not exist in the attempt worktree")
        checked.append(target)
    return checked


def _clip(data: bytes) -> tuple[str, int, bool]:
    size = len(data)
    clipped = data[:_OUTPUT_LIMIT_BYTES]
    return clipped.decode("utf-8", errors="ignore"), size, size > len(clipped)


def _base(kind: str, root: Path, timeout: int, command: list[str], *,
          run_id: str, step_id: str) -> dict:
    return {
        "kind": kind,
        "command": command,
        "cwd": str(root),
        "timeout_seconds": timeout,
        "output_limit_bytes": _OUTPUT_LIMIT_BYTES,
        "run_id": str(run_id),
        "step_id": str(step_id),
        "attempt_scope": f"{run_id}:{step_id}",
    }


def _pytest(root: Path, targets: list[str], timeout: int, *,
            run_id: str, step_id: str) -> dict:
    command = [
        sys.executable, "-m", "pytest", "-q", "-s", "--maxfail=1",
        "-p", "no:cacheprovider", "--rootdir", str(root), *targets,
    ]
    base = _base("pytest", root, timeout, command,
                 run_id=run_id, step_id=step_id)
    timed_out = False
    exit_status = -1
    kept = bytearray()
    output_bytes = 0
    output_lock = threading.Lock()

    def drain(stream) -> None:
        nonlocal output_bytes
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                with output_lock:
                    output_bytes += len(chunk)
                    remaining = _OUTPUT_LIMIT_BYTES - len(kept)
                    if remaining > 0:
                        kept.extend(chunk[:remaining])
        except OSError:
            return

    try:
        process = subprocess.Popen(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env_scrub.scrubbed_env(
                PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1",
                PYTEST_ADDOPTS="",
            ),
            start_new_session=True,
        )
        reader = threading.Thread(target=drain, args=(process.stdout,), daemon=True)
        reader.start()
        try:
            process.wait(timeout=timeout)
            exit_status = int(process.returncode)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            exit_status = -9
        # A test must not detach a descendant and leave it running after the
        # focused probe itself exits.  The group may already be gone.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        reader.join(timeout=2)
        if reader.is_alive() and process.stdout is not None:
            process.stdout.close()
            reader.join(timeout=1)
    except OSError as exc:
        message = f"{type(exc).__name__}: pytest could not start".encode()
        kept.extend(message[:_OUTPUT_LIMIT_BYTES])
        output_bytes = len(message)
    with output_lock:
        output, _, _ = _clip(bytes(kept))
        truncated = output_bytes > len(kept)
    return {
        **base,
        "passed": exit_status == 0 and not timed_out,
        "exit_status": exit_status,
        "timed_out": timed_out,
        "output": output,
        "output_bytes": output_bytes,
        "output_truncated": truncated,
    }


def _godot_child(send, kwargs: dict) -> None:
    """Run the HTTP adapter in a killable process and return a bounded result."""
    try:
        safe_env = env_scrub.scrubbed_env()
        os.environ.clear()
        os.environ.update(safe_env)
        result = _run_godot_scenario(**kwargs)
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
        output, output_bytes, truncated = _clip(raw)
        send.send({
            "hard_passed": bool(result.get("hard_passed")),
            "all_passed": bool(result.get("all_passed")),
            "timed_out": bool(result.get("timed_out")),
            "output": output,
            "output_bytes": output_bytes,
            "output_truncated": truncated,
        })
    except BaseException as exc:
        message = f"{type(exc).__name__}: Godot focused check failed"
        try:
            send.send({"error": message, "output": message,
                       "output_bytes": len(message.encode()),
                       "output_truncated": False})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send.close()


def _godot(root: Path, scenario: str, timeout: int, *,
           run_id: str, step_id: str, project_id: str, operation_id: str) -> dict:
    if not isinstance(scenario, str) or not _SCENARIO.fullmatch(scenario):
        raise ValueError("godot_scenario requires one short scenario name")
    kwargs = dict(
        scenario=scenario,
        project_root=str(root),
        run_id=run_id,
        step_id=step_id,
        project_id=project_id,
        operation_id=operation_id,
        _timeout_seconds=timeout,
    )
    # A socket timeout is an inactivity bound, not a total deadline: a peer can
    # stream one byte before each socket timeout forever. Isolate the complete
    # adapter call in a process so the wall clock can stop both reads and any
    # late local mutation after the result has been returned.
    if _PROCESS_START_METHOD == "subprocess":
        process = subprocess.Popen(
            [sys.executable, "-c", _GODOT_WORKER], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env_scrub.scrubbed_env(), start_new_session=True,
        )
        deadline_expired = False
        try:
            stdout, _ = process.communicate(
                json.dumps(kwargs, ensure_ascii=False).encode("utf-8"), timeout=timeout
            )
        except subprocess.TimeoutExpired:
            deadline_expired = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, _ = process.communicate()
        try:
            result = json.loads(stdout) if stdout else None
        except json.JSONDecodeError:
            result = None
        return _godot_result(root, scenario, timeout, run_id, step_id,
                             result, deadline_expired)

    context = multiprocessing.get_context(_PROCESS_START_METHOD)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_godot_child, args=(send, kwargs), daemon=True)
    process.start()
    send.close()
    result = None
    deadline_expired = False
    try:
        if receive.poll(timeout):
            try:
                result = receive.recv()
            except EOFError:
                result = None
        else:
            deadline_expired = True
            process.kill()
        process.join(timeout=2)
        if process.is_alive():
            process.kill()
            process.join()
    finally:
        receive.close()
    return _godot_result(root, scenario, timeout, run_id, step_id,
                         result, deadline_expired)


def _godot_result(root: Path, scenario: str, timeout: int,
                  run_id: str, step_id: str, result: dict | None,
                  deadline_expired: bool) -> dict:
    timed_out = deadline_expired
    if result is None:
        message = ("Godot focused check exceeded its wall-clock deadline"
                   if deadline_expired else "Godot focused check worker exited without a result")
        result = {"output": message, "output_bytes": len(message.encode("utf-8")),
                  "output_truncated": False}
    passed = bool(result.get("hard_passed") and result.get("all_passed"))
    return {
        **_base("godot_scenario", root, timeout,
                ["godot_playtest_scenario", scenario],
                run_id=run_id, step_id=step_id),
        "passed": passed,
        "exit_status": 0 if passed else (-9 if timed_out else 1),
        "timed_out": timed_out,
        "output": result["output"],
        "output_bytes": result["output_bytes"],
        "output_truncated": result["output_truncated"],
    }


def focused_check(*, kind: str = "", targets: list[str] | None = None,
                  scenario: str = "", timeout_seconds: int = 120,
                  project_root: str = "", run_id: str = "", step_id: str = "",
                  project_id: str = "", operation_id: str = "", **kwargs) -> dict:
    """Run one focused probe against the host-injected attempt worktree."""
    try:
        forbidden = sorted(_FORBIDDEN_ARGS.intersection(kwargs))
        if forbidden:
            raise ValueError("unsupported host/control arguments")
        root = _root(project_root)
        timeout = _timeout(timeout_seconds)
        if kind == "pytest":
            if scenario:
                raise ValueError("pytest does not accept a Godot scenario")
            return _pytest(root, _pytest_targets(root, targets), timeout,
                           run_id=run_id, step_id=step_id)
        if kind == "godot_scenario":
            if targets:
                raise ValueError("godot_scenario does not accept pytest targets")
            return _godot(root, scenario, timeout, run_id=run_id, step_id=step_id,
                          project_id=project_id, operation_id=operation_id)
        raise ValueError("unsupported check kind; use pytest or godot_scenario")
    except (TypeError, ValueError) as exc:
        return {"error": str(exc), "passed": False}
