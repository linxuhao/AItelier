"""The unchanged contract vectors run against fake and production document store."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from fractions import Fraction

import pytest
from core.pipeline_benchmark import BenchmarkStore, BenchmarkError, normalized_score
from core import pipeline_optimization as scoring

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('benchmark_contract', ROOT/'contracts/rsi/benchmark/test_contract.py')
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


@pytest.mark.parametrize('provider', ['fake', 'production'])
def test_identical_literal_conformance(provider, tmp_path):
    counter = 0
    def factory():
        nonlocal counter
        counter += 1
        return BenchmarkStore(tmp_path/f'vector-{counter}.json')
    failures = contract.run_conformance(contract.fake.BenchmarkFake if provider == 'fake' else factory)
    Path('/evidence', provider+'-conformance.json').write_text(json.dumps({'failures':failures,'literal_vectors':len(contract.DOC['vectors']),'production_instances':counter},indent=2))
    assert failures == []


def published(path):
    world = contract.World(lambda: BenchmarkStore(path)); world.published()
    return world


def test_real_document_reopen_prefix_and_refusal_no_write(tmp_path):
    path = tmp_path/'benchmark.json'; world=published(path)
    row=world.row({'arm':'baseline','case_id':'train-01','repetition':1})
    assert world.f.append_receipt('owned-recorder',row)==1
    first=world.f.scoreboard_snapshot(contract.BID,contract.VER)
    reopened=BenchmarkStore(path)
    assert reopened.scoreboard_snapshot(contract.BID,contract.VER)==first
    assert reopened.get_private_refs(contract.DOC['owner'],contract.BID,contract.VER)==world.f.get_private_refs(contract.DOC['owner'],contract.BID,contract.VER)
    before=path.read_bytes()
    with pytest.raises(BenchmarkError) as error:reopened.append_receipt('owned-recorder',row)
    assert error.value.code=='duplicate_run_receipt' and path.read_bytes()==before
    changed=world.row({'arm':'baseline','case_id':'train-01','repetition':2})
    assert reopened.append_receipt('owned-recorder',changed)==2
    assert world.f.scoreboard_snapshot(contract.BID,contract.VER)['rows'][:1]==first['rows']
    script='from core.pipeline_benchmark import BenchmarkStore; import sys,json; print(json.dumps(BenchmarkStore(sys.argv[1]).scoreboard_snapshot(sys.argv[2],1)))'
    result=subprocess.run([sys.executable,'-B','-c',script,str(path),contract.BID],capture_output=True,text=True,check=True,cwd=ROOT)
    snapshot=json.loads(result.stdout);assert snapshot['row_count']==2 and snapshot['rows'][:1]==first['rows']
    # Returned values have no reference to persistent state, across both instances.
    snapshot['rows'][0]['raw_points']=0
    assert reopened.list_receipts(contract.BID,contract.VER)[0]['raw_points']==5
    document=json.loads(path.read_bytes());assert document['schema']=='rsi.benchmark.store.v1' and len(document['rows'])==2


def test_accepted_scorer_dependency_and_normalization(monkeypatch):
    actual=scoring._exact_score;seen=[]
    def observed(value):seen.append(value);return actual(value)
    monkeypatch.setattr(scoring,'_exact_score',observed)
    assert normalized_score(0.1,3)==Fraction(10,3)
    assert seen==[Fraction(10,3)]
    assert hashlib.sha256((ROOT/'core/pipeline_optimization.py').read_bytes()).hexdigest()=='52173bac8d9d5f7771e2ea8bc697e766cd5c409133e2d4442a99158605670cbd'
    assert BenchmarkStore.__module__=='core.pipeline_benchmark'
    assert BenchmarkStore.__bases__==(object,)
    assert str(Path(sys.modules[BenchmarkStore.__module__].__file__))=='/src/core/pipeline_benchmark.py'


def test_new_version_receipts_are_globally_unique_and_definitions_immutable(tmp_path):
    path=tmp_path/'benchmark.json';world=published(path)
    row=world.row({'arm':'baseline','case_id':'train-01','repetition':1});world.f.append_receipt('owned',row)
    h=json.loads(json.dumps(contract.FIX['header']));h['version']=2
    world.f.create_draft(contract.DOC['owner'],h)
    for cid in contract.CASES:
        world.f.set_public_input(contract.DOC['owner'],contract.BID,2,cid,{'prompt':'Owned version2 input'})
        world.f.set_private_refs(contract.DOC['owner'],contract.BID,2,cid,{'rubric_ref':'owned-r','checker_ref':'owned-c','max_points':5})
    world.f.publish(contract.DOC['owner'],contract.BID,2)
    definition=world.f.get_definition(contract.BID,2);new=dict(row,version=2,**{k:definition['cases']['train-01'][k] for k in ['input_identity','checker_identity']})
    before=path.read_bytes()
    with pytest.raises(BenchmarkError) as e:world.f.append_receipt('owned',new)
    assert e.value.code=='duplicate_run_receipt' and path.read_bytes()==before
    with pytest.raises(BenchmarkError) as e:BenchmarkStore(path).set_public_input(contract.DOC['owner'],contract.BID,1,'train-01',{'prompt':'change'})
    assert e.value.code=='immutable' and path.read_bytes()==before
