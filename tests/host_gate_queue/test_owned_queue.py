"""Twenty-two finite accepted vectors, actual CLI/client/HTTP and owned children.

UNRUN by the Source author. No skip, in-process client stub, engine or live State.
The only function wrapper is the pipe barrier around the real daemon admission.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import select
from pathlib import Path
import subprocess

import pytest

from owned_support import BASE, FIXTURE, Harness, REPO, SHA, TIMEOUT, canonical, checksum


@pytest.fixture
def h(tmp_path):
    instance=Harness(tmp_path)
    try: yield instance
    finally: instance.close()


def continued(h, ticket):
    r=h.record(ticket)
    h.decide(ticket,"keep-reuse",retain=[c["attempt_id"] for c in r["contexts"][1:]],
             ack=[f["fingerprint"] for f in r["failures"]],purpose="continue-contexts")


def test_B01_bootstrap_real_client_and_stdlib_surfaces(h):
    registration=h.file("registrations.json",h.registrations())
    args=["submit","--registrations",registration,"--purpose","fixture",str(h.worktree),SHA]
    # These fail before the first HTTP request, not after a fake green adapter.
    result=h.cli(*args,owned=False,changes={"GATE_QUEUE_AITELIER_ROOT":h.aroot,
                 "AITELIER_DRIVER_ID":"fixture-driver"},rc=5)
    assert "adapter-unavailable" in result.stderr and "interpreter" in result.stderr
    assert not h.requests and not list((h.root/"tickets").glob("*"))
    h.cli(*args,changes={"GATE_QUEUE_AITELIER_ROOT":None},rc=5)
    missing=h.path/"missing-root"; (missing/"cli").mkdir(parents=True)
    (missing/"cli/client.py").write_text("")
    h.cli(*args,changes={"GATE_QUEUE_AITELIER_ROOT":missing},rc=5)
    h.cli(*args,changes={"AITELIER_DRIVER_ID":None,"AITELIER_ADMIN_TOKEN":"fixture-admin"},rc=5)
    assert not h.requests
    ticket=h.submit()
    first=h.requests[0]
    assert (first["method"],first["path"],first["driver_header_matches"]) == ("GET","/api/drivers/me",True)
    assert not any(r["admin_header_present"] for r in h.requests)
    r=h.record(ticket); p=r["adapter_provenance"]
    assert p["python"]==h.python and p["aitelier_root"]==str(h.aroot)
    assert p["cli_client_py_sha256"]==checksum((h.aroot/"cli/client.py").read_bytes())
    assert p["driver_credentials_py_sha256"]==checksum((h.aroot/"core/driver_credentials.py").read_bytes())
    assert not any(name.startswith(("api", "core.state")) for name in p["platform_modules_loaded"])
    count=len(h.requests)
    u=h.cli("submit-unowned",str(h.worktree),SHA,"--unowned-reason","fixture",owned=False).stdout.strip()
    h.cli("status",u,owned=False); h.cli("status",ticket,"--json",owned=False)
    h.cli("status","gq-20000101T000000Z-00000000",owned=False,rc=3)
    for args in (("list",),("daemon-status",),("--help",)):
        h.cli(*args,owned=False)
    h.cli("cancel",u,"--expected-sha",SHA,"--reason","fixture",owned=False)
    assert len(h.requests)==count


def test_B02_original_launcher_environment_survives_client_dotenv(h):
    (h.aroot/".env").write_text("GATE_QUEUE_FIXTURE_DOTENV=marker\nAITELIER_ADMIN_TOKEN=fixture-admin-not-used\n")
    a=h.submit(); h.complete(a)
    child=json.loads((h.reports/a/"child-environment.json").read_bytes())
    assert child["binding"]==h.env["GATE_BINDING_FILE"]
    assert {"HOME","GATE_RUN_SH","GATE_BINDING_FILE"}<=set(child["names"])
    assert not ({"AITELIER_DRIVER_ID","AITELIER_DRIVER_TOKEN","AITELIER_DRIVER_TOKEN_FILE",
                 "GATE_QUEUE_AITELIER_ROOT","GATE_QUEUE_FIXTURE_DOTENV","AITELIER_ADMIN_TOKEN","PYTHONPATH"}&set(child["names"]))
    assert not any(r["admin_header_present"] for r in h.requests)


def test_B03_daemon_configuration_refusal_keeps_bound_ticket_exact(h):
    u=h.cli("submit-unowned",str(h.worktree),SHA,"--unowned-reason","fixture",owned=False).stdout.strip()
    os.mkfifo(h.control/u); h.controls[u]=os.open(h.control/u,os.O_RDWR|os.O_NONBLOCK)
    a=h.submit(); raw=h.raw(a); h.requests.clear()
    daemon=h.daemon(owned=False,once=False)
    h.event("start",u); h.release(u); h.event("finish",u)
    _,stderr=h.finish(daemon,rc=5)
    assert "adapter-unavailable" in stderr and h.raw(a)==raw
    h.no_run(a,"queued"); assert not h.requests and not (h.root/"daemon.pid").exists()
    with (h.root/"daemon.lock").open("a") as fd:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)


def producer_failure(h):
    a=h.submit(); assert len(h.record(a)["contexts"])==5
    daemon=h.daemon(selected=True)
    h.timeline.append(dict(event="source_failure_consumed",source="fixture evaluated review"))
    raw=h.raw(a)
    with (h.root/"mutation.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        h.report(a,rc=4)
        assert h.raw(a)==raw and not h.write_posts()
    h.report(a)
    posts=h.write_posts(); assert posts
    post=posts[0]
    assert post["queue_state_at_POST"]=="held" and post["failures_at_POST"]==1, "queue disposition must precede failed State POST"
    assert len(posts)==1
    assert "failure-disposition" in post["history_at_POST"]
    assert post["body"]["fence"]==1 and post["body"]["expected_version"]==0
    assert not any("record_evidence" in r["path"] for r in h.requests)
    b=h.submit("-B")
    daemon.stdin.write("continue\n"); daemon.stdin.flush()
    h.event("start",b); h.release(b); h.event("finish",b); h.finish(daemon)
    h.no_run(a,"held")
    assert len(list((h.root/"tickets").glob("*/ticket.json")))==2
    assert len(h.write_posts())==1
    return a


def test_P01_normal_producer_holds_before_State_report_and_B_advances(h):
    a=producer_failure(h)
    assert len(h.record(a)["contexts"])==5
    assert (h.path/"source-review.json").read_bytes()==canonical(FIXTURE["source_review_report"])+b"\n"


def test_P02_ordinary_submit_has_no_silent_legacy_bypass(h):
    h.cli("submit",str(h.worktree),SHA,owned=False,rc=5)
    assert not list((h.root/"tickets").glob("*")) and not h.requests
    u=h.cli("submit-unowned",str(h.worktree),SHA,"--unowned-reason","fixture",owned=False).stdout.strip()
    assert "contexts" not in h.record(u) and "schema_version" not in h.record(u)


@pytest.mark.parametrize("change",["newer","failed","empty","identity"])
def test_P03_currentness_and_closed_owner_are_real_fields(h,change):
    a=h.submit(); node=h.nodes["fixture.one"]
    if change=="newer":
        newer=copy.deepcopy(node["attempts"][0]); newer.update(seq=1000,attempt_id="new-attempt")
        node["attempts"].insert(0,newer)
    elif change=="failed": node["attempts"][0].update(status="failed",terminal_observation_id="closed-observation",lease_state=None)
    elif change=="empty": node["attempts"]=[]
    else: node["attempts"][0]["context_hash"]="f"*64
    h.finish(h.daemon()); h.no_run(a,"held")
    assert not h.write_posts() and not any("terminal-report" in r["path"] for r in h.requests)


def test_P04_Source_and_runtime_proof_artifact_have_distinct_identity(h):
    a=h.submit(); assert all(h.attempts[c["attempt_id"]]["artifact_ref"] is None for c in h.record(a)["contexts"])
    h.complete(a)
    observation=h.observation(a,status="candidate")
    h.report(a,failure=False,observation=observation,physical=h.physical(a))
    assert h.write_posts()[0]["body"]["artifact"]=="c"*64
    assert h.write_posts()[0]["body"]["artifact_kind"]=="sha256"
    assert h.record(a)["source_sha"]==SHA and h.write_posts()[0]["body"]["fence"]==1


def test_P05_registration_base_Source_mismatch_refuses(h):
    registration=h.registrations(); registration["registrations"][0]["attempt"]["context"]["base_sha"]="f"*40
    h.cli("submit","--registrations",h.file("wrong-source.json",registration),"--purpose","fixture",str(h.worktree),SHA,rc=5)
    assert not list((h.root/"tickets").glob("*")) and not h.write_posts()


@pytest.mark.parametrize("change",["admin","me","claim-fence","mixed-owner"])
def test_P06_own_identity_and_distinct_attempt_claim_fences(h,change):
    registration=h.registrations(); env={}
    if change=="admin": env={"AITELIER_DRIVER_ID":None,"AITELIER_ADMIN_TOKEN":"fixture-admin"}
    elif change=="me": h.me["driver_id"]="foreign-driver"
    elif change=="claim-fence": registration["registrations"][0]["attempt"]["owner_fence"]=5
    else: registration["registrations"][0]["attempt"]["owner_driver_id"]="foreign-driver"
    # The response is an independent copy of the literal registration input.
    if change=="claim-fence": h.nodes["fixture.one"]["attempts"][0]["owner_fence"]=1
    h.cli("submit","--registrations",h.file("bad-owner.json",registration),"--purpose","fixture",str(h.worktree),SHA,changes=env,rc=5)
    assert not list((h.root/"tickets").glob("*")) and not h.write_posts()


def test_P07_one_shared_run_keeps_only_explicit_healthy_four(h):
    a=h.submit(); h.report(a); h.no_run(a,"held"); continued(h,a)
    r=h.record(a)
    assert len(r["contexts"])==5 and len(r["active_context_ids"])==4
    assert r["excluded_context_ids"]==["attempt-fixture-one"]
    h.complete(a)
    assert h.record(a)["verdict"]=="passed" and len(h.record(a)["failures"])==1
    # The failed member cannot consume the later green; the other four can.
    h.report(a,request="excluded-candidate",failure=False,observation=h.observation(a,status="candidate",observation_id="excluded"),physical=h.physical(a),rc=5)
    for number,context in enumerate(h.record(a)["contexts"][1:]):
        observation=h.observation(a,status="candidate",observation_id="retained-"+str(number),context=context)
        h.cli("report-context",*h.identity_args(a,"retained-"+str(number)),"--context",context["attempt_id"],
              "--observation",observation,"--physical-receipt",h.physical(a))
    assert len(h.write_posts())==5
    assert [r["body"]["status"] for r in h.write_posts()]==["failed"]+["candidate"]*4
    assert len(list((h.root/"tickets").glob("*/ticket.json")))==1


def test_P08_changed_member_does_not_destroy_useful_four_or_clear_foreign_owner(h):
    a=h.submit(); newer=copy.deepcopy(h.nodes["fixture.one"]["attempts"][0])
    newer.update(seq=1000,attempt_id="foreign-attempt",owner_driver_id="foreign-driver",owner_fence=2)
    h.nodes["fixture.one"]["attempts"].insert(0,newer)
    h.finish(h.daemon()); h.no_run(a,"held")
    continued(h,a); h.complete(a)
    r=h.record(a)
    assert len(r["contexts"])==5 and len(r["active_context_ids"])==4
    assert newer["owner_driver_id"]=="foreign-driver" and not h.write_posts()


@pytest.mark.parametrize("change",["none","new-failure","binding","unknown"])
def test_P09_baseline_keep_is_exact_and_accepts_no_contexts(h,change):
    a=h.submit(); h.report(a); r=h.record(a)
    h.decide(a,"keep-reuse",retain=[],ack=[f["fingerprint"] for f in r["failures"]],
             purpose="baseline-evidence:comparison",authority="attempt-fixture-one")
    if change=="new-failure":
        context=h.record(a)["contexts"][1]
        failure=h.failure(a,decision_id="fixture-failure-two",affected_context_ids=[context["attempt_id"]],
                          applicability_reason="second governing source failure")
        observation=h.observation(a,status="running",observation_id="fixture-observation-two",context=context)
        h.cli("report-context",*h.identity_args(a,"new-failure"),"--context",context["attempt_id"],
              "--failure-decision",failure,"--observation",observation)
    elif change=="binding":
        path=h.root/"tickets"/a/"ticket.json"; value=h.record(a); value["purpose"]="changed"
        path.write_bytes(canonical(value))  # deliberate corruption after real ordinary submit
    elif change=="unknown": h.mode["fixture.one"]="unavailable"
    if change=="none":
        h.complete(a); assert h.record(a)["active_context_ids"]==[]
    else:
        h.finish(h.daemon()); h.no_run(a,"held")
    assert len(h.record(a)["failures"])>=1


def test_P10_cancel_wins_against_real_daemon_stale_selection(h):
    a=h.submit(); b=h.submit("-B"); preimage=h.raw(a)
    daemon=h.daemon(selected=True)
    h.cli("daemon","--once",rc=4)
    with (h.root/"mutation.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        h.decide(a,"cancel",request="busy-cancel",rc=4)
        assert h.raw(a)==preimage and not h.write_posts()
    h.decide(a,"cancel")
    r=h.record(a); history=r["history"][-1]
    assert Path(history["path"]).read_bytes()==preimage
    assert history["sha256"]==checksum(preimage)
    daemon.stdin.write("continue\n"); daemon.stdin.flush()
    h.event("start",b); h.release(b); h.event("finish",b); h.finish(daemon)
    h.no_run(a,"cancelled")
    assert len([e for e in h.timeline if e.get("event")=="start"])==1


def test_P11_admission_wins_finish_and_exclude_until_actual_quiescence(h):
    a=h.submit(); daemon=h.daemon(); start=h.event("start",a)
    assert daemon.poll() is None and start["identity"]["nonce"]==h.record(a)["invocation_identity"]["nonce"]
    h.report(a,rc=6)
    r=json.loads(h.cli("status",a,"--json",owned=False).stdout)
    assert r["state"]=="running" and r["excluded_context_ids"]==["attempt-fixture-one"]
    assert r["state_delivery_by_context"]["attempt-fixture-one"]["state_delivery"]=="deferred-live"
    assert not h.write_posts() and daemon.poll() is None
    h.release(a); h.event("finish",a); h.finish(daemon)
    # Same immutable failure decision, a new wrapper ID and actual completion report.
    observation=h.observation(a,observation_id="actual-completion")
    h.report(a,request="final-delivery",observation=observation,physical=h.physical(a))
    assert len(h.write_posts())==1 and h.write_posts()[0]["queue_state_at_POST"]=="done"
    assert len(h.record(a)["failures"])==1 and len(h.record(a)["active_context_ids"])==4


@pytest.mark.parametrize("transport",["commit-disconnect","refuse"])
def test_P12_unknown_delivery_replays_exact_observation_without_new_work(h,transport):
    a=h.submit(); h.mode["write"]=transport
    observation=h.observation(a); failure=h.failure(a)
    first=h.report(a,observation=observation,failure=failure,rc=7)
    assert json.loads(first.stdout)["state_delivery"] in ("unknown","refused")
    h.no_run(a,"held"); body=copy.deepcopy(h.write_posts()[0]["body"])
    h.mode["write"]="normal"
    h.report(a,observation=observation,failure=failure)
    assert len(h.write_posts())==2 and all(p["body"]==body for p in h.write_posts())
    assert any(r["path"].endswith("get_attempt") and r["body"]=={"attempt_id":"attempt-fixture-one"} for r in h.requests)
    assert len(h.record(a)["failures"])==1 and len(list((h.root/"tickets").glob("*/ticket.json")))==1


def test_P13_failed_observation_requires_explicit_governing_decision(h):
    a=h.submit(); h.report(a,failure=False,rc=5); assert not h.write_posts()
    observation=h.observation(a,status="running",observation_id="generic-note")
    value=json.loads(Path(observation).read_bytes()); value["detail"]="worker says FAIL; not a director decision"
    Path(observation).write_bytes(canonical(value))
    h.report(a,failure=False,observation=observation)
    assert h.record(a)["state"]=="queued" and h.record(a)["failures"]==[]


def test_P14_unknown_A_is_held_once_and_independent_B_runs(h):
    a=h.submit(); b=h.submit("-B"); h.mode["fixture.one"]="unavailable"
    daemon=h.daemon(); h.event("start",b); h.release(b); h.event("finish",b); h.finish(daemon)
    h.no_run(a,"held"); assert h.record(a)["hold"]=="owner_unknown"
    count=sum(r["body"]=={"project_id":"fixture-project","node_key":"fixture.one"} for r in h.requests)
    h.finish(h.daemon())
    assert sum(r["body"]=={"project_id":"fixture-project","node_key":"fixture.one"} for r in h.requests)==count


@pytest.mark.parametrize("case",["cancelled","exact-live","uncertain","exited-no-verdict"])
def test_P15_restart_never_reissues_admitted_or_cancelled_work(h,case):
    a=h.submit()
    if case=="cancelled":
        h.decide(a,"cancel"); h.finish(h.daemon()); h.no_run(a,"cancelled"); return
    daemon=h.daemon(); h.event("start",a)
    daemon.kill(); h.finish(daemon,rc=-9)
    if case=="uncertain":
        path=h.root/"tickets"/a/"ticket.json"; record=h.record(a)
        record["invocation_identity"]["birth"]=None
        path.write_bytes(canonical(record))  # actual admitted ticket, damaged durable spawn identity
        b=h.submit("-B"); h.finish(h.daemon(),rc=7)
        assert h.record(a)["hold"]=="recovery_unknown" and h.record(b)["state"]=="queued"
        h.release(a); h.event("finish",a)
    else:
        restarted=h.daemon()
        h.log_event(restarted,"adopting exact owned invocation")
        h.release(a,"noverdict" if case=="exited-no-verdict" else 0)
        h.event("finish",a); h.finish(restarted)
        assert h.record(a)["state"]==("failed" if case=="exited-no-verdict" else "done")
    assert len([e for e in h.timeline if e.get("event")=="start" and e["ticket"]==a])==1
    assert not any("docker" in str(r) for r in h.requests)


def test_P16_actual_exit_one_is_native_red_and_differs_from_no_execution(h):
    a=h.submit(); h.complete(a,rc=1); r=h.record(a)
    assert r["state"]=="done" and r["verdict"]=="failed" and r["gate_exit_code"]==1
    assert not h.write_posts() and r["admitted_at"] and r["report_dir"]


def test_P17_CAS_history_idempotency_and_cancelled_finality(h):
    a=h.submit(); pre=h.raw(a)
    for option,value in (("--expected-sha","f"*40),("--binding-sha256","f"*64),("--expected-revision","99")):
        h.decide(a,"cancel",request="bad-"+option[2:],extra=(option,value),rc=5)
        assert h.raw(a)==pre
    other=h.submit("-other")
    wrong=h.identity_args(a,"wrong-ticket"); wrong[0]=other
    h.cli("decide",*wrong,"--action","cancel","--reason","wrong ticket",rc=5)
    assert h.raw(a)==pre
    args=["decide",*h.identity_args(a,"cancel-original"),"--action","cancel","--reason","first"]
    h.cli(*args); after=h.raw(a)
    h.cli(*args); assert h.raw(a)==after
    args[-1]="changed"; h.cli(*args,rc=5); assert h.raw(a)==after
    r=h.record(a); history=r["history"][-1]
    assert Path(history["path"]).read_bytes()==pre and history["sha256"]==checksum(pre)
    h.decide(a,"keep-reuse",request="cannot-revive",purpose="continue-contexts",retain=[],ack=[],rc=5)
    assert h.raw(a)==after and not h.write_posts()
    b=h.submit("-retry"); assert b!=a and h.record(b)["state"]=="queued"


@pytest.mark.parametrize("malformed",["wrapped-node","missing-me","evidence-object","seq","wrapped-attempt"])
def test_P18_actual_unwrapped_HTTP_shapes_are_required(h,malformed):
    a=h.submit()
    if malformed=="wrapped-node": h.nodes["fixture.one"]={"result":h.nodes["fixture.one"]}
    elif malformed=="missing-me": h.me={}
    elif malformed=="evidence-object": h.evidence["attempt-fixture-source-review"]={"result":[]}
    elif malformed=="seq": h.nodes["fixture.one"]["attempts"][0].pop("seq")
    else:
        source=h.attempts["attempt-fixture-source-review"]
        h.attempts["attempt-fixture-source-review"]={"result":source}
    if malformed in ("evidence-object","wrapped-attempt"):
        h.report(a,rc=5 if malformed=="evidence-object" else 7)
        assert not h.write_posts() and h.record(a)["state"]=="queued"
    elif malformed=="missing-me":
        before=h.raw(a); h.finish(h.daemon(),rc=5); assert h.raw(a)==before
    else:
        h.finish(h.daemon()); h.no_run(a,"held")
    assert h.requests and all("arguments" not in (r["body"] or {}) for r in h.requests)


def test_P09_authoritative_State_failed_without_product_fail_report_can_keep_baseline(h):
    a=h.submit()
    h.nodes["fixture.one"]["attempts"][0].update(status="failed",terminal_observation_id="authoritative-terminal",lease_state=None)
    h.finish(h.daemon()); h.no_run(a,"held")
    r=h.record(a)
    assert len(r["failures"])==1 and "authoritative_State_failure" in r["failures"][0]
    assert not h.write_posts()
    h.decide(a,"keep-reuse",retain=[],ack=[r["failures"][0]["fingerprint"]],
             purpose="baseline-evidence:comparison",authority="attempt-fixture-one")
    h.complete(a)
    assert h.record(a)["active_context_ids"]==[] and h.record(a)["verdict"]=="passed"


def test_P15_adopted_no_verdict_exit_finishes_deferred_State_delivery_once(h):
    # The original P15 checked state/no-rerun but omitted completion delivery.
    # Exercise the same real child through daemon loss, adoption and final report.
    a=h.submit()
    original=h.daemon()
    start=h.event("start",a)
    identity=h.record(a)["invocation_identity"]
    assert identity["pid"]==start["identity"]["pid"] and identity["birth"]
    assert identity["nonce"]==start["identity"]["nonce"]
    h.report(a,rc=6)
    live=h.record(a)
    assert live["state"]=="running" and not h.write_posts()
    assert live["excluded_context_ids"]==["attempt-fixture-one"]
    assert len(live["contexts"])==5 and len(live["active_context_ids"])==4
    assert live["state_delivery_by_context"]["attempt-fixture-one"]["state_delivery"]=="deferred-live"
    original.kill()  # Only this fixture's owned daemon; the same child remains live.
    h.finish(original,rc=-9)
    adopted=h.daemon()
    h.log_event(adopted,"adopting exact owned invocation")
    assert h.record(a)["invocation_identity"]==identity and not h.write_posts()
    h.release(a,"noverdict")
    h.event("finish",a)
    h.finish(adopted)
    assert h.child_pidfds and all(select.select([fd],[],[],TIMEOUT)[0] for fd in h.child_pidfds), "owned child exit was not acknowledged"
    completed=h.record(a)
    assert completed["state"]=="failed"
    assert completed["gate_exit_code"] is completed["verdict"] is completed["report_dir"] is None
    assert not (h.reports/a/"gate.gate.exit").exists() and not (h.reports/a/"manifest.json").exists()
    assert completed["invocation_identity"]==identity
    assert completed["excluded_context_ids"]==live["excluded_context_ids"]
    assert completed["active_context_ids"]==live["active_context_ids"]
    assert completed["failures"]==live["failures"]
    assert sum(e.get("event")=="start" and e["ticket"]==a for e in h.timeline)==1
    assert not h.write_posts(), "daemon exit alone must not forward a terminal State report"
    # The recovery observer knows this exact child exited, but not its wait status.
    exit_ref=completed["invocation_exit"]
    raw=Path(exit_ref["path"]).read_bytes()
    assert checksum(raw)==exit_ref["sha256"]
    exit_receipt=json.loads(raw)
    assert exit_receipt["invocation_identity"]==identity and exit_receipt["runner_rc"] is None
    proof=h.file("owned-child-no-verdict-exit.json",dict(invocation_identity=identity,
        child_exit_ack="pidfd-readable",process_observed_exited=True,no_native_verdict=True))
    physical=h.file("physical-no-verdict.json",dict(ticket=a,source_sha=SHA,
        invocation_identity=identity,outcome="owned-quiescent",
        actual_ownership_refs_and_sha256=[dict(path=proof,sha256=checksum(Path(proof).read_bytes()))],
        raw_gate_exit_ref_or_null=None,
        report_scope=dict(attempt_ids=[c["attempt_id"] for c in completed["contexts"]])))
    observation=h.observation(a,observation_id="actual-recovered-no-verdict-completion")
    result=h.report(a,request="recovered-final-delivery",observation=observation,physical=physical)
    assert json.loads(result.stdout)["state_delivery"]=="acknowledged"
    assert len(h.write_posts())==1 and h.write_posts()[0]["body"]["status"]=="failed"
    assert "artifact" not in h.write_posts()[0]["body"] and "artifact_kind" not in h.write_posts()[0]["body"]
    # Identical wrapper replay must not send another terminal State report.
    h.report(a,request="recovered-final-delivery",observation=observation,physical=physical)
    assert len(h.write_posts())==1 and not any("record_evidence" in r["path"] for r in h.requests)
    final=h.record(a)
    assert final["excluded_context_ids"]==live["excluded_context_ids"] and final["failures"]==live["failures"]
    assert final["gate_exit_code"] is final["verdict"] is None
    assert original.poll() is not None and adopted.poll() is not None
