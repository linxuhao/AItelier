#!/usr/bin/env python3
"""DOES THIS SCENARIO'S VERDICT DEPEND ON WHAT A FRAME COSTS IN REAL TIME?

A play-test scenario schedules its assertions by FRAME (`at: 200`). What the
game has done by frame 200 depends on how much GAME TIME those frames carried,
and under a real-time-capped clock that is decided by how expensive the frames
happened to be: `Engine.max_fps = N` sleeps only for the REMAINDER of 1/N, so a
frame that costs more hands the game a bigger delta.

Measured 2026-09-17: four scenarios on the wuxia tree were green only because
each run photographed four frames, and those four frames were worth 1.2 s of
extra game time in a 230-frame budget. Turning the pictures off — a change with
nothing to do with time — turned six of their assertions red. Nobody declared
that dependency, nobody knew they had it, and the 2x2 matrix that found it was
run by hand, once, and wired into nothing. This is that matrix, wired in.

THE MEASUREMENT. Each scenario is run four times: under both clocks, each with
and without an injected per-frame real cost (AITELIER_PROBE_FRAME_LOAD_USEC).

  findings   real_time_cap: load 0 vs load L. An assertion decided differently
             is a verdict bought by frame cost. THIS IS THE DETECTOR.
  immunity   fixed_delta: load 0 vs load L. Must be empty — under a fixed delta
             the game time is the same however expensive the frames are, which
             is exactly what `--fixed-fps` was adopted for. A non-empty immunity
             list means that property is gone, and it is reported as loudly as
             the findings rather than as an aside.

    python3 /srv/clock_sensitivity.py <spec.json> <project_dir> [--json out]

Exit code carries the verdict, because a caller that pipes stdout away must
still be able to read it: 0 = nothing depends on frame cost, 1 = something does,
2 = the fixed delta is not immune (the worse news of the two).

Takes the engine four times per scenario, so: one at a time, never while a gate
is in flight, and prefer a subset spec.
"""
import argparse, importlib.util, json, os, sys, time
from pathlib import Path

HARNESS = os.environ.get("GODOT_HARNESS_PY", "/srv/godot_harness.py")

# Roughly what four captured frames were worth per frame in the 230-frame
# scenarios that flipped (1.2 s / 230). Big enough to move a budget-critical
# scenario, small enough that a 230-frame run still finishes in ~5 s.
DEFAULT_LOAD_USEC = 5300

CELLS = [
    ("fixed_delta_load0", 60, 0),
    ("fixed_delta_loaded", 60, DEFAULT_LOAD_USEC),
    ("real_time_cap_load0", 0, 0),
    ("real_time_cap_loaded", 0, DEFAULT_LOAD_USEC),
]


def _load_harness(path=HARNESS):
    s = importlib.util.spec_from_file_location("godot_harness", path)
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def assertion_rows(report: dict) -> dict:
    """{(scenario, name, node, expr, frame): (passed, actual)} — the IDENTITY of
    every assertion that was evaluated, and what it decided.

    Keyed the way the no-assertion-disappears check keys them, so a row that
    moves is a moved row and not a renamed one. `actual` rides along for the
    report but is NOT part of the key: two runs may legally disagree about a
    float while agreeing about the verdict, and a detector that fired on that
    would be noise from its first run."""
    out = {}
    scen = ((report or {}).get("behavior") or {}).get("scenarios") or []
    for s in scen:
        for a in s.get("asserts") or []:
            key = (s.get("name", ""), a.get("name", ""), a.get("node", ""),
                   a.get("expr", ""), int(a.get("frame", -1)))
            out[key] = (bool(a.get("passed")), a.get("actual"))
    return out


def compare(rows_a: dict, rows_b: dict, label_a="a", label_b="b") -> list:
    """Findings: one per assertion the two passes do not decide the same way,
    plus one per assertion that exists in only one of them.

    Missing-on-one-side is a finding and not a shrug: an assertion evaluated in
    one pass and absent from the other is the strongest form of the same
    dependency — that run did not get far enough to judge it at all."""
    out = []
    for key in sorted(set(rows_a) | set(rows_b),
                      key=lambda k: (k[0], k[4], k[1])):
        scen, name, node, expr, frame = key
        a, b = rows_a.get(key), rows_b.get(key)
        if a is None or b is None:
            kind = "missing"
        elif a[0] != b[0]:
            kind = "verdict_differs"
        else:
            continue
        out.append({"scenario": scen, "assertion": name, "node": node,
                    "expr": expr, "frame": frame, "kind": kind,
                    label_a: None if a is None else a[0],
                    label_b: None if b is None else b[0],
                    label_a + "_actual": None if a is None else a[1],
                    label_b + "_actual": None if b is None else b[1]})
    return out


