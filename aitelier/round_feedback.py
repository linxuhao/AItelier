"""Provisional observations for coding rounds; never full-gate authority."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from aitelier.gate_coverage import spec_digest, validate_coverage
from aitelier.scoped_gate import _git, _snapshot, _graph, _closure, _RESOURCE, _DECL

PURPOSE = "provisional_round_feedback"


def expand_feedback_spec(spec):
    """Include authored repeatability executions without modifying the reader."""
    result = deepcopy(spec)
    names = [s["name"] for s in result["scenarios"]]
    if len(names) != len(set(names)) or any(n.endswith("__repeatability") for n in names):
        raise ValueError("duplicate/reserved source scenario identity")
    result["scenarios"] += [dict(deepcopy(s), name=s["name"] + "__repeatability")
                            for s in spec["scenarios"] if s.get("repeatability")]
    return result


def feedback_plan(repo, base, head, spec, source, sentinels, mandatory, requested=()):
    """Prioritize direct literal consumers, without claiming complete impact."""
    started = time.monotonic()
    base = _git(repo, "rev-parse", base + "^{commit}").decode().strip()
    head = _git(repo, "rev-parse", head + "^{commit}").decode().strip()
    changed = _git(repo, "diff", "--name-only", "-z", base, head).decode().split("\0")
    changed = sorted(filter(None, changed))
    names = [s["name"] for s in spec["scenarios"]]
    if not sentinels or not mandatory or len(names) != len(set(names)):
        raise ValueError("feedback requires nonempty sentinel/mandatory policy and unique scenarios")
    unknown_policy = set(sentinels) | set(mandatory) | set(requested)
    if unknown_policy - set(names):
        raise ValueError("unknown feedback policy/request names: " + str(sorted(unknown_policy - set(names))))
    witnesses, symbols, uncertainty, shared = {}, {}, {}, set()
    for revision in (base, head):
        files, texts = _snapshot(repo, revision)
        edges, autoload, opaque = _graph(files, texts)
        shared.update(set(changed) & _closure(edges, autoload))
        for path in changed:
            if path in texts:
                symbols.setdefault(path, set()).update(_DECL.findall(texts[path]))
        for scenario in spec["scenarios"]:
            name = scenario["name"]
            authored_name = name.removesuffix("__repeatability")
            scene = str(scenario.get("scene", spec.get("scene", ""))).removeprefix("res://")
            direct = {scene} | {r[6:] for r in _RESOURCE.findall(texts.get(scene, ""))}
            hits = direct & set(changed)
            # This is the strict split reader's basename/name contract, not a
            # filename-prefix approximation of runtime dependencies.
            if source == "playtest/" and f"playtest/{authored_name}.yaml" in changed:
                hits.add(f"playtest/{authored_name}.yaml")
            if hits:
                witnesses.setdefault(name, set()).update(hits)
            closure = _closure(edges, {scene} | autoload)
            reasons = {k: opaque[k] for k in sorted(closure & set(opaque))}
            if reasons:
                uncertainty[name] = reasons
    direct_files = set().union(*witnesses.values()) if witnesses else set()
    unmapped = sorted(set(changed) - direct_files)
    broad = bool(shared or "project.godot" in changed or "playtest/_common.yaml" in changed
                 or (source != "playtest/" and "playtest_spec.yaml" in changed))
    available = bool(requested or (witnesses and not broad))
    chosen = set(witnesses) | set(sentinels) | set(mandatory) | set(requested) if available else set(names)
    chosen |= {n + "__repeatability" for n in list(chosen) if n + "__repeatability" in names}
    selected = [n for n in names if n in chosen]
    omitted = [n for n in names if n not in chosen]
    reason = ("shared runtime change recommends FULL" if broad else
              "no direct consumer; feedback unavailable, FULL recommended" if not available else
              "diagnostic priority only; omitted context remains UNKNOWN")
    scope = {"purpose": PURPOSE, "coverage": "subset" if omitted else "full",
             "all_scenarios": names, "selected_scenarios": selected,
             "unselected_scenarios": omitted, "fallback_full": not available,
             "selection_basis": {"base_sha": base, "head_sha": head, "spec_sha256": spec_digest(spec),
                 "method": "direct_literal_diagnostic_priority", "changed_files": changed,
                 "changed_symbols": {k: sorted(v) for k, v in sorted(symbols.items())},
                 "direct_consumers": {k: sorted(v) for k, v in sorted(witnesses.items())},
                 "sentinels": list(sentinels), "mandatory": list(mandatory), "requested": list(requested),
                 "shared_runtime_changes": sorted(shared), "opaque_dependencies": uncertainty,
                 "unmapped_changes": unmapped,
                 "fallback_reason": reason, "selection_elapsed_sec": time.monotonic() - started},
             "feedback_available": available,
             "full_recommended": bool(broad or unmapped or uncertainty or not witnesses),
             "unselected_observations": {n: "UNMEASURED; order/save/context UNKNOWN" for n in omitted}}
    if validate_coverage(scope):
        raise ValueError(validate_coverage(scope))
    return scope


def assertion_identities(spec):
    from docker.godot.godot_harness import _normalize_timeline
    identities = {}
    for scenario in spec["scenarios"]:
        name = scenario["name"]
        if name in identities:
            raise ValueError("duplicate scenario: " + name)
        entries, errors = _normalize_timeline(scenario.get("timeline") or [])
        if errors:
            raise ValueError("; ".join(errors))
        rows = Counter()
        for entry in entries:
            for assertion in entry.get("assert") or []:
                expr = assertion.get("expr", "")
                label = assertion.get("name", expr)
                if "mode" in assertion:
                    expr = assertion.get("attr", "") + " " + assertion["mode"] + " since frame 0"
                node = assertion.get("node", "")
                if not all(isinstance(v, str) for v in (label, node, expr)):
                    raise ValueError("nontextual assertion identity")
                rows[(label, node, expr, int(entry["at"]))] += 1
        if not rows:
            raise ValueError("zero authored assertions: " + name)
        identities[name] = rows
    return identities


def verify_feedback(raw, spec):
    """Verify the exact selected multiset, hard verdict and repeat observations."""
    identities = assertion_identities(spec)
    errors = []
    if not isinstance(raw, dict):
        return ["raw response is not an object"]
    if raw.get("passed") is not True or raw.get("spec_used") is not True:
        errors.append("hard gate/spec_used is not true")
    if any(raw.get(k) for k in ("no_project", "blind_builder", "gate_skipped", "spec_errors", "spec_load_errors", "errors")):
        errors.append("engine/spec/visibility error")
    behavior = raw.get("behavior") or {}
    rows = behavior.get("scenarios") or []
    if not isinstance(rows, list) or [r.get("name") if isinstance(r, dict) else None for r in rows] != list(identities):
        return errors + ["scenario inventory/order differs from selected source"]
    if behavior.get("all_passed") is not True:
        errors.append("selected behavior is not passed")
    by_name = {}
    for row in rows:
        name = row["name"]
        assertions = row.get("asserts") or []
        malformed = (not isinstance(assertions, list) or any(
            not isinstance(a, dict) or type(a.get("frame")) is not int
            or not all(isinstance(a.get(k), str) for k in ("name", "node", "expr"))
            for a in assertions))
        if malformed:
            errors.append("malformed selected assertion identity: " + name)
            assertions = []
        actual = Counter((a["name"], a["node"], a["expr"], a["frame"]) for a in assertions)
        if actual != identities[name] or row.get("ran") is not True or row.get("passed") is not True:
            errors.append("selected source/ran/verdict mismatch: " + name)
        if any(a.get("passed") is not True or a.get("error") for a in assertions):
            errors.append("selected assertion failed: " + name)
        if row.get("errors") or row.get("input_dead"):
            errors.append("selected engine/input error: " + name)
        by_name[name] = assertions
    for name in identities:
        repeated = name + "__repeatability"
        if repeated in identities and by_name[name] != by_name[repeated]:
            errors.append("repeatability observations differ: " + name)
    return errors


def run_feedback(repo, out_dir, scope, full_spec, owner, transport, verifier=verify_feedback):
    """Retain the first raw reply before classification; no acceptance boolean."""
    from aitelier.tools.godot_playtest.impl import select_scenarios
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = {"purpose": PURPOSE, "passed": False, "full_test_passed": False,
              "release_disposition": "unresolved", "evidence_state": "partial",
              "baseline_pruning_allowed": False, "state_acceptance_allowed": False,
              "gate_coverage": scope, "owner_identity": owner,
              "started_at": datetime.now(timezone.utc).isoformat(), "errors": []}
    raw_path = target / "round_feedback_raw.json"
    if raw_path.exists() or (target / "playtest_report.json").exists():
        raise ValueError("feedback report artifacts already exist; retain the first attempt")
    try:
        from docker.godot import godot_harness
        report["harness_binding"] = {"normalizer_source_sha256": hashlib.sha256(
            Path(godot_harness.__file__).read_bytes()).hexdigest(),
            "feedback_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if validate_coverage(scope) or scope.get("purpose") != PURPOSE:
            raise ValueError("missing/invalid provisional coverage")
        if scope["all_scenarios"] != [s["name"] for s in full_spec["scenarios"]]:
            raise ValueError("full scenario inventory differs")
        if scope["selection_basis"]["spec_sha256"] != spec_digest(full_spec):
            raise ValueError("full contract digest differs")
        head = _git(repo, "rev-parse", "HEAD").decode().strip()
        if head != scope["selection_basis"]["head_sha"] or _git(repo, "status", "--porcelain", "--untracked-files=normal").strip():
            raise ValueError("feedback source must be the clean bound HEAD")
        if not scope.get("feedback_available"):
            report["selected_state"] = "unavailable"
        else:
            selected, unknown = select_scenarios(full_spec, scope["selected_scenarios"])
            if unknown:
                raise ValueError("unknown selected scenario")
            assertion_identities(selected)
            payload = {"project_dir": str(repo), "spec": selected, "captures": 0, **owner}
            (target / "round_feedback_request.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))
            raw = transport(payload)
            raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            report["raw_artifact"] = {"path": str(raw_path), "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest()}
            findings = verifier(raw, selected)
            if _git(repo, "rev-parse", "HEAD").decode().strip() != head or _git(repo, "status", "--porcelain", "--untracked-files=normal").strip():
                findings.append("source changed during feedback")
            report["errors"] = findings
            report["selected_state"] = "selected_fail" if findings else "selected_pass"
            report["selected_passed"] = not findings
    except Exception as exc:
        report["selected_state"] = "unavailable"
        report["errors"].append(type(exc).__name__ + ": " + str(exc))
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["elapsed_sec"] = time.monotonic() - started
    if owner.get("run_id"):
        from aitelier.gate_evidence import stamp_report
        stamp_report(report, run_id=owner["run_id"], out_dir=str(target))
    path = target / "playtest_report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"written": str(path), **report}
