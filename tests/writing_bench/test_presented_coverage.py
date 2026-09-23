"""Regression shapes from wrong-chapter/omitted-tail production reviews.

Fixtures are synthetic; no private novel prose is checked into this repository.
"""
import copy
import json
from pathlib import Path

import pytest

from aitelier.writing_bench.reading import (
    Coverage, Material, ReviewSession, bounded_page, frame, load_certificate,
    material_identity, validate_certificate,
)
from aitelier.writing_bench.storage import BenchError, encode, sha

KEY = "a" * 64
TARGETS = [{"chapter": 32, "title": "离开", "prose_sha256": "b" * 64}]


def materials(long=False):
    prose = "# 第32章：离开\n\n他丢下车，带所有人离开。\n最终损耗一发，余七发。\n"
    history = "# 第31章：前情\n" + ("旧史：没有开枪。\n" * (4000 if long else 10))
    return [Material("current_prose.md", "step:prepare", frame(KEY, "current_prose.md", prose)),
            Material("review_context.md", "step:prepare", frame(KEY, "review_context.md", history))]


def coverage(long=False):
    return Coverage("literary", KEY, TARGETS, materials(long))


def report(**extra):
    return {"review_key": KEY, "reviewed_chapters": copy.deepcopy(TARGETS),
            "passed": True, "read_complete": True, "feedback": "第32章选择弃车，损耗一发。", "findings": [], **extra}


