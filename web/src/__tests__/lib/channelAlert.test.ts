// Step 25 C12 (design 7.9.2): the channel-down alert line of a channel's card. Each row of the
// two tables (a down channel, a connected one) has an example; where several hold, the first
// wins. Times come from the answer's server clock and are shown in the owner's zone.
import { describe, expect, it, vi } from 'vitest';
import { TEST_ALERT_LIMITS, channelAlertLine, channelAlertText, ownerTime, type ChannelClock } from '../../lib/channelAlert';
import { t } from '../../lib/i18n.svelte';
import { langStore } from '../../stores/i18n';
import type { OwnerChannel, OwnerChannelAlertRow } from '../../lib/api';

const MIN = 60_000;
// 12:00 in Paris (CET).
const NOW = Date.parse('2026-11-23T11:00:00Z');
const iso = (ms: number) => new Date(ms).toISOString();
const ago = (minutes: number) => iso(NOW - minutes * MIN);
const CLOCK: ChannelClock = {now: iso(NOW), owner_tz: 'Europe/Paris'};
// The end of the quiet hours a clock built without the answer's rules names (HH:00).
const QUIET_END = String(TEST_ALERT_LIMITS.quiet_until_hour).padStart(2, '0') + ':00';

type Status = NonNullable<OwnerChannel['status']>;
const channel = (name: 'slack' | 'wechat', status: Partial<Status> | null, over: Partial<OwnerChannel> = {}): OwnerChannel => ({
  channel: name, bound: true, sender_ref: name === 'slack' ? 'U1' : 'wx1', bound_at: '2026-11-01T00:00:00Z', revoked_at: null,
  is_default: name === 'slack' ? 1 : 0,
  status: status === null ? null : {connected: true, detail: null, at: ago(0), live: true, down_since: null, up_since: ago(600),
    alert_down: null, alert_up: null, last_told: null, last_down_sent_at: null, alert_due: null, ...status},
  ...over});
const ledger = (over: Partial<OwnerChannelAlertRow>): OwnerChannelAlertRow => ({
  kind: 'down', down_since: ago(30), state: 'sent', attempt: 1, via: 'slack', claimed_at: ago(20), finished_at: ago(20), error: null, ...over});
const slackUp = channel('slack', {});
/** The line of a WeChat card with `status`, Slack connected unless given. */
const line = (status: Partial<Status>, slack: OwnerChannel = slackUp, over: Partial<OwnerChannel> = {}, clock = CLOCK) => {
  const wechat = channel('wechat', status, over);
  return channelAlertLine(wechat, [slack, wechat], clock);
};
const words = (status: Partial<Status>, slack: OwnerChannel = slackUp, clock = CLOCK) =>
  channelAlertText(line(status, slack, {}, clock), clock, t);
const downFor = (minutes: number, extra: Partial<Status> = {}): Partial<Status> =>
  ({connected: false, detail: 'bridge unreachable', down_since: ago(minutes), ...extra});

