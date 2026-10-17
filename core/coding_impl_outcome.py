"""Owned observations for coding_impl's FAILED candidate, never release acceptance."""
from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from core import closed_git

MESSAGE = "Tests FAIL; cause undetermined; release blocked"
CONFIG_NAMES = frozenset("conftest.py pytest.ini pyproject.toml setup.cfg setup.py tox.ini uv.lock poetry.lock Pipfile Pipfile.lock pdm.lock package.json package-lock.json npm-shrinkwrap.json yarn.lock pnpm-lock.yaml bun.lock bun.lockb run_tests.sh".split())
EXCLUDED = frozenset(".git .venv venv node_modules __pycache__ .tox".split())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def report_sha(report):
    return hashlib.sha256(json.dumps(report, indent=2, ensure_ascii=False).encode()).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_binding(sf, run):
    if not run or run.get("graph_name") != "coding_impl":
        raise ValueError("unsupported graph")
    graph = sf._graph_for_run(run["id"])
    pin = sf.graph_version_for_run(run["id"])
    if graph is None or graph.name != "coding_impl" or not pin or pin.get("version") is None:
        raise ValueError("unavailable builtin graph pin")
    return {"run_id": run["id"], "project_id": run["project_id"],
            "config_name": "coding_impl", "config_version": pin["version"],
            "config_digest": digest(graph.to_dict())}


def current_binding(run_id):
    from api.dependencies import get_skillflow
    sf = get_skillflow()
    return run_binding(sf, sf.get_run(run_id))


def owned_snapshot(repo, run_id, *, original=False):
    from api.dependencies import get_db_manager
    from core.run_isolation import record
    binding = current_binding(run_id)
    rec = record(get_db_manager(), run_id)
    if (not rec or rec["mode"] != "worktree" or rec.get("note")
            or rec.get("released_at") or rec["project_id"] != binding["project_id"]
            or rec["config_name"] != "coding_impl"
            or Path(rec["worktree_path"]).resolve() != Path(repo).resolve()):
        raise ValueError("original owned worktree unavailable (direct/relay/released)")
    # Root pytest.ini is an explicit effective-config witness for this initial profile.
    if (not (Path(repo) / "pytest.ini").is_file() or (Path(repo) / "pytest.ini").is_symlink()
            or any((Path(repo) / name).exists() for name in ("pytest.toml", ".pytest.toml", ".pytest.ini"))):
        raise ValueError("effective pytest config is not an owned root pytest.ini")
    head = closed_git.resolve_commit(repo, "HEAD")
    clean, why = closed_git.clean_against_head(repo)
    if not clean or not head or original and head != rec["base_sha"]:
        raise ValueError("actual bytes are not the clean measured commit: " + why)
    tree = closed_git.ls_tree(repo, head)
    config = tree.get("pytest.ini")
    if not config or config.type != "blob" or config.mode not in (0o100644, 0o100755):
        raise ValueError("effective pytest config is not a tracked regular root pytest.ini")
    return {**binding, "repo": str(Path(repo).resolve()), "base_sha": rec["base_sha"],
            "base_tree": closed_git.run(repo, "rev-parse", rec["base_sha"] + "^{tree}").stdout.strip(),
            "candidate_sha": head, "candidate_tree": closed_git.run(repo, "rev-parse", head + "^{tree}").stdout.strip(),
            "files": {p: e.sha for p, e in tree.items()}}


def junit_observations(path, repo, files):
    root = ET.parse(path).getroot()
    cases = {}
    for case in root.iter("testcase"):
        raw, name, classname = case.get("file"), case.get("name"), case.get("classname", "")
        if not raw or not name:
            raise ValueError("ambiguous pytest case identity")
        module = Path(raw)
        if module.is_absolute():
            module = module.relative_to(Path(repo))
        rel = module.as_posix()
        if (".." in module.parts or rel not in files or module.suffix != ".py"
                or (Path(repo) / module).resolve() != Path(repo) / module):
            raise ValueError("outside/untracked pytest module")
        identity = json.dumps([rel, classname, name], separators=(",", ":"))
        outcomes = [x for x in case if x.tag in {"failure", "error", "skipped"}]
        if identity in cases or len(outcomes) > 1:
            raise ValueError("duplicate/ambiguous pytest observation")
        detail = outcomes[0] if outcomes else None
        cases[identity] = {"module": rel, "status": detail.tag if detail is not None else "passed",
                           "observation": ({"type": detail.get("type", ""), "message": detail.get("message", ""),
                                            "body": "".join(detail.itertext())} if detail is not None else None)}
    if not cases or not any(c["status"] != "skipped" for c in cases.values()):
        raise ValueError("zero/all-skipped pytest population")
    return cases


