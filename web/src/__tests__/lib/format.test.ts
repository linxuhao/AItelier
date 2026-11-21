// Message time (design-message-time §9): a message's time in the VIEWER's zone,
// with the date when it is not the viewer's local today. Every case pins the
// zone: on a UTC host an unpinned test passes with or without the conversion.
import { describe, expect, it } from 'vitest';
import { formatFullTime, formatMessageTime } from '../../lib/format';

const withTZ = (tz: string, fn: () => void) => {
  const prev = process.env.TZ;
  process.env.TZ = tz;
  try { fn(); } finally {
    if (prev === undefined) delete process.env.TZ; else process.env.TZ = prev;
  }
};
const at = (iso: string) => Date.parse(iso);

describe('formatMessageTime', () => {
  it('shows only the time on the local today, the date on another day, and the year in another year', () => {
    withTZ('Europe/Paris', () => {
      const now = at('2026-11-20T15:00:00Z');
      expect(formatMessageTime('2026-11-20T10:14:23.123456+00:00', 'en', now)).toBe('11:14');
      expect(formatMessageTime('2026-11-19T17:49:00+00:00', 'en', now)).toBe('Nov 19, 18:49');
      expect(formatMessageTime('2026-11-19T17:49:00+00:00', 'zh', now)).toBe('11月19日 18:49');
      const lastYear = formatMessageTime('2025-11-19T17:49:00+00:00', 'en', now);
      expect(lastYear).toContain('2025');
      expect(lastYear).toContain('18:49');
    });
    withTZ('Asia/Shanghai', () => {
      expect(formatMessageTime('2026-11-20T10:14:00Z', 'en', at('2026-11-20T12:00:00Z'))).toBe('18:14');
      expect(formatMessageTime('2026-11-19T10:14:00Z', 'en', at('2026-11-20T12:00:00Z'))).toBe('Nov 19, 18:14');
    });
  });

  it('compares the LOCAL day, not the UTC day (both directions)', () => {
    // Paris: 23:30Z on the 19th is 00:30 on the 20th, the same local day as 09:00Z on the 20th.
    withTZ('Europe/Paris', () => {
      expect(formatMessageTime('2026-11-19T23:30:00Z', 'en', at('2026-11-20T09:00:00Z'))).toBe('00:30');
    });
    // Shanghai: both on the 19th in UTC, but 23:30 on the 19th and 01:00 on the 20th locally.
    withTZ('Asia/Shanghai', () => {
      expect(formatMessageTime('2026-11-19T15:30:00Z', 'en', at('2026-11-19T17:00:00Z'))).toBe('Nov 19, 23:30');
    });
  });

  it('keeps one local day across the autumn DST switch', () => {
    withTZ('Europe/Paris', () => {
      const now = at('2026-10-25T12:00:00Z');
      expect(formatMessageTime('2026-10-25T00:30:00Z', 'en', now)).toBe('02:30');
      expect(formatMessageTime('2026-10-25T01:30:00Z', 'en', now)).toBe('02:30');
    });
  });

  it('answers "" for a missing or unparseable time, never "Invalid Date" (E5)', () => {
    for (const bad of [null, undefined, '', 'not-a-time']) {
      expect(formatMessageTime(bad, 'en')).toBe('');
      expect(formatFullTime(bad, 'en')).toBe('');
    }
  });
});

describe('formatFullTime', () => {
  it('gives the local date and time with its zone, then the UTC instant', () => {
    withTZ('Europe/Paris', () => {
      const [local, utc] = formatFullTime('2026-11-20T10:14:23.123456+00:00', 'en').split('\n');
      expect(local).toContain('Friday');
      expect(local).toContain('November 20, 2026');
      expect(local).toContain('11:14:23');
      expect(local).toMatch(/GMT\+1|CET/);
      expect(utc).toBe('2026-11-20T10:14:23.123Z');
    });
  });
});