describe('a down channel', () => {
  it('1: not bound, or not configured: no line', () => {
    expect(line(downFor(30), slackUp, {bound: false})).toEqual({key: 'none'});
    expect(line(downFor(30, {detail: 'not configured'}))).toEqual({key: 'none'});
    expect(line(downFor(30, {detail: 'not configured: slack-app-token / slack-bot-token missing'}))).toEqual({key: 'none'});
  });

  it('2: no real reading yet', () => {
    expect(words(downFor(30, {live: false, detail: 'bridge not reached yet'}))).toBe('Real status not read yet (bridge not reached yet)');
    expect(line({connected: false, detail: 'x', down_since: null}).key).toBe('notLive');
  });

  it('3: alerted - how long, when (owner\'s zone) and through which channel', () => {
    expect(words(downFor(30, {alert_down: ledger({finished_at: ago(20)})}))).toBe('Down 30 min; alerted on Slack at 11:40');
  });

  it('4: told about an earlier outage and not yet that it came back: no new alert', () => {
    expect(words(downFor(30, {last_told: {kind: 'down', down_since: ago(90), at: ago(80)}})))
      .toBe('Down 30 min; told it was down at 10:40 and not yet that it came back, so no new alert');
  });

  it('5: a claim under ten minutes old: alerting', () => {
    expect(words(downFor(30, {alert_down: ledger({state: 'claimed', claimed_at: ago(9), finished_at: null})}))).toBe('Alerting through Slack…');
  });

  it('6: given up - the second attempt failed, or was claimed and never reported', () => {
    expect(words(downFor(60, {alert_down: ledger({attempt: 2, state: 'failed', error: '409 recipient_unknown'})})))
      .toBe('Down alert not sent: 409 recipient_unknown');
    expect(words(downFor(60, {alert_down: ledger({attempt: 2, state: 'claimed', claimed_at: ago(10), finished_at: null})})))
      .toBe('Down alert not sent: the host never reported back');
  });

  it('7: no other bound live connected channel', () => {
    expect(line(downFor(30), channel('slack', {connected: false, down_since: ago(5)}))).toEqual({key: 'noVia'});
    expect(line(downFor(30), channel('slack', {live: false, detail: 'channel plugin stopped'}))).toEqual({key: 'noVia'});
    expect(line(downFor(30), channel('slack', {}, {bound: false}))).toEqual({key: 'noVia'});
  });

  it('8: due and nobody claimed it', () => {
    expect(words(downFor(30, {alert_due: {kind: 'down', attempt: 1}})))
      .toBe('The alert is due; the steward\'s host did not claim it this round');
  });

  it('9: waiting - the latest of ten minutes in, six hours after the last delivered one, 30 minutes after a failed first', () => {
    expect(words(downFor(4))).toBe('Down 4 min; alert at 12:06 if still down');
    expect(words(downFor(30, {last_down_sent_at: ago(300)}))).toBe('Down 30 min; alert at 13:00 if still down');
    expect(words(downFor(30, {alert_down: ledger({state: 'failed', claimed_at: ago(20), error: 'socket closed'})})))
      .toBe('First alert not sent (socket closed); Down 30 min; alert at 12:10 if still down');
    expect(words(downFor(30, {alert_down: ledger({state: 'claimed', claimed_at: ago(15), finished_at: null})})))
      .toBe('First alert not sent (the host never reported back); Down 30 min; alert at 12:15 if still down');
  });

  it('9: a time in the quiet hours is pushed to their end', () => {
    const late: ChannelClock = {now: '2026-11-23T21:55:00Z', owner_tz: 'Europe/Paris'};  // 22:55 local
    const status = {connected: false, detail: 'x', down_since: '2026-11-23T21:50:00Z'};
    expect(channelAlertText(line(status, slackUp, {}, late), late, t)).toBe(`Down 5 min; alert at ${QUIET_END} if still down`);
  });

  it('several hold: the one that holds the alert back wins over one that only moves its time', () => {
    // The six hours and under ten minutes: the six hours.
    expect(words(downFor(3, {last_down_sent_at: ago(60)}))).toBe('Down 3 min; alert at 17:00 if still down');
    // The six hours and the quiet hours: the later, the quiet hours' end.
    const late: ChannelClock = {now: '2026-11-23T20:00:00Z', owner_tz: 'Europe/Paris'};  // 21:00 local
    const status = {connected: false, detail: 'x', down_since: '2026-11-23T19:00:00Z', last_down_sent_at: '2026-11-23T18:00:00Z'};
    expect(channelAlertText(line(status, slackUp, {}, late), late, t)).toBe(`Down 60 min; alert at ${QUIET_END} if still down`);
    // No other channel and the quiet hours: no other channel.
    const quiet: ChannelClock = {now: '2026-11-23T23:00:00Z', owner_tz: 'Europe/Paris'};
    expect(line({connected: false, detail: 'x', down_since: '2026-11-23T21:00:00Z'}, channel('slack', {connected: false, down_since: ago(1)}), {}, quiet))
      .toEqual({key: 'noVia'});
    // Alerted beats everything after it.
    expect(line(downFor(30, {alert_down: ledger({}), alert_due: {kind: 'down', attempt: 1}})).key).toBe('alerted');
  });
});

