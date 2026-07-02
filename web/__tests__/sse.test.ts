/**
 * Tests for web/src/lib/sse.ts — SSE EventSource manager.
 *
 * Mocks the global EventSource API to verify connection lifecycle,
 * event parsing, out-of-order detection, handler management, and
 * reconnection backoff.
 *
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { connect, disconnect, on, off } from '../src/lib/sse';

// ── Mock EventSource ────────────────────────────────────────────────

interface MockEventSourceInstance {
  close: ReturnType<typeof vi.fn>;
  onopen: (() => void) | null;
  onerror: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  readyState: number;
}

let MockEventSourceCtor: ReturnType<typeof vi.fn>;
let activeInstance: MockEventSourceInstance | null;

beforeEach(() => {
  activeInstance = null;
  MockEventSourceCtor = vi.fn().mockImplementation(function (this: MockEventSourceInstance, _url: string) {
    const instance: MockEventSourceInstance = {
      close: vi.fn(),
      onopen: null,
      onerror: null,
      onmessage: null,
      readyState: 0, // CONNECTING
    };
    activeInstance = instance;
    return instance;
  });
  vi.stubGlobal('EventSource', MockEventSourceCtor);
});

afterEach(() => {
  vi.unstubAllGlobals();
  // Disconnect any active SSE connection
  disconnect();
  vi.restoreAllMocks();
});

// ── Helper ──────────────────────────────────────────────────────────

function simulateMessage(rawData: string): void {
  if (activeInstance?.onmessage) {
    activeInstance.onmessage({ data: rawData } as MessageEvent);
  }
}

function simulateOpen(): void {
  if (activeInstance?.onopen) {
    activeInstance.onopen();
  }
}

function simulateError(): void {
  if (activeInstance?.onerror) {
    // Set readyState to CLOSED to trigger reconnect path
    activeInstance.readyState = 2; // CLOSED
    activeInstance.onerror({} as Event);
  }
}

// ── Connect ─────────────────────────────────────────────────────────

describe('connect', () => {
  it('creates EventSource with /api/events/stream', () => {
    connect();
    expect(MockEventSourceCtor).toHaveBeenCalledWith('/api/events/stream');
  });

  it('sets onopen handler and triggers connected state', () => {
    connect();
    expect(activeInstance).not.toBeNull();
    expect(typeof activeInstance!.onopen).toBe('function');
  });

  it('closes previous connection if already connected', () => {
    connect();
    const firstClose = activeInstance!.close;
    connect();
    expect(firstClose).toHaveBeenCalledTimes(1);
  });
});

// ── onmessage / event parsing ──────────────────────────────────────

describe('event parsing and dispatch', () => {
  it('dispatches parsed events to registered handlers', () => {
    const handler = vi.fn();
    on('test_event', handler);
    connect();

    simulateMessage(
      JSON.stringify({ log: JSON.stringify({ type: 'test_event', _ts: 100, message: 'hello' }) }),
    );

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'test_event', _ts: 100, message: 'hello' }),
    );
  });

  it('ignores malformed outer JSON', () => {
    const handler = vi.fn();
    on('test_event', handler);
    connect();

    simulateMessage('not-json');

    expect(handler).not.toHaveBeenCalled();
  });

  it('ignores missing log field', () => {
    const handler = vi.fn();
    on('test_event', handler);
    connect();

    simulateMessage(JSON.stringify({ no_log: true }));

    expect(handler).not.toHaveBeenCalled();
  });

  it('ignores malformed inner JSON', () => {
    const handler = vi.fn();
    on('test_event', handler);
    connect();

    simulateMessage(JSON.stringify({ log: 'not-json-either' }));

    expect(handler).not.toHaveBeenCalled();
  });
});

// ── _ts dedup ───────────────────────────────────────────────────────

describe('_ts out-of-order dedup', () => {
  beforeEach(() => {
    disconnect(); // reset _lastTs = 0 before each dedup test
  });

  it('processes events with increasing _ts', () => {
    const handler = vi.fn();
    on('test_event', handler);
    connect();

    simulateMessage(
      JSON.stringify({ log: JSON.stringify({ type: 'test_event', _ts: 100 }) }),
    );
    simulateMessage(
      JSON.stringify({ log: JSON.stringify({ type: 'test_event', _ts: 101 }) }),
    );

    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('discards events with _ts <= last seen', () => {
    const handler = vi.fn();
    on('test_event', handler);
    connect();

    simulateMessage(
      JSON.stringify({ log: JSON.stringify({ type: 'test_event', _ts: 100 }) }),
    );
    // Stale event (same _ts)
    simulateMessage(
      JSON.stringify({ log: JSON.stringify({ type: 'test_event', _ts: 100 }) }),
    );
    // Older event
    simulateMessage(
      JSON.stringify({ log: JSON.stringify({ type: 'test_event', _ts: 99 }) }),
    );

    // Only the first event should be dispatched
    expect(handler).toHaveBeenCalledTimes(1);
  });
});

// ── Handler management ──────────────────────────────────────────────

describe('on / off handler management', () => {
  it('can register and remove handlers', () => {
    const handler = vi.fn();
    on('test_event', handler);
    off('test_event', handler);

    connect();
    simulateMessage(
      JSON.stringify({ log: JSON.stringify({ type: 'test_event', _ts: 1 }) }),
    );

    expect(handler).not.toHaveBeenCalled();
  });

  it('supports multiple handlers for the same event type', () => {
    const handler1 = vi.fn();
    const handler2 = vi.fn();
    on('test_event', handler1);
    on('test_event', handler2);
    connect();

    simulateMessage(
      JSON.stringify({ log: JSON.stringify({ type: 'test_event', _ts: 1 }) }),
    );

    expect(handler1).toHaveBeenCalledTimes(1);
    expect(handler2).toHaveBeenCalledTimes(1);
  });
});

// ── Disconnect ──────────────────────────────────────────────────────

describe('disconnect', () => {
  it('closes the EventSource connection', () => {
    connect();
    disconnect();
    expect(activeInstance!.close).toHaveBeenCalled();
  });

  it('does not throw if not connected', () => {
    expect(() => disconnect()).not.toThrow();
  });
});

// ── Reconnect backoff ──────────────────────────────────────────────

describe('reconnect backoff', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('schedules a reconnect on error', () => {
    connect();
    simulateError();

    // Should schedule reconnect (1s default delay)
    expect(vi.getTimerCount()).toBeGreaterThanOrEqual(1);
  });

  it('creates a new EventSource after reconnect delay', () => {
    connect();
    simulateError();

    expect(MockEventSourceCtor).toHaveBeenCalledTimes(1);

    // Advance past the 1s reconnect delay
    vi.advanceTimersByTime(1100);

    // Should have created a new EventSource
    expect(MockEventSourceCtor).toHaveBeenCalledTimes(2);
  });
});
