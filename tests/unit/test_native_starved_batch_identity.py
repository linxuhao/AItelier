"""Finish proof belongs to one actual native assistant batch, never a reused id."""
import json
from types import SimpleNamespace

import pytest
from core.dpe_pipeline import PipelineEngine, NativeOutputCapExhausted
from tests.unit import test_native_direct_code as f
from tests.unit.test_native_starved_output_reclaim import traced_host, starve, payloads


def tc(name, cid):
    return {"id":cid, "function":{"name":name, "arguments":"{}"}}


def delta(index, role, *, segment=0, **fields):
    return ("prompt_delta", {"index":index, "segment":segment, "role":role, **fields})


def legacy_rows():
    return [delta(0, "system", content="owned"),
            ("output_cap_starved", {"turn":1, "attempt":1}),
            delta(1, "assistant", content_null=True,
                  tool_calls=json.dumps([tc("read", "read"), tc("finish_step", "finish")])),
            delta(2, "tool", tool_call_id="read", name="read", content='{"ok":true}'),
            delta(3, "tool", tool_call_id="finish", name="finish_step", content='{"completed":true}')]


@pytest.mark.parametrize("variant", ["failed-read", "failed-finish", "missing-first", "missing-finish",
    "reverse-order", "duplicate-calls", "duplicate-results", "empty-id", "other-segment",
    "user-boundary", "assistant-boundary", "malformed-result", "array-result", "null-result",
    "wrong-name", "malformed-args", "array-args"])
def test_incomplete_or_invalid_legacy_batch_cannot_clear_pending(variant):
    rows = legacy_rows()
    if variant == "failed-read": rows[3][1]["content"] = '{"error":"missing file"}'
    elif variant == "failed-finish": rows[4][1]["content"] = '{"error":"finish refused"}'
    elif variant == "missing-first": rows.pop(3)
    elif variant == "missing-finish": rows.pop()
    elif variant == "reverse-order":
        rows[3][1]["tool_call_id"], rows[4][1]["tool_call_id"] = "finish", "read"
        rows[3][1]["name"], rows[4][1]["name"] = "finish_step", "read"
    elif variant == "duplicate-calls": rows[2][1]["tool_calls"] = json.dumps([tc("read","read"),tc("finish_step","read")])
    elif variant == "duplicate-results": rows[4][1]["tool_call_id"] = "read"
    elif variant == "empty-id": rows[2][1]["tool_calls"] = json.dumps([tc("read", ""),tc("finish_step", "finish")])
    elif variant == "other-segment":
        rows[3][1]["segment"] = rows[4][1]["segment"] = 1
    elif variant in ("user-boundary", "assistant-boundary"):
        rows[3][1]["index"] = 3; rows[4][1]["index"] = 4
        rows.insert(3, delta(2, "user" if variant == "user-boundary" else "assistant", content="new batch"))
    elif variant == "malformed-result": rows[3][1]["content"] = "not JSON"
    elif variant == "array-result": rows[3][1]["content"] = "[]"
    elif variant == "null-result": rows[3][1]["content"] = "null"
    elif variant == "wrong-name": rows[3][1]["name"] = "list"
    elif variant in ("malformed-args", "array-args"):
        calls=json.loads(rows[2][1]["tool_calls"])
        calls[1]["function"]["arguments"] = "{" if variant == "malformed-args" else "[]"
        rows[2][1]["tool_calls"] = json.dumps(calls)
    # A later successful read deliberately reuses the earlier id. Its receipt
    # is a distinct batch and cannot replace the missing/failed old receipt.
    rows += [delta(8, "assistant", content_null=True, tool_calls=json.dumps([tc("read","read")])),
             delta(9, "tool", tool_call_id="read", name="read", content='{"ok":true}')]
    assert PipelineEngine._rebuild_from_deltas(rows, 6)["starved_recovery_pending"]