describe('a connected channel', () => {
  const told = {last_told: {kind: 'down' as const, down_since: ago(90), at: ago(80)}};
  it('1: not bound, or not configured: no line', () => {
    expect(line({...told}, slackUp, {bound: false})).toEqual({key: 'none'});
  });

  it('2: no real reading yet', () => {
    expect(line({live: false, detail: 'connecting'})).toEqual({key: 'notLive', detail: 'connecting'});
  });

  it('3: told it is back, within 24 hours of the reconnect', () => {
    expect(words({up_since: ago(30), alert_up: ledger({kind: 'up', finished_at: ago(25)}), last_told: {kind: 'up', down_since: ago(90), at: ago(25)}}))
      .toBe('Told it is back on Slack at 11:35');
  });

  it('4: the recovery notice given up, within 24 hours', () => {
    expect(words({...told, up_since: ago(60), alert_up: ledger({kind: 'up', attempt: 2, state: 'failed', error: 'socket closed'})}))
      .toBe('Recovery notice not sent: socket closed');
  });

  it('5: nothing owed - and 3 and 4 only for a day', () => {
    expect(line({})).toEqual({key: 'none'});
    expect(line({up_since: ago(25 * 60), alert_up: ledger({kind: 'up'}), last_told: {kind: 'up', down_since: ago(26 * 60), at: ago(25 * 60)}}))
      .toEqual({key: 'none'});
  });

  it('6: a claim under ten minutes old: sending', () => {
    expect(line({...told, up_since: ago(5), alert_up: ledger({kind: 'up', state: 'claimed', claimed_at: ago(2), finished_at: null})}))
      .toEqual({key: 'upSending'});
  });

  it('7: no other channel to carry it', () => {
    expect(words({...told, up_since: ago(5)}, channel('slack', {connected: false, down_since: ago(1)})))
      .toBe('No other channel to send the recovery notice through');
  });

  it('8: due and nobody claimed it', () => {
    expect(line({...told, up_since: ago(5), alert_due: {kind: 'up', attempt: 1}})).toEqual({key: 'upUnclaimed'});
  });

  it('9: waiting - now, or 30 minutes after a failed first; pushed out of the quiet hours', () => {
    expect(words({...told, up_since: ago(5)})).toBe('Recovery notice at 12:00');
    expect(words({...told, up_since: ago(20), alert_up: ledger({kind: 'up', state: 'failed', claimed_at: ago(10), error: '409'})}))
      .toBe('Recovery notice not sent the first time (409); Recovery notice at 12:20');
    const quiet: ChannelClock = {now: '2026-11-23T22:30:00Z', owner_tz: 'Europe/Paris'};  // 23:30 local
    expect(channelAlertText(line({...told, up_since: '2026-11-23T22:00:00Z'}, slackUp, {}, quiet), quiet, t))
      .toBe(`Recovery notice at ${QUIET_END}`);
  });

  it('a connected channel past 24 hours at 23:30 has no line', () => {
    const quiet: ChannelClock = {now: '2026-11-23T22:30:00Z', owner_tz: 'Europe/Paris'};
    const status = {up_since: '2026-11-22T20:00:00Z', alert_up: ledger({kind: 'up', finished_at: '2026-11-22T20:05:00Z'}),
      last_told: {kind: 'up' as const, down_since: '2026-11-22T19:00:00Z', at: '2026-11-22T20:05:00Z'}};
    expect(line(status, slackUp, {}, quiet)).toEqual({key: 'none'});
  });
});