def tool_message(value, name="read", call="read-1"):
    return [{"role": "assistant", "content": None,
             "tool_calls": [{"id": call, "type": "function", "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call, "content": json.dumps(value, ensure_ascii=False)}]


def native_page(m, start=0, end=None, raw=False):
    lines = m.text.splitlines(keepends=True);end = len(lines) if end is None else end
    return {"source": m.source, "path": m.path, "start_line": start,
            "returned_lines": end-start, "total_lines": len(lines), "truncated": end < len(lines),
            "content": ("".join(lines[start:end]) if raw else
                        "\n".join(f"{i+1}\t{lines[i].rstrip(chr(10))}" for i in range(start, end)))}


def full(c):
    # These are model-visible user messages, not read requests or fake receipts.
    c.observe([{"role": "user", "content": m.text} for m in c.materials.values()])
    return c


def test_initial_full_injection_does_not_require_duplicate_read():
    c = full(coverage())
    cert = c.certificate(report(), {"run_id": "r", "step_id": "literary_review"})
    validate_certificate(cert, c.identity, report())


def test_only_history_or_header_and_model_claim_never_proves_new_prose():
    c = coverage();ms = list(c.materials.values())
    c.observe([{"role": "user", "content": ms[1].text},
               {"role": "assistant", "content": ms[0].text}])
    with pytest.raises(BenchError, match="not been presented"):
        c.certificate(report(), {})
    assert c.missing()[0]["path"] == "current_prose.md"


@pytest.mark.parametrize("raw", [False, True])
def test_disjoint_pages_require_middle_even_when_eof_seen(raw):
    c = coverage();p,ctx = list(c.materials.values());c.observe([{"role":"user","content":ctx.text}])
    n=len(p.text.splitlines())
    c.observe(tool_message(native_page(p,0,1,raw)))
    c.observe(tool_message(native_page(p,n-1,n,raw),call="last"))
    with pytest.raises(BenchError,match="not been presented"):c.certificate(report(),{})
    c.observe(tool_message(native_page(p,1,n-1,raw),call="middle"))
    assert not c.missing()


def test_requested_whole_file_but_returned_prefix_does_not_pass():
    c=coverage();p,ctx=list(c.materials.values());c.observe([{"role":"user","content":ctx.text}])
    page=native_page(p,0,2)
    messages=tool_message(page)
    messages[0]["tool_calls"][0]["function"]["arguments"]='{"start_line":0,"end_line":999999}'
    c.observe(messages)
    assert c.missing()


def test_forged_counts_or_changed_text_cannot_cover_hole():
    c=coverage();p=list(c.materials.values())[0]
    v=native_page(p,0,1);v['returned_lines']=len(p.text.splitlines());v['truncated']=False
    c.observe(tool_message(v))
    assert c.ranges[p.path]==[]
    v=native_page(p);v['content']=v['content'].replace('余七发','余八发');c.observe(tool_message(v,call='bad'))
    assert c.ranges[p.path]==[]


def test_outer_complete_recall_only_credits_actual_inner_read_window():
    c=coverage();p,ctx=list(c.materials.values());c.observe([{"role":"user","content":ctx.text}])
    original=native_page(p,0,2)
    recalled={"content":json.dumps(original,ensure_ascii=False),"truncated":False,"complete":True}
    c.observe(tool_message(recalled,"recall_observation"))
    assert c.missing()
    c.observe(tool_message(native_page(p,2),call='rest'))
    assert not c.missing()


def test_projected_large_result_is_not_a_read_even_if_raw_trace_has_tail():
    from core.dpe_pipeline import _project_native_messages
    c=coverage(long=True);p,ctx=list(c.materials.values());c.observe([{"role":"user","content":p.text}])
    raw=tool_message(native_page(ctx))
    visible,stats=_project_native_messages(raw)
    assert stats['compacted_tool_results']==1
    c.observe(visible)
    assert c.missing() and c.ranges[ctx.path]==[]
    # Bounded custom pages survive the exact same production projection.
    offset=0
    while offset<len(ctx.text):
        page=bounded_page(ctx,offset,999999)
        messages,stats=_project_native_messages(tool_message(page,'novel_bench_read',str(offset)))
        assert stats['compacted_tool_results']==0
        c.observe(messages);offset=page['end']
    assert not c.missing()


def test_initial_truncated_prefix_is_only_prefix_and_wrong_source_is_ignored():
    c=coverage();p,ctx=list(c.materials.values());c.observe([{"role":"user","content":ctx.text}])
    c.observe([{"role":"user","content":p.text.splitlines(keepends=True)[0]+'...[truncated]'}])
    v=native_page(p);v['source']='self';c.observe(tool_message(v))
    assert c.missing()


@pytest.mark.parametrize('target', [[],[{"chapter":31,"title":"前情","prose_sha256":"b"*64}],
                                    [{"chapter":32,"title":"离开","prose_sha256":"c"*64}]])
def test_current_chapter_title_and_hash_bind_report(target):
    c=full(coverage())
    with pytest.raises(BenchError,match='target mismatch'):c.certificate(report(reviewed_chapters=target),{})


def test_guard_blocks_write_then_gives_specific_resume_and_preserves_human_judgment(tmp_path):
    s=ReviewSession(coverage(),tmp_path,{'run_id':'r','step_id':'literary_review','step_instance_id':1})
    err=s.guard('create_verdict',report())
    assert err['missing_review_material'][0]['read']['tool']=='novel_bench_read'
    with pytest.raises((BenchError,FileNotFoundError)):load_certificate(tmp_path,'literary')
    full(s.coverage)
    assert s.guard('create_verdict',report()) is None
    cert=load_certificate(tmp_path,'literary');validate_certificate(cert,s.coverage.identity,report())
    with pytest.raises(BenchError,match='different verdict'):
        validate_certificate(cert,s.coverage.identity,report(feedback='未经同一阅读会话签出的结论'))


def test_new_executor_cannot_borrow_old_attestation(tmp_path):
    s=ReviewSession(full(coverage()),tmp_path,{'run_id':'r','step_instance_id':1})
    assert s.guard('write_verdict',{'content':report()}) is None
    newer=ReviewSession(coverage(),tmp_path,{'run_id':'r','step_instance_id':2})
    assert newer.guard('write_verdict',{'content':report()})['error']
    assert s.guard('write_verdict',{'content':report()})['error']=='review executor was superseded'


def test_negative_unread_report_is_allowed_but_never_certified(tmp_path):
    s=ReviewSession(coverage(),tmp_path,{'run_id':'r'})
    assert s.guard('write_verdict',{'content':report(passed=False,read_complete=False)}) is None
    with pytest.raises((BenchError,FileNotFoundError)):load_certificate(tmp_path,'literary')


def test_certificate_cannot_be_reused_for_changed_materials():
    c=full(coverage());cert=c.certificate(report(),{'run_id':'r'})
    altered=list(c.materials.values());altered[0]=Material(altered[0].path,altered[0].source,altered[0].text+'新结尾\n')
    with pytest.raises(BenchError,match='different materials'):
        validate_certificate(cert,material_identity('literary',KEY,TARGETS,altered),report())


def test_host_system_injection_is_counted_but_assistant_echo_is_not():
    c=coverage()
    c.observe([{'role':'system','content':m.text} for m in c.materials.values()])
    assert not c.missing()


def test_every_target_in_multichapter_review_requires_coverage():
    targets=TARGETS+[{'chapter':33,'title':'次日','prose_sha256':'c'*64}]
    ms=materials()+[Material('chapter33.md','step:prepare',frame(KEY,'chapter33.md','# 第33章：次日\n不能遗漏。\n'))]
    c=Coverage('literary',KEY,targets,ms)
    c.observe([{'role':'user','content':x.text} for x in ms[:2]])
    with pytest.raises(BenchError,match='not been presented'):
        c.certificate(report(reviewed_chapters=targets),{})
    c.observe([{'role':'user','content':ms[2].text}])
    assert not c.missing()


def test_boolean_chapter_does_not_alias_integer_target():
    c=Coverage('literary',KEY,[{'chapter':1,'title':'一','prose_sha256':'b'*64}],materials())
    full(c)
    with pytest.raises(BenchError,match='target mismatch'):
        c.certificate(report(reviewed_chapters=[{'chapter':True,'title':'一','prose_sha256':'b'*64}]),{})


def test_concurrent_replacement_cannot_be_overwritten_by_late_certificate(tmp_path,monkeypatch):
    import threading
    from aitelier.writing_bench import reading
    old=ReviewSession(full(coverage()),tmp_path,{'run_id':'r','claim_epoch':1})
    started=threading.Event();replaced=threading.Event();new=[]
    def replace():
        started.set()
        new.append(ReviewSession(coverage(),tmp_path,{'run_id':'r','claim_epoch':2}))
        replaced.set()
    thread=threading.Thread(target=replace)
    original=reading.immutable
    def at_publication(path,raw):
        thread.start();assert started.wait(2)
        # Without the shared lock the new owner installs its pointer here,
        # then the old executor overwrites it. With the lock it must wait.
        replaced.wait(0.15)
        return original(path,raw)
    monkeypatch.setattr(reading,'immutable',at_publication)
    assert old.guard('write_verdict',report()) is None
    thread.join(3);assert replaced.is_set() and not thread.is_alive()
    pointer=json.loads((tmp_path/'literary-session.json').read_text())
    assert pointer['session']==new[0].session and pointer['certificate'] is None
    assert old.guard('write_verdict',report())['error']=='review executor was superseded'


def test_empty_review_material_is_rejected_before_observer_installation():
    with pytest.raises(BenchError,match='nonempty'):
        Material('empty.md','step:prepare','')
