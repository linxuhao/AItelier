"""Accepted package-descriptor inputs bind to real State producers and receipts.

UNRUN in the source-only authoring workflow; the director registers the offline
evaluation. Every producer, evidence row and receipt below is created through
the public State service; no SQL receipt is forged. Git source dependencies keep
their unchanged selected-base inclusion check (see the existing source tests).

Effect observers wrap the ORIGINAL functions and still call them, so a refusal
is proven to precede base request, host-contract pin, relay pin, execution claim
and launcher by observation rather than by replacing those effects.
"""
import base64
import copy
import hashlib
import json

import pytest
from skillflow.graph import PipelineGraph, StepNode

from core.review_input_bundle import PREFIX, manifest
from tests.integration.test_state_graph_entrypoints import _frozen, _required, live, spec  # noqa: F401

DESCRIPTOR = (json.dumps({"schema": "package-release/v1", "package": "fixture-sdk", "version": "1.0.0",
                          "source": "0" * 40}, sort_keys=True) + "\n").encode()


def _terminal(live, key, payload):
    service = live.service
    service.store.add_nodes("game", [spec(key)])
    attempt = service.start_external_attempt("game", key, 1, "fixture", key + "-ext", key + "-req")
    artifact = hashlib.sha256(payload).hexdigest()
    # Fresh, scoped report bytes per producer: the body names this attempt and
    # its artifact, so candidate report-digest reuse protection is never hit.
    body = json.dumps({"status": "candidate", "settled": True, "usable": True,
                       "attempt_id": attempt["attempt_id"], "artifact": artifact}, sort_keys=True).encode()
    report = live.tmp / (key + "-terminal.json")
    report.write_bytes(body)
    return service.report_external_attempt(attempt["attempt_id"], key + "-end", 0, attempt["context_hash"],
        "candidate", str(report), hashlib.sha256(body).hexdigest(), True, artifact, "sha256")


def _accept(live, key, payload):
    """Real terminal candidate + independent evidence rows + verify_node."""
    done = _terminal(live, key, payload)
    artifact = done["artifact_ref"]
    for check in ("behaviour", "review"):
        path = live.tmp / (key + "-" + check + ".json")
        path.write_text(json.dumps({"status": "completed", "settled": True, "usable": True,
                                    "verdict": "pass", "criterion_id": check,
                                    "attempt_id": done["attempt_id"]}))
        live.service.record_evidence(done["attempt_id"], key + check, check, "pass", artifact, str(path),
                                     hashlib.sha256(path.read_bytes()).hexdigest())
    receipt = live.service.verify_node("game", key, 1, done["attempt_id"])
    return done, receipt["receipt_id"]


def _arguments(producer, receipt_id=None, primary="package.json", raw=DESCRIPTOR):
    items = [{"name": "package.json", "reference": "package://fixture-sdk/1.0.0",
              "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw),
              "content_base64": base64.b64encode(raw).decode()}]
    arguments = {"producer": {"project_id": "game", "attempt_id": producer["attempt_id"],
                              "artifact": producer["artifact_ref"]}, "items": items}
    if receipt_id is not None:
        arguments["acceptance"] = {"receipt_id": receipt_id, "primary_item": primary}
    return arguments


def _raw_expected(arguments):
    """The expected identity built WITHOUT the strict constructor, so a request
    the constructor would reject still reaches the public start_attempt path."""
    out = {"producer": copy.deepcopy(arguments["producer"]), "items": [
        {k: item[k] for k in ("name", "reference", "sha256", "size")} for item in arguments["items"]]}
    if "acceptance" in arguments:
        out["acceptance"] = copy.deepcopy(arguments["acceptance"])
    return out


def _check(producer, receipt_id=None, primary="package.json", raw=DESCRIPTOR, cid="package-input"):
    arguments = _arguments(producer, receipt_id, primary, raw)
    # Independent deep copies: expected never aliases the supplied arguments.
    return _required(cid, "review_input_bundle", copy.deepcopy(manifest(arguments)), **arguments)


def _watch(live, monkeypatch):
    """Count calls to the ORIGINAL effect functions while still invoking them."""
    import core.run_isolation as isolation
    import core.run_launcher as launcher
    calls = []
    for obj, name in [(launcher, "start_config_run"), (isolation, "request_base"),
                      (live.service, "request_attempt_base"),
                      (live.service.attempts, "pin_host_contract"),
                      (live.service.attempts, "claim_launch"), (live.service.attempts, "pin_relay")]:
        original = getattr(obj, name)
        def wrapped(*a, _n=name, _o=original, **k):
            calls.append(_n)
            return _o(*a, **k)
        monkeypatch.setattr(obj, name, wrapped)
    return calls


