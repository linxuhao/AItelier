"""Owned SkillFlow trace, real tools and outer native execution across reclaim."""
import copy
import json
from types import SimpleNamespace

import pytest
from core.ai_router import OUTPUT_CAP_CEILING
from core.dpe_pipeline import NativeOutputCapExhausted, NativeTurnBudgetExhausted, PipelineEngine
from tests.unit import test_native_direct_code as f


def starve():
    return SimpleNamespace(text="", reasoning_content="RECLAIM-STARVED", truncated=True, tool_calls=[])


def traced_host(sf, rid, claim, root, script, calls, budget):
    stream = iter(script)
    def provider(messages, **kwargs):
        calls.append({"messages": copy.deepcopy(messages), **kwargs})
        item = next(stream)
        if isinstance(item, BaseException):
            raise item
        e.factory.get_native_agent("fake").gateway.last_usage = {
            "completion_tokens": OUTPUT_CAP_CEILING if item.truncated else 7,
            "reasoning_tokens": OUTPUT_CAP_CEILING if item.truncated else 0,
        }
        return item
    e, ws = f.host(sf, rid, claim, root, provider)
    e.factory.get_max_tool_turns = lambda _: budget
    e.factory.get_native_agent("fake").gateway.max_output_tokens = OUTPUT_CAP_CEILING
    return e, ws


def payloads(sf, event):
    return [json.loads(row[0]) for row in sf._conn.execute(
        "SELECT payload_json FROM skillflow_trace WHERE event=? ORDER BY seq", (event,))]


def seed_crash(sf, rid, claim, root, budget, script=None):
    calls = []
    script = script or [f.response("create", file="partial.py", content="OWNED=1\n"),
                        starve(), KeyboardInterrupt("owned correction call crashed")]
    e, ws = traced_host(sf, rid, claim, root, script, calls, budget)
    with pytest.raises(KeyboardInterrupt):
        f.execute(e, ws, rid, claim)
    assert (root / "partial.py").read_text() == "OWNED=1\n"
    return e, calls


@pytest.mark.parametrize("after", [RuntimeError("restored transport failed"),
    SimpleNamespace(text="", reasoning_content="", truncated=False, tool_calls=[]),
    SimpleNamespace(text="done", reasoning_content="", truncated=False, tool_calls=[]),
    SimpleNamespace(text="", reasoning_content="", truncated=False,
        tool_calls=[{"id":"bad", "function":{"name":"finish_step", "arguments":"{"}}])])
def test_reclaimed_required_correction_cannot_complete_partial_work(tmp_path, monkeypatch, after):
    sf, rid, claim, root = f.run_fixture(tmp_path, monkeypatch)
    old, before = seed_crash(sf, rid, claim, root, 4)
    calls = []
    restored, ws = traced_host(sf, rid, claim, root, [after], calls, 4)
    with pytest.raises(NativeOutputCapExhausted):
        f.execute(restored, ws, rid, claim)
    assert len(before) == 3 and len(calls) == 1
    assert calls[0]["tool_choice"] == "auto"
    assert any("Required starvation recovery" in str(m) for m in calls[0]["messages"])
    resumed = payloads(sf, "resumed_from_trace")[-1]
    assert resumed["turns"] == 3 and resumed["dropped_tail"] == 0
    failure = payloads(sf, "output_cap_exhausted")[-1]
    assert failure["turn_budget"] == {"max_turns":4, "turns_used":4}
    assert failure["written_files"] == ["partial.py"]
    assert failure["first_write_turn"] == 1
    assert failure["output_budget"]["completion_tokens"] == OUTPUT_CAP_CEILING + 7 * (int(not isinstance(after, BaseException)) + 1)
    assert failure["output_budget"]["reasoning_tokens"] == OUTPUT_CAP_CEILING
    assert failure["output_budget"]["peak_turn_completion_tokens"] == OUTPUT_CAP_CEILING
    assert not payloads(sf, "output_starvation_recovered")
    assert (root / "partial.py").read_text() == "OWNED=1\n"
    sf._conn.close()


def test_reclaimed_productive_finish_uses_last_original_turn(tmp_path, monkeypatch):
    sf, rid, claim, root = f.run_fixture(tmp_path, monkeypatch)
    old, before = seed_crash(sf, rid, claim, root, 4)
    calls = []
    restored, ws = traced_host(sf, rid, claim, root,
        [f.response("finish_step", summary="validated retained output")], calls, 4)
    assert f.execute(restored, ws, rid, claim) is True
    assert len(before) + len(calls) == 4 and calls[-1]["tool_choice"] == "auto"
    recovered = payloads(sf, "output_starvation_recovered")[-1]
    assert recovered["turn"] == 4
    assert not restored._resume_from_trace("p", 4)["starved_recovery_pending"]
    assert (root / "partial.py").read_text() == "OWNED=1\n"
    sf._conn.close()


def test_repeated_reclaims_cannot_replenish_provider_turns(tmp_path, monkeypatch):
    sf, rid, claim, root = f.run_fixture(tmp_path, monkeypatch)
    old, before = seed_crash(sf, rid, claim, root, 5)
    restored_calls = []
    for _ in range(2):
        e, ws = traced_host(sf, rid, claim, root,
            [KeyboardInterrupt("repeated correction call crash")], restored_calls, 5)
        with pytest.raises(KeyboardInterrupt):
            f.execute(e, ws, rid, claim)
    last_calls = []
    e, ws = traced_host(sf, rid, claim, root, [f.response("finish_step")], last_calls, 5)
    with pytest.raises(NativeTurnBudgetExhausted):
        f.execute(e, ws, rid, claim)
    assert len(before) + len(restored_calls) == 5 and not last_calls
    assert [p["turn"] for p in payloads(sf, "output_starvation_turn_started")] == [3,4,5]
    failure = payloads(sf, "turn_budget_exhausted")[-1]
    assert failure["turn_budget"]["max_turns"] == failure["turn_budget"]["turns_used"] == 5
    assert failure["written_files"] == ["partial.py"]
    assert not payloads(sf, "output_cap_escalated") and not payloads(sf, "turn_granted_for_escalation")
    sf._conn.close()


