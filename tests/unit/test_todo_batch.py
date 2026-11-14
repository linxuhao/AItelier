"""Todo batches (core/todo_batch.py), driven through the real CLI against real git checkouts.

K finders, one verifier per (deduplicated, capped) finding, a deterministic fan-in - all under
one State attempt. Every call is a fresh `python -m core.todo_cli` process, as the DSH tools
make it, so every rule below is a statement about the files on disk.
"""
import hashlib
import json
from pathlib import Path

import pytest

from core.todo_runtime import builtin_library
from core.todo_templates import load_template
from tests.unit.test_todo_plan_cli import Cli, T0, base, cli, commit_all, git, repo  # noqa: F401

TEMPLATES = Path(__file__).resolve().parents[2] / "todo_templates"
ATT = "att-b"
FIND_C, FIND_S = f"{ATT}~find-correctness", f"{ATT}~find-security"
# Three findings per finder; correctness/f3 and security/s1 are the same (file, line, claim) -
# the claim differs only in whitespace, which is not a different claim.
CORRECTNESS = [
    {"id": "f1", "file": "app.py", "line": 1, "claim": "x is never validated", "failure_scenario": "x = None crashes"},
    {"id": "f2", "file": "app.py", "line": 2, "claim": "y overflows", "failure_scenario": "y = 2**64"},
    {"id": "f3", "file": "other.py", "line": 1, "claim": "o is read before it is set", "failure_scenario": "import order"},
]
SECURITY = [
    {"id": "s1", "file": "./other.py", "line": 1, "claim": "o is read  before it is set", "failure_scenario": "import"},
    {"id": "s2", "file": "app.py", "claim": "secrets are logged", "failure_scenario": "log shipping"},
    {"id": "s3", "file": "other.py", "line": 9, "claim": "path traversal", "failure_scenario": "../../etc"},
]
UNIQUE = ["correctness_f1", "correctness_f2", "correctness_f3", "security_s2", "security_s3"]


def params(base_sha: str, **over) -> dict:
    return {"target": "the reviewed change", "reviewed_sha": base_sha, "dimensions": ["correctness", "security"],
            "scope": ["**"], **over}


def batch_create(cli: Cli, repo: Path, base_sha: str, attempt: str = ATT, template: str = "find_verify", **over):
    return cli.run("batch-create", "--template", template, "--params", json.dumps(params(base_sha, **over)),
                   "--attempt", attempt, "--node-key", "review.demo", "--context-hash", "c" * 64,
                   "--base-sha", base_sha, "--workspace", str(repo), "--goal", "review the change")


def reports(cli: Cli, attempt: str = ATT) -> Path:
    return cli.store / "batch" / attempt / "reports"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def work(cli: Cli, sub: str, item: str, session: str, **kw) -> dict:
    cli.ok("begin", "--attempt", sub, "--item", item, "--session", session, **kw)
    return cli.ok("done", "--attempt", sub, "--item", item, "--session", session, **kw)


def finders_pass(cli: Cli, attempt: str = ATT) -> None:
    write_json(reports(cli, attempt) / "find-correctness.json", {"findings": CORRECTNESS})
    write_json(reports(cli, attempt) / "find-security.json", {"findings": SECURITY})
    for sub, session in ((f"{attempt}~find-correctness", "s-find-c"), (f"{attempt}~find-security", "s-find-s")):
        assert work(cli, sub, "find", session)["outcome"] == "pass"


def verdict(cli: Cli, key: str, base_sha: str, attempt: str = ATT, **over) -> None:
    write_json(reports(cli, attempt) / f"verify-{key}.json",
               {"finding_id": key, "verdict": "confirmed", "evidence": "ran it", "artifact_ref": base_sha, **over})


def verifiers_pass(cli: Cli, base_sha: str, keys=UNIQUE, attempt: str = ATT) -> None:
    for key in keys:
        verdict(cli, key, base_sha, attempt)
        assert work(cli, f"{attempt}~verify-{key}", "verify", f"s-verify-{key}")["outcome"] == "pass"


def record(cli: Cli, attempt: str = ATT) -> dict:
    return json.loads((cli.store / f"{attempt}.batch.json").read_text())


