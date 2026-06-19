# File: api/sse_manager.py

import asyncio
import json
from typing import Dict, AsyncGenerator, Set


class StreamManager:
    """Broadcast-based SSE event stream.

    Each consumer gets its own asyncio.Queue.  push_log fans out to ALL
    active queues so stale connections can't steal events from new ones.
    """

    def __init__(self):
        self._queues: Dict[str, Set[asyncio.Queue]] = {}

    def _get_queues(self, task_id: str) -> Set[asyncio.Queue]:
        if task_id not in self._queues:
            self._queues[task_id] = set()
        return self._queues[task_id]

    async def push_log(self, task_id: str, message: str):
        """Fan out to every active consumer on this channel."""
        queues = self._queues.get(task_id, set())
        dead = []
        for q in queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            queues.discard(q)

    async def event_generator(self, task_id: str) -> AsyncGenerator[str, None]:
        """Subscribe to the broadcast channel with a private queue."""
        queue: asyncio.Queue = asyncio.Queue()
        queues = self._get_queues(task_id)
        queues.add(queue)
        try:
            while True:
                message = await queue.get()
                if message == "__END__":
                    break
                payload = {"log": message}
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            queues.discard(queue)


# Global singleton
stream_manager = StreamManager()
