"""Missing immutable catalog marker must refuse before no-op success."""
import json
from pathlib import Path
from test_pipeline_normal_versions import world, call, snapshot


def test_missing_catalog_marker_refuses_noop_before_effects(world):
    fx, api = world
    first = call(fx, 'edit_template', role='maker', template='active')
    assert first.get('changed'), first
    physical = api.read(first['version_id'])['native']['physical_name']
    (fx.gen / (physical + '.bundle.json')).unlink()
    before = snapshot(fx)
    result = call(fx, 'edit_template', role='maker', template='active')
    after = snapshot(fx)
    Path('/evidence/partial-marker.json').write_text(json.dumps({
        'physical': physical, 'before':json.loads(before), 'result':result,
        'after':json.loads(after), 'unchanged':before == after}, indent=2))
    assert result.get('error') and 'prepared_seals' not in result, result
    assert before == after