def subplan_doc(cli: Cli, sub: str) -> dict:
    return json.loads((cli.store / f"{sub}.json").read_text())


# -- 1. the template, and which tool may use it ----------------------------------------

def test_find_verify_loads_by_expansion_and_each_tool_refuses_the_other_kind(cli, repo, base):
    template = load_template("find_verify", builtin_library(), TEMPLATES)
    assert template.kind == "batch" and [s["id"] for s in template.stages] == ["find", "verify"]
    assert template.budget == {"max_parallel": 4, "max_parallel_verify": 4, "deadline_minutes": 120}
    listed = {t["name"]: t for t in cli.ok("templates", store=False)["templates"]}
    assert listed["find_verify"]["kind"] == "batch" and "error" not in listed["find_verify"]
    assert {n: listed["find_verify"]["params"][n]["default"] for n in ("max_findings_per_finder", "max_verify")} == \
        {"max_findings_per_finder": 8, "max_verify": 8}

    code, out = cli.run("create", "--template", "find_verify", "--params", json.dumps(params(base)), "--attempt", ATT,
                        "--node-key", "n", "--context-hash", "c" * 64, "--base-sha", base, "--workspace", str(repo),
                        "--goal", "g")
    assert code == 2 and out["refused"] == "batch_template", out
    code, out = batch_create(cli, repo, base, template="code_change")
    assert code == 2 and out["refused"] == "not_a_batch_template", out
    assert not list(cli.store.glob("*.json")), "a refused batch leaves no plan or record behind"


def test_reviewed_sha_is_required(cli, repo, base):
    body = params(base)
    del body["reviewed_sha"]
    code, out = cli.run("batch-create", "--template", "find_verify", "--params", json.dumps(body), "--attempt", ATT,
                        "--node-key", "n", "--context-hash", "c" * 64, "--base-sha", base, "--workspace", str(repo),
                        "--goal", "g")
    assert code == 2 and out["refused"] == "missing_param" and "reviewed_sha" in out["reason"], out


# -- 2. sub-plan ids: derived, and never shared ------------------------------------------

@pytest.mark.parametrize("dimensions", [["correctness", "  correctness "], ["Security", "security"],
                                        ["error handling", "error\thandling"], ["a/b", "a b"]])
def test_dimensions_that_differ_only_in_whitespace_case_or_punctuation_are_refused(cli, repo, base, dimensions):
    code, out = batch_create(cli, repo, base, dimensions=dimensions)
    assert code == 2 and out["refused"] == "duplicate_dimension", out
    assert sorted(out["dimensions"]) == sorted(dimensions)
    assert not list(cli.store.glob("*.json")), "nothing is frozen when two finders would share a plan file"


def test_create_freezes_one_find_subplan_per_dimension_and_is_idempotent(cli, repo, base):
    out = cli.ok("batch-create", "--template", "find_verify", "--params", json.dumps(params(base)), "--attempt", ATT,
                 "--node-key", "review.demo", "--context-hash", "c" * 64, "--base-sha", base,
                 "--workspace", str(repo), "--goal", "review the change")
    assert out["created"] is True and out["stage"] == "find"
    assert [s["id"] for s in out["subplans"]] == [FIND_C, FIND_S]
    assert [d["subplan"] for d in out["dispatch"]] == [FIND_C, FIND_S]
    prompt = out["dispatch"][0]["prompt"]
    assert f"Sub-plan: {FIND_C}" in prompt and base in prompt and str(repo) in prompt
    assert f"Report: {reports(cli) / 'find-correctness.json'}" in prompt
    sub = subplan_doc(cli, FIND_C)
    assert sub["plan"]["batch"] == ATT and [i["id"] for i in sub["plan"]["items"]] == ["find"], \
        "a sub-plan has no appended deliver: the batch attempt delivers once"
    assert record(cli)["budget"] == {"max_parallel": 4, "max_parallel_verify": 4, "deadline_minutes": 120,
                                     "max_children": 2 + 8}
    assert set(record(cli)["normalisation"]) == {"dimension_key", "finding_id", "verify_key", "dedup_key"}
    again = batch_create(cli, repo, base)
    assert again[0] == 0 and again[1]["created"] is False and again[1]["batch_hash"] == out["batch_hash"]
    code, differs = batch_create(cli, repo, base, max_verify=3)
    assert code == 2 and differs["refused"] == "batch_differs", differs