def flagged_scenarios(findings: list) -> list:
    return sorted({f["scenario"] for f in findings})


def run_cells(gh, spec: dict, project_dir: str, load_usec: int,
              timeout: int = 900) -> dict:
    """The four passes, strictly serial. Serial is not a performance choice: the
    engine is a global exclusive lock, and running two at once would put the two
    clocks in competition for the CPU, which is the very thing one of them is
    supposed to be immune to."""
    reports = {}
    saved_fps = gh.PLAYTEST_FIXED_FPS
    saved_env = os.environ.get("AITELIER_PROBE_FRAME_LOAD_USEC")
    try:
        for label, fps, load in CELLS:
            load = load and load_usec
            gh.PLAYTEST_FIXED_FPS = fps
            if load:
                os.environ["AITELIER_PROBE_FRAME_LOAD_USEC"] = str(load)
            else:
                os.environ.pop("AITELIER_PROBE_FRAME_LOAD_USEC", None)
            print("== %-22s fixed_fps=%-3d load=%dus  start %s"
                  % (label, fps, load, time.strftime("%H:%M:%SZ", time.gmtime())),
                  file=sys.stderr)
            t0 = time.monotonic()
            reports[label] = gh.playtest_project(project_dir, spec=spec,
                                                 timeout=timeout)
            print("   wall %.2f s" % (time.monotonic() - t0), file=sys.stderr)
    finally:
        gh.PLAYTEST_FIXED_FPS = saved_fps
        os.environ.pop("AITELIER_PROBE_FRAME_LOAD_USEC", None)
        if saved_env is not None:
            os.environ["AITELIER_PROBE_FRAME_LOAD_USEC"] = saved_env
    return reports


def _game_times(report):
    return {s["name"]: {"frames_stepped": s.get("frames_stepped"),
                        "game_time_sec": s.get("game_time_sec"),
                        "wall_sec": s.get("wall_sec")}
            for s in ((report or {}).get("timing") or {}).get("scenarios") or []}


def analyse(reports: dict) -> dict:
    rows = {k: assertion_rows(v) for k, v in reports.items()}
    findings = compare(rows["real_time_cap_load0"], rows["real_time_cap_loaded"],
                       "real_time_cap_load0", "real_time_cap_loaded")
    immunity = compare(rows["fixed_delta_load0"], rows["fixed_delta_loaded"],
                       "fixed_delta_load0", "fixed_delta_loaded")
    return {
        "assertions_per_cell": {k: len(v) for k, v in rows.items()},
        "scenarios_flagged": flagged_scenarios(findings),
        "findings": findings,
        "fixed_delta_immunity_broken": flagged_scenarios(immunity),
        "immunity_findings": immunity,
        "game_time_per_cell": {k: _game_times(v) for k, v in reports.items()},
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("project_dir")
    ap.add_argument("--json", dest="out")
    ap.add_argument("--harness", default=HARNESS)
    ap.add_argument("--load-usec", type=int, default=DEFAULT_LOAD_USEC)
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args(argv)
    gh = _load_harness(a.harness)
    spec = json.loads(Path(a.spec).read_text())
    reports = run_cells(gh, spec, a.project_dir, a.load_usec, a.timeout)
    doc = analyse(reports)
    doc.update(spec=a.spec, project_dir=a.project_dir, load_usec=a.load_usec,
               measured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    text = json.dumps(doc, indent=1, ensure_ascii=False)
    if a.out:
        Path(a.out).write_text(text)
    print(text)
    print("FLAGGED %d scenario(s) whose verdict depends on real frame cost: %s"
          % (len(doc["scenarios_flagged"]),
             ", ".join(doc["scenarios_flagged"]) or "(none)"), file=sys.stderr)
    print("FIXED-DELTA IMMUNITY broken for: %s"
          % (", ".join(doc["fixed_delta_immunity_broken"]) or "(none)"),
          file=sys.stderr)
    if doc["fixed_delta_immunity_broken"]:
        return 2
    return 1 if doc["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
