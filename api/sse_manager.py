# File: api/sse_manager.py

import asyncio
import json
from typing import Dict, AsyncGenerator, Set


# Per-connection backlog before a consumer is considered gone and dropped.
_QUEUE_MAX = 1000
# Events retained for a channel with no consumer at all.
_BUFFER_MAX = 500
# Channels retained at all. Nothing subscribes to most of them.
_BUFFER_CHANNEL_MAX = 64


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
            # No active consumers — buffer for later replay, but bounded: with
            # nobody watching overnight this grew for the life of the process,
            # and the first visitor then had the whole backlog replayed into
            # their queue in one go. Keep the newest; a replay is a courtesy,
            # not a delivery guarantee.
            # Bounded in key COUNT too. api/meta_routers pushes to a
            # per-project channel that nothing subscribes to, so every project
            # accumulated its own permanent buffer — capping each one at 500
            # only made the leak per-key instead of unbounded.
            if (task_id not in self._buffers
                    and len(self._buffers) >= _BUFFER_CHANNEL_MAX):
                return
            buf = self._buffers.setdefault(task_id, [])
            buf.append(message)
            if len(buf) > _BUFFER_MAX:
                del buf[:-_BUFFER_MAX]
            return

        dead = []
        for q in queues:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            queues.discard(q)
            # Tell the generator it is over. Bounding the queue made this
            # eviction path live for the first time, and dropping the queue
            # alone leaves `event_generator` awaiting a queue nobody will ever
            # feed — while still yielding its 15s `: ping`. The socket stays
            # open, the client's EventSource never fires `onerror`, and
            # web/src/lib/sse.ts only reconnects from onerror: the tab looks
            # connected and silently receives nothing, forever. A cap that
            # converts "slow client" into "permanently dead client with no
            # symptom" is worse than the leak it replaced.
            try:
                q.get_nowait()          # make room; the queue is full by definition
            except asyncio.QueueEmpty:  # pragma: no cover - full implies non-empty
                pass
            try:
                q.put_nowait("__END__")
            except asyncio.QueueFull:   # pragma: no cover
                pass

    async def event_generator(self, task_id: str) -> AsyncGenerator[str, None]:
        """Subscribe to the broadcast channel with a private queue.

        Any messages buffered before the first consumer connects are
        replayed first.
        """
        # Bounded on purpose. This was `asyncio.Queue()` — unbounded — which
        # meant `put_nowait` could never raise, so the slow-consumer eviction in
        # push_log was dead code and a connected-but-not-reading client
        # accumulated events without limit. On a public hostname that is a
        # memory lever anyone can pull by opening a stream and not reading it.
        # The cap is generous: a live client drains continuously, so reaching it
        # means the consumer is gone, which is exactly when we want it dropped.
        queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
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
