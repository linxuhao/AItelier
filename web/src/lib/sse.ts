/**
 * AItelier SSE Manager — EventSource lifecycle manager and event dispatcher.
 *
 * Ported from web/js/sse.js (IIFE → TypeScript ES module).
 * Integrates with:
 *   - connectionStore    (connection state signalling)
 *   - notificationStore  (event log for the notification panel)
 */

import { setConnected, setDisconnected } from '../stores/connection';
import { addNotification } from '../stores/notifications';

type EventHandler = (data: Record<string, unknown>) => void;

// ── Private state ──────────────────────────────────────────────────

/** Active EventSource instance. */
let _eventSource: EventSource | null = null;

/** Map of event type → set of handler callbacks. */
const _handlers = new Map<string, Set<EventHandler>>();

/** Monotonic sequence number for out-of-order detection. */
let _lastTs = 0;

/** True while a full-state refresh is in-flight. */
let _refreshing = false;

/** Reconnect timer ID for exponential backoff. */
let _reconnectTimer: ReturnType<typeof setTimeout> | null = null;

/** Current reconnect delay (starts at 1s, doubles up to 30s). */
let _reconnectDelay = 1000;

/** Maximum reconnect delay. */
const _MAX_RECONNECT_DELAY = 30000;


// ── Event parsing ─────────────────────────────────────────────────

/**
 * Parse the two-layer SSE event format.
 *
 * Wire format from the backend:
 *   data: {"log": "<json-encoded-event-string>"}\n\n
 *
 * The inner JSON string is an object with at minimum {type, _ts, ...}.
 */
function _parseEvent(rawData: string): Record<string, unknown> | null {
  if (!rawData || typeof rawData !== 'string') {
    return null;
  }

  let outer: Record<string, unknown>;
  try {
    outer = JSON.parse(rawData);
  } catch {
    return null;
  }

  const logStr = outer?.log;
  if (!logStr || typeof logStr !== 'string') {
    return null;
  }

  let eventObj: Record<string, unknown>;
  try {
    eventObj = JSON.parse(logStr);
  } catch {
    return null;
  }

  return eventObj && typeof eventObj === 'object' ? eventObj : null;
}


// ── Out-of-order detection ────────────────────────────────────────

/**
 * Check whether an event should be processed based on its _ts field.
 * Events with _ts <= _lastTs are considered stale.
 */
function _isFresh(event: Record<string, unknown>): boolean {
  const ts = event._ts;
  if (typeof ts !== 'number') {
    // No _ts field — always process (legacy events)
    return true;
  }
  if (ts <= _lastTs) {
    return false;
  }
  _lastTs = ts;
  return true;
}


// ── Handler management ─────────────────────────────────────────────

/**
 * Register a handler for a specific event type.
 */
export function on(eventType: string, handler: EventHandler): void {
  if (!eventType || typeof handler !== 'function') {
    return;
  }
  if (!_handlers.has(eventType)) {
    _handlers.set(eventType, new Set());
  }
  _handlers.get(eventType)!.add(handler);
}

/**
 * Remove a previously registered handler.
 */
export function off(eventType: string, handler: EventHandler): void {
  if (!eventType || typeof handler !== 'function') {
    return;
  }
  const set = _handlers.get(eventType);
  if (!set) {
    return;
  }
  set.delete(handler);
  if (set.size === 0) {
    _handlers.delete(eventType);
  }
}

/**
 * Dispatch a parsed event object to all registered handlers for its type.
 * Also pushes the event to the notification store.
 */
function _dispatch(event: Record<string, unknown>): void {
  if (!event || !event.type) {
    return;
  }

  const etype = event.type as string;
  const set = _handlers.get(etype);
  if (set) {
    // Iterate over a copy so handler removal during iteration is safe
    const copy = [...set];
    for (const handler of copy) {
      try {
        handler(event);
      } catch {
        // A bad handler must not break other handlers or the SSE stream
      }
    }
  }

  // Push to notification store for the notification panel
  try {
    addNotification({
      id: String(event._ts ?? Date.now()),
      type: etype,
      message: String(event.message ?? event.type ?? ''),
      timestamp: typeof event._ts === 'number' ? event._ts : Date.now(),
      data: event,
    });
  } catch {
    // Silently ignore notification store errors
  }
}


// ── Connection lifecycle ───────────────────────────────────────────

/**
 * Handle a successful SSE connection.
 */
function _onConnected(): void {
  _lastTs = 0;
  _reconnectDelay = 1000; // Reset backoff on successful connection
  setConnected();

  // Trigger a one-time full state refresh via synthetic event
  if (!_refreshing) {
    _refreshing = true;
    _dispatch({ type: 'sse_connected', _ts: Date.now() });
    setTimeout(() => {
      _refreshing = false;
    }, 5000);
  }
}

/**
 * Handle an SSE connection error / disconnect.
 */
function _onDisconnected(): void {
  setDisconnected();
}

/**
 * Schedule a reconnection attempt with exponential backoff.
 */
function _scheduleReconnect(): void {
  if (_reconnectTimer !== null) {
    clearTimeout(_reconnectTimer);
  }
  _reconnectTimer = setTimeout(() => {
    _reconnectTimer = null;
    connect();
  }, _reconnectDelay);
  _reconnectDelay = Math.min(_reconnectDelay * 2, _MAX_RECONNECT_DELAY);
}


// ── Public API ─────────────────────────────────────────────────────

/**
 * Establish an EventSource connection to the global pipeline SSE stream.
 * If already connected, the previous connection is closed first.
 */
export function connect(): void {
  // Close any existing connection
  if (_eventSource !== null) {
    _eventSource.close();
    _eventSource = null;
  }

  const url = '/api/events/stream';
  const es = new EventSource(url);
  _eventSource = es;

  // ── onopen ──
  es.onopen = () => {
    _onConnected();
  };

  // ── onerror ──
  es.onerror = () => {
    _onDisconnected();
    // If the EventSource is not already closed (readyState is CONNECTING)
    // and we have no active source, schedule a reconnect. Spec numeric
    // literals, not EventSource.CLOSED statics — the constructor may be a
    // test double without the static constants (2 === undefined → the
    // reconnect path silently never ran under test).
    const CONNECTING = 0;
    const CLOSED = 2;
    if (es.readyState === CLOSED || es.readyState === CONNECTING) {
      _eventSource = null;
      _scheduleReconnect();
    }
  };

  // ── onmessage ──
  es.onmessage = (event: MessageEvent) => {
    if (!event || !event.data) {
      return;
    }

    const parsed = _parseEvent(event.data);
    if (parsed === null) {
      return;
    }

    // Out-of-order detection
    if (!_isFresh(parsed)) {
      return;
    }

    _dispatch(parsed);
  };
}

/**
 * Close the active EventSource connection and clean up.
 * Registered handlers are preserved for the next connect() call.
 */
export function disconnect(): void {
  if (_reconnectTimer !== null) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
  if (_eventSource !== null) {
    _eventSource.close();
    _eventSource = null;
  }
  _lastTs = 0;
  _reconnectDelay = 1000;
}