@pytest.mark.parametrize("with_names", [True, False])
def test_complete_successful_legacy_finish_is_resumable(with_names):
    rows = legacy_rows()
    if not with_names:
        rows[3][1].pop("name"); rows[4][1].pop("name")
    # Later reused ids cannot erase an earlier genuine successful finish.
    rows += [delta(8,"assistant", content_null=True,tool_calls=json.dumps([tc("read","read")])),
             delta(9,"tool",tool_call_id="read",name="read",content='{"error":"later failed read"}')]
    assert not PipelineEngine._rebuild_from_deltas(rows, 6)["starved_recovery_pending"]


def test_old_successful_finish_does_not_clear_a_later_starvation():
    rows=legacy_rows()+[("output_cap_starved", {"turn":3,"attempt":1})]
    assert PipelineEngine._rebuild_from_deltas(rows, 6)["starved_recovery_pending"]


def test_reused_read_id_retains_actual_first_failure_accounting_across_reclaim(tmp_path, monkeypatch):
    sf,rid,claim,root=f.run_fixture(tmp_path,monkeypatch)
    bad=SimpleNamespace(text="",reasoning_content="",truncated=False,tool_calls=[
        {"id":"same", "function":{"name":"read", "arguments":json.dumps({"path":"missing.py"})}},
        {"id":"finish", "function":{"name":"finish_step", "arguments":"{}"}}])
    calls=[]
    e,ws=traced_host(sf,rid,claim,root,[f.response("create",file="partial.py",content="OWNED=1\n"),starve(),bad],calls,6)
    with pytest.raises(NativeOutputCapExhausted): f.execute(e,ws,rid,claim)
    assert payloads(sf,"output_cap_exhausted")[-1]["tool_failures"] == 1
    read=SimpleNamespace(text="",reasoning_content="",truncated=False,tool_calls=[
        {"id":"same", "function":{"name":"read", "arguments":json.dumps({"path":"partial.py"})}}])
    e,ws=traced_host(sf,rid,claim,root,[read],calls,6)
    trace=e._trace_cb
    def crash_after_receipt(category,event,payload):
        trace(category,event,payload)
        if event == "prompt_delta" and payload.get("role") == "tool" and payload.get("name") == "read":
            raise KeyboardInterrupt("owned read receipt durable")
    e._trace_cb=crash_after_receipt
    with pytest.raises(KeyboardInterrupt): f.execute(e,ws,rid,claim)
    e,ws=traced_host(sf,rid,claim,root,[ValueError("transport after unrelated successful read")],calls,6)
    with pytest.raises(NativeOutputCapExhausted): f.execute(e,ws,rid,claim)
    failure=payloads(sf,"output_cap_exhausted")[-1]
    assert failure["tool_failures"] == 1 and failure["reads_searches"] == 2
    assert failure["turn_budget"]["turns_used"] == 5
    assert not payloads(sf,"output_starvation_recovered")
    assert (root/"partial.py").read_text() == "OWNED=1\n"
    sf._conn.close()


def test_actual_legacy_completed_finish_remains_resumable(tmp_path, monkeypatch):
    sf,rid,claim,root=f.run_fixture(tmp_path,monkeypatch)
    calls=[]
    e,ws=traced_host(sf,rid,claim,root,[f.response("create",file="partial.py",content="OWNED=1\n"),starve(),f.response("finish_step")],calls,6)
    assert f.execute(e,ws,rid,claim) is True
    # Owned legacy-trace fixture: remove the new host event, retain the actual
    # tool calls and ordered durable receipts exactly as older hosts did.
    sf._conn.execute("DELETE FROM skillflow_trace WHERE step_instance_id=? AND event='output_starvation_recovered'", (claim.token.step_instance_id,))
    sf._conn.commit()
    e,ws=traced_host(sf,rid,claim,root,[f.response("finish_step")],calls,6)
    assert f.execute(e,ws,rid,claim) is True
    assert not any("Required starvation recovery" in str(m) for m in calls[-1]["messages"])
    assert (root/"partial.py").read_text() == "OWNED=1\n"
    sf._conn.close()