def inventory(snapshot, cases):
    modules = {c["module"] for c in cases.values()}
    selected = {p: sha for p, sha in snapshot["files"].items()
            if p in modules or not (set(Path(p).parts) & EXCLUDED) and
            (Path(p).name in CONFIG_NAMES or fnmatch.fnmatch(Path(p).name, "requirements*.txt")
             or fnmatch.fnmatch(Path(p).name, "constraints*.txt"))}
    if any((Path(snapshot["repo"]) / p).is_symlink() for p in selected):
        raise ValueError("ambiguous linked test/config input")
    return selected


def execution_profile(identity, snapshot, junit_dir):
    if not isinstance(identity, dict) or identity.get("schema") != "pytest-execution/1":
        raise ValueError("execution identity unavailable")
    if (identity.get("transport") != "host_launcher" or identity.get("comparable") is not True
            or not re.fullmatch(r"(?:[A-Za-z0-9._:/-]+@)?sha256:[a-f0-9]{64}", identity.get("image", ""))
            or any(identity.get(k) != snapshot[k] for k in ("run_id", "project_id", "repo"))):
        raise ValueError("execution identity not owned/immutable/comparable")
    runtime = identity.get("runtime")
    env = {"HOME": "/tmp", "PATH": "/usr/local/bin:/usr/bin:/bin",
           "PYTHONPATH": snapshot["repo"] + ":" + snapshot["repo"] + "/src", "PYTHONDONTWRITEBYTECODE": "1"}
    if (not isinstance(runtime, dict) or not runtime.get("python_executable") or not runtime.get("python_version")
            or identity.get("env") != env or not isinstance(identity.get("distributions"), list)
            or identity.get("distributions_digest") != digest(identity["distributions"])
            or not re.fullmatch(r"[a-f0-9]{64}", identity.get("entry_sha256", ""))):
        raise ValueError("incomplete execution profile")
    argv = identity.get("effective_argv")
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        raise ValueError("effective command unavailable")
    expected = "--junitxml=" + str(Path(junit_dir) / "junit.xml")
    if argv.count(expected) != 1 or sum(a.startswith("--junitxml=") for a in argv) != 1:
        raise ValueError("JUnit argument is not this invocation's allocated path")
    return {k: identity[k] for k in ("image", "entry_sha256", "runtime", "distributions_digest", "env")} | {
        "effective_argv": ["--junitxml=<owned-report>" if a == expected else a for a in argv]}


def measurement(report, before, after, cases, profile):
    if not before or before != after or not cases or not profile:
        raise ValueError("measurement bytes/observations/profile unavailable or changed")
    if (report.get("returncode") not in (0, 1) or any(report.get(k) for k in
            ("skipped", "timed_out", "infrastructure_unavailable", "no_tests_collected", "import_error",
             "collection_errors", "repo_gate_absent", "repo_gate_unmeasured", "repo_gate_unattributable", "failure_identity_error"))):
        raise ValueError("incomplete pytest measurement")
    legs = {}
    for key in ("node", "repo_gate"):
        leg = report.get(key)
        legs[key] = leg is not None
        if leg is not None and (leg.get("passed") is not True or leg.get("skipped") or
                key == "repo_gate" and (leg.get("measured") != "measured_pass" or
                                         report.get("purpose") == "provisional_round_feedback")):
            raise ValueError("unsupported nonpassing leg")
    failures = [k for k, c in cases.items() if c["status"] in {"failure", "error"}]
    if report.get("passed") is False and not failures or report.get("passed") is True and failures:
        raise ValueError("contradictory pytest observation")
    return {"schema": "prechange-report/1", "binding": {k: v for k, v in before.items() if k != "files"},
            "inventory": inventory(before, cases), "cases": cases, "profile": profile, "legs": legs,
            "report_sha256": report_sha(report)}


def outcome_schema():
    constants = {"schema": "coding-outcome/1", "disposition": "candidate_tests_failed",
                 "tests_passed": False, "release_passed": False, "cause_attribution": "undetermined",
                 "reason": "repeated_prechange_observations", "measured": True, "comparable": True}
    strings = "baseline_artifact_sha256 run_id project_id config_name config_digest base_sha base_tree candidate_sha candidate_tree".split()
    properties = {k: {"const": v} for k, v in constants.items()}
    properties.update({k: {"type": "string", "minLength": 1} for k in strings})
    properties["config_version"] = {"type": "integer", "minimum": 1}
    properties["unresolved_failures"] = {"type": "array", "minItems": 1, "items": {"type": "string"}}
    properties["new_observed_failures"] = {"type": "array", "maxItems": 0}
    return {"type": "object", "required": list(properties), "properties": properties}