def test_successful_read_before_reclaim_keeps_recovery_and_accounting(tmp_path, monkeypatch):
    sf, rid, claim, root = f.run_fixture(tmp_path, monkeypatch)
    old, before = seed_crash(sf, rid, claim, root, 5, [
        f.response("create", file="partial.py", content="OWNED=1\n"), starve(),
        f.response("read", path="partial.py"), KeyboardInterrupt("after productive read")])
    calls = []
    e, ws = traced_host(sf, rid, claim, root, [RuntimeError("later transport")], calls, 5)
    with pytest.raises(NativeOutputCapExhausted):
        f.execute(e, ws, rid, claim)
    failure = payloads(sf, "output_cap_exhausted")[-1]
    assert failure["turn_budget"]["turns_used"] == 5
    assert failure["reads_searches"] == 1
    assert failure["output_budget"]["completion_tokens"] == OUTPUT_CAP_CEILING + 14
    assert len(before) + len(calls) == 5
    sf._conn.close()


def test_crash_before_starvation_prompt_is_traced_still_restores_consumed_turn(tmp_path, monkeypatch):
    sf, rid, claim, root = f.run_fixture(tmp_path, monkeypatch)
    calls = []
    e, ws = traced_host(sf, rid, claim, root, [
        f.response("create", file="partial.py", content="OWNED=1\n"), starve()], calls, 3)
    trace = e._trace_cb
    def crash_after_event(cat, event, payload):
        trace(cat, event, payload)
        if event == "output_starvation_correction":
            raise KeyboardInterrupt("crash before tool-less assistant prompt delta")
    e._trace_cb = crash_after_event
    with pytest.raises(KeyboardInterrupt):
        f.execute(e, ws, rid, claim)
    after = []
    e, ws = traced_host(sf, rid, claim, root, [f.response("finish_step")], after, 3)
    assert f.execute(e, ws, rid, claim) is True
    assert payloads(sf, "resumed_from_trace")[-1]["turns"] == 2
    assert len(calls) + len(after) == 3
    assert any("Required starvation recovery" in str(m) for m in after[0]["messages"])
    sf._conn.close()


def test_quota_after_reclaim_remains_original_infrastructure_failure(tmp_path, monkeypatch):
    sf, rid, claim, root = f.run_fixture(tmp_path, monkeypatch)
    old, before = seed_crash(sf, rid, claim, root, 4)
    quota = Exception("You have exceeded the 5-hour usage quota. It will reset tomorrow.")
    calls = []
    e, ws = traced_host(sf, rid, claim, root, [quota], calls, 4)
    with pytest.raises(Exception) as caught:
        f.execute(e, ws, rid, claim)
    assert caught.value is quota and len(calls) == 1
    assert (root / "partial.py").exists() and not payloads(sf, "output_starvation_recovered")
    sf._conn.close()


def test_new_attempt_and_stale_prose_do_not_restore_old_recovery():
    rows = [("user_prompt", {"mode":"native", "attempt":1}),
            ("prompt_delta", {"index":0,"role":"user","content":"begin"}),
            ("output_cap_starved", {"attempt":1,"turn":2}),
            ("output_starvation_turn_started", {"attempt":1,"turn":3}),
            ("user_prompt", {"mode":"native", "attempt":2}),
            ("prompt_delta", {"index":1,"role":"user", "attempt":2,
                              "content":"old note: output cap and turn budget are unchanged"})]
    rebuilt = PipelineEngine._rebuild_from_deltas(rows, 5)
    assert not rebuilt["starved_recovery_pending"]
    assert rebuilt["turns"] == 0 and rebuilt["current_max_turns"] == 5


def test_new_owned_instance_does_not_inherit_another_instances_pending_recovery(tmp_path, monkeypatch):
    sf, rid, claim, root = f.run_fixture(tmp_path, monkeypatch)
    old, before = seed_crash(sf, rid, claim, root, 4)
    next_root = tmp_path / "next-code"
    next_root.mkdir()
    f.git(next_root, "init", "-q")
    (next_root / "baseline.py").write_text("baseline=True")
    f.git(next_root, "add", "--", "baseline.py")
    f.git(next_root, "commit", "-qm", "next owned base")
    next_run = sf.create_run("g", project_id="p")
    sf._workspace._code_path_resolver = lambda pid, run_id=None: next_root if run_id == next_run else root
    sf.start_run(next_run)
    sf.advance_run(next_run)
    next_claim = sf.claim_next_step(next_run)
    assert next_claim.token.step_instance_id != claim.token.step_instance_id
    calls = []
    ordinary = SimpleNamespace(text="ordinary completed output", reasoning_content="",
                               truncated=False, tool_calls=[])
    e, ws = traced_host(sf, next_run, next_claim, next_root, [
        f.response("create", file="next.py", content="NEXT=1\n"), ordinary], calls, 2)
    assert f.execute(e, ws, next_run, next_claim) is True
    assert len(calls) == 2 and calls[-1]["tool_choice"] == "none"
    assert not any("Required starvation recovery" in str(m) for m in calls[0]["messages"])
    assert (next_root / "next.py").read_text() == "NEXT=1\n"
    assert (root / "partial.py").read_text() == "OWNED=1\n"
    assert not e._resume_from_trace("p", 2)["starved_recovery_pending"]
    sf._conn.close()
