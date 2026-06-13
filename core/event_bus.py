# core/event_bus.py
# In-process pub/sub event bus for pipeline-to-orchestrator communication.
# Thread-safe: pipeline runs in asyncio.to_thread, subscribers on the event loop.

import asyncio
import threading
from collections import defaultdict
from typing import Callable


class EventType:
    """Event type constants for the DPE event bus."""
    # Pipeline lifecycle (existing events, now broadcast)
    PIPELINE_START = "pipeline_start"
    STEP_START = "step_start"
    STEP_END = "step_end"
    STEP_DONE = "step_done"
    STEP_ATTEMPT = "step_attempt"
    PIPELINE_END = "pipeline_end"
    PIPELINE_ERROR = "pipeline_error"

    # Agent events
    AGENT_CALL = "agent_call"
    AGENT_RESPONSE = "agent_response"
    AGENT_MESSAGE = "agent_message"
    FILES_WRITTEN = "files_written"

    # Validation events
    GATE_CHECK = "gate_check"
    GATE_FAIL = "gate_fail"
    BUILD_PASS = "build_pass"
    BUILD_FAIL = "build_fail"
    RED_FAIL = "red_fail"
    RED_SUGGESTIONS = "red_suggestions"

    # Checkpoint events (new)
    CHECKPOINT_REACHED = "checkpoint_reached"
    CHECKPOINT_APPROVED = "checkpoint_approved"
    CHECKPOINT_REJECTED = "checkpoint_rejected"
    CHECKPOINT_TIMEOUT = "checkpoint_timeout"

    # Agent notification (injected into chat via SSE)
    AGENT_NOTIFICATION = "agent_notification"


# Module-level singleton — initialized in api/main.py lifespan
event_bus: "EventBus | None" = None


class EventBus:
    """
    Lightweight in-process pub/sub.
    Thread-safe for cross-thread usage (pipeline in to_thread, subscribers in event loop).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        self._loop = loop
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, handler: Callable):
        """Register handler(event_type, data) for a specific event type."""
        with self._lock:
            self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable):
        """Remove a handler for a specific event type."""
        with self._lock:
            handlers = self._subscribers.get(event_type, [])
            if handler in handlers:
                handlers.remove(handler)

    def publish(self, event_type: str, data: dict):
        """
        Fire-and-forget publish to all subscribers.
        Sync handlers called directly. Async handlers scheduled on the event loop.
        """
        with self._lock:
            handlers = list(self._subscribers.get(event_type, []))

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            handler(event_type, data), self._loop
                        )
                else:
                    handler(event_type, data)
            except Exception:
                # Don't let a bad subscriber crash the pipeline
                pass