describe('the backend\'s rules', () => {
  // The answer's alert_limits (core/state_channels.py alert_limits) set every time on the card.
  const rules = (over: Partial<NonNullable<ChannelClock['limits']>>): ChannelClock =>
    ({...CLOCK, limits: {...TEST_ALERT_LIMITS, ...over}});

  it('the answer\'s values move the times: ten minutes in, the retry, the six hours, the unreported claim', () => {
    expect(words(downFor(4), slackUp, rules({}))).toBe('Down 4 min; alert at 12:06 if still down');
    expect(words(downFor(4), slackUp, rules({after_minutes: 20}))).toBe('Down 4 min; alert at 12:16 if still down');
    expect(words(downFor(30, {alert_down: ledger({state: 'failed', claimed_at: ago(20), error: 'x'})}), slackUp,
      rules({retry_after_minutes: 45}))).toBe('First alert not sent (x); Down 30 min; alert at 12:25 if still down');
    expect(words(downFor(30, {last_down_sent_at: ago(300)}), slackUp, rules({down_every_minutes: 420})))
      .toBe('Down 30 min; alert at 14:00 if still down');
    const claimed = downFor(30, {alert_down: ledger({state: 'claimed', claimed_at: ago(15), finished_at: null})});
    expect(line(claimed, slackUp, {}, rules({})).key).toBe('waiting');
    expect(line(claimed, slackUp, {}, rules({unreported_minutes: 20})).key).toBe('alerting');
  });

  it('the answer\'s attempts and quiet hours', () => {
    const second = downFor(30, {alert_down: ledger({state: 'failed', attempt: 2, claimed_at: ago(40), error: 'x'})});
    expect(line(second, slackUp, {}, rules({})).key).toBe('gaveUp');
    expect(line(second, slackUp, {}, rules({attempts: 3})).key).toBe('waiting');
    const evening: ChannelClock = {now: '2026-11-23T20:30:00Z', owner_tz: 'Europe/Paris'};  // 21:30 local
    const status = {connected: false, detail: 'x', down_since: '2026-11-23T20:25:00Z'};
    expect(channelAlertText(line(status, slackUp, {}, evening), evening, t)).toBe('Down 5 min; alert at 21:35 if still down');
    const early = {...evening, limits: {...TEST_ALERT_LIMITS, quiet_from_hour: 21}};
    expect(channelAlertText(line(status, slackUp, {}, early), early, t)).toBe(`Down 5 min; alert at ${QUIET_END} if still down`);
  });

  it('the answer\'s quiet_until_hour is the time the card names, never a copy of it', () => {
    // 22:55 local, down since 22:50: the alert falls at 23:00 local, in the quiet hours.
    const late: ChannelClock = {now: '2026-11-23T21:55:00Z', owner_tz: 'Europe/Paris', limits: {...TEST_ALERT_LIMITS, quiet_until_hour: 7}};
    const status = {connected: false, detail: 'x', down_since: '2026-11-23T21:50:00Z'};
    expect(channelAlertText(line(status, slackUp, {}, late), late, t)).toBe('Down 5 min; alert at 07:00 if still down');
    const quiet: ChannelClock = {now: '2026-11-23T22:30:00Z', owner_tz: 'Europe/Paris', limits: {...TEST_ALERT_LIMITS, quiet_until_hour: 7}};
    const told = {last_told: {kind: 'down' as const, down_since: '2026-11-23T21:00:00Z', at: '2026-11-23T21:10:00Z'}};
    expect(channelAlertText(line({...told, up_since: '2026-11-23T22:00:00Z'}, slackUp, {}, quiet), quiet, t)).toBe('Recovery notice at 07:00');
  });
});

describe('the clock and the zone', () => {
  it('N and T follow the server\'s now, never the browser\'s clock', () => {
    // The answer says 12:00; a browser five minutes slow changes nothing.
    vi.useFakeTimers();
    try {
      vi.setSystemTime(NOW - 5 * MIN);
      expect(words(downFor(4))).toBe('Down 4 min; alert at 12:06 if still down');
      expect(words(downFor(30, {alert_down: ledger({state: 'claimed', claimed_at: ago(10), finished_at: null})})))
        .toBe('First alert not sent (the host never reported back); Down 30 min; alert at 12:20 if still down');
    } finally { vi.useRealTimers(); }
  });

  it('HH:MM is in the owner\'s zone when the browser\'s differs', () => {
    const shanghai: ChannelClock = {now: iso(NOW), owner_tz: 'Asia/Shanghai'};
    expect(ownerTime(ago(20), 'Asia/Shanghai')).toBe('18:40');
    const browser = new Date(ago(20)).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    const text = channelAlertText(line(downFor(30, {alert_down: ledger({})}), slackUp, {}, shanghai), shanghai, t);
    expect(text).toBe('Down 30 min; alerted on Slack at 18:40');
    if (Intl.DateTimeFormat().resolvedOptions().timeZone !== 'Asia/Shanghai') expect(text.endsWith(browser)).toBe(false);
  });

  it('the words are the owner\'s language', () => {
    langStore.set('zh-CN');
    try {
      expect(words(downFor(30, {alert_down: ledger({finished_at: ago(20)})}))).toBe('断开 30 分钟，11:40 已在 Slack 告警');
      expect(words(downFor(4))).toBe('断开 4 分钟，12:06 仍断就告警');
      // 460 F7: due, and the host did not claim it this round - its via not registered there, a
      // reading not taken, or the host stopped; the card does not tell these apart (7.9.9 (a)).
      expect(words(downFor(30, {alert_due: {kind: 'down', attempt: 1}}))).toBe('该告警了，宿主这一轮没有认领');
      expect(words({last_told: {kind: 'down', down_since: ago(90), at: ago(80)}, up_since: ago(5), alert_due: {kind: 'up', attempt: 1}}))
        .toBe('该发恢复通知了，宿主这一轮没有认领');
    } finally { langStore.set('en'); }
  });
});
