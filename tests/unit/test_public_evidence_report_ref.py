"""The anonymous evidence reads do not name a host path in `report_ref`.

An evidence row records the report it was verified from as `report_ref`. The
product stores that reference as given (`retain_report` keeps the recorded value
and the report store lives under the host's AItelier home), so the row carries an
absolute host path. A `report_ref` such as `/home/<user>/.AItelier/...` tells an
anonymous visitor the host user name, the directory layout and other directors'
report file names.

The anonymous answer keeps every row, every key, and `report_sha256` / `verdict` /
`criterion_id`, and replaces each absolute `report_ref` with its relative form
(the store-relative `<sha[:2]>/<sha>` tail, or the basename), so rows stay
distinguishable. A trusted caller keeps the recorded value byte for byte.

The reads covered are the ones that can return an evidence row: `evidence`,
`attempt_detail` (its `evidence` rows) and `get_node` (acceptance provenance that
names the external report). `get_receipt` returns RECEIPT_EVIDENCE columns only,
with no `report_ref`, so it is asserted to stay free of the field instead.

Real store, real `execute` handler; the caller's trust is the only thing that
changes between the two arms, as in `test_public_attempt_context_source_path.py`.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from core.state_commands import execute
from core.state_database import StateDatabase
from core.state_service import StateService

PID = "demo-project"
NODE = "goal"
CHILD = "child"
ARTIFACT = "a" * 64
ABSOLUTE_PREFIXES = ("/home/", "/Users/", "/srv/", "/opt/", "/app/")
# A host-path shape a scan must catch in any string, whatever key holds it.
HOST_PATH = re.compile(r"(^|\s)/(home|Users|srv|opt|app|var)/")


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


def _rows_with_report_ref(value):
    """Every mapping that carries a `report_ref` key, at any depth."""
    if isinstance(value, dict):
        if "report_ref" in value:
            yield value
        for item in value.values():
            yield from _rows_with_report_ref(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _rows_with_report_ref(item)


def _report_refs(value):
    return [row["report_ref"] for row in _rows_with_report_ref(value)]


def _args(action, aid):
    return {
        "evidence": {"attempt_id": aid},
        "attempt_detail": {"attempt_id": aid},
        "get_node": {"project_id": PID, "node_key": CHILD},
        "get_receipt": {"project_id": PID, "receipt_id": "receipt-missing"},
    }[action]


def _report(path: Path, criterion: str, verdict: str = "pass") -> tuple[str, str]:
    """A complete structured report the product's evidence path accepts."""
    body = json.dumps({"status": "completed", "settled": True, "usable": True,
                       "criterion_id": criterion, "verdict": verdict,
                       "artifact": ARTIFACT}, sort_keys=True).encode()
    path.write_bytes(body)
    return str(path), hashlib.sha256(body).hexdigest()


def _seeded(tmp_path):
    """A public project with one attempt whose evidence rows carry an absolute
    `report_ref`, recorded through the product's own evidence write. The node is
    ACCEPTED, and a dependent node names it, so `get_node` on the dependent carries
    the external report reference in its acceptance provenance too."""
    svc = StateService(StateDatabase(str(tmp_path / "state.sqlite")),
                       actor="evidence-report-ref", project_read_trusted=True)
    svc.create_project(PID, "Demo project")
    svc.store.add_nodes(PID, [{"key": NODE, "goal": "Deliver", "acceptance": [
        {"id": "behaviour", "kind": "test", "description": "Behaviour passes"},
        {"id": "review", "kind": "review", "description": "Reviewed"}]}])
    svc.open_project(PID)
    attempt = svc.start_external_attempt(PID, NODE, 1, "my-harness", "job", "req-job")
    aid = attempt["attempt_id"]
    terminal = tmp_path / "job-candidate.json"
    terminal.write_bytes(json.dumps({"attempt": aid, "status": "candidate", "settled": True,
                                     "usable": True}, sort_keys=True).encode())
    svc.report_external_attempt(aid, "completion-1", attempt["observation_version"],
                                attempt["context_hash"], "candidate", str(terminal),
                                hashlib.sha256(terminal.read_bytes()).hexdigest(),
                                quiescent=True, artifact=ARTIFACT, artifact_kind="sha256")
    for criterion in ("behaviour", "review"):
        path, sha = _report(tmp_path / f"{criterion}.json", criterion)
        svc.record_evidence(aid, f"job-{criterion}", criterion, "pass", ARTIFACT, path, sha, "verifier")
    svc.verify_node(PID, NODE, 1, aid)
    # A dependent node names the accepted one, so its `get_node` carries the
    # acceptance provenance (which names the external report) as a dependency.
    svc.store.add_nodes(PID, [{"key": CHILD, "goal": "Child", "dependencies": [NODE],
                               "acceptance": [{"id": "x", "kind": "test", "description": "d"}]}])
    return svc, aid