def _launch(live, check, request_key, node="consumer", extra=(), workflow="state_fixture"):
    return live.service.start_attempt("game", node, 1, workflow, request_key,
                                      frozen_prerequisites=_frozen(check, *extra))


def _assert_no_effect(live, attempt, calls):
    assert attempt["status"] == "failed" and attempt["run_id"] is None
    assert live.sf.list_runs(project_id=attempt["execution_project_id"]) == []
    assert calls == []
    assert "host_contract" not in (live.service.attempts.get(attempt["attempt_id"])["context"] or {})


def test_accepted_package_descriptor_launches_with_actual_witness(live):
    producer, receipt = _accept(live, "pkg", DESCRIPTOR)
    live.service.store.add_nodes("game", [spec("consumer", ["pkg"])])
    attempt = _launch(live, _check(producer, receipt), "package-positive")
    assert attempt["status"] == "running" and attempt["run_id"]
    from core.seed_publication import seed_dir
    path = seed_dir(live.sf, attempt["execution_project_id"], attempt["workflow"]) / (PREFIX + "package.json")
    assert path.read_bytes() == DESCRIPTOR
    retained = live.service.attempts.get(attempt["attempt_id"])["context"]["frozen_prerequisites"]
    assert retained["checks"][0]["expected"]["acceptance"] == {
        "receipt_id": receipt, "primary_item": "package.json"}


@pytest.mark.parametrize("case", ["wrong-receipt", "wrong-primary", "digest-mismatch", "missing-receipt",
                                  "wrong-artifact", "foreign-project", "unknown-attempt",
                                  "nonterminal", "not-dependency"])
def test_false_producer_or_receipt_refuses_before_any_launch_effect(live, monkeypatch, case):
    producer, receipt = _accept(live, "pkg", DESCRIPTOR)
    live.service.store.add_nodes("game", [spec("consumer", ["pkg"]), spec("unrelated-consumer")])
    node = "consumer"
    if case == "wrong-receipt":
        _, other = _accept(live, "pkg-other", b"other package descriptor\n")
        check = _check(producer, other)
    elif case == "wrong-primary":
        # Built without the strict constructor so the request reaches start_attempt.
        arguments = _arguments(producer, receipt, primary="absent.json")
        check = _required("package-input", "review_input_bundle", _raw_expected(arguments), **arguments)
    elif case == "digest-mismatch":
        check = _check(producer, receipt, raw=b"different descriptor\n")
    elif case == "missing-receipt":
        check = _check(producer, "receipt-missing")
    elif case == "wrong-artifact":
        check = _check({**producer, "artifact_ref": "f" * 64}, receipt)
    elif case == "foreign-project":
        check = _check(producer, receipt)
        check["arguments"]["producer"]["project_id"] = "foreign"
        check["expected"] = copy.deepcopy(manifest(check["arguments"]))
    elif case == "unknown-attempt":
        check = _check({**producer, "attempt_id": "attempt-nonexistent"}, receipt)
    elif case == "nonterminal":
        live.service.store.add_nodes("game", [spec("running-producer")])
        running = live.service.start_external_attempt("game", "running-producer", 1, "fixture", "r-ext", "r-req")
        check = _check({**producer, "attempt_id": running["attempt_id"]}, receipt)
    else:
        check = _check(producer, receipt)
        node = "unrelated-consumer"
    calls = _watch(live, monkeypatch)
    attempt = _launch(live, check, "refuse-" + case, node)
    _assert_no_effect(live, attempt, calls)


@pytest.mark.parametrize("mutation", ["producer", "acceptance", "two-bundles"])
def test_frozen_expected_mismatch_refuses_before_base_request(live, monkeypatch, mutation):
    """Regression for the retained efa601c witnesses: valid arguments with a
    DIFFERENT actual frozen `expected`, or two individually valid bundles, were
    refused only after request_attempt_base -> request_base -> pin_host_contract."""
    producer, receipt = _accept(live, "pkg", DESCRIPTOR)
    live.service.store.add_nodes("game", [spec("consumer", ["pkg"])])
    check = _check(producer, receipt)
    extra = []
    if mutation == "producer":
        check["expected"]["producer"]["attempt_id"] = "attempt-frozen-other"
    elif mutation == "acceptance":
        check["expected"]["acceptance"]["receipt_id"] = "receipt-frozen-other"
    else:
        second = copy.deepcopy(check)
        second["id"] = "package-two"
        extra = [second]
    required = copy.deepcopy(check["expected"])
    calls = _watch(live, monkeypatch)
    attempt = _launch(live, check, "frozen-" + mutation, extra=extra)
    _assert_no_effect(live, attempt, calls)
    refused = live.service.attempts.get(attempt["attempt_id"])
    assert refused["context"]["frozen_prerequisites"]["checks"][0]["expected"] == required
    if mutation == "two-bundles":
        assert "exactly one review input bundle" in (refused.get("error") or "")
    else:
        # Compared with the ACTUAL frozen expected, not a manifest rebuilt from arguments.
        assert "frozen expected identity" in (refused.get("error") or "")