def test_one_attempt_is_either_a_plan_or_a_batch(cli, repo, base):
    batch_create(cli, repo, base)
    code, out = cli.run("create", "--template", "investigate", "--params",
                        json.dumps({"report_path": "r.json", "require_keys": [], "scope": ["r.json"]}),
                        "--attempt", ATT, "--node-key", "n", "--context-hash", "c" * 64, "--base-sha", base,
                        "--workspace", str(repo), "--goal", "g")
    assert code == 2 and out["refused"] == "batch_exists", out
    cli.ok("create", "--template", "investigate", "--params",
           json.dumps({"report_path": "r.json", "require_keys": [], "scope": ["r.json"]}),
           "--attempt", "att-plain", "--node-key", "n", "--context-hash", "c" * 64, "--base-sha", base,
           "--workspace", str(repo), "--goal", "g")
    code, out = batch_create(cli, repo, base, attempt="att-plain")
    assert code == 2 and out["refused"] == "plan_exists", out


# -- 3. expansion: after every finder, deduplicated, capped, nothing dropped ---------------

def test_expansion_waits_for_every_finder(cli, repo, base):
    batch_create(cli, repo, base)
    write_json(reports(cli) / "find-correctness.json", {"findings": CORRECTNESS})
    assert work(cli, FIND_C, "find", "s-find-c")["outcome"] == "pass"
    assert cli.ok("batch-status", "--attempt", ATT)["expand_due"] is False
    out = cli.refused("batch-expand", "--attempt", ATT)
    assert out["refused"] == "find_unfinished" and out["unfinished"] == [FIND_S], out


def test_duplicates_across_finders_get_one_verifier_and_every_source_is_kept(cli, repo, base):
    batch_create(cli, repo, base)
    finders_pass(cli)
    assert cli.ok("batch-status", "--attempt", ATT)["expand_due"] is True
    out = cli.ok("batch-expand", "--attempt", ATT)
    verify = [s["id"] for s in out["subplans"] if s["stage"] == "verify"]
    assert verify == [f"{ATT}~verify-{k}" for k in UNIQUE], "6 findings, one duplicate pair: 5 verifiers"
    assert out["unverified"] == 0
    merged = next(e for e in record(cli)["verify"] if e["item"] == "correctness_f3")["finding"]
    assert [(s["find"], s["id"]) for s in merged["sources"]] == [(FIND_C, "f3"), (FIND_S, "s1")]
    assert cli.ok("batch-expand", "--attempt", ATT)["subplans"] == out["subplans"], "expansion happens once"


def test_findings_beyond_the_cap_are_listed_unverified_never_dropped(cli, repo, base):
    batch_create(cli, repo, base, max_verify=4)
    finders_pass(cli)
    out = cli.ok("batch-expand", "--attempt", ATT)
    assert [s["item"] for s in out["subplans"] if s["stage"] == "verify"] == UNIQUE[:4]
    assert out["unverified"] == 1
    assert [f["finding_id"] for f in record(cli)["unverified"]] == ["security_s3"]
    verifiers_pass(cli, base, UNIQUE[:4])
    cli.ok("batch-fanin", "--attempt", ATT)
    summary = json.loads((reports(cli) / "find-verify-summary.json").read_text())
    assert [f["finding_id"] for f in summary["unverified"]] == ["security_s3"]
    assert summary["counts"]["unverified"] == 1 and len(summary["findings"]) == 4


def test_a_finder_reporting_more_than_its_maximum_fails_instead_of_being_truncated(cli, repo, base):
    batch_create(cli, repo, base, max_findings_per_finder=2)
    write_json(reports(cli) / "find-correctness.json", {"findings": CORRECTNESS})
    out = work(cli, FIND_C, "find", "s-find-c")
    assert out["outcome"] == "fail" and "at most 2" in json.dumps(out["results"]), out


# -- 4. binding rules ---------------------------------------------------------------------

