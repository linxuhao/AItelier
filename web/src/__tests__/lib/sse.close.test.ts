/**
 * A reconnect must not leave the old EventSource running.
 *
 * `es.onerror` with `readyState === CONNECTING` means the browser is ALREADY
 * retrying this EventSource on its own. The handler nulled `_eventSource` and
 * scheduled its own reconnect without closing it — so the orphan kept retrying,
 * `connect()`'s `if (_eventSource !== null) close()` guard could no longer see
 * it, and every error while CONNECTING added one more permanent connection from
 * a single tab. `_isFresh` de-duplicated the doubled events client-side, so the
 * symptom was invisible in the UI and the entire cost landed on the server.
 *
 * It matters more now than when it was written: the project page stopped
 * polling and refreshes from this stream, so a leak here multiplies the one
 * connection the new design rests on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const opened: FakeES[] = [];

class FakeES {
  static CONNECTING = 0;
  readyState = 0;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  constructor(public url: string) { opened.push(this); }
  close() { this.closed = true; this.readyState = 2; }
}

describe('sse reconnect', () => {
  beforeEach(() => {
    opened.length = 0;
    vi.stubGlobal('EventSource', FakeES as unknown as typeof EventSource);
    vi.useFakeTimers();
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.resetModules(); });

  it('closes the old stream when the browser is already retrying it', async () => {
    const sse = await import('../../lib/sse');
    sse.connect();
    expect(opened).toHaveLength(1);
    const first = opened[0];

    first.readyState = FakeES.CONNECTING;   // browser auto-retrying
    first.onerror!();

    expect(first.closed).toBe(true);        // ← the fix
  });

  it('does not accumulate live streams across repeated errors', async () => {
    const sse = await import('../../lib/sse');
    sse.connect();
    for (let i = 0; i < 5; i++) {
      const es = opened[opened.length - 1];
      es.readyState = FakeES.CONNECTING;
      es.onerror!();
      await vi.advanceTimersByTimeAsync(60000);  // past any backoff
    }
    const live = opened.filter((e) => !e.closed);
    expect(live.length).toBeLessThanOrEqual(1);
  });
});
