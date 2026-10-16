"""Owned syntax-only JavaScript lint, registered in SkillFlow's lint registry."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile

from core import datadir, repository_executor


LINT_TIMEOUT = 120


def eslint_backend(fp: Path, *, run_id: str = "", step_id: str = "",
                   project_root: str = "") -> dict:
    """Parse one JS/module/CommonJS input through the owned host launcher.

    Identity is supplied explicitly by the engine. The launcher checks its
    existing run binding; the run hash below only locates the private ticket.
    No project config, plugin, npm installer or inline rule is evaluated.
    """
    result = {"file": str(fp), "passed": False, "error_message": ""}
    try:
        if not all(isinstance(v, str) and v for v in (run_id, step_id, project_root)):
            raise repository_executor.IsolationUnavailable(
                "JavaScript lint requires explicit run_id, step_id and project_root")
        root = Path(project_root)
        target = Path(fp)
        if (not root.is_absolute() or os.path.normpath(project_root) != project_root
                or root.resolve(strict=True) != root or not target.is_absolute()):
            raise repository_executor.IsolationUnavailable("lint roots/targets must be canonical absolute paths")
        relative = target.relative_to(root).as_posix()
        if target != root / relative or ".." in Path(relative).parts:
            raise repository_executor.IsolationUnavailable("lint target must be inside the owned repository")
        repository_executor.lint_relative_file(relative)
        data = repository_executor.read_regular(root, relative, repository_executor.LINT_INPUT_CAP,
                                                single_link=True)
        if data is None:
            raise repository_executor.IsolationUnavailable("lint target is not an existing regular file")
        if not os.environ.get(repository_executor.HOST_LAUNCHER_ENV):
            raise repository_executor.IsolationUnavailable("JavaScript lint requires the owned host launcher")
        tickets = repository_executor.node_report_root(datadir.aitelier_home(), run_id)
        tickets.mkdir(parents=True, exist_ok=True)
        repository_executor._real_dir(tickets)
        # No untrusted command writes here: only one immutable input is mounted RO.
        with tempfile.TemporaryDirectory(prefix="lint-", dir=tickets) as directory:
            ticket = Path(directory)
            snapshot = ticket / ("input" + target.suffix.lower())
            with snapshot.open("xb") as stream:
                stream.write(data)
            done = repository_executor.execute(
                root, [relative], LINT_TIMEOUT, run_id=run_id,
                writable_dirs=[ticket], profile=repository_executor.LINT_PROFILE)
        result.update(returncode=done.returncode, stdout=done.stdout, stderr=done.stderr,
                      input_sha256=hashlib.sha256(data).hexdigest(),
                      passed=done.returncode == 0,
                      error_message="" if done.returncode == 0 else
                          f"ESLint exited {done.returncode}: {done.stdout.strip() or done.stderr.strip()}")
    except subprocess.TimeoutExpired as exc:
        result.update(returncode=-1, timed_out=True,
                      stdout=exc.output or "", stderr=exc.stderr or "",
                      error_message=f"JavaScript lint timed out after {LINT_TIMEOUT}s")
    except (OSError, ValueError, repository_executor.IsolationUnavailable) as exc:
        result.update(returncode=-1, unavailable=True,
                      error_message=f"JavaScript lint unavailable: {exc}")
    return result


def register_all() -> None:
    from skillflow.lint_backends import register_backend

    register_backend("eslint", eslint_backend)