def test_a_finder_session_cannot_take_a_verify_subplan_and_a_fresh_session_can(cli, repo, base):
    batch_create(cli, repo, base)
    finders_pass(cli)
    cli.ok("batch-expand", "--attempt", ATT)
    sub = f"{ATT}~verify-security_s2"
    out = cli.refused("begin", "--attempt", sub, "--item", "verify", "--session", "s-find-c")
    assert out["refused"] == "distinct_from" and out["bound_subplan"] == FIND_C, out
    assert subplan_doc(cli, sub)["child_session"] is None
    cli.ok("begin", "--attempt", sub, "--item", "verify", "--session", "s-fresh")
    assert subplan_doc(cli, sub)["child_session"] == "s-fresh"
    # Every finder of the batch is excluded, not only the one whose finding this is.
    out = cli.refused("begin", "--attempt", f"{ATT}~verify-security_s3", "--item", "verify", "--session", "s-find-s")
    assert out["refused"] == "distinct_from", out


def test_one_session_holds_one_subplan(cli, repo, base):
    batch_create(cli, repo, base)
    cli.ok("begin", "--attempt", FIND_C, "--item", "find", "--session", "s-1")
    out = cli.refused("begin", "--attempt", FIND_S, "--item", "find", "--session", "s-1")
    assert out["refused"] == "session_holds_other_subplan" and out["bound_subplan"] == FIND_C, out
    out = cli.refused("begin", "--attempt", FIND_S, "--item", "find")
    assert out["refused"] == "usage", "a sub-plan always binds the session that works it"


def test_verify_bind_and_rebind_are_refused_while_the_cited_find_report_is_missing_or_lacks_the_finding(cli, repo, base):
    batch_create(cli, repo, base)
    finders_pass(cli)
    cli.ok("batch-expand", "--attempt", ATT)
    sub = f"{ATT}~verify-correctness_f1"
    report = reports(cli) / "find-correctness.json"
    original = report.read_text()

    report.unlink()
    out = cli.refused("begin", "--attempt", sub, "--item", "verify", "--session", "s-v1")
    assert out["refused"] == "find_report_missing" and out["find_report"] == str(report), out
    write_json(report, {"findings": CORRECTNESS[1:]})                 # f1 is gone from it
    out = cli.refused("begin", "--attempt", sub, "--item", "verify", "--session", "s-v1")
    assert out["refused"] == "finding_not_in_report", out
    write_json(report, {"findings": [{**CORRECTNESS[0], "claim": "something else"}, *CORRECTNESS[1:]]})
    assert cli.refused("begin", "--attempt", sub, "--item", "verify", "--session", "s-v1")["refused"] == \
        "finding_not_in_report", "the same id with another claim is not the finding that was expanded"
    assert subplan_doc(cli, sub)["child_session"] is None and "s-v1" not in record(cli)["bindings"]

    report.write_text(original)
    cli.ok("begin", "--attempt", sub, "--item", "verify", "--session", "s-v1")
    report.unlink()                                                   # the child dies; its report goes too
    out = cli.refused("begin", "--attempt", sub, "--item", "verify", "--session", "s-v2", "--rebind")
    assert out["refused"] == "find_report_missing", out
    assert subplan_doc(cli, sub)["child_session"] == "s-v1", "a refused rebind moves nothing"
    report.write_text(original)
    rebound = cli.ok("begin", "--attempt", sub, "--item", "verify", "--session", "s-v2", "--rebind")
    assert rebound["child_session"] == "s-v2" and rebound["rebound_from"] == "s-v1"

    # A merged finding cites both finders' reports; losing either one refuses it.
    (reports(cli) / "find-security.json").unlink()
    out = cli.refused("begin", "--attempt", f"{ATT}~verify-correctness_f3", "--item", "verify", "--session", "s-v3")
    assert out["refused"] == "find_report_missing" and out["find_report"].endswith("find-security.json"), out


# -- 5. the verdict, the summary and finishing --------------------------------------------

