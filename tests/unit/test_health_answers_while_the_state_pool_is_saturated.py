"""/health must answer while every slot of the shared anyio worker pool is held.

The probe used to be a plain `def`, so FastAPI serviced it with
``run_in_threadpool`` on anyio's DEFAULT limiter -- the same slots that
``POST /api/state/query/{action}`` and its per-request ``get_service`` dependency
take. One wave of 58 concurrent ``get_attempt`` calls at concurrency 40 filled
every slot and /health queued behind State work: p95 53.1 ms, max 1171.1 ms
while State p50 was 1539.8 ms (live backend, 2026-11-16).

This test holds EVERY slot of that limiter for seconds and requires the probe to
answer anyway. It pins the property rather than sampling a timing: on the old
route the probe cannot answer until a slot is released, so the assertion is a
hard timeout, not a latency budget, and it does not depend on how expensive a
read happens to be on the machine running it.

The two live-source invariants of the payload are pinned next door, in
``test_health_reports_the_gate_export.py`` (the digest follows a rewritten file)
and ``test_health_reports_process_age.py`` (uptime advances, start time does not).
"""
import asyncio
import threading
import time

import anyio
import httpx

from api.main import app


async def test_health_answers_while_every_slot_of_the_shared_pool_is_held():
    limiter = anyio.to_thread.current_default_thread_limiter()
    slots = limiter.total_tokens
    # The default limiter is the shared pool this test exists to saturate; a
    # limit small enough to be a different object would make the test vacuous.
    assert slots >= 2, slots

    held = threading.Event()
    started = []

    def occupy_one_slot():
        started.append(1)    # this slot is ours, and stays ours, until `held`
        held.wait()

    tasks = [asyncio.create_task(anyio.to_thread.run_sync(occupy_one_slot))
             for _ in range(slots)]
    try:
        # Every slot must really be OCCUPIED before the probe is asked, or the
        # request could be answered by a slot nobody took. Yielding to the loop
        # is what lets the tasks above run at all.
        deadline = time.monotonic() + 30
        while len(started) < slots:
            assert time.monotonic() < deadline, f"only {len(started)}/{slots} slots started"
            await asyncio.sleep(0.01)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://test") as client:
            response = await asyncio.wait_for(client.get("/health"), timeout=3.0)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["engine"] == "DPE SOTA v3.0"
        # The payload is still the live one, produced by the probe's own thread:
        # a missing /proc is declared, but with /proc present there must be an
        # answer.
        assert "started_at" in body["process"]
    finally:
        held.set()
        await asyncio.gather(*tasks, return_exceptions=True)
