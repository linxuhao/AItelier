"""The live-connection table: counted from the SSE registry, identity-split.

The count is a projection of the queue set the generator already maintains —
there is no second bookkeeping to drift. What these tests pin:

- a connection registers who/since/channel and unregisters on teardown
- the presence broadcast carries COUNTS ONLY (it lands in anonymous browsers;
  an email in it would be the reflog leak all over again, push-delivered)
- /api/connections gives everyone the counts and only a writer the detail
"""
import asyncio
import json

import pytest

from api.sse_manager import StreamManager


async def gen_next(gen):
    return await gen.__anext__()


async def _drain_one(gen):
    """Advance the generator until its first yield, so it registers."""
    return await gen.__anext__()


@pytest.mark.asyncio
async def test_connect_registers_and_disconnect_unregisters():
    m = StreamManager()
    gen = m.event_generator("__global__", who="op@example.com")
    # First yield needs an event in the queue or it blocks on the 15s ping;
    # push after registering — registration happens before the first await.
    task = asyncio.ensure_future(_drain_one(gen))
    await asyncio.sleep(0)          # let the generator run to its first await
    snap = m.connection_snapshot()
    assert [c["who"] for c in snap] == ["op@example.com"]
    assert snap[0]["channel"] == "__global__"
    await m.push_log("__global__", "x")
    await task
    await gen.aclose()
    assert m.connection_snapshot() == []


@pytest.mark.asyncio
async def test_presence_broadcast_counts_only_no_emails():
    m = StreamManager()
    g1 = m.event_generator("__global__", who="op@example.com")
    t1 = asyncio.ensure_future(_drain_one(g1))
    await asyncio.sleep(0)
    g2 = m.event_generator("__global__", who=None)
    t2 = asyncio.ensure_future(_drain_one(g2))
    await asyncio.sleep(0)

    # g1's queue now holds two presence events: its own connect broadcast
    # (total 1) and g2's (total 2). The second is the one under test.
    first = await asyncio.wait_for(t1, 1)
    second = await asyncio.wait_for(gen_next(g1), 1)
    ev = json.loads(json.loads(second.removeprefix("data: ").strip())["log"])
    assert ev == {"type": "presence", "total": 2,
                  "authenticated": 1, "anonymous": 1}
    assert "op@example.com" not in first + second
    t2.cancel()
    await g1.aclose(); await g2.aclose()


def test_endpoint_hides_viewers_from_non_writers(monkeypatch, client):
    from api import main as main_mod
    from api import authz

    m = main_mod.stream_manager
    m._conn_meta[1] = {"who": "op@example.com", "since": 0.0,
                       "channel": "__global__"}
    try:
        monkeypatch.setattr(authz, "request_can_write", lambda r: False)
        body = client.get("/api/connections").json()
        assert body["total"] == 1 and body["authenticated"] == 1
        assert "viewers" not in body
        assert "op@example.com" not in json.dumps(body)

        monkeypatch.setattr(authz, "request_can_write", lambda r: True)
        body = client.get("/api/connections").json()
        assert body["viewers"][0]["who"] == "op@example.com"
    finally:
        m._conn_meta.pop(1, None)
