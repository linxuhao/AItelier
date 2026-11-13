"""The anonymous attempt reads do not publish the attempt's context digest.

An attempt's `context_hash` is sha256 of its PRE-redaction context, and that
context carries `host_contract.source_repo`. An anonymous caller holds every
other byte of the context in the same answer, so a published digest lets them
set `source_repo` to a guessed path, hash it, and compare: a match confirms the
guess. The projection in `core.state_commands` therefore withholds the digest
for anonymous callers on every attempt-bearing read, and the trusted caller
keeps the recorded value.

Covered, for an unsigned caller (`project_read_trusted=False`) on
`get_attempt`, `attempt_detail`, `list_attempts`, `get_node`:
  c1 every `context_hash` reachable in the answer is None, the recorded digest
     never appears as a value or as a substring of any string in the answer,
     and the read is still non-empty; the trusted read of the same attempt does
     carry sha256(canonical(context)), so the forbidden digest really exists.
  c4 the digest is withheld on the two other channels that carry it:
     `external_observations[].context_hash` (attempt_detail) and the
     `provenance_json` string of an acceptance receipt (attempt_detail
     `receipts[]`, get_node `dependency_receipts[].acceptance`). A
     `provenance_json` that is not a JSON object comes back byte-identical.

Real store, real `execute` handler; the caller's trust is the only thing that
changes between the two arms, as in `test_public_attempt_context_source_path.py`.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from core.state_commands import _without_provenance_digest, execute
from core.state_database import StateDatabase
from core.state_graph import canonical, digest
from core.state_service import StateService

PID = "demo-project"
NODE = "a"
REPO_PATH = "/home/somebody/.AItelier/projects/demo-repo"
COMMON_DIR = "/home/somebody/.AItelier/projects/demo-repo/.git"
DESCRIPTOR = {"source_repo": REPO_PATH, "seed_file": "plan.md", "output_step": "work",
              "scheduler_owned": True, "repo_mode": "code"}
ARTIFACT = "a" * 64
CRITERIA = ("behaviour", "review")
READS = ["get_attempt", "attempt_detail", "list_attempts", "get_node"]


def _strings(value):
    """Every string value in a JSON-like answer, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _context_hashes(value):
    """Every `context_hash` value in a JSON-like answer, at any depth, including
    the ones inside a `provenance_json` string (parsed, never assumed)."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "context_hash":
                yield item
            if key == "provenance_json" and isinstance(item, str):
                try:
                    provenance = json.loads(item)
                except (TypeError, ValueError):
                    provenance = None
                if isinstance(provenance, dict) and "context_hash" in provenance:
                    yield provenance["context_hash"]
            yield from _context_hashes(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _context_hashes(item)


def _host_contracts(value):
    """Every `host_contract` mapping in a JSON-like answer, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "host_contract" and isinstance(item, dict):
                yield item
            yield from _host_contracts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _host_contracts(item)


def _args(action, aid):
    return {
        "get_attempt": {"attempt_id": aid},
        "attempt_detail": {"attempt_id": aid},
        "list_attempts": {"project_id": PID, "node_key": NODE, "limit": 100},
        "get_node": {"project_id": PID, "node_key": NODE},
    }[action]


def _spec(key):
    return {"key": key, "goal": "Deliver " + key, "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Required behaviour passes"},
        {"id": "review", "kind": "review", "description": "A subagent independently reviews it"}]}


def _seeded(tmp_path):
    """A public project with one reserved attempt whose host contract is pinned,
    built the way the product writes it (`pin_host_contract`)."""
    svc = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                       actor="attempt-digest", project_read_trusted=True)
    svc.create_project(PID, "Demo project")
    svc.store.add_nodes(PID, [{"key": NODE, "goal": "A", "acceptance": [
        {"id": "c", "kind": "test", "description": "d"}]}])
    svc.portfolio.bind_source(PID, REPO_PATH, COMMON_DIR, 0)
    svc.open_project(PID)
    attempt = svc.attempts.reserve(PID, NODE, 1, "feature", "request-1")
    pinned = svc.attempts.pin_host_contract(attempt["attempt_id"], dict(DESCRIPTOR))
    assert pinned["context"]["host_contract"] == DESCRIPTOR, "fixture must really pin the descriptor"
    return svc, attempt["attempt_id"]


