"""Statement-only checks. Run in the authorized isolated SDK1.5.94 container.

Exact accepted 0f0 mathematical/data modules and 5ebe version codec are supplied
as declared read-only dependency exports by the test runner. No native evaluator
binding, model, private golden checker or production service is invoked here.
"""
import concurrent.futures
import importlib.metadata
import json
import os
from pathlib import Path
import threading

import pytest
from pydantic import ValidationError
from skillflow.graph import PipelineGraph, GraphResolver
from core import pipeline_benchmark as data
from core import pipeline_optimization as numerical
from accepted_versioning import contract as version_codec

from .contract import (Amount, Envelope, EvaluationRequest, EvaluationResult,
                       benchmark_row, digest, scorer_receipt, validate_result)
from .fake import BOUND, USAGE, ZERO, FakeFixture, FakeReservations, BudgetDenied, load_fixture
from .conformance import SCENARIOS, ADMISSION_SCENARIOS, run_scenario, run_admission_scenario


def evidence(name, value):
    target = os.getenv("EVALUATOR_EVIDENCE")
    if target:
        Path(target, name + ".json").write_text(json.dumps(value, indent=2) + "\n")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s:s["name"])
def test_same_shared_scenarios(scenario):
    evidence("scenario-"+scenario["name"], run_scenario(FakeFixture, scenario))


def test_literal_sdk_schema_and_existing_version_codec(tmp_path):
    fixture = load_fixture()
    path = tmp_path / "graph.json"  # JSON is valid YAML; normal public loader
    path.write_text(json.dumps(fixture["unit"]["graph"]))
    graph = PipelineGraph.from_yaml(path)
    assert graph.validate() == [] and GraphResolver(graph).validate() == []
    assert graph.steps[0].agent_config in fixture["unit"]["roles"]
    assert graph.steps[0].output_fixed == {"findings":"findings.md"}
    for arm in ("reference", "candidate"):
        obj=fixture["identity"][arm]
        assert version_codec.version_id_of(obj["logical_content"]) == obj["logical_version"]
    assert importlib.metadata.version("skillflow-py") == "1.5.94"
    evidence("imports", {"sdk_version":importlib.metadata.version("skillflow-py"),
                         "graph":__import__('skillflow.graph',fromlist=['']).__file__,
                         "contract":__import__('contracts.rsi.evaluator.contract',fromlist=['']).__file__,
                         "fake":__import__('contracts.rsi.evaluator.fake',fromlist=['']).__file__,
                         "benchmark":data.__file__, "numerical":numerical.__file__,
                         "version_codec":version_codec.__file__})


def test_strict_request_refusals_before_any_effect():
    fixture=FakeFixture(); request=fixture.prepare()
    payload=request.model_dump(mode="json")
    mutations=[{"max_parallel":3},{"max_parallel":True},{"repetitions":2},{"repetitions":3.0},
               {"unknown":"private-material"},{"cells":payload["cells"][:-1]},
               {"cells":payload["cells"]+payload["cells"][:1]},
               {"cells":list(reversed(payload["cells"]))},
               {"holdout_selection":digest("not-train")},
               {"plan_identity":digest("wrong-plan")},
               {"split":"other"}]
    for change in mutations:
        with pytest.raises(ValidationError):
            EvaluationRequest.model_validate_json(json.dumps(dict(payload,**change)))
    assert fixture.observations()["launches"] == fixture.observations()["physical_sends"] == 0
    with pytest.raises(ValueError): fixture.set_fault("unknown-fault")
    with pytest.raises(TypeError): fixture.evaluate(payload)


def test_actual_benchmark_store_projection_and_unchanged_numerical_adapter(tmp_path):
    fixture=FakeFixture(); request=fixture.prepare(); result=fixture.evaluate(request)
    validate_result(request,result)
    package=load_fixture()["benchmark"]
    store=data.BenchmarkStore(tmp_path/'benchmark.json')
    header=package['header']; store.create_draft('trusted-coordinator',header)
    for cid in header['cases']:
        store.set_public_input('trusted-coordinator',header['benchmark_id'],1,cid,
                               {"prompt":package['public'][cid]['prompt']})
        store.set_private_refs('trusted-coordinator',header['benchmark_id'],1,cid,
                               package['private'][cid]['refs'])
    published=store.publish('trusted-coordinator',header['benchmark_id'],1)
    assert published['definition_digest'] == request.benchmark.definition
    for receipt in result.receipts:
        row=benchmark_row(receipt,request.benchmark)
        assert set(row) == set(data.ROW)
        store.append_receipt('trusted-coordinator',row)
    snapshot=store.scoreboard_snapshot(header['benchmark_id'],1)
    assert snapshot['row_count'] == 72
    with pytest.raises(data.BenchmarkError):
        store.append_receipt('trusted-coordinator',benchmark_row(result.receipts[0],request.benchmark))
    expected={s.arm:{'bundle_identity':s.native_bundle,'runtime':benchmark_row(result.receipts[0],request.benchmark)['runtime']}
              for s in request.selections}
    aggregate=store.aggregate(header['benchmark_id'],1,expected)
    assert aggregate['status']=='INVALID' and len(aggregate['missing_cells'])==48
    assert aggregate['expected_cells']==120  # train is NOT a full-document aggregate
    cases=request.benchmark.cases
    manifest={'train_case_ids':[c.case_id for c in cases if c.split=='train'],
              'holdout_case_ids':[c.case_id for c in cases if c.split=='holdout'],
              'case_families':{c.case_id:c.family for c in cases},
              'family_inventory':['family-a','family-b','family-c','family-d'],
              'repetitions':3,
              'arms':{s.arm:{'bundle_id':s.native_bundle,'runtime_id':digest(request.runtime),
                             'checker_id':request.benchmark.checker_package} for s in request.selections},
              'input_ids':{c.case_id:c.prompt_identity for c in cases}}
    receipts=[scorer_receipt(r) for r in result.receipts]
    numerical_result=numerical.evaluate_matrix(manifest,receipts,stage='train')
    assert numerical_result['status']=='train_complete'
    assert numerical_result['public_summary']['overall_difference']['exact']=='10'
    assert not numerical_result['grants_authority']
    assert numerical.evaluate_matrix(manifest,receipts[:-1],stage='train')['status']=='invalid'
    assert numerical.evaluate_matrix(manifest,receipts+receipts[:1],stage='train')['status']=='invalid'
    changed=[dict(receipts[0],accounting='unknown')]+receipts[1:]
    assert numerical.evaluate_matrix(manifest,changed,stage='train')['status']=='invalid'
    evidence('accepted-data-and-numerical',{'rows':snapshot['row_count'],
             'full_document_status':aggregate['status'],'missing_holdout':len(aggregate['missing_cells']),
             'stage_status':numerical_result['status'],'difference':numerical_result['public_summary']['overall_difference']})


