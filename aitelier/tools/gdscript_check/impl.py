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

import re

from aitelier.gate_skip_log import log_gate_skip


def _baseline_root(staging_root):
    """The repo this step's staging dir is a delta OF, or None if unresolvable.

    Staging lives at ``<workspaces>/<project_id>/<graph>/<step>.tmp``; the
    project id is the first segment under the workspaces dir, and the code repo
    for it is whatever the host's resolver says (worktree-aware, so a run in its
    own worktree is baselined against ITS worktree, not the shared checkout).
    """
    try:
        from core.datadir import workspaces_dir
        ws = Path(workspaces_dir()).resolve()
        rel = staging_root.resolve().relative_to(ws)
    except Exception:
        return None
    pid = rel.parts[0] if rel.parts else ""
    if not pid:
        return None
    try:
        from api.dependencies import _existing_repo_code_path
        p = _existing_repo_code_path(pid)
    except Exception:
        return None
    if not p or p is True:
        return None
    root = Path(str(p))
    return root if root.is_dir() else None


def _same_diagnosis(a, b):
    """Same parse complaints, ignoring order, repetition and res:// line refs."""
    def sig(msg):
        msg = re.sub(r"res://\S+", "", msg or "")
        return frozenset(x.strip() for x in re.split(r"SCRIPT ERROR:", msg) if x.strip())
    sa, sb = sig(a), sig(b)
    return bool(sa) and sa == sb


def _preexisting_failures(results, staging_root, timeout):
    """Of the files that failed, which ones ALSO fail as shipped, identically.

    WHY: ``godot --check-only --script`` parses ONE file with no project import,
    so a ``const X = preload(...)`` used as a type annotation is reported as
    "X is a constant but does not contain a type" -- in code that imports and
    runs fine. Measured 2026-09-11 on the wuxia tree: 2 of 7 SHIPPED files fail
    this check untouched (scripts/data/monthly_travel_step.gd,
    scripts/data/chain_logic.gd). A step that merely re-anchored a comment in
    such a file was failed for a defect it did not write and could not fix, and
    with ``validation_on_exhaustion: fail`` it could never pass -- the
    release.mainline-green round died exactly there.

    A step is answerable for what IT broke. So a failing file is forgiven only
    when the same path, as shipped, fails with the SAME diagnosis. A file the
    step actually broke fails here and passes in the baseline; a NEW file has no
    baseline and is never forgiven. When the baseline cannot be established at
    all, the strict verdict stands -- a gate that cannot check must not pass.
    """
    base = _baseline_root(staging_root)
    if base is None:
        return set()
    pairs = {}
    for r in results:
        if r.get("passed"):
            continue
        try:
            rel = Path(r["file"]).resolve().relative_to(staging_root.resolve())
        except (ValueError, KeyError, OSError):
            continue
        cand = base / rel
        if cand.is_file():
            pairs[str(cand.resolve())] = (r["file"], r.get("error_message") or "")
    if not pairs:
        return set()
    try:
        body = json.dumps({"files": sorted(pairs), "timeout": timeout}).encode("utf-8")
        req = urllib.request.Request(
            _BUILDER_URL.rstrip("/") + "/checkgd", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout + 60) as resp:
            base_report = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return set()
    forgiven = set()
    for br in base_report.get("results", []):
        if br.get("passed"):
            continue
        hit = pairs.get(str(Path(br.get("file", "")).resolve()))
        if not hit:
            continue
        staged_file, staged_err = hit
        if _same_diagnosis(staged_err, br.get("error_message") or ""):
            forgiven.add(staged_file)
    return forgiven


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

    # Do not fail a step for a file it did not break. --check-only parses one
    # file with no project import, so idioms that are fine at runtime are
    # reported as errors; forgive only a failure that reproduces identically on
    # the same path as shipped. See _preexisting_failures.
    if not report.get("all_passed", True):
        forgiven = _preexisting_failures(report.get("results", []), root, timeout)
        for r in report.get("results", []):
            if not r.get("passed") and r.get("file") in forgiven:
                r["passed"] = True
                r["preexisting"] = True
                r["error_message"] = "pre-existing: same failure on the shipped file (not broken by this step): " + (r.get("error_message") or "")
        report["all_passed"] = all(r.get("passed") for r in report.get("results", []))

    # Report paths relative to the staging root: the absolute container path is
    # noise in an agent's retry prompt.
    for r in report.get("results", []):
        try:
            r["file"] = str(Path(r["file"]).relative_to(root))
        except (ValueError, KeyError):
            pass
    return report
