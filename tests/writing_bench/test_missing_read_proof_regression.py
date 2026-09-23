"""This same test file is run against the unmodified baseline for the RED proof."""
import pytest
from test_bench import bench, request, verdict, RULES, CONTRACTS
from aitelier.writing_bench.storage import BenchError

@pytest.mark.parametrize('extra', [{}, {'reviewed_chapters':[{'chapter':31,'title':'错误的前情','prose_sha256':'0'*64}]}])
def test_self_claimed_complete_without_current_target_or_host_reading_must_fail(bench, extra):
    bench.freeze(request(bench), 'read-regression', RULES, CONTRACTS)
    key=bench.input('read-regression')[1]['literary_key']
    with pytest.raises(BenchError):
        bench.literary('read-regression', verdict(key, **extra))
