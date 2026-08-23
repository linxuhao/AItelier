"""godot_playtest — headless play-test of the project's Godot scene.

Used as a tool STEP after the compile gate (mirrors run_tests / godot_compile).
It POSTs the consolidated repo path to the ``godot-builder`` sidecar's
``/playtest`` route, which copies the project, injects an autoload probe, runs
the main scene headless for a bounded number of frames (auto-pressing an input
action so the game progresses), and reports:
  * every runtime error (SCRIPT ERROR / push_error) with a res:// file + line
  * a JSON snapshot of the live scene tree's script variables — the runtime
    state an agent needs to actually SEE what the game is doing.
  * PNGs of real rendered frames, unpacked into ``<out_dir>/frames/``.
The outcome lands in ``playtest_report.json`` for 5_review to fold into its
verdict, so runtime failures loop back through the goal-loop alongside parse
errors.

It ALWAYS succeeds as a step:
- No ``project.godot`` → not a Godot project → pass without touching the builder.
- Builder unreachable → pass with a LOUD ``gate_skipped`` note rather than
  stalling on infra (a missing sidecar is not a code defect — but the scene
  shipped without a runtime smoke test, so 5_review must see it).
"""

import base64
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

_BUILDER_URL = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")


def _read_spec(repo: Path) -> dict | None:
    """Load the authored ``playtest_spec.yaml`` from the repo root (the TDD-style
    oracle: architect declares scene/actions/surface, PM sets scenario assert
    thresholds). Absent/invalid → None → the sidecar runs the legacy canned smoke
    test. Best-effort: a malformed spec must not crash the gate."""
    p = repo / "playtest_spec.yaml"
    if not p.is_file():
        return None
    try:
        import yaml
        spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        return spec if isinstance(spec, dict) and spec.get("scenarios") else None
    except Exception:
        return None


def _unpack_frames(report: dict, target_dir: Path) -> None:
    """Materialise the sidecar's base64 frame captures under ``<out_dir>/frames/``
    and leave a relative path behind.

    The sidecar mounts the workspace read-only and copies projects to a container-
    local temp dir, so the PNGs cannot be written where they belong — they ride
    home inside the JSON. Unpacking them here is what makes the report readable:
    a reviewer opens frames/..., and playtest_report.json never holds a blob."""
    frames_dir = target_dir / "frames"
    for cap in report.get("captures") or []:
        blob = cap.pop("png_b64", "")
        if not blob:
            continue
        name = os.path.basename(str(cap.get("file") or "frame.png"))
        frames_dir.mkdir(parents=True, exist_ok=True)
        (frames_dir / name).write_bytes(base64.b64decode(blob))
        cap["file"] = f"frames/{name}"


def godot_playtest(*, project_root: str = "", out_dir: str = "",
                   workspace_root: str = "", **kwargs) -> dict:
    """Run the headless play-test via godot-builder; write playtest_report.json.

    Reads an authored ``playtest_spec.yaml`` if present → scenario-driven TDD
    play-test (input timeline + live Expression assertions); else the legacy
    canned smoke test. Returns {written, passed}. The report holds {passed (HARD:
    crash/didn't-run), behavior (ADVISORY per-scenario asserts), frames, errors[],
    state, spec_used, summary} for the reviewer to read."""
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "frames": 0, "errors": [], "state": {},
              "behavior": None, "spec_used": False, "summary": ""}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    elif not (repo / "project.godot").is_file():
        report["summary"] = "No project.godot — not a Godot project; play-test skipped."
    else:
        payload = {"project_dir": str(repo)}
        spec = _read_spec(repo)
        if spec:
            payload["spec"] = spec
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            _BUILDER_URL.rstrip("/") + "/playtest", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            # 1200s, not 420. The play-test scales with the project and this gate
            # does not degrade gracefully: on timeout the caller records
            # gate_skipped + passed:true, so as a project grows the gate does not
            # get slower, it DISAPPEARS — and it disappears as a pass.
            # jinyong-spine, 2026-08-23: 24 scripts/11 scenarios -> 55/20, and the
            # builder log shows the exception on wfile.write(body) while sending
            # the 200 — the run had FINISHED and the answer had nowhere to go.
            with urllib.request.urlopen(req, timeout=1200) as resp:
                report = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                TimeoutError) as e:
            report["gate_skipped"] = True
            report["summary"] = (
                f"godot-builder unreachable ({_BUILDER_URL}): {e}. "
                "Play-test gate skipped — scene NOT smoke-tested.")
        # The sidecar says there is no Godot project at a path where THIS
        # process just stat'd project.godot. It is not looking at the same
        # bytes we are — a bind mount whose source directory was replaced keeps
        # resolving to the old, unlinked inode. That is a hard failure, not a
        # skip: on run jinyong-ui it returned passed:true with file_count 0 and
        # captures 0, the reviewer read it as clean, and 21 scripts plus a real
        # failing assertion went unseen for the whole run. gate_skipped does not
        # cover this — the builder was perfectly reachable; it was blind.
        if report.get("no_project"):
            report["passed"] = False
            report["blind_builder"] = True
            report["summary"] = (
                f"godot-builder cannot see {repo} — it reports no project.godot "
                f"at a path this process can read. Its workspace mount is stale; "
                f"recreate the container (a restart is not enough). "
                f"Play-test gate NOT run.")

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    _unpack_frames(report, target_dir)
    (target_dir / "playtest_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    return {"written": "playtest_report.json", "passed": report.get("passed", True)}
