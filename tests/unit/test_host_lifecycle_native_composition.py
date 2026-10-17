"""Controller authority does not waive the genuine native served-unit gate."""
import pytest
from skillflow import SkillFlowError
from core import coding_workspace as cw
from core import pipeline_frozen_bundle as fb
import test_pipeline_frozen_bundle_resume as resume


@pytest.mark.parametrize('mode', ['rollback', 'drift'])
def test_trusted_controller_still_refuses_every_unserved_lifecycle(tmp_path, monkeypatch, mode):
    sf, registry, gen, rid, _, _, _ = resume.setup(tmp_path, monkeypatch, mode)
    block = fb.launch_block('gen_toy', gen, sf=sf)
    assert block
    before = resume.snapshot(sf, gen, rid)
    pin = sf.get_run(rid)['graph_version']
    calls = [lambda: sf.create_run('gen_toy', project_id='owned-native-control'),
             lambda: sf.approve_checkpoint(rid, expected_checkpoint={}, decision_id='owned'),
             lambda: sf.reject_checkpoint(rid, 'make', 'owned native refusal'),
             lambda: sf.resume_run(rid), lambda: sf.reactivate_run(rid)]
    # Existing controller context is deliberately present. No guard is replaced.
    with cw._trusted('create'):
        for call in calls:
            with pytest.raises(SkillFlowError) as refused:
                call()
            assert block in str(refused.value)
            assert resume.snapshot(sf, gen, rid) == before
            assert sf.get_run(rid)['graph_version'] == pin
    sf._conn.close()
