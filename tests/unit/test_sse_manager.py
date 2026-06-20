# tests/unit/test_sse_manager.py
# Unit tests for api/sse_manager.py

import asyncio
import json
import pytest
from api.sse_manager import StreamManager


class TestStreamManager:
    @pytest.fixture
    def manager(self):
        return StreamManager()

    @pytest.mark.asyncio
    async def test_push_and_consume(self, manager):
        """Push messages and consume them via event_generator."""
        task_id = "test_task_1"
        await manager.push_log(task_id, "msg1")
        await manager.push_log(task_id, "msg2")
        await manager.push_log(task_id, "__END__")

        events = []
        async for raw in manager.event_generator(task_id):
            events.append(raw)

        assert len(events) == 2
        for event in events:
            assert event.startswith("data: ")
            payload = json.loads(event[6:])
            assert "log" in payload

        # Queue should be cleaned up after __END__
        assert task_id not in manager._queues

    @pytest.mark.asyncio
    async def test_end_sentinel_terminates_stream(self, manager):
        """__END__ should close the generator without producing a data event."""
        task_id = "test_task_2"
        await manager.push_log(task_id, "__END__")

        events = []
        async for raw in manager.event_generator(task_id):
            events.append(raw)

        assert len(events) == 0
        assert task_id not in manager._queues

    @pytest.mark.asyncio
    async def test_queue_cleanup_on_disconnect(self, manager):
        """Queue should be cleaned up when generator is cancelled."""
        task_id = "test_task_3"
        await manager.push_log(task_id, "partial")

        gen = manager.event_generator(task_id)
        # Consume one message
        event = await gen.__anext__()
        assert "partial" in event

        # Simulate client disconnect — close generator
        await gen.aclose()

        # Allow finally block to run
        await asyncio.sleep(0.05)
        assert task_id not in manager._queues

    @pytest.mark.asyncio
    async def test_multiple_tasks_isolated(self, manager):
        """Different task IDs should have independent queues."""
        await manager.push_log("task_a", "a1")
        await manager.push_log("task_b", "b1")
        await manager.push_log("task_a", "a2")
        await manager.push_log("task_b", "__END__")
        await manager.push_log("task_a", "__END__")

        # Consume task_a
        a_events = []
        async for raw in manager.event_generator("task_a"):
            a_events.append(raw)
        assert len(a_events) == 2

        # task_b was already consumed by event_generator above (since __END__ was pushed before we started)
        # So task_b queue was already cleaned. Re-push for a clean test:
        await manager.push_log("task_b2", "b2")
        await manager.push_log("task_b2", "__END__")
        b_events = []
        async for raw in manager.event_generator("task_b2"):
            b_events.append(raw)
        assert len(b_events) == 1

    @pytest.mark.asyncio
    async def test_get_queues_creates_on_demand(self, manager):
        """_get_queues should create a set if it doesn't exist."""
        qs = manager._get_queues("new_task")
        assert "new_task" in manager._queues
        assert qs is manager._queues["new_task"]
        assert isinstance(qs, set)
