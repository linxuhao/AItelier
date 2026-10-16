"""Retention equality must use accepted canonical numeric types, not Python ==."""
import copy
import json
from pathlib import Path
from test_pipeline_normal_versions import world, call, snapshot
from core import pipeline_frozen_bundle as fb, pipeline_registry as pr
from contracts.rsi.pipeline_versioning import contract as c
from contracts.rsi.pipeline_versioning.native_provider import project_capture


def test_typed_existing_alias_collision_refuses_before_effects(world):
    fx, api = world
    intent = {'kind':'role','role':'maker','changes':{'temperature':1.0}}
    unit = fb.read_unit(fx.gen,'gen_vt')
    candidate = api._candidate('gen_vt',api._capture_live('gen_vt')[0],unit,intent)
    compiled = fb.compile_bundle(candidate,c.seal_slug(project_capture(candidate)[1]))
    preview = fb.preview_adoption('gen_vt',unit,compiled)
    roles = json.loads(unit['roles_text'])
    added = {k:v for k,v in json.loads(preview['roles_text']).items() if k not in roles}
    alias, definition = next((k,v) for k,v in added.items() if v.get('temperature') == 1.0)
    retained = copy.deepcopy(definition); retained['temperature'] = True
    roles[alias] = retained
    (fx.gen/'gen_vt.roles.json').write_text(json.dumps(roles,ensure_ascii=False,indent=2))
    assert 'error' not in pr.reload_generated_pipeline(fx.sf,fx.registry,'gen_vt')
    # The added historical-only entry changes neither active capture nor graph pin.
    unit2 = fb.read_unit(fx.gen,'gen_vt')
    candidate2 = api._candidate('gen_vt',api._capture_live('gen_vt')[0],unit2,intent)
    assert candidate2['identity'] == candidate['identity']
    before = snapshot(fx)
    result = call(fx,'edit_role',role='maker',temperature=1.0)
    after = snapshot(fx)
    Path('/evidence/typed-retained-alias.json').write_text(json.dumps({
        'alias':alias,'retained':retained,'proposed':definition,'result':result,
        'before':json.loads(before),'after':json.loads(after),'unchanged':before==after},indent=2))
    assert result.get('error') and 'prepared_seals' not in result, result
    assert before == after