def _answer(service, trusted, action, aid):
    service.project_read_trusted = trusted
    return execute(service, action, _args(action, aid))


def _recorded_digest(service, aid):
    """sha256(canonical(recorded context)): the pre-redaction digest the product
    records, recomputed from the stored context, never from an answer."""
    return digest(service.attempts.get(aid)["context"])


# c1 ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", READS)
def test_anonymous_attempt_read_withholds_the_recorded_context_digest(tmp_path, action):
    service, aid = _seeded(tmp_path)
    recorded = _recorded_digest(service, aid)
    answer = _answer(service, False, action, aid)

    # (a) every context_hash reachable in the answer is None, at any depth.
    hashes = list(_context_hashes(answer))
    assert hashes, f"anonymous {action} carries no context_hash key at all: the key must stay"
    assert all(h is None for h in hashes), f"anonymous {action} publishes a digest: {hashes}"

    # (b) the recorded digest appears nowhere: not as a value, not as a substring
    # of any string in the answer.
    assert recorded not in set(_context_hashes(answer))
    assert not any(recorded in s for s in _strings(answer)), \
        f"anonymous {action} carries the recorded digest inside a string"

    # (c) the read is still non-empty: the attempt, its node and its status are
    # present, and the other host_contract keys are intact.
    strings = set(_strings(answer))
    assert aid in strings, f"anonymous {action} lost the attempt id"
    assert NODE in strings, f"anonymous {action} lost the node key"
    assert "reserved" in strings, f"anonymous {action} lost the attempt status"
    contracts = list(_host_contracts(answer))
    assert len(contracts) == 1, f"anonymous {action} carries {len(contracts)} host contracts"
    assert {k: v for k, v in contracts[0].items() if k != "source_repo"} == \
        {k: v for k, v in DESCRIPTOR.items() if k != "source_repo"}


@pytest.mark.parametrize("action", READS)
def test_trusted_attempt_read_keeps_the_recorded_context_digest(tmp_path, action):
    service, aid = _seeded(tmp_path)
    recorded = _recorded_digest(service, aid)
    answer = _answer(service, True, action, aid)

    # The control: the forbidden digest really exists and the trusted caller gets
    # exactly sha256(canonical(recorded context)) for each attempt it reads.
    hashes = list(_context_hashes(answer))
    assert hashes and all(h == recorded for h in hashes), \
        f"trusted {action} must carry the recorded digest, got {hashes}"


@pytest.mark.parametrize("action", READS)
def test_the_guess_is_not_confirmed_by_the_anonymous_answer(tmp_path, action):
    """TEETH: the attack the digest enabled. The attacker has the anonymous answer
    (context with source_repo blanked, digest withheld). They fill the TRUE
    source_repo back in, recompute the digest of that context, and compare with
    the value a trusted read of the same attempt returns. A match would confirm
    the guess; the anonymous answer must not let it match."""
    service, aid = _seeded(tmp_path)
    trusted = _answer(service, True, action, aid)
    published = {h for h in _context_hashes(trusted)}
    assert len(published) == 1, f"the trusted read must publish exactly one digest, got {published}"
    (published,) = published

    anonymous = _answer(service, False, action, aid)
    # The attacker's reconstruction: the anonymous context with the TRUE path put
    # back, hashed the way the product hashes a context. Sound only if it can
    # reproduce the published value, so first prove the probe is not vacuous.
    contexts = list(_contexts(anonymous))
    assert contexts, f"anonymous {action} carries no attempt context to probe"
    rebuilt = json.loads(json.dumps(contexts[0]))
    rebuilt["host_contract"]["source_repo"] = REPO_PATH
    assert digest(rebuilt) == published, "the true guess must reproduce the recorded digest (probe is sound)"

    # The anonymous answer must not give the attacker anything to compare that
    # reconstruction against: no digest it publishes equals the recorded one.
    assert published not in set(_context_hashes(anonymous)), \
        f"anonymous {action} publishes the recorded digest a guess would be checked against"