def test_a_verify_report_is_judged_against_the_bound_finding_and_the_reviewed_sha(cli, repo, base):
    batch_create(cli, repo, base)
    finders_pass(cli)
    cli.ok("batch-expand", "--attempt", ATT)
    key, sub = "security_s2", f"{ATT}~verify-security_s2"
    cli.ok("begin", "--attempt", sub, "--item", "verify", "--session", "s-v")
    for wrong, said in (({"artifact_ref": "f" * 40}, "artifact_ref"), ({"finding_id": "security_s3"}, "finding_id"),
                        ({"verdict": "probably"}, "verdict")):
        verdict(cli, key, base, **wrong)
        out = cli.ok("done", "--attempt", sub, "--item", "verify", "--session", "s-v")
        assert out["outcome"] == "fail" and said in json.dumps(out["results"]), (wrong, out)
    verdict(cli, key, base, verdict="refuted")
    assert cli.ok("done", "--attempt", sub, "--item", "verify", "--session", "s-v")["outcome"] == "pass"


def test_finish_waits_for_every_subplan_and_reads_their_observations_without_rerunning_a_predicate(cli, repo, base):
    batch_create(cli, repo, base)
    out_dir = cli.tmp_path / "finish"
    finders_pass(cli)
    out = cli.refused("finish", "--attempt", ATT, "--out", str(out_dir))
    assert out["refused"] == "unfinished" and any(f["state"] == "not_expanded" for f in out["failing"]), out
    cli.ok("batch-expand", "--attempt", ATT)
    verifiers_pass(cli, base, UNIQUE[:-1])
    out = cli.refused("finish", "--attempt", ATT, "--out", str(out_dir))
    assert [f["subplan"] for f in out["failing"]] == [f"{ATT}~verify-security_s3"], out
    assert not out_dir.exists() or not list(out_dir.iterdir()), "no candidate while a sub-plan is open"
    verifiers_pass(cli, base, UNIQUE[-1:])

    # Every find report is deleted: re-running findings_shape would now FAIL both finders, so a
    # finish that passes can only have read the observations already recorded.
    observed = {s: subplan_doc(cli, s)["items"]["find"]["observed_at"] for s in (FIND_C, FIND_S)}
    for name in ("find-correctness.json", "find-security.json"):
        (reports(cli) / name).unlink()
    done = cli.ok("finish", "--attempt", ATT, "--out", str(out_dir), now=T0 + 500)
    assert {s: subplan_doc(cli, s)["items"]["find"]["observed_at"] for s in (FIND_C, FIND_S)} == observed
    assert done["candidate"] is True and done["artifact"] == git(repo, "rev-parse", "HEAD")
    candidate = json.loads(Path(done["report"]).read_text())
    summary_path = reports(cli) / "find-verify-summary.json"
    assert candidate["batch"] == {"summary": str(summary_path), "evidence": False,
                                  "summary_sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                                  "counts": {"confirmed": len(UNIQUE), "refuted": 0, "unsure": 0, "unverified": 0}}
    assert candidate["plan_hash"] == record(cli)["batch_hash"]
    assert {o["subplan"] for o in candidate["observations"]} == {FIND_C, FIND_S, *(f"{ATT}~verify-{k}" for k in UNIQUE)}
    assert Path(done["git_bundle"]["path"]).is_file()
    sub = cli.refused("finish", "--attempt", FIND_C, "--out", str(out_dir))
    assert sub["refused"] == "subplan_finish", sub


def test_finish_refuses_a_head_the_verifiers_did_not_certify(cli, repo, base):
    """Director review C4 (2026-11-14): one unreviewed commit after the verifiers certified
    base must not become the candidate."""
    batch_create(cli, repo, base)
    finders_pass(cli)
    cli.ok("batch-expand", "--attempt", ATT)
    verifiers_pass(cli, base)
    (repo / "unreviewed.txt").write_text("not reviewed\n")
    moved = commit_all(repo, "unreviewed")
    out_dir = cli.tmp_path / "finish"
    out = cli.refused("finish", "--attempt", ATT, "--out", str(out_dir))
    assert out["refused"] == "head_not_certified" and out["head"] == moved and out["certified"] == base, out
    assert base in out["reason"] and moved in out["reason"], out
    assert not out_dir.exists() or not list(out_dir.iterdir()), "a candidate was written for an uncertified HEAD"
    git(repo, "reset", "-q", "--hard", base)
    done = cli.ok("finish", "--attempt", ATT, "--out", str(out_dir))
    assert done["artifact"] == base, done


def test_the_summary_is_not_evidence_and_carries_each_verdict_with_its_artifact_ref(cli, repo, base):
    batch_create(cli, repo, base)
    finders_pass(cli)
    cli.ok("batch-expand", "--attempt", ATT)
    early = cli.refused("batch-fanin", "--attempt", ATT)
    assert early["refused"] == "unfinished", early
    verifiers_pass(cli, base)
    first = cli.ok("batch-fanin", "--attempt", ATT)
    again = cli.ok("batch-fanin", "--attempt", ATT)
    assert first["summary_sha256"] == again["summary_sha256"], "the fan-in is deterministic"
    summary = json.loads(Path(first["summary"]).read_text())
    assert summary["evidence"] is False
    assert [f["finding_id"] for f in summary["findings"]] == UNIQUE and summary["unverified"] == []
    for finding in summary["findings"]:
        assert finding["artifact_ref"] == base and finding["verdict"] == "confirmed"
        assert finding["verify_report"] == str(reports(cli) / f"verify-{finding['finding_id']}.json")
        assert all(Path(s["report"]).name.startswith("find-") for s in finding["sources"])
    # A verify report rewritten after it passed is not summarised under its old verdict.
    verdict(cli, "security_s2", base, artifact_ref="e" * 40)
    assert cli.refused("batch-fanin", "--attempt", ATT)["refused"] == "verify_report_changed"


# -- 6. budget, deadline and re-dispatch ----------------------------------------------------

@pytest.fixture
def tight(tmp_path, monkeypatch) -> Path:
    """find_verify with one finder and two verifiers in flight at a time."""
    directory = tmp_path / "tpl"
    directory.mkdir()
    body = (TEMPLATES / "find_verify.yaml").read_text()
    body = body.replace("max_parallel: 4", "max_parallel: 1").replace("max_parallel_verify: 4", "max_parallel_verify: 2")
    (directory / "find_verify.yaml").write_text(body)
    monkeypatch.setenv("AITELIER_TODO_TEMPLATES", str(directory))
    return directory


def test_the_dispatch_list_is_cut_to_the_free_slots_and_a_dead_session_is_offered_again(cli, repo, base, tight):
    out = batch_create(cli, repo, base)[1]
    assert [d["subplan"] for d in out["dispatch"]] == [FIND_C] and out["pending"] == [FIND_S]
    cli.ok("begin", "--attempt", FIND_C, "--item", "find", "--session", "s-find-c")
    status = cli.ok("batch-status", "--attempt", ATT)
    assert status["dispatch"] == [] and status["in_flight"] == [FIND_C] and status["pending"] == [FIND_S]
    status = cli.ok("batch-status", "--attempt", ATT, "--live-sessions", json.dumps(["root"]))
    assert [{k: r[k] for k in ("subplan", "stage", "dead_session", "prompt")} for r in status["redispatch"]] == \
        [{"subplan": FIND_C, "stage": "find", "dead_session": "s-find-c", "prompt": record(cli)["find"][0]["prompt"]}]
    assert [(d["subplan"], d.get("replaces_session")) for d in status["dispatch"]] == [(FIND_C, "s-find-c")]

    write_json(reports(cli) / "find-correctness.json", {"findings": CORRECTNESS})
    cli.ok("begin", "--attempt", FIND_C, "--item", "find", "--session", "s-find-c-2", "--rebind")
    assert cli.ok("done", "--attempt", FIND_C, "--item", "find", "--session", "s-find-c-2")["outcome"] == "pass"
    assert [d["subplan"] for d in cli.ok("batch-status", "--attempt", ATT)["dispatch"]] == [FIND_S]
    write_json(reports(cli) / "find-security.json", {"findings": SECURITY})
    assert work(cli, FIND_S, "find", "s-find-s")["outcome"] == "pass"
    expanded = cli.ok("batch-expand", "--attempt", ATT)
    assert [d["subplan"] for d in expanded["dispatch"]] == [f"{ATT}~verify-{k}" for k in UNIQUE[:2]]
    assert expanded["pending"] == [f"{ATT}~verify-{k}" for k in UNIQUE[2:]]


def test_the_deadline_is_a_notice_that_stops_nothing(cli, repo, base):
    batch_create(cli, repo, base)
    cli.ok("begin", "--attempt", FIND_C, "--item", "find", "--session", "s-find-c")
    assert cli.ok("batch-status", "--attempt", ATT, now=T0 + 119 * 60)["deadline_passed"] is False
    late = cli.ok("batch-status", "--attempt", ATT, now=T0 + 120 * 60)
    assert late["deadline_passed"] is True and late["overdue"] == [FIND_C, FIND_S]
    assert "deadline passed" in late["brief"]
    assert subplan_doc(cli, FIND_C)["child_session"] == "s-find-c", "nothing is unbound or stopped"
    write_json(reports(cli) / "find-correctness.json", {"findings": []})
    assert cli.ok("done", "--attempt", FIND_C, "--item", "find", "--session", "s-find-c",
                  now=T0 + 200 * 60)["outcome"] == "pass"


def test_list_shows_subplans_under_their_batch_and_the_batch_only_when_the_next_step_is_the_directors(cli, repo, base):
    batch_create(cli, repo, base)
    rows = {r["attempt_id"]: r for r in cli.ok("list")["plans"]}
    assert set(rows) == {FIND_C, FIND_S} and {r["batch"] for r in rows.values()} == {ATT}
    finders_pass(cli)
    rows = {r["attempt_id"]: r for r in cli.ok("list")["plans"]}
    assert rows[ATT]["kind"] == "batch" and rows[ATT]["remaining"] == ["expand"]
    cli.ok("batch-expand", "--attempt", ATT)
    assert ATT not in {r["attempt_id"] for r in cli.ok("list")["plans"]}, "verify sub-plans stand for the work now"
    verifiers_pass(cli, base)
    rows = {r["attempt_id"]: r for r in cli.ok("list")["plans"]}
    assert rows[ATT]["remaining"] == [] and rows[ATT]["next"] is None and rows[ATT]["child_session"] is None
    status = cli.ok("status", "--attempt", ATT)
    assert status["kind"] == "batch" and status["complete"] is True and "todo_finish" in status["brief"]


# -- 7. second director review: loader, dispatch-once, prompt sha, one session per batch -----

@pytest.fixture
def custom(tmp_path, monkeypatch) -> Path:
    directory = tmp_path / "tpl"
    directory.mkdir()
    monkeypatch.setenv("AITELIER_TODO_TEMPLATES", str(directory))
    return directory


SCOPE_PARAM = """  scope:
    type: list[str]
    required: true
    description: Globs of the reviewed checkout the finders and verifiers look at.
"""


def test_a_batch_template_needs_no_scope_and_binds_no_predicate_over_tracked_files(cli, repo, base, custom):
    shipped = (TEMPLATES / "find_verify.yaml").read_text()
    assert SCOPE_PARAM in shipped
    (custom / "find_verify.yaml").write_text(shipped.replace(SCOPE_PARAM, "").replace("Look at: ${scope}. ", ""))
    body = params(base)
    del body["scope"]
    out = cli.ok("batch-create", "--template", "find_verify", "--params", json.dumps(body), "--attempt", ATT,
                 "--node-key", "n", "--context-hash", "c" * 64, "--base-sha", base, "--workspace", str(repo), "--goal", "g")
    assert out["created"] is True
    assert [i["id"] for i in subplan_doc(cli, FIND_C)["plan"]["items"]] == ["find"], "no deliver item is forced on it"

    forbidden = {"changes_within": "{scope: [\"**\"]}", "files_changed": "{paths: [\"${report}\"]}",
                 "git_delivered": "{base: $reviewed_sha}"}
    for predicate, args in forbidden.items():
        bad = shipped.replace("          - predicate: findings_shape\n",
                              f"          - predicate: {predicate}\n            args: {args}\n"
                              "          - predicate: findings_shape\n", 1)
        (custom / "find_verify.yaml").write_text(bad)
        code, out = cli.run("templates", store=False)
        listed = {t["name"]: t for t in out["templates"]}
        assert predicate in listed["find_verify"]["error"] and "binds only" in listed["find_verify"]["error"], listed


def test_each_prompt_is_offered_once_with_its_sha_and_a_live_session_is_never_offered_again(cli, repo, base):
    out = batch_create(cli, repo, base)[1]
    for entry in out["dispatch"]:
        assert entry["prompt_sha256"] == hashlib.sha256(entry["prompt"].encode()).hexdigest()
    assert [e["prompt_sha256"] for e in record(cli)["find"]] == [e["prompt_sha256"] for e in out["dispatch"]]
    again = cli.ok("batch-status", "--attempt", ATT, "--record-dispatch")
    assert again["dispatch"] == [] and [a["subplan"] for a in again["awaiting_begin"]] == [FIND_C, FIND_S]

    sha = out["dispatch"][0]["prompt_sha256"]
    cli.ok("begin", "--attempt", FIND_C, "--item", "find", "--session", "s-find-c", "--prompt-sha256", sha)
    assert record(cli)["binding_log"] == [{"session": "s-find-c", "subplan": FIND_C, "at": T0, "prompt_sha256": sha}]
    live = cli.ok("batch-status", "--attempt", ATT, "--record-dispatch", "--live-sessions", json.dumps(["s-find-c"]))
    row = next(r for r in live["subplans"] if r["id"] == FIND_C)
    assert row["bound_prompt_sha256"] == sha == row["prompt_sha256"]
    assert live["dispatch"] == [] and live["redispatch"] == [] and live["in_flight"] == [FIND_C], \
        "a live finder is never offered again"

    dead = cli.ok("batch-status", "--attempt", ATT, "--record-dispatch", "--live-sessions", "[]")
    assert [(d["subplan"], d["replaces_session"]) for d in dead["dispatch"]] == [(FIND_C, "s-find-c")]
    later = cli.ok("batch-status", "--attempt", ATT, "--record-dispatch", "--live-sessions", "[]")
    assert later["dispatch"] == [] and {a["subplan"] for a in later["awaiting_begin"]} == {FIND_C, FIND_S}
    assert later["redispatch"][0]["already_offered"] is True
    code, refused = cli.run("begin", "--attempt", "att-x", "--item", "x", "--prompt-sha256", sha)
    assert code == 2 and refused["refused"] == "usage"


def test_a_session_holds_one_subplan_per_batch_even_across_rebinds(cli, repo, base):
    batch_create(cli, repo, base)
    finders_pass(cli)
    cli.ok("batch-expand", "--attempt", ATT)
    one, two = f"{ATT}~verify-correctness_f1", f"{ATT}~verify-correctness_f2"
    cli.ok("begin", "--attempt", one, "--item", "verify", "--session", "s-v1")
    cli.ok("begin", "--attempt", two, "--item", "verify", "--session", "s-v2")
    # s-v2 died; s-v1 (a verifier of this batch) may not take its sub-plan over.
    out = cli.refused("begin", "--attempt", two, "--item", "verify", "--session", "s-v1", "--rebind")
    assert out["refused"] == "session_holds_other_subplan" and out["bound_subplan"] == one, out
    cli.ok("begin", "--attempt", two, "--item", "verify", "--session", "s-v3", "--rebind")
    # s-v2 was rebound away from `two`; it still held a sub-plan of this batch, for good.
    out = cli.refused("begin", "--attempt", f"{ATT}~verify-security_s2", "--item", "verify", "--session", "s-v2")
    assert out["refused"] == "session_holds_other_subplan" and out["bound_subplan"] == two, out


@pytest.mark.parametrize("ids", [["F1", "f1"], [" f1", "f2"], ["f 1", "f2"]])
def test_finding_ids_that_differ_only_in_case_or_carry_whitespace_are_refused(cli, repo, base, ids):
    batch_create(cli, repo, base)
    findings = [{**CORRECTNESS[0], "id": ids[0]}, {**CORRECTNESS[1], "id": ids[1]}]
    write_json(reports(cli) / "find-correctness.json", {"findings": findings})
    out = work(cli, FIND_C, "find", "s-find-c")
    assert out["outcome"] == "fail" and "findings[" in json.dumps(out["results"]), out
