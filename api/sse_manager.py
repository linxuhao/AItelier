# File: api/sse_manager.py

import asyncio
import json
from typing import Dict, AsyncGenerator, Set


class StreamManager:
    """Broadcast-based SSE event stream.

    Each consumer gets its own asyncio.Queue.  push_log fans out to ALL
    active queues so stale connections can't steal events from new ones.
    Messages pushed before any consumer connects are buffered and replayed
    to the first consumer.
    """

    def __init__(self):
        self._queues: Dict[str, Set[asyncio.Queue]] = {}
        self._buffers: Dict[str, list] = {}  # pre-connect buffer per task_id

    def _get_queues(self, task_id: str) -> Set[asyncio.Queue]:
        if task_id not in self._queues:
            self._queues[task_id] = set()
        return self._queues[task_id]

    async def push_log(self, task_id: str, message: str):
        """Fan out to every active consumer on this channel.

        If no consumer is connected yet, buffer the message for the next
        consumer that subscribes via event_generator.
        """
        queues = self._queues.get(task_id, set())
        if not queues:
            # No active consumers — buffer for later replay
            buf = self._buffers.setdefault(task_id, [])
            buf.append(message)
            return

        dead = []
        for q in queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            queues.discard(q)

    async def event_generator(self, task_id: str) -> AsyncGenerator[str, None]:
        """Subscribe to the broadcast channel with a private queue.

        Any messages buffered before the first consumer connects are
        replayed first.
        """
        queue: asyncio.Queue = asyncio.Queue()
        queues = self._get_queues(task_id)
        queues.add(queue)

        # Replay buffered messages first
        buf = self._buffers.pop(task_id, [])
        for msg in buf:
            queue.put_nowait(msg)

        try:
            while True:
                # Heartbeat: if no event arrives within the interval, emit an SSE
                # comment line. Proxies (e.g. a Cloudflare tunnel) close a
                # connection that is idle for ~100s; the comment keeps the socket
                # active so the browser EventSource never sees a spurious drop.
                # Comments carry no "data:" field, so the frontend ignores them.
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if message == "__END__":
                    break
                payload = {"log": message}
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            queues.discard(queue)
            if not queues:  # clean up empty set to avoid leaking keys
                self._queues.pop(task_id, None)


# Global singleton
stream_manager = StreamManager()
