# File: api/sse_manager.py

import asyncio
import json
import logging
from typing import Dict, AsyncGenerator, Set


# Per-connection backlog before a consumer is considered gone and dropped.
_log = logging.getLogger("aitelier.sse")

_QUEUE_MAX = 1000
# Events retained for a channel with no consumer at all.
_BUFFER_MAX = 500
# Channels retained at all. Nothing subscribes to most of them.
_BUFFER_CHANNEL_MAX = 64

# Concurrent SSE connections, across all channels.
#
# There was no cap at all. Each connection is an asyncio task, a queue and a
# socket, held open for as long as the client likes, on a public hostname where
# the clients are strangers — and it is the cheapest thing to open in bulk,
# because the server does the holding. The per-connection queue is bounded and
# the replay buffer is bounded; the NUMBER of them was not, which is the one
# dimension an anonymous caller picks.
#
# Sized for the real workload: one connection per open tab, and the measured
# event rate is ~0.4/s, so fan-out at this ceiling is a few hundred put_nowait
# per second — nothing. It is a backstop against bulk-opening, not a quota.
_MAX_CONNECTIONS = 512


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

    def _connection_count(self) -> int:
        return sum(len(q) for q in self._queues.values())

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
        if self._connection_count() >= _MAX_CONNECTIONS:
            # Refuse by ENDING the stream rather than by hanging or erroring:
            # the client sees a normal close, EventSource retries with its own
            # backoff, and a transient crowd resolves itself. A comment line
            # first so the response is a well-formed SSE body either way.
            _log.warning("SSE connection refused: %d already open (cap %d)",
                         self._connection_count(), _MAX_CONNECTIONS)
            yield ": at capacity\n\n"
            return

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
