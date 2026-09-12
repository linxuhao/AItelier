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
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from core import external_deps
from core.datadir import aitelier_home

import re

from aitelier.gate_skip_log import log_gate_skip


def _git(root, *args, binary=False):
    """Run git in `root`. Returns stdout (str or bytes), or None on any failure."""
    try:
        r = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(root), *args],
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout if binary else r.stdout.decode("utf-8", "replace").strip()


def _baseline_head(checked_root):
    """(work-tree root, HEAD sha) of the git tree holding `checked_root`, or None.

    The baseline is the checked tree's OWN HEAD. It used to be inferred instead:
    read the project id out of ``<workspaces>/<project_id>/…`` and ask the host
    resolver for that project's code path. Both halves broke on the step this
    gate actually runs on. ``t_impl`` declares ``output: target: code``, so
    skillflow points StepValidator at the code repo root — a per-run worktree
    under ``~/.AItelier/worktrees/``, which is not under the workspaces dir, so
    ``relative_to`` raised and the whole exemption returned None. And the
    resolver was called without ``run_id``, i.e. asked the project-keyed
    question, whose answer is the SHARED checkout rather than this run's tree.
    Run 4a2d71bf died on a file it had not broken with the exemption code fully
    present and never executed.

    A git work tree answers both questions by itself, needs no id, and is the
    correct baseline for a `target: code` step: HEAD is exactly "the code as it
    was before this step wrote anything".
    """
    top = _git(checked_root, "rev-parse", "--show-toplevel")
    if not top:
        return None
    head = _git(top, "rev-parse", "HEAD")
    if not head:
        return None          # unborn branch: nothing shipped, nothing to forgive
    return Path(top), head


def _export_baseline(top, head, rels):
    """Write the HEAD blob of each rel path under the sidecar-visible data root.

    The sidecar mounts only ``~/.AItelier``, so the baseline copies have to live
    there — it cannot read a git object store, and `git worktree`/`git archive`
    of the whole tree would cost far more than the handful of failing files.
    Keyed by HEAD sha, so a second call in the same run reuses the export.
    Returns {absolute export path: rel} for the paths that exist at HEAD; a path
    with no HEAD version (a file this step CREATED) is absent, and so is never
    forgiven.
    """
    out = {}
    base = aitelier_home() / "gdscript_baseline" / head[:12]
    for rel in rels:
        dest = base / rel
        if not dest.is_file():
            blob = _git(top, "cat-file", "blob", f"HEAD:{rel}", binary=True)
            if blob is None:
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(blob)
            except OSError:
                continue
        out[str(dest.resolve())] = rel
    return out


def _same_diagnosis(a, b):
    """Same parse complaints, ignoring order, repetition and res:// line refs."""
    def sig(msg):
        msg = re.sub(r"res://\S+", "", msg or "")
        return frozenset(x.strip() for x in re.split(r"SCRIPT ERROR:", msg) if x.strip())
    sa, sb = sig(a), sig(b)
    return bool(sa) and sa == sb


def _preexisting_failures(results, checked_root, timeout):
    """Of the files that failed, which ones ALSO fail at HEAD, identically.

    WHY: ``godot --check-only --script`` parses ONE file with no project import,
    so every reference that lives in another file is unresolvable. Measured
    2026-09-12 on the wuxia tree at HEAD, unmodified: 60 of 354 shipped .gd
    files fail this check (16.9%) — `const K := preload(...)` used as a type,
    `extends` a class defined in another script, and constants folded from an
    imported constant. A step that merely re-anchored a comment in such a file
    was failed for a defect it did not write and could not fix, and with
    ``validation_on_exhaustion: fail`` it could never pass.

    A step is answerable for what IT broke. So a failing file is forgiven only
    when the same path, at the checked tree's HEAD, fails with the SAME
    diagnosis. A file the step actually broke fails here and passes at HEAD; a
    file the step created has no HEAD version and is never forgiven. When the
    baseline or the builder cannot be reached at all, the strict verdict stands
    -- a gate that cannot check must not pass.
    """
    resolved = _baseline_head(checked_root)
    if resolved is None:
        return set()
    top, head = resolved
    staged = {}
    for r in results:
        if r.get("passed"):
            continue
        try:
            rel = str(Path(r["file"]).resolve().relative_to(top.resolve()))
        except (ValueError, KeyError, OSError):
            continue
        staged[rel] = (r["file"], r.get("error_message") or "")
    if not staged:
        return set()
    exported = _export_baseline(top, head, sorted(staged))
    if not exported:
        return set()
    try:
        body = json.dumps({"files": sorted(exported), "timeout": timeout}).encode("utf-8")
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
        rel = exported.get(str(Path(br.get("file", "")).resolve()))
        if rel is None:
            continue
        staged_file, staged_err = staged[rel]
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
