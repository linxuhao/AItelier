"""godot_compile — parse-check the project's GDScript over the whole repo.

Used as a tool STEP after the final verifier (mirrors run_tests / the old
unity_compile). Godot has no ahead-of-time compile, but importing the project
parse-checks EVERY script and .tscn together and surfaces parse errors / broken
resource references — the whole-repo check scripts cross-reference each other
need. Runs via the license-free ``godot-builder`` sidecar (docker/godot/
godot_harness.py). The outcome lands in ``compile_report.json`` for 5_review to
fold into its verdict, so parse errors loop back through the goal-loop alongside
the verifier's semantic issues.

It ALWAYS succeeds as a step:
- No ``project.godot`` in the repo → not a Godot project → pass without touching
  the builder (Python/web projects never need it).
- Builder unreachable → pass with a LOUD ``gate_skipped`` note rather than
  stalling the pipeline on an infra problem (a missing sidecar is not a code
  defect — but the code shipped UNVERIFIED, so 5_review must see it).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from core import external_deps

_BUILDER_URL = os.environ.get("GODOT_BUILDER_URL", "http://godot-builder:8080")


def _is_godot(repo: Path) -> bool:
    return (repo / "project.godot").is_file()


def _write_playtest_summary(target_dir: Path, pt: dict) -> None:
    """Distil playtest_report.json into a prompt-sized `playtest_summary.md`.

    WHY THIS EXISTS. A context source of `{step: "5_compile"}` inlines the
    step directory by `rglob("*")` — and this step's directory holds the 100+
    rendered PNGs the readability gate photographs. skillflow reads every one of
    them as UTF-8-with-replacement, so ~56MB of binary lands in the prompt, and
    AItelier's assembler then cuts the block at MAX_CONTEXT_LINES (1500). Sorted
    order puts `frames/` before `playtest_report.json`, so the file that gets
    cut is ALWAYS the play-test report — the single most informative artefact in
    the run.

    Live, jinyong-encounter 2026-08-23: 5_review reported "playtest gate NOT RUN
    — playtest_report.json ABSENT" as a blocking finding while the file sat on
    disk, 98KB, 3526 lines, `passed: true`, with 23 scenarios evaluated. The PM
    then planned the next round around a gate it believed had never run. The
    file was never absent; it was inlined past the cut.

    So: emit a summary that fits. The full report stays on disk and stays
    reachable through the step's read tool for anyone who wants the state dumps.
    """
    b = pt.get("behavior") or {}
    scenarios = b.get("scenarios") or []
    errors = pt.get("errors") or []
    lines = [
        "# playtest_summary.md",
        "",
        "> Distilled from `playtest_report.json` (full report on disk — read it "
        "for node-state snapshots and per-frame captures).",
        "",
        f"- hard gate `passed`: **{pt.get('passed')}**  "
        f"(crash / scene-load / illegal spec key / input-not-received)",
        f"- `spec_used`: {pt.get('spec_used')}   `frames`: {pt.get('frames')}   "
        f"runtime errors: {len(errors)}",
        f"- summary: {pt.get('summary', '')}",
        "",
    ]

    lines.append("## Runtime errors (hard)")
    if errors:
        for e in errors[:40]:
            lines.append(f"- {e if isinstance(e, str) else json.dumps(e, ensure_ascii=False)}")
    else:
        lines.append("- none")
    lines.append("")

    if scenarios:
        failed = [s for s in scenarios if not s.get("passed")]
        lines.append(f"## Scenarios — {len(failed)}/{len(scenarios)} with failing "
                     f"assertions (advisory, does not flip the hard gate)")
        for s in scenarios:
            a = s.get("asserts") or []
            ok = sum(1 for x in a if x.get("passed"))
            mark = "PASS" if s.get("passed") else "FAIL"
            lines.append(f"- `{mark}`  {s.get('name')}  **{ok}/{len(a)}**")
        lines.append("")

        if failed:
            lines.append("## Failing assertions")
            for s in failed:
                lines.append(f"### {s.get('name')}")
                for x in s.get("asserts") or []:
                    if x.get("passed"):
                        continue
                    parts = [f"- `{x.get('name')}`",
                             f"expr `{str(x.get('expr'))[:160]}`",
                             f"actual `{str(x.get('actual'))[:120]}`"]
                    # `actual` on a comparison assert is just `false`. `observed`
                    # is the value the property ACTUALLY held — the only thing
                    # here that tells a reader what to fix. It has to reach THIS
                    # file: the planner and the implementer read the summary,
                    # not the 100k-line JSON.
                    if "observed" in x:
                        parts.append(f"observed `{str(x.get('observed'))[:200]}`")
                    if x.get("error"):
                        parts.append(f"error `{str(x.get('error'))[:160]}`")
                    lines.append(" | ".join(parts))
                lines.append("")
    else:
        lines.append("## Scenarios")
        lines.append("- no `playtest_spec.yaml` behaviour contract was evaluated "
                     "(`spec_used: false`) — only the state snapshot exists.")
        lines.append("")

    (target_dir / "playtest_summary.md").write_text(
        "\n".join(lines), encoding="utf-8")


def godot_compile(*, project_root: str = "", out_dir: str = "",
                  workspace_root: str = "", **kwargs) -> dict:
    """Parse-check the repo's GDScript via godot-builder, then (if it passed)
    play-test it. Writes compile_report.json always, and playtest_report.json
    always. Returns {written, passed}."""
    repo = Path(project_root or workspace_root).resolve()
    report = {"passed": True, "returncode": 0, "file_count": 0,
              "errors": [], "warning_count": 0, "summary": ""}

    if not repo.exists():
        report.update(passed=False, summary=f"Project root not found: {repo}")
    elif not _is_godot(repo):
        report["summary"] = "No project.godot — not a Godot project; compile skipped."
    else:
        body = json.dumps({"project_dir": str(repo)}).encode("utf-8")
        req = urllib.request.Request(
            _BUILDER_URL.rstrip("/") + "/compile", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            # 600s, not 180. The play-test scales with the project: jinyong-spine
            # grew from 24 scripts to 55 and from 11 scenarios to 20, and the
            # builder started finishing the run only AFTER the client had hung
            # up — its log shows the exception on `wfile.write(body)` while
            # sending the 200, i.e. the work was done and the answer had nowhere
            # to go. The caller then recorded gate_skipped + passed:true.
            #
            # That is the failure mode worth naming: at 180s the gate does not
            # get slower as a project grows, it DISAPPEARS — and it disappears
            # as a pass. Keep this comfortably under the 5_compile step's own
            # timeout_seconds so the step budget, not this socket, is the bound.
            with urllib.request.urlopen(req, timeout=600) as resp:
                report = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                TimeoutError) as e:
            # Infra problem, not a code defect → don't fail the run, but flag it
            # LOUDLY: this branch only runs when the repo IS a Godot project, so a
            # skip here means real GDScript shipped UNVERIFIED. gate_skipped lets
            # 5_review surface that instead of reading a bare passed:true as clean.
            report["gate_skipped"] = True
            report["summary"] = (
                external_deps.unreachable("GODOT_BUILDER_URL", _BUILDER_URL, e)
                + " Compile gate skipped — GDScript NOT verified.")
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
                f"Compile gate NOT run.")

    target_dir = Path(out_dir) if out_dir else repo
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "compile_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    # ── Chain the headless play-test (compile → if passed → playtest) ──
    # Play-testing code that didn't parse is pointless — the scene load would
    # fail and pile a redundant failure on top of the parse errors.
    if report.get("passed", True) and _is_godot(repo):
        from aitelier.tools.godot_playtest.impl import godot_playtest
        pt = godot_playtest(project_root=str(repo), out_dir=str(target_dir))
        pt_passed = pt.get("passed", True)
        try:
            _write_playtest_summary(target_dir, json.loads(
                (target_dir / "playtest_report.json").read_text(encoding="utf-8")))
        except Exception as e:  # a summary is an aid, never a reason to fail
            (target_dir / "playtest_summary.md").write_text(
                f"# playtest_summary.md\n\nCould not summarise "
                f"playtest_report.json: {e}\nRead the full report instead.\n",
                encoding="utf-8")
    else:
        reason = ("Parse failed — play-test skipped (fix parse errors first)."
                  if not report.get("passed", True)
                  else "No project.godot — not a Godot project; play-test skipped.")
        skipped = {"passed": True, "frames": 0, "errors": [], "state": {},
                   "summary": reason}
        (target_dir / "playtest_report.json").write_text(
            json.dumps(skipped, indent=2), encoding="utf-8")
        _write_playtest_summary(target_dir, skipped)
        pt_passed = True

    return {"written": ["compile_report.json", "playtest_report.json",
                        "playtest_summary.md"],
            "passed": report.get("passed", True) and pt_passed}