def _contexts(value):
    """Every attempt `context` mapping in a JSON-like answer, at any depth."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "context" and isinstance(item, dict):
                yield item
            yield from _contexts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _contexts(item)


def test_the_digest_is_the_canonical_hash_of_the_recorded_context(tmp_path):
    # Pins the value the control compares against: the same sha256 the product
    # records (canonical JSON, sorted keys), not a looser re-serialisation.
    service, aid = _seeded(tmp_path)
    context = service.attempts.get(aid)["context"]
    assert digest(context) == hashlib.sha256(canonical(context).encode("utf-8")).hexdigest()


# c4 ---------------------------------------------------------------------------


def _accept_external(svc, reports, project_id, node_key, external_id):
    """One external attempt observed as a candidate and accepted through the
    product's own writes (the recipe of tests/unit/test_state_receipt_read.py).
    Returns the attempt id and the receipt."""
    reports.mkdir(parents=True, exist_ok=True)
    attempt = svc.start_external_attempt(project_id, node_key, 1, "my-harness",
                                         external_id, "req-" + external_id)
    terminal = reports / f"{external_id}-candidate.json"
    terminal.write_bytes(json.dumps({"attempt": attempt["attempt_id"], "status": "candidate",
                                     "settled": True, "usable": True}, sort_keys=True).encode())
    svc.report_external_attempt(
        attempt["attempt_id"], "completion-1", attempt["observation_version"],
        attempt["context_hash"], "candidate", str(terminal),
        hashlib.sha256(terminal.read_bytes()).hexdigest(),
        quiescent=True, artifact=ARTIFACT, artifact_kind="sha256")
    for criterion in CRITERIA:
        path = reports / f"{external_id}-{criterion}.json"
        body = json.dumps({"status": "completed", "settled": True, "usable": True,
                           "criterion_id": criterion, "verdict": "pass",
                           "artifact": ARTIFACT}, sort_keys=True).encode()
        path.write_bytes(body)
        svc.record_evidence(attempt["attempt_id"], f"{external_id}-{criterion}", criterion, "pass",
                            ARTIFACT, str(path), hashlib.sha256(body).hexdigest(), "verifier")
    receipt = svc.verify_node(project_id, node_key, 1, attempt["attempt_id"])
    return attempt["attempt_id"], receipt


def _external_service(tmp_path):
    svc = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                       actor="owner@example", project_read_trusted=True)
    svc.create_project(PID, "Demo project")
    svc.store.add_nodes(PID, [_spec(NODE)])
    svc.open_project(PID)
    return svc


def test_external_observation_context_hash_is_withheld_from_anonymous_detail(tmp_path):
    svc = _external_service(tmp_path)
    aid, _receipt = _accept_external(svc, tmp_path / "reports", PID, NODE, "job")

    trusted_rows = _answer(svc, True, "attempt_detail", aid)["external_observations"]
    assert trusted_rows, "the fixture must carry an external observation to check"
    # The trusted row carries the digest, non-empty and unchanged by the read.
    assert all(row["context_hash"] for row in trusted_rows)
    trusted_values = [row["context_hash"] for row in trusted_rows]

    anon_rows = _answer(svc, False, "attempt_detail", aid)["external_observations"]
    assert len(anon_rows) == len(trusted_rows)
    assert all(row["context_hash"] is None for row in anon_rows)
    # Every other observation column is kept.
    for row, original in zip(anon_rows, trusted_rows):
        assert {k: v for k, v in row.items() if k != "context_hash"} == \
            {k: v for k, v in original.items() if k != "context_hash"}
    # The trusted read is unchanged by the anonymous one.
    assert [row["context_hash"] for row in _answer(svc, True, "attempt_detail", aid)["external_observations"]] \
        == trusted_values


def test_acceptance_provenance_context_hash_is_withheld_from_anonymous_detail(tmp_path):
    svc = _external_service(tmp_path)
    aid, _receipt = _accept_external(svc, tmp_path / "reports", PID, NODE, "job")

    trusted_receipts = _answer(svc, True, "attempt_detail", aid)["receipts"]
    assert trusted_receipts, "the fixture must carry an acceptance receipt to check"
    trusted_provenance = json.loads(trusted_receipts[0]["provenance_json"])
    recorded = _recorded_digest(svc, aid)
    assert trusted_provenance.get("context_hash") == recorded, "the trusted provenance must carry the digest"

    anon_receipts = _answer(svc, False, "attempt_detail", aid)["receipts"]
    assert len(anon_receipts) == len(trusted_receipts)
    anon_text = anon_receipts[0]["provenance_json"]
    anon_provenance = json.loads(anon_text)
    assert anon_provenance["context_hash"] is None
    # The other provenance keys are intact.
    assert {k: v for k, v in anon_provenance.items() if k != "context_hash"} == \
        {k: v for k, v in trusted_provenance.items() if k != "context_hash"}
    assert recorded not in anon_text


def test_dependency_receipt_provenance_context_hash_is_withheld_from_anonymous_node(tmp_path):
    """get_node's `dependency_receipts[].acceptance` carries the acceptance row of
    a DEPENDENCY node, so the anonymous read of a dependant must withhold the
    digest in that row's provenance_json too.

    Built through the product: a `base` node accepted through an external
    attempt, and a `top` node that depends on it, so `node_context` names the
    receipt. If this fixture were structurally impossible the test would say so
    rather than skip."""
    svc = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                       actor="owner@example", project_read_trusted=True)
    svc.create_project(PID, "Demo project")
    svc.store.add_nodes(PID, [_spec("base")])
    svc.store.add_nodes(PID, [{**_spec("top"), "dependencies": ["base"]}])
    svc.open_project(PID)
    base_attempt, _receipt = _accept_external(svc, tmp_path / "reports", PID, "base", "job")
    base_context = _recorded_digest(svc, base_attempt)

    svc.project_read_trusted = True
    trusted = execute(svc, "get_node", {"project_id": PID, "node_key": "top"})
    dep = trusted["dependency_receipts"]["base"]["acceptance"]
    assert dep, "the dependant's read must carry the dependency's acceptance"
    trusted_provenance = json.loads(dep["provenance_json"])
    assert trusted_provenance.get("context_hash") == base_context, \
        "the trusted dependency provenance must carry the recorded digest"

    svc.project_read_trusted = False
    anonymous = execute(svc, "get_node", {"project_id": PID, "node_key": "top"})
    anon_dep = anonymous["dependency_receipts"]["base"]["acceptance"]
    anon_provenance = json.loads(anon_dep["provenance_json"])
    assert anon_provenance["context_hash"] is None
    assert {k: v for k, v in anon_provenance.items() if k != "context_hash"} == \
        {k: v for k, v in trusted_provenance.items() if k != "context_hash"}
    assert base_context not in anon_dep["provenance_json"]


def test_a_non_object_provenance_read_end_to_end_comes_back_byte_identical(tmp_path):
    """The read path, not only the helper: an acceptance row whose `provenance_json`
    is not a JSON object is returned byte-identical to an anonymous caller, and the
    read still succeeds.

    NOT product-reachable: `verify_node` always writes `canonical(dict)`, and the
    schema default is `'{}'` (an object). The row is therefore written directly
    into a store that holds an otherwise-real acceptance, and the test documents
    that this is a defensive guard for a legacy or hand-edited row."""
    svc = _external_service(tmp_path)
    aid, receipt = _accept_external(svc, tmp_path / "reports", PID, NODE, "job")
    odd = "[1,2,3]"
    with svc.store.transaction(write=True) as conn:
        # The receipt table is append-only by trigger. This throwaway test database
        # lifts the one trigger that blocks the UPDATE, for this write only, so the
        # odd value reaches the real read path; no shipped store code is changed.
        conn.execute("DROP TRIGGER IF EXISTS state_acceptance_no_update")
        conn.execute("UPDATE state_acceptances SET provenance_json=? WHERE receipt_id=?",
                     (odd, receipt["receipt_id"]))
    anonymous = _answer(svc, False, "attempt_detail", aid)
    assert anonymous["receipts"], "the read must still return the receipt"
    assert anonymous["receipts"][0]["provenance_json"] == odd


def test_a_provenance_that_is_not_a_json_object_comes_back_byte_identical():
    """The redaction never raises or drops a field on a provenance string it cannot
    read as a JSON object: such a string is returned exactly as it was."""
    for text in ("[1,2,3]", "not json", "", '"just a string"', '{"no_digest":true}', "null"):
        assert _without_provenance_digest(text) == text, f"changed {text!r}"


def test_a_provenance_object_without_a_digest_is_returned_unchanged():
    text = '{"execution_kind":"external","harness":"my-harness"}'
    assert _without_provenance_digest(text) == text
