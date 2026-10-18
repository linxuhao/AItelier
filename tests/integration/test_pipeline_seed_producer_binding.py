"""Adopted real ToolLoader/import/SDK producer-binding regression."""
import hashlib
import json
from pathlib import Path
import pytest,yaml
from tests.integration.test_nested_callback_seed import runtime,bundle,save
from core import pipeline_bundle as pb
from core.run_launcher import start_config_run
from skillflow.context import ContextResolver
from skillflow.graph import _normalize_context_spec

@pytest.mark.asyncio
async def test_explicit_producer_bound_task_artifact_is_not_launch_seed(runtime,tmp_path):
 sf,db,ws,registry,outbound=runtime
 name='gen_step_bound_review'
 value=bundle(name,'plain','unused','unused')
 graph=yaml.safe_load(value['graph_yaml'])
 graph['steps'][0]['tool_params']={'out_dir':'$STEP_DIR'}
 graph['steps'][1]['context']=[{'source':{'config':name,'step':'probe','output':'task.md'},'required':True}]
 value['graph_yaml']=yaml.safe_dump(graph)
 tool=value['tools']['nested_seed_probe']
 tool['impl.py']='from pathlib import Path\ndef nested_seed_probe(*,out_dir="",**kwargs):\n p=Path(out_dir);p.mkdir(parents=True,exist_ok=True);(p/"task.md").write_text("LEGITIMATE PREPARED TASK ARTIFACT\\n");return {"passed":True}\n'
 schema=yaml.safe_load(tool['tool.yaml']);schema['parameters']['out_dir']={'type':'string','required':True};tool['tool.yaml']=yaml.safe_dump(schema)
 imported=pb.import_pipeline(sf,registry,value)
 launched=start_config_run(db,ws,name,'owned-step-bound',seed_text='ORIGINAL LAUNCH SEED')
 assert launched['status']=='started',launched
 sf.advance_run(launched['run_id'])
 project=sf._workspace.get_project_path('owned-step-bound')
 expected={'source':{'config':name,'step':'probe','output':'task.md'},'required':True}
 # Positive control: real produced/published task resolves at its explicit producer.
 direct=ContextResolver(project).resolve([_normalize_context_spec(expected)],current_config=name)
 assert list(direct.values())==['LEGITIMATE PREPARED TASK ARTIFACT\n'],direct
 persisted=yaml.safe_load(Path(imported['path']).read_text())
 facts={'host_registry_sha256':hashlib.sha256((Path(__file__).parents[2]/'core/pipeline_registry.py').read_bytes()).hexdigest(),'original_source':expected,'persisted_context':persisted['steps'][1]['context'],'positive_resolved':direct,'project':str(project),'launch':launched}
 try:
  claim=sf.claim_next_step(launched['run_id'])
  facts['actual_context']=claim.inputs.get('_resolved_context')
 except Exception as exc:
  facts['error']={'type':type(exc).__name__,'message':str(exc)}
 save('step-bound-first-raw', facts)
 assert 'error' not in facts,facts
 assert persisted['steps'][1]['context']==[expected],facts
 assert facts['actual_context']==direct,facts