def test_receiver_not_reading_own_seed_refuses_before_base_request(live, monkeypatch):
    name = "state_unseeded_fixture"
    graph = PipelineGraph(name=name, begin="work", steps=[StepNode(id="work", context=[
        {"source": {"config": name, "output": "other.md", "required": False}}])])
    live.sf.register_graph(graph)
    live.registry.register_one(live.sf, name, hint_overrides={"scheduler_owned": True, "repo_mode": "none",
                               "seed_file": "plan.md", "output_step": "work"})
    producer, receipt = _accept(live, "pkg", DESCRIPTOR)
    live.service.store.add_nodes("game", [spec("consumer", ["pkg"])])
    calls = _watch(live, monkeypatch)
    attempt = _launch(live, _check(producer, receipt), "unseeded", workflow=name)
    _assert_no_effect(live, attempt, calls)


def test_ordinary_candidate_review_input_needs_no_acceptance(live):
    producer = _terminal(live, "candidate-only", DESCRIPTOR)
    live.service.store.add_nodes("game", [spec("consumer")])
    attempt = _launch(live, _check(producer), "candidate-review")
    assert attempt["status"] == "running"
    # Being reviewed does not promote the candidate producer.
    assert live.service.attempts.get(producer["attempt_id"])["status"] == "candidate"


def test_existing_run_replay_recovers_without_new_admission_effects(live, monkeypatch):
    producer, receipt = _accept(live, "pkg", DESCRIPTOR)
    live.service.store.add_nodes("game", [spec("consumer", ["pkg"])])
    check = _check(producer, receipt)
    first = _launch(live, check, "recover")
    assert first["status"] == "running" and first["run_id"]
    calls = _watch(live, monkeypatch)
    again = _launch(live, check, "recover")
    assert again["attempt_id"] == first["attempt_id"] and again["run_id"] == first["run_id"]
    assert len(live.sf.list_runs(project_id=first["execution_project_id"])) == 1
    assert not {"request_attempt_base", "request_base", "pin_host_contract", "start_config_run"} & set(calls)


def test_unknown_launch_with_inputs_is_not_duplicated(live, monkeypatch):
    import core.run_launcher as launcher
    producer, receipt = _accept(live, "pkg", DESCRIPTOR)
    live.service.store.add_nodes("game", [spec("consumer", ["pkg"])])
    launched = []
    def uncertain(*a, **k):
        launched.append(1)
        raise OSError("uncertain transport failure")
    monkeypatch.setattr(launcher, "start_config_run", uncertain)
    first = _launch(live, _check(producer, receipt), "unknown")
    assert first["status"] == "unknown" and first["run_id"] is None
    recovered = live.service.recover_attempt(first["attempt_id"])
    assert recovered["recovery_required"] is True
    assert launched == [1]


def test_replay_with_changed_acceptance_conflicts_without_second_launch(live):
    producer, receipt = _accept(live, "pkg", DESCRIPTOR)
    live.service.store.add_nodes("game", [spec("consumer", ["pkg"])])
    first = _launch(live, _check(producer, receipt), "replay")
    assert first["status"] == "running"
    changed = _check(producer, receipt)
    changed["arguments"]["acceptance"]["receipt_id"] = receipt + "-changed"
    changed["expected"] = copy.deepcopy(manifest(changed["arguments"]))
    with pytest.raises(Exception):
        _launch(live, changed, "replay")
    assert len(live.sf.list_runs(project_id=first["execution_project_id"])) == 1


def test_acceptance_shape_is_strict_and_old_manifest_identity_is_unchanged():
    raw = DESCRIPTOR
    item = {"name": "package.json", "reference": "package://x", "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw), "content_base64": base64.b64encode(raw).decode()}
    producer = {"project_id": "game", "attempt_id": "a", "artifact": "b" * 64}
    ordinary = manifest({"producer": producer, "items": [item]})
    assert set(ordinary) == {"producer", "items"}
    accepted = manifest({"producer": producer, "items": [item],
                         "acceptance": {"receipt_id": "r", "primary_item": "package.json"}})
    assert accepted["acceptance"] == {"receipt_id": "r", "primary_item": "package.json"}
    for bad in ({"receipt_id": "r"}, {"receipt_id": "r", "primary_item": "absent"},
                {"receipt_id": "r", "primary_item": "package.json", "extra": "x"}):
        with pytest.raises(ValueError):
            manifest({"producer": producer, "items": [item], "acceptance": bad})