def test_fake_budget_exact_atomic_multiscope_unknown_and_nonretryable():
    one=Envelope(upper=BOUND,elapsed_ms=10)
    admission=FakeReservations({'cell-a':one,'cell-b':one,'campaign':one})
    barrier=threading.Barrier(2)
    def reserve(cell):
        barrier.wait()
        try: admission.reserve((cell,'campaign'),BOUND); return True
        except BudgetDenied as exc:
            assert exc.retryable is False
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        result=list(executor.map(reserve,('cell-a','cell-b')))
    assert sorted(result)==[False,True]
    assert admission.used['campaign']==BOUND
    loser='cell-a' if not result[0] else 'cell-b'
    assert admission.used[loser]==ZERO  # failed multi-scope reserve changes NOTHING
    winner='cell-b' if loser=='cell-a' else 'cell-a'
    admission.settle((winner,'campaign'),BOUND,None)
    assert admission.closed and admission.used['campaign']==BOUND
    with pytest.raises(BudgetDenied): admission.reserve((loser,'campaign'),BOUND)
    expired=FakeReservations({'scope':one})
    with pytest.raises(BudgetDenied): expired.reserve(('scope',),BOUND,elapsed_ms=10)
    assert expired.used['scope']==ZERO
    known=FakeReservations({'scope':one})
    known.reserve(('scope',),BOUND)
    known.settle(('scope',),BOUND,USAGE)
    assert known.used['scope']==USAGE
    evidence('fake-only-budget',{'atomic_admissions':result,'unknown_retained':admission.used['campaign'].model_dump(),
                               'real_enforcement_proved':False})


def test_result_identity_and_public_projection_reject_tampering():
    fixture=FakeFixture(); request=fixture.prepare(); result=fixture.evaluate(request)
    first=result.receipts[0]
    bad=first.model_copy(update={'public_package':digest('tampered')})
    receipts=(bad,)+result.receipts[1:]
    altered=result.model_copy(update={'receipts':receipts,
                                    'receipt_catalog':tuple((r.receipt_id,digest(r)) for r in receipts)})
    with pytest.raises(ValueError): validate_result(request,altered)
    projection=result.optimizer_projection.model_copy(update={'train_prompts':(('train-01','PRIVATE_CHECKER'),)})
    with pytest.raises(ValueError): validate_result(request,result.model_copy(update={'optimizer_projection':projection}))


@pytest.mark.parametrize("vector", ADMISSION_SCENARIOS, ids=lambda v:v["name"])
def test_same_shared_admission_vectors(vector):
    evidence("admission-"+vector["name"], run_admission_scenario(FakeFixture,vector))


def test_export_exact_wire_schemas():
    from .contract import CellReceipt, Refusal, TrainProjection
    schema = {c.__name__:c.model_json_schema() for c in
              (EvaluationRequest, EvaluationResult, CellReceipt, Refusal, TrainProjection)}
    assert schema["EvaluationRequest"]["additionalProperties"] is False
    evidence("wire-schemas",schema)


def test_source_concrete_profile_matches_public_host_configuration():
    from core.model_routes import ModelRoutes
    source=Path(__file__).parents[3]
    examples=json.loads((source/"llm_providers.example.json").read_text())
    fixture=FakeFixture(); request=fixture.prepare()
    model=request.runtime.requested_model
    assert ModelRoutes(source/"model_routes.example.json").resolve(model)==[model]
    assert request.runtime.endpoint.provider in examples
    role=load_fixture()["unit"]["roles"]["gen_eval_fixture__investigator"]
    assert role["model"]==model
    assert role["thinking"]["effort"]==request.runtime.effort
    evidence("source-concrete-profile",{"requested_model":model,"endpoint":request.runtime.endpoint.model_dump(),
              "source_profile_only":True,"live_availability_pricing_transport_verified":False})


def test_literal_wire_examples():
    fixture=FakeFixture(); request=fixture.prepare(); result=fixture.evaluate(request)
    refused=FakeFixture(); refused_request=refused.prepare(); refused.set_fault("empty_grants")
    refusal=refused.evaluate(refused_request)
    evidence("wire-examples",{"evidence_kind":"simulation",
             "request":request.model_dump(mode="json"),
             "first_receipt":result.receipts[0].model_dump(mode="json"),
             "refusal":refusal.model_dump(mode="json"),
             "result_header":result.model_dump(mode="json",exclude={"receipts","receipt_catalog"})})
