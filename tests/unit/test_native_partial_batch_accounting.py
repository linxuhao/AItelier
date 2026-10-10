"""Observed partial receipts survive a dropped native conversation tail."""
import json
from types import SimpleNamespace

import pytest
from core.dpe_pipeline import NativeOutputCapExhausted
from tests.unit import test_native_direct_code as f
from tests.unit.test_native_starved_output_reclaim import traced_host, starve, payloads


def call(name, cid, **params):
    return {"id":cid, "function":{"name":name,"arguments":json.dumps(params)}}


def response(*calls):
    return SimpleNamespace(text="",reasoning_content="",truncated=False,tool_calls=list(calls))


def partial_crash(sf, rid, claim, root, kind, *, remainder_path="never-read.py"):
    prefixes = {
        "failed-read": [call("read","prefix",path="missing.py")],
        "successful-read": [call("read","prefix",path="A.py")],
        "grant": [call("ask_more_turns","prefix",turns=3,reason="remaining owned work")],
        "multiple-reads": [call("read","failed-prefix",path="missing.py"),call("read","prefix",path="A.py")],
    }[kind]
    batch=response(*prefixes,call("create","write-B",file="B.py",content="B=True\n"),
                   call("read","unexecuted",path=remainder_path),call("finish_step","finish"))
    calls=[]
    e,ws=traced_host(sf,rid,claim,root,[f.response("create",file="A.py",content="A=True\n"),starve(),batch],calls,6)
    trace=e._trace_cb
    def crash_after_prefix(category,event,payload):
        trace(category,event,payload)
        if event == "prompt_delta" and payload.get("role") == "tool" and payload.get("tool_call_id") == "prefix":
            raise KeyboardInterrupt("owned prefix receipt durable before B delta")
    e._trace_cb=crash_after_prefix
    with pytest.raises(KeyboardInterrupt): f.execute(e,ws,rid,claim)
    assert len(calls)==3
    assert (root/"A.py").read_text()=="A=True\n" and (root/"B.py").read_text()=="B=True\n"
    deltas=payloads(sf,"prompt_delta")
    assert any(p.get("tool_call_id")=="prefix" for p in deltas)
    assert not any(p.get("tool_call_id") in ("write-B","unexecuted","finish") for p in deltas)
    return calls


@pytest.mark.parametrize("kind,reads,failures,max_turns", [
    ("failed-read",1,1,6), ("successful-read",1,0,6),
    ("multiple-reads",2,1,6), ("grant",0,0,9)])
def test_durable_partial_receipts_and_declared_remainder_are_counted_once(
        tmp_path, monkeypatch, kind, reads, failures, max_turns):
    sf,rid,claim,root=f.run_fixture(tmp_path,monkeypatch)
    calls=partial_crash(sf,rid,claim,root,kind)
    e,ws=traced_host(sf,rid,claim,root,[RuntimeError("owned post-crash transport failure")],calls,6)
    with pytest.raises(NativeOutputCapExhausted): f.execute(e,ws,rid,claim)
    report=payloads(sf,"output_cap_exhausted")[-1]
    # The current retained-batch owner settles the declared remainder before
    # calling a provider. Its missing-file read is real work, not an invented
    # call and not a replay of the already observed prefix receipt.
    assert report["reads_searches"]==reads+1 and report["tool_failures"]==failures+1
    observed=payloads(sf,"prompt_delta")
    remainder=[x for x in observed if x.get("role")=="tool" and x.get("tool_call_id")=="unexecuted"]
    assert len(remainder)==1 and 'never-read.py' in remainder[0]["content"]
    prefix=[x for x in observed if x.get("role")=="tool" and x.get("tool_call_id")=="prefix"]
    # Append-only recovery may re-emit a retained delta at the SAME position;
    # it must neither alter that receipt nor execute/count its tool twice.
    assert len({(x.get("segment",0),x["index"],x["tool_call_id"]) for x in prefix})==1
    assert len({x["content"] for x in prefix})==1
    assert any(x["already_observed"] and not x["executed_now"] for x in payloads(sf,"native_tool_result_recovered"))
    assert payloads(sf,"native_batch_recovery")[-1]["provider_called"] is False
    assert report["written_files"]==["A.py","B.py"] and report["first_write_turn"]==1
    assert report["turn_budget"]=={"max_turns":max_turns,"turns_used":3}
    assert len(calls)==3 and not payloads(sf,"output_starvation_recovered")
    assert (root/"A.py").read_text()=="A=True\n" and (root/"B.py").read_text()=="B=True\n"
    if kind=="grant":
        assert report["expansion_requests"]==[{
            "turn":3,"asked":3,"granted":3,"reason":"remaining owned work"}]
    else:
        assert report["expansion_requests"]==[]
    assert not payloads(sf,"turn_granted_for_escalation")
    assert not payloads(sf,"output_cap_escalated")
    sf._conn.close()


def test_fenced_write_from_partial_batch_is_acknowledged_without_reexecution(tmp_path, monkeypatch):
    sf,rid,claim,root=f.run_fixture(tmp_path,monkeypatch)
    calls=partial_crash(sf,rid,claim,root,"successful-read",remainder_path="A.py")
    before=[p for p in payloads(sf,"side_effect_completed") if "B.py" in p.get("written_files",[])]
    assert len(before)==1
    e,ws=traced_host(sf,rid,claim,root,[response(
        call("create","reissue-B",file="B.py",content="B=True\n"),
        call("finish_step","finish",summary="validated retained work"))],calls,6)
    assert f.execute(e,ws,rid,claim) is True
    after=[p for p in payloads(sf,"side_effect_completed") if "B.py" in p.get("written_files",[])]
    assert after==before
    rebuilt=e._resume_from_trace("p",6)
    assert rebuilt["tool_failures"]==0 and rebuilt["reads_searches"]==2
    assert not rebuilt["starved_recovery_pending"]
    assert len(calls)==3 and (root/"B.py").read_text()=="B=True\n"
    assert payloads(sf,"native_batch_recovery")[-1]["provider_called"] is False
    assert any(x["tool"]=="create" and x["executed_now"] is False for x in payloads(sf,"side_effect_replay_refused"))
    sf._conn.close()