def _answer(service, trusted, action, aid):
    service.project_read_trusted = trusted
    return execute(service, action, _args(action, aid))


def _host_rows(service, aid):
    """The recorded evidence rows, read straight from the store (the trusted truth)."""
    with service.store.transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM state_evidence WHERE attempt_id=? ORDER BY seq", (aid,))]


# The evidence-row reads: each returns `state_evidence` rows that carry a
# `report_ref`. `get_node` does not return evidence rows; its report reference
# reaches an anonymous caller only through an acceptance's provenance, and that
# channel has its own test below.
EVIDENCE_READS = ["evidence", "attempt_detail"]


def _evidence_rows(answer):
    """The evidence rows of an answer: every mapping that is a `state_evidence` row
    (it has an `evidence_id`). Observation rows also carry `report_ref`; they are
    scanned for leaks but are not evidence rows."""
    return [row for row in _rows_with_report_ref(answer) if "evidence_id" in row]


@pytest.mark.parametrize("action", EVIDENCE_READS)
def test_anonymous_evidence_read_names_no_host_path(tmp_path, action):
    service, aid = _seeded(tmp_path)
    recorded = _host_rows(service, aid)
    assert recorded and all(r["report_ref"].startswith("/") for r in recorded), \
        "fixture must record an absolute host report_ref"

    answer = _answer(service, False, action, aid)

    # (b1) A recursive scan of every string value finds no host absolute path.
    leaked = [s for s in _strings(answer) if HOST_PATH.search(s)]
    assert leaked == [], f"anonymous {action} names a host path: {leaked}"
    assert not any(s.startswith(ABSOLUTE_PREFIXES) for s in _strings(answer)), \
        f"anonymous {action} carries an absolute host path"

    # (b2) The evidence rows are still there and identifiable: each report_ref is a
    # non-empty relative reference, the store-relative `<sha[:2]>/<sha>` form.
    rows = _evidence_rows(answer)
    assert rows, f"anonymous {action} lost every evidence row"
    for row in rows:
        ref = row["report_ref"]
        assert isinstance(ref, str) and ref, f"anonymous {action} blanked report_ref"
        assert not ref.startswith("/"), f"anonymous {action} kept an absolute ref: {ref!r}"
        assert re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{64}", ref), \
            f"anonymous {action} report_ref is not the store-relative form: {ref!r}"


@pytest.mark.parametrize("action", EVIDENCE_READS)
def test_anonymous_scan_really_inspects_the_rows(tmp_path, action):
    # The control against a vacuous projection: the scan must see real evidence
    # rows, and every recorded row must be present, so "no host path" can not pass
    # on an empty answer.
    service, aid = _seeded(tmp_path)
    answer = _answer(service, False, action, aid)
    rows = _evidence_rows(answer)
    recorded = _host_rows(service, aid)
    assert len(rows) > 0, f"anonymous {action} scanned zero evidence rows"
    assert len(rows) == len(recorded), \
        f"anonymous {action} scanned {len(rows)} evidence rows, not the {len(recorded)} recorded"
    assert len(list(_strings(answer))) > 0, f"anonymous {action} scanned no strings"


@pytest.mark.parametrize("action", EVIDENCE_READS)
def test_anonymous_evidence_rows_keep_identity_fields(tmp_path, action):
    service, aid = _seeded(tmp_path)
    recorded = _host_rows(service, aid)
    answer = _answer(service, False, action, aid)

    rows = _evidence_rows(answer)
    by_sha = {r["report_sha256"]: r for r in recorded}
    assert len(rows) == len(recorded), f"anonymous {action} did not keep every evidence row"
    for row in rows:
        original = by_sha[row["report_sha256"]]
        assert row["verdict"] == original["verdict"]
        assert row["criterion_id"] == original["criterion_id"]
        # The distinguishing store identity of the recorded reference survives.
        assert row["report_ref"].endswith(original["report_ref"].rsplit("/", 1)[-1]), \
            f"anonymous {action} lost the identity of the ref: {row['report_ref']!r}"


@pytest.mark.parametrize("action", EVIDENCE_READS)
def test_trusted_evidence_read_keeps_the_recorded_report_ref(tmp_path, action):
    service, aid = _seeded(tmp_path)
    recorded = _host_rows(service, aid)
    answer = _answer(service, True, action, aid)

    # The control: a trusted caller sees every recorded report_ref byte for byte.
    trusted_refs = [row["report_ref"] for row in _evidence_rows(answer)]
    assert trusted_refs, f"trusted {action} carries no evidence row"
    assert sorted(trusted_refs) == sorted(r["report_ref"] for r in recorded), \
        f"trusted {action} changed a recorded report_ref"
    assert all(ref.startswith("/") for ref in trusted_refs), \
        f"trusted {action} must keep the absolute recorded value"


