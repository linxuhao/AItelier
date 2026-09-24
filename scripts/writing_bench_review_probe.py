#!/usr/bin/env python3
"""Read-only real-model rehearsal of a frozen Writing Bench review.

Input is an operator-prepared JSON plan containing exact material/report targets.
This exercises the production gateway, projection, bounded reader and host
coverage guard in a separate process. It is NOT a production SkillFlow review
receipt and cannot approve or promote a novel. The output explicitly says so.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import sys
import time

from core.ai_router import AIGateway
from core.dpe_pipeline import _project_native_messages
from aitelier.writing_bench.reading import (
    Material, Coverage, ReviewSession, bounded_page, load_certificate,
    validate_certificate,
)
from aitelier.writing_bench.storage import decode, encode, immutable, require, sha


def run(plan: dict, out: Path, *, providers: str, routes: str, model: str, turns: int) -> dict:
    require(not out.exists(), "probe output already exists; do not overwrite a past review")
    out.mkdir(parents=True)
    materials=[]
    for spec in plan['materials']:
        raw=Path(spec['file']).read_bytes()
        require(sha(raw)==spec['sha256'], 'probe material changed: '+spec['path'])
        materials.append(Material(spec['path'],spec['source'],raw.decode()))
    phase=plan['phase'];key=plan['review_key'];targets=plan['targets']
    observer=ReviewSession(Coverage(phase,key,targets,materials),out/'host-evidence',
                {'run_id':'read-only-probe','step_id':phase,'step_instance_id':'not-a-production-step'})
    gateway=AIGateway(model,config_path=providers,routes_path=routes,
                      enable_thinking=True,thinking_effort='high',temperature=0.1,max_output_tokens=10000)
    presentations=[]
    def present(messages):
        observer.observe(messages)
        presentations.append({'request_sha256':sha(encode(messages)),
                              'missing_after_request':observer.coverage.missing()})
        immutable(out/f'presented-{len(presentations):03d}.json',encode(messages))
    gateway.on_messages_presented=present
    system=Path(plan['template_file']).read_text()
    system+='\n这是只读审稿演练，不接受或推送正文。按独立判断审当前稿，输出结构见write_verdict；发现问题照实拒绝。'
    request={'phase':phase,'review_key':key,'targets':targets,
             'required_materials':[m.descriptor() for m in materials],
             'read_method':'novel_bench_read(path="review/<材料文件名>",start=0,length=8000)，沿next_start读完；current_prose已在下方。'}
    prose=next(m for m in materials if m.path=='current_prose.md')
    messages=[{'role':'system','content':system},
              {'role':'user','content':encode(request).decode()+'\n'+prose.text}]
    import yaml
    graph=yaml.safe_load((Path(__file__).resolve().parents[1]/"configs/novel_writing_bench_v2.yaml").read_text())
    step_id="literary_review" if phase=="literary" else "ledger_audit"
    schema=next(x for x in graph['steps'] if x['id']==step_id)['validation'][0]['inline_schema']
    read_schema={'type':'object','additionalProperties':False,'required':['path'],
                 'properties':{'path':{'type':'string'},'start':{'type':'integer','minimum':0},
                               'length':{'type':'integer','minimum':1,'maximum':8000}}}
    tools=[{'type':'function','function':{'name':'novel_bench_read',
           'description':'Read only an exact required material; follow next_start.','parameters':read_schema}},
           {'type':'function','function':{'name':'write_verdict',
           'description':'Submit the complete independent review. Missing host-observed text is refused.',
           'parameters':schema}}]
    started=time.monotonic();report=None;refusals=[]
    for turn_no in range(1,turns+1):
        projected,stats=_project_native_messages(messages)
        response=gateway.generate_native(projected,tools=tools)
        event={'turn':turn_no,'projection':stats,'usage':gateway.last_usage,
               'text':response.text,'tool_calls':response.tool_calls}
        immutable(out/f'response-{turn_no:03d}.json',encode(event))
        assistant={'role':'assistant','content':response.text or None,'tool_calls':response.tool_calls}
        if response.reasoning_content:assistant['reasoning_content']=response.reasoning_content
        messages.append(assistant)
        if not response.tool_calls:
            messages.append({'role':'user','content':'请继续读取尚缺的材料，或用write_verdict提交完整真实判断。不要用普通文字替代审查结果。'})
            continue
        for call in response.tool_calls:
            name=call['function']['name'];params=decode(call['function']['arguments'].encode())
            if name=='novel_bench_read':
                material=next((m for m in materials if 'review/'+m.path==params.get('path')),None)
                result=(bounded_page(material,params.get('start',0),params.get('length',8000))
                        if material else {'error':'unknown required material'})
            elif name=='write_verdict':
                result=observer.guard('write_verdict',params)
                if result is None:
                    report=params;result={'written':'review.json','production_acceptance':False}
                    immutable(out/'review.json',encode(report))
                else:refusals.append({'turn':turn_no,'reason':result})
            else:result={'error':'tool not granted in read-only probe'}
            messages.append({'role':'tool','tool_call_id':call['id'],'content':encode(result).decode()})
        if report is not None:break
    require(report is not None,'probe exhausted without a valid report; previous outputs retained')
    complete=False
    if report.get('read_complete'):
        certificate=load_certificate(out/'host-evidence',phase)
        validate_certificate(certificate,observer.coverage.identity,report);complete=True
    result={'status':'review_returned','protocol':'isolated-real-model-presentation-probe','production_step':False,
            'candidate_code_commit':plan['candidate_code_commit'],'novel_commit':plan['novel_commit'],
            'targets':targets,'phase':phase,'actual_model_requests':len(presentations),'reading_complete_verified':complete,
            'model_passed':report['passed'],'report_sha256':sha(encode(report)),
            'presentations':presentations,'refusals':refusals,'elapsed_seconds':round(time.monotonic()-started,3),
            'model':model,'novel_files_modified':False,'manual_checkpoint_answered':False,
            'promotion_or_backup_executed':False}
    immutable(out/'probe_result.json',encode(result));return result


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--plan',required=True);ap.add_argument('--out',required=True)
    ap.add_argument('--providers',required=True);ap.add_argument('--routes',required=True)
    ap.add_argument('--model',default='novel_alt');ap.add_argument('--turns',type=int,default=28)
    args=ap.parse_args();require(1<=args.turns<=40,'bounded probe turns required')
    plan=decode(Path(args.plan).read_bytes())
    result=run(plan,Path(args.out),providers=args.providers,routes=args.routes,model=args.model,turns=args.turns)
    print(encode(result).decode())


if __name__=='__main__':main()