def compare(baseline, candidate, baseline_sha):
    if baseline.get("schema") != "prechange-report/1" or candidate.get("schema") != "prechange-report/1":
        raise ValueError("unknown measurement schema")
    old, new = baseline["binding"], candidate["binding"]
    keys = ("run_id", "project_id", "config_name", "config_version", "config_digest", "repo", "base_sha", "base_tree")
    if any(old[k] != new[k] for k in keys) or old["candidate_sha"] != old["base_sha"] or old["candidate_tree"] != old["base_tree"]:
        raise ValueError("baseline binding mismatch")
    if any(baseline[k] != candidate[k] for k in ("profile", "inventory", "legs")):
        raise ValueError("test/config/profile/leg changed")
    oldcases, cases = baseline["cases"], candidate["cases"]
    if not oldcases.keys() <= cases.keys():
        raise ValueError("pytest case disappeared")
    failed = []
    for key, case in cases.items():
        previous = oldcases.get(key)
        if case["status"] == "skipped":
            if previous != case:
                raise ValueError("lost/new skipped measurement")
        elif case["status"] in {"failure", "error"}:
            if previous != case:
                raise ValueError("new observed failure")
            failed.append(key)
    if not failed:
        raise ValueError("no repeated failed observations")
    return {"schema": "coding-outcome/1", "disposition": "candidate_tests_failed", "tests_passed": False,
            "release_passed": False, "cause_attribution": "undetermined", "reason": "repeated_prechange_observations",
            "measured": True, "comparable": True, "baseline_artifact_sha256": baseline_sha,
            **{k: v for k, v in new.items() if k != "repo"},
            "unresolved_failures": failed, "new_observed_failures": []}


def finish_measurement(report, out_dir, before, after, cases, profile, *, capture=False, source=""):
    """Publish bounded owned measurement or leave normal absolute routing intact."""
    target = Path(out_dir)
    current = measurement(report, before, after, cases, profile)
    if capture:
        (target / "prechange_report.json").write_text(json.dumps(current, indent=2), encoding="utf-8")
    elif source == "baseline" and report.get("passed") is False:
        path = target.parent / "baseline" / "prechange_report.json"
        baseline = json.loads(path.read_text())
        full = target.parent / "baseline" / "test_report.json"
        if baseline["report_sha256"] != file_sha(full):
            raise ValueError("baseline full report mismatch")
        report["coding_outcome"] = compare(baseline, current, file_sha(path))
        current["report_sha256"] = report_sha(report)
        (target / "candidate_report.json").write_text(json.dumps(current, indent=2), encoding="utf-8")
        return True
    return False


def load_coding_outcome(sf, ws, run, registry):
    """Derive result truth BEFORE presentation caps, never from current_node."""
    if run.get("graph_name") != "coding_impl" or run.get("status") != "completed":
        return {}
    invalid = {"status": "result_evidence_invalid", "execution_status": "completed", "tests_passed": False,
               "release_passed": False, "verdict": "result_evidence_invalid; no release", "message": "result_evidence_invalid; no release"}
    try:
        import jsonschema
        binding = run_binding(sf, run)
        report_path = ws.get_final_path(run["project_id"], "test", "coding_impl") / "test_report.json"
        report = json.loads(report_path.read_text())
        if not isinstance(report, dict) or report.get("run_binding") != binding:
            raise ValueError("report run binding missing/mismatched")
        if not isinstance(report.get("summary"), str) or not report["summary"] or not isinstance(report.get("failures"), list):
            raise ValueError("malformed report")
        if any(report.get(k) for k in ("skipped", "no_tests_collected", "timed_out", "infrastructure_unavailable",
                                       "collection_errors", "import_error", "repo_gate_absent", "repo_gate_unmeasured", "repo_gate_unattributable")):
            raise ValueError("contradictory unmeasured completed report")
        if report.get("passed") is True and not report["failures"] and not report.get("coding_outcome"):
            return {}
        if report.get("passed") is not False or not report["failures"]:
            raise ValueError("contradictory report")
        outcome = report["coding_outcome"]
        jsonschema.validate(outcome, outcome_schema())
        if any(outcome[k] != v for k, v in binding.items()):
            raise ValueError("outcome binding mismatch")
        base = ws.get_final_path(run["project_id"], "baseline", "coding_impl") / "prechange_report.json"
        if outcome["baseline_artifact_sha256"] != file_sha(base):
            raise ValueError("baseline artifact mismatch")
        envelope = json.loads(base.read_text())
        if any(envelope["binding"][k] != v for k, v in binding.items()):
            raise ValueError("baseline binding mismatch")
        fullbase = ws.get_final_path(run["project_id"], "baseline", "coding_impl") / "test_report.json"
        if envelope["report_sha256"] != file_sha(fullbase):
            raise ValueError("baseline full report mismatch")
        candidate = json.loads((report_path.parent / "candidate_report.json").read_text())
        if candidate["report_sha256"] != file_sha(report_path):
            raise ValueError("candidate full report mismatch")
        if compare(envelope, candidate, file_sha(base)) != outcome:
            raise ValueError("contradictory failed candidate envelope")
        return {"status": "candidate_tests_failed", "execution_status": "completed", "tests_passed": False,
                "release_passed": False, "verdict": MESSAGE, "message": MESSAGE,
                "coding_outcome": {k: v for k, v in outcome.items() if k != "unresolved_failures"},
                "unresolved_failure_count": len(outcome["unresolved_failures"]),
                "failure_references": [key[:300] for key in outcome["unresolved_failures"][:20]]}
    except Exception:
        return invalid
