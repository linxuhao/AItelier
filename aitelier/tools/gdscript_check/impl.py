"""gdscript_check — per-task GDScript parse gate.

Runs `godot --headless --check-only --script <file>` on the .gd files a step
just wrote, via the godot-builder sidecar (this container has no Godot binary;
the sidecar mounts ~/.AItelier read-only at the identical path, so it reads the
step's staging dir directly — nothing is uploaded).

WHY THIS EXISTS: t_impl's importability validation globs `*.py`, so on a Godot
project it matches zero files and passes vacuously. The only real GDScript
parse check was `5_compile`, at the very END of the pipeline — a syntax error
written by the first task survived every remaining task. Run jinyong-play
shipped a `get_move_range()` BFS loop with tab depths 3/4/5/7 and a duplicated
guard; nothing mechanical caught it, only a reviewer counting tabs by eye.

Deliberately NOT routed through skillflow's `lint` + linter_manifest.json:
lint's `_run_backend` returns passed=True for an unrecognised backend name, so
one typo in the LLM-authored manifest would switch this gate off in silence —
the same shape as the `$STEP_DIR` vacuous pass the base config already warns
about. Which checker guards syntax is not the architect's to choose.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from core import external_deps

from aitelier.gate_skip_log import log_gate_skip

_BUILDER_URL = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")


def gdscript_check(files: list[str] | None = None, workspace_root: str = "",
                   timeout: int = 180, **kwargs) -> dict:
    """Parse-check every .gd file matching `files` under `workspace_root`.

    Returns StepValidator's shape: {all_passed, results: [{file, passed,
    error_message}]}. An empty match set passes — a step that wrote no
    GDScript has nothing to parse.
    """
    root = Path(workspace_root or ".").resolve()
    paths: list[Path] = []
    for pattern in (files or ["*.gd"]):
        matches = sorted(root.rglob(pattern)) if "*" in pattern else [root / pattern]
        paths.extend(p for p in matches if p.is_file())
    if not paths:
        return {"all_passed": True, "results": []}

    body = json.dumps({"files": [str(p) for p in paths],
                       "timeout": timeout}).encode("utf-8")
    req = urllib.request.Request(
        _BUILDER_URL.rstrip("/") + "/checkgd", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout + 60) as resp:
            report = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as e:
        # A sidecar that is down must not fail every task in the loop — but say
        # so loudly, the way godot_compile does, so a skipped gate never reads
        # as a green one.
        #
        # "Loudly" used to mean this print() plus `gate_skipped` in the return
        # dict, and BOTH are unread on this path: the print goes to the container
        # log (not mounted, gone on recreation), and StepValidator drops every key
        # but `all_passed` the moment it is True — see aitelier/gate_skip_log.py.
        # godot_compile's flag survives because it lands in compile_report.json and
        # 5_review reads that file; a validation tool has no such file. So the fact
        # goes to the mounted gate-skip log, where it outlives the run.
        # (5_compile remains the backstop: it parse-checks the whole repo at the
        # end, and flags its OWN skip into compile_report.json when the builder is
        # still down then. This log is what tells you the per-task gate was off.)
        log_gate_skip("gdscript_check", "godot-builder unreachable",
                      url=_BUILDER_URL, error=e, unchecked_files=len(paths),
                      workspace=root)
        print(f"[gdscript_check] GATE SKIPPED: "
              + external_deps.unreachable("GODOT_BUILDER_URL", _BUILDER_URL, e)
              + f" {len(paths)} .gd file(s) NOT parse-checked", flush=True)
        return {"all_passed": True, "results": [], "gate_skipped": True}

    # "Sent 21 files, got 0 results back" must never read as a pass. The sidecar
    # drops every path it cannot stat, so a stale mount there turns a real check
    # into an empty one — the exact shape that let run jinyong-ui ship unverified.
    checked = len(report.get("results", []))
    if checked < len(paths):
        return {"all_passed": False, "results": report.get("results", []),
                "error_message": report.get("error_message") or (
                    "godot-builder checked %d of the %d .gd file(s) it was given "
                    "— it cannot see the rest. Its workspace mount is stale; "
                    "recreate the container." % (checked, len(paths)))}

    # Report paths relative to the staging root: the absolute container path is
    # noise in an agent's retry prompt.
    for r in report.get("results", []):
        try:
            r["file"] = str(Path(r["file"]).relative_to(root))
        except (ValueError, KeyError):
            pass
    return report
