#!/usr/bin/env python3
"""THE MICROBENCHMARK, as a script that runs and a file it writes.

What a frame budget buys, measured at the three clock settings the harness can
be in, on a real Godot project, with the probe's own accumulated `delta` as the
reading. It exists because the three numbers this answers with were, for one
round, only prose in a report: no script, no log, no artifact. A number nobody
can re-produce is an argument wearing a number.

    --fixed-fps N       delta is 1/N by decree, no real-time synchronisation
    Engine.max_fps = N  delta is >= 1/N because the engine SLEEPS to make it so
    neither            delta is whatever the frame happened to cost

The first two both give a determined slice of game time; only the first gives it
without paying real seconds for it. The third gives none, which is the reason
one of the other two has to be on.

    python3 /srv/game_time_probe.py <project_dir> [frames] [--json out.json]

Takes the engine, so it obeys the same rule as every other engine run: one at a
time, and not while a gate is in flight.
"""
import argparse, importlib.util, json, os, shutil, sys, time
from pathlib import Path

HARNESS = os.environ.get("GODOT_HARNESS_PY", "/srv/godot_harness.py")


def _load_harness(path=HARNESS):
    s = importlib.util.spec_from_file_location("godot_harness", path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


# Each setting is (label, fixed_fps, probe max_fps env). "" leaves the env var
# unset so the python side's own default for that fixed_fps applies; "0" forces
# the cap OFF, which is the only way to reach the uncapped reading.
SETTINGS = [
    ("fixed_fps_60", 60, ""),
    ("max_fps_60", 0, ""),
    ("uncapped", 0, "0"),
]


def measure(gh, project_dir: str, frames: int, timeout: int = 600) -> list:
    """One staged copy, three probe runs on it. Returns a row per setting."""
    proj = Path(project_dir)
    dst = gh._copy_project(proj)
    rows = []
    try:
        gh._inject_probe(dst)
        gh._import_resources(dst, timeout)
        state_path = dst.parent / "probe_state.json"
        spec_path = dst.parent / "gtprobe_spec.json"
        # No timeline at all: the reading must be about the CLOCK, so nothing
        # the scenario does may be part of it.
        spec_path.write_text(json.dumps({"frames": frames, "timeline": []}))
        saved = gh.PLAYTEST_FIXED_FPS
        try:
            for label, fixed, cap in SETTINGS:
                gh.PLAYTEST_FIXED_FPS = fixed
                extra = {"AITELIER_PROBE_SPEC": str(spec_path)}
                if cap != "":
                    extra["AITELIER_PROBE_MAX_FPS"] = cap
                t0 = time.monotonic()
                probe, errs, timed_out = gh._run_probe(
                    dst, state_path, frames, timeout, extra, render=False)
                wall = time.monotonic() - t0
                t = (probe or {}).get("timing") or {}
                stepped = int(t.get("frames_stepped", 0) or 0)
                game = int(t.get("game_usec", 0) or 0) / 1e6
                step = int(t.get("step_usec", 0) or 0) / 1e6
                rows.append({
                    "setting": label,
                    "fixed_fps": fixed,
                    "probe_max_fps_env": cap,
                    "frames_requested": frames,
                    "frames_stepped": stepped,
                    "game_time_sec": round(game, 6),
                    "step_wall_sec": round(step, 6),
                    "subprocess_wall_sec": round(wall, 6),
                    "mean_delta_sec": round(game / stepped, 8) if stepped else None,
                    "game_time_over_frames_div_60": (
                        round(game / (stepped / 60.0), 6) if stepped else None),
                    "errors": errs,
                    "timed_out": timed_out,
                })
        finally:
            gh.PLAYTEST_FIXED_FPS = saved
    finally:
        # `_copy_project` documents that the caller owns the parent dir.
        shutil.rmtree(dst.parent, ignore_errors=True)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("frames", nargs="?", type=int, default=600)
    ap.add_argument("--json", dest="out")
    ap.add_argument("--harness", default=HARNESS)
    a = ap.parse_args(argv)
    gh = _load_harness(a.harness)
    rows = measure(gh, a.project_dir, a.frames)
    doc = {"project_dir": a.project_dir, "frames": a.frames,
           "harness": a.harness,
           "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "rows": rows}
    text = json.dumps(doc, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    for r in rows:
        print("%-14s frames=%4d game_time=%9.4f s  step_wall=%9.4f s  "
              "mean_delta=%s" % (r["setting"], r["frames_stepped"],
                                 r["game_time_sec"], r["step_wall_sec"],
                                 r["mean_delta_sec"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