@pytest.mark.parametrize("action", EVIDENCE_READS)
def test_anonymous_answer_differs_from_trusted_only_in_report_ref(tmp_path, action):
    service, aid = _seeded(tmp_path)
    anonymous = _answer(service, False, action, aid)
    trusted = _answer(service, True, action, aid)

    assert anonymous != trusted, "the fixture must carry a path for the anonymous read to remove"
    assert _strip_report_refs(anonymous) == _strip_report_refs(trusted), \
        f"anonymous {action} changed something other than report_ref"


def _strip_report_refs(value):
    """Copy with every `report_ref`, and the one inside `provenance_json`, removed:
    what remains must be identical for the anonymous and the trusted answer."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "report_ref":
                continue
            if k == "provenance_json" and isinstance(v, str):
                out[k] = _provenance_without_report_ref(v)
            else:
                out[k] = _strip_report_refs(v)
        return out
    if isinstance(value, list):
        return [_strip_report_refs(v) for v in value]
    return value


def _provenance_without_report_ref(text):
    try:
        provenance = json.loads(text)
    except (TypeError, ValueError):
        return text
    if isinstance(provenance, dict):
        provenance.pop("report_ref", None)
    return json.dumps(provenance, sort_keys=True)


def _provenance_report_refs(value):
    """The `report_ref` inside every acceptance `provenance_json`, at any depth."""
    for key, item in (value.items() if isinstance(value, dict) else []):
        if key == "provenance_json" and isinstance(item, str):
            ref = json.loads(item).get("report_ref")
            if ref is not None:
                yield ref
        else:
            yield from _provenance_report_refs(item)
    if isinstance(value, list):
        for item in value:
            yield from _provenance_report_refs(item)


def test_get_node_acceptance_provenance_is_projected_for_anonymous_only(tmp_path):
    # The dependent node's acceptance provenance names the accepted node's external
    # report. Anonymous: relative. Trusted: the recorded absolute value.
    service, aid = _seeded(tmp_path)
    anonymous = _answer(service, False, "get_node", aid)
    trusted = _answer(service, True, "get_node", aid)

    anon_refs = list(_provenance_report_refs(anonymous))
    trusted_refs = list(_provenance_report_refs(trusted))
    assert anon_refs, "the dependent node must carry acceptance provenance (the control)"
    assert trusted_refs and all(r.startswith("/") for r in trusted_refs), \
        "trusted provenance must keep the recorded absolute report_ref"
    assert all(not r.startswith("/") for r in anon_refs), \
        f"anonymous provenance names an absolute path: {anon_refs}"
    assert all(re.fullmatch(r"[0-9a-f]{2}/[0-9a-f]{64}", r) for r in anon_refs), anon_refs
    leaked = [s for s in _strings(anonymous) if HOST_PATH.search(s)]
    assert leaked == [], f"anonymous get_node names a host path: {leaked}"


def test_receipt_evidence_shape_has_no_report_ref():
    # get_receipt returns RECEIPT_EVIDENCE columns only, so it never carries a
    # report_ref and is not part of the projection. Pin the shape it returns.
    from core.state_attempts import StateAttempts
    assert "report_ref" not in StateAttempts.RECEIPT_EVIDENCE
    assert set(StateAttempts.RECEIPT_EVIDENCE) == {
        "evidence_id", "criterion_id", "kind", "verdict", "artifact_ref", "report_sha256"}


# The second channel: a `file://` report reference. `report_external_attempt`
# accepts a `file://` URL and a NON-terminal observation keeps it verbatim (the
# terminal and quiescent writes are normalised to the store path, so the raw
# `file://` value is only reachable through a non-terminal observation). The
# evidence and provenance arms below seed the raw row directly, the same shape
# that observation writes, so the projection is tested on the value it must fix.
FILE_HOME = "file:///home/somebody/.AItelier/director/reports/r.json"
FILE_STORE_SHA = "9f" + "c" * 62
FILE_STORE = "file:///home/somebody/.AItelier/state-report-evidence/9f/" + FILE_STORE_SHA
FILE_TAIL_BASENAME = "r.json"


def _raw_file_row(service, aid, ref):
    """Write one evidence row whose report_ref is the given raw reference."""
    with service.store.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO state_evidence(evidence_id,attempt_id,criterion_id,kind,verdict,artifact_ref,"
            "report_ref,report_sha256,reviewer,detail,payload_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("raw-" + ref[-8:], aid, "behaviour", "behaviour", "pass", ARTIFACT, ref,
             "0" * 64, "verifier", "", "0" * 64, "2026-01-01T00:00:00Z"))


def _raw_file_observation(service, aid, ref):
    """Write one non-terminal observation whose report_ref is the given raw reference."""
    with service.store.transaction(write=True) as conn:
        conn.execute(
            "INSERT INTO state_external_observations(attempt_id,observation_id,version,status,"
            "resulting_status,quiescent,artifact_ref,artifact_kind,report_ref,report_sha256,actor,"
            "detail,payload_hash,context_hash,created_at,late_after_abandon,fence) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, "raw-observation", 99, "running", "running", 0, None, None, ref, "1" * 64,
             "reporter", "", "2" * 64, "3" * 64, "2026-01-01T00:00:00Z", 0, None))


def _raw_file_service(tmp_path):
    service, aid = _seeded(tmp_path)
    _raw_file_row(service, aid, FILE_HOME)
    _raw_file_row(service, aid, FILE_STORE)
    _raw_file_observation(service, aid, FILE_HOME)
    return service, aid


FILE_READS = ["evidence", "attempt_detail"]


@pytest.mark.parametrize("action", FILE_READS)
def test_anonymous_file_url_report_ref_is_projected(tmp_path, action):
    service, aid = _raw_file_service(tmp_path)
    answer = _answer(service, False, action, aid)

    # No "/home", no "file:" anywhere in the anonymous answer.
    texts = list(_strings(answer))
    assert not any("/home" in s for s in texts), f"anonymous {action} names /home: {texts}"
    assert not any("file:" in s for s in texts), f"anonymous {action} keeps a file: URL"

    # The exact relative tail, for the store form and for the non-store form.
    refs = [row["report_ref"] for row in _rows_with_report_ref(answer)]
    assert f"9f/{FILE_STORE_SHA}" in refs, f"anonymous {action} lost the store-relative tail: {refs}"
    assert FILE_TAIL_BASENAME in refs, f"anonymous {action} lost the basename tail: {refs}"


@pytest.mark.parametrize("action", FILE_READS)
def test_trusted_file_url_report_ref_is_unchanged(tmp_path, action):
    service, aid = _raw_file_service(tmp_path)
    answer = _answer(service, True, action, aid)
    refs = [row["report_ref"] for row in _rows_with_report_ref(answer)]
    # The control: the recorded file:// values come back exactly as stored.
    assert FILE_HOME in refs and FILE_STORE in refs, f"trusted {action} changed a file:// ref: {refs}"


def test_anonymous_file_url_provenance_is_projected_on_the_read_path():
    # The dependency channel, exercised on the projection the read applies. The
    # receipt a dependent node reads is the node's VERIFIED receipt, and
    # state_acceptances is append-only, so an end-to-end `get_node` cannot be given a
    # file:// provenance without a fabricated receipt. This arm therefore applies the
    # same `execute` rewrite (`_without_host_report_refs`) to an acceptance answer
    # shaped exactly as `get_node` returns it, and asserts the anonymous and trusted
    # results. The store-path end-to-end arm is `test_get_node_acceptance_provenance_*`.
    from core.state_commands import _without_host_report_refs
    answer = {"dependency_receipts": {"goal": {"acceptance": {
        "receipt_id": "receipt-x",
        "provenance_json": json.dumps({"report_ref": FILE_HOME, "execution_kind": "external"},
                                      sort_keys=True, separators=(",", ":"))}}}}
    anonymous = _without_host_report_refs(answer)
    texts = list(_strings(anonymous))
    assert not any("/home" in s or "file:" in s for s in texts), "anonymous provenance names a file:// host path"
    anon_refs = list(_provenance_report_refs(anonymous))
    assert anon_refs == [FILE_TAIL_BASENAME], f"anonymous provenance lost the relative tail: {anon_refs}"
    # The trusted answer is not passed through the projection, so it is unchanged.
    assert list(_provenance_report_refs(answer)) == [FILE_HOME], "trusted provenance changed the file:// ref"


def test_file_url_outside_the_store_loses_its_prefix_to_the_basename():
    # A file:// URL that is neither store-relative nor an absolute store path keeps
    # only its basename: the host prefix must not survive any shape.
    from core.state_commands import _host_report_relative
    assert _host_report_relative("file:///home/somebody/.AItelier/director/reports/r.json") == "r.json"
    assert _host_report_relative("file://localhost/home/somebody/reports/r.json") == "r.json"


def test_host_path_matcher_has_a_positive_control():
    # The scan must be able to find the leak it guards against.
    assert HOST_PATH.search("/home/somebody/.AItelier/director/reports/r.json")
    assert HOST_PATH.search("x /srv/aitelier/state-report-evidence/ab/abc")
    assert not HOST_PATH.search("ab/" + "c" * 64)
    assert not HOST_PATH.search("r.json")
