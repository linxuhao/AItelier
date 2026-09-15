"""Force two observer probes to overlap; neither probe is a durable owner."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import resource_ownership as ro


@pytest.mark.parametrize("contenders", [2, 8])
def test_snapshot_probe_cannot_make_every_admission_lose(resource_authority, monkeypatch, contenders):
    authority = resource_authority
    first_probe, second_probe = threading.Event(), threading.Event()
    release_first, release_second = threading.Event(), threading.Event()
    attempted, counter_lock = set(), threading.Lock()
    all_attempted = threading.Event()
    original = ro.Authority._lock
    def lock(self, name, **kwargs):
        try:
            fd = original(self, name, **kwargs)
        except BlockingIOError:
            if name == 'godot.effect.lock' and not release_first.is_set():
                with counter_lock:
                    attempted.add(threading.get_ident())
                    if len(attempted) == contenders - 1:
                        all_attempted.set()
            raise
        if threading.current_thread().name.endswith('_0') and name == 'godot.effect.lock' and not first_probe.is_set():
            first_probe.set()
            assert release_first.wait(4)
        if threading.current_thread().name.endswith('_1') and name == 'godot-service.effect.lock' and not second_probe.is_set():
            second_probe.set()
            assert release_second.wait(4)
        return fd
    monkeypatch.setattr(ro.Authority, '_lock', lock)
    def admit():
        try:
            return authority.acquire('godot')
        except RuntimeError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=contenders, thread_name_prefix='controlled') as pool:
        a = pool.submit(admit)
        assert first_probe.wait(2)
        others = [pool.submit(admit) for _ in range(contenders - 1)]
        try:
            # Old readers can hold distinct probe locks concurrently and each
            # misclassify the other as a ledger-less owner. A serialized reader
            # cannot reach this second probe until the first transaction ends.
            overlap = second_probe.wait(.3)
            if overlap:
                assert all_attempted.wait(2)
            release_first.set()
            if overlap:
                a.result(timeout=2)
            release_second.set()
            results = [a.result(timeout=3), *(item.result(timeout=3) for item in others)]
        finally:
            release_first.set()
            release_second.set()
    leases = [value for value in results if isinstance(value, ro.Lease)]
    try:
        assert len(leases) == 1, [str(value) for value in results]
        assert sum(isinstance(value, RuntimeError) for value in results) == contenders - 1
    finally:
        for lease in leases:
            lease.close(settled=True)
    assert authority.snapshot() == ([], [])
