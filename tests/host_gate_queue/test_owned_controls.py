"""Six bounded negative controls; preserve the first actual assertion failure.

Each mutation runs only in its own disposable export. The real queue/HTTP/child
are exercised. A client stub appears only in C06, where zero HTTP must be RED.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from owned_support import BASE, Harness, REPO, SHA, TIMEOUT
from test_owned_queue import producer_failure


@pytest.fixture
def h(tmp_path):
    instance=Harness(tmp_path)
    try: yield instance
    finally: instance.close()


def first_failure(h, label, exercise, expected):
    with pytest.raises(AssertionError) as failure:
        exercise()
    raw=str(failure.value)
    assert expected in raw, (label,raw)
    # The RED is an artifact of the proof, never a Source commit or replacement green.
    path=h.path/(label+"-FIRST-FAIL.txt")
    path.write_text(raw+"\n")
    h.timeline.append(dict(event="first-failure-control",control=label,raw_failure=raw,retained=str(path)))
    print("FIRST_FAIL " + json.dumps(dict(control=label,raw=raw)))


def mutate(h, before, after):
    source=h.source.read_text()
    assert source.count(before)==1, (before,source.count(before))
    h.source.write_text(source.replace(before,after,1))


def test_C01_accepted_8a755_original_missing_binding_is_RED(h):
    baseline=h.path/"baseline-8a755.py"
    baseline.write_bytes(subprocess.run(["git","-C",str(REPO),"show",BASE+":scripts/gate_queue.py"],
                                       check=True,capture_output=True,timeout=TIMEOUT).stdout)
    assert hashlib.sha256(baseline.read_bytes()).hexdigest()=="57752cb17ee37df089c9b1c4d641a5ebb81edf8f15c4b9bb347c781f86f2ea9c"
    def exercise():
        result=h.cli("submit",str(h.worktree),SHA,owned=False,source=baseline,rc=None)
        count=len(list((h.root/"tickets").glob("*/ticket.json")))
        assert count==0, "ordinary missing-registrations submit produced a ticket; expected zero"
        assert result.returncode==5
    first_failure(h,"C01",exercise,"ordinary missing-registrations submit produced a ticket")
    # The same original Source has no covered report producer; retain that operational error too.
    result=h.cli("report-context","fixture",owned=False,source=baseline,rc=2)
    assert "invalid choice" in result.stderr and not h.requests
    h.file("C01-original-missing-producer.json",dict(rc=result.returncode,stderr=result.stderr))


def test_C02_reversing_failure_order_is_caught_at_literal_HTTP_boundary(h):
    mutate(h,"                outcome = apply_failure(record, decision, decision_raw, report_raw)",
             "                adapter.client._client.post('/api/state/commands/report_external_attempt', json=observation)\n"
             "                outcome = apply_failure(record, decision, decision_raw, report_raw)")
    first_failure(h,"C02",lambda: producer_failure(h),"queue disposition must precede failed State POST")


def test_C03_stale_dequeue_without_reread_launches_cancelled_A_and_is_RED(h):
    a=h.submit(); b=h.submit("-B")
    mutate(h,'        record = read_ticket(record["ticket"])\n        if record["state"] != "queued":',
             '        # negative control: trust the stale selected record\n        if record["state"] != "queued":')
    daemon=h.daemon(selected=True); h.decide(a,"cancel")
    daemon.stdin.write("continue\n"); daemon.stdin.flush()
    def exercise():
        row=h.event("start",a)
        count=sum(e.get("event")=="start" and e["ticket"]==a for e in h.timeline)
        assert count==0, "A_child_launches observed 1 versus expected0 after cancel committed"
    first_failure(h,"C03",exercise,"A_child_launches observed 1 versus expected0")
    h.release(a); h.event("finish",a); h.finish(daemon)
    assert h.record(b)["state"]=="queued"


def test_C04_plain_python_recipe_original_bootstrap_counterexample(h):
    h.python=h.ordinary
    first_failure(h,"C04",lambda: producer_failure(h),"adapter-unavailable")
    assert not h.requests and not list((h.root/"tickets").glob("*"))


def test_C05_inherited_post_import_env_leaks_and_is_RED(h):
    mutate(h,"text=True, env=launch_env)","text=True, env=None)")
    (h.aroot/".env").write_text("GATE_QUEUE_FIXTURE_DOTENV=marker\nAITELIER_ADMIN_TOKEN=fixture-admin-not-used\n")
    a=h.submit(); h.complete(a)
    child=json.loads((h.reports/a/"child-environment.json").read_bytes())
    def exercise():
        assert "GATE_QUEUE_FIXTURE_DOTENV" not in child["names"], "child env contains GATE_QUEUE_FIXTURE_DOTENV; expected absent"
    first_failure(h,"C05",exercise,"child env contains GATE_QUEUE_FIXTURE_DOTENV")
    assert "AITELIER_ADMIN_TOKEN" in child["names"]


def test_C06_in_process_transport_stub_is_not_accepted_as_real_client(h):
    registration=h.registrations()
    fixture=h.file("C06-stub-nodes.json",h.nodes)
    source=h.source.read_text(); start=source.index("def _state_adapter("); end=source.index("\n\nclass StateAdapter:",start)
    # Deliberately wrong transport: enough literal replies to manufacture a green
    # producer, no APIClient/httpx/credentials import and no loopback requests.
    bad='''def _state_adapter(expected_driver_id):
    nodes = json.loads(Path(os.environ["QUEUE_NEGATIVE_STUB_NODES"]).read_text())
    class Reply:
        status_code = 200
        def __init__(self, value): self.value = value; self.content = json.dumps(value).encode()
        def raise_for_status(self): pass
        def json(self): return self.value
    class Stub:
        def __init__(self): self._client = self
        def post(self, path, json, **kwargs): return Reply(nodes[json["node_key"]])
    return StateAdapter(Stub(), dict(me_actor="driver:"+expected_driver_id))
'''
    h.source.write_text(source[:start]+bad+source[end:]); h.env["QUEUE_NEGATIVE_STUB_NODES"]=fixture
    h.submit(registration=registration)
    def exercise():
        assert h.requests, "loopback server saw 0 requests / no driver header; expected GET /api/drivers/me first"
    first_failure(h,"C06",exercise,"loopback server saw 0 requests")
