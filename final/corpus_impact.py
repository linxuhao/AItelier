"""Corpus impact of harness.a-scenario-has-one-reading on a game repo's playtest/.

usage: python final/corpus_impact.py <base_harness.py> <cand_harness.py> <repo_dir>

<repo_dir> holds a `playtest/` extracted from the game repo (git archive; the
game repo itself is never checked out or edited). The contract is loaded by the
same reader the gates use (aitelier.tools.godot_playtest.impl.read_spec) and
handed to each harness's real `_playtest_spec` with the engine stubbed out, so
only the parse-time checks speak. A spec error the candidate reports and the
base does not is a new error. Every `/`-qualified click, hover or assert target
is listed afterwards; whether each resolves as a path in the engine is not
measured here.
"""
import importlib.util
import sys
from collections import Counter
from pathlib import Path

from aitelier.tools.godot_playtest.impl import read_spec


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def fake(dst, state_path, frames, timeout, extra, scene="", capture_at=None,
             timing=None, render=True):
        if timing is not None:
            timing["game_usec"] = int(frames * 1_000_000 / mod.PLAYTEST_FIXED_FPS) \
                if mod.PLAYTEST_FIXED_FPS > 0 else 0
        return {"frames": frames, "nodes": {}, "asserts": [{"passed": True}]}, [], False

    mod._run_probe = fake
    return mod


def main(base_path, cand_path, repo):
    spec, info = read_spec(Path(repo))
    print("READ_SPEC source=%s errors=%d notes=%d" % (info["source"], len(info["errors"]),
                                                     len(info["notes"])))
    for e in info["errors"]:
        print("  READ_SPEC_ERROR", e)
    scenarios = spec["scenarios"]
    print("SPEC_TOP_LEVEL_KEYS", sorted(spec))
    print("SCENARIOS_CHECKED", len(scenarios))
    base, cand = load(base_path, "h_base"), load(cand_path, "h_cand")
    tmp = Path("/tmp/onereading_corpus_dst/proj")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    rb = base._playtest_spec(tmp, spec, 300, 120)
    rc = cand._playtest_spec(tmp, spec, 300, 120)
    base_errs = set(rb["spec_errors"])
    new = [e for e in rc["spec_errors"] if e not in base_errs]
    print("BASE_SPEC_ERRORS", len(rb["spec_errors"]))
    print("CAND_SPEC_ERRORS", len(rc["spec_errors"]))
    print("NEW_SPEC_ERRORS", len(new))
    top = [e for e in new if e.startswith("spec has unknown top-level key")]
    for e in top:
        print("  NEW_TOP_LEVEL", e)
    per_scen = {}
    for e in new:
        if not e.startswith("scenario "):
            continue
        name = e.split("'", 2)[1]
        per_scen.setdefault(name, []).append(e)
    reasons = Counter()
    for name in sorted(per_scen):
        kinds, labels = [], set()
        for e in per_scen[name]:
            if "has unknown key(s)" in e:
                keys = e.split("has unknown key(s) ", 1)[1].split(" - allowed", 1)[0]
                kinds.append("unknown key(s) " + keys)
                labels.add("unknown key(s) " + keys)
            elif "has key description of type" in e:
                kinds.append("description not a string")
                labels.add("description not a string")
            elif "has a non-integer `at`" in e:
                kinds.append("non-integer at: " + e.split(": ", 1)[1].split(". Frames", 1)[0])
                labels.add("non-integer `at`")
            elif "must not decrease in file order" in e:
                kinds.append("order: " + e.split(": ", 1)[1].split(";", 1)[0])
                labels.add("`at` decreases in file order")
            else:
                kinds.append("other: " + e)
                labels.add("other")
        for label in labels:
            reasons[label] += 1
        print("NEW_ERROR_SCENARIO %s | %s" % (name, " | ".join(kinds)))
    print("SCENARIOS_WITH_NEW_ERRORS %d of %d checked" % (len(per_scen), len(scenarios)))
    for k, n in sorted(reasons.items()):
        print("REASON_COUNT %d scenarios: %s" % (n, k))
    refused = sum(1 for s in rc["behavior"]["scenarios"] if not s["ran"])
    print("CAND_SCENARIOS_NOT_RUN %d of %d" % (refused, len(rc["behavior"]["scenarios"])))

    paths = []
    for sc in scenarios:
        tl, _ = cand._normalize_timeline(sc.get("timeline") or [])
        for e in tl:
            for kind in ("click", "hover"):
                if e.get(kind):
                    target = str(e[kind]).split(" ", 1)[0]
                    if "/" in target:
                        paths.append((sc["name"], e["at"], kind, str(e[kind])))
            for a in e.get("assert") or []:
                node = str(a.get("node", ""))
                if "/" in node:
                    paths.append((sc["name"], e["at"], "assert", str(a.get("name", node))))
    print("SLASH_TARGETS %d in %d scenarios (engine resolution: not measured this round)"
          % (len(paths), len({p[0] for p in paths})))
    for p in paths:
        print("  SLASH_TARGET %s | at %s | %s | %s" % p)


if __name__ == "__main__":
    main(*sys.argv[1:4])
