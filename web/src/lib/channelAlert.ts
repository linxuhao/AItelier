/**
 * The channel-down alert line of a channel's card (plugin step 25, design 7.9.2).
 *
 * A pure function of the owner's channel list as the backend answers it: each channel's
 * facts (connected, live, down_since, up_since), the alert ledger's projections (alert_down,
 * alert_up, last_told, last_down_sent_at) and what the backend's own due function says
 * (alert_due), plus the answer's server clock (`now`) and the owner's zone (`owner_tz`). The
 * first row of the table that holds is the line; a row that holds an alert back comes before
 * one that only moves its time, so the time a card promises is the time the backend's due
 * function will name. Times are the server's; HH:MM is shown in the owner's zone.
 */
import type {OwnerChannel, OwnerChannelAlertRow, OwnerChannelName} from './api';

const MIN = 60_000;
// The backend's rules (core/state_channels.py: ALERT_AFTER, ALERT_RETRY_AFTER,
// ALERT_UNREPORTED, ALERT_ATTEMPTS, ALERT_DOWN_EVERY, QUIET_FROM / QUIET_UNTIL).
const ALERT_AFTER = 10 * MIN, RETRY_AFTER = 30 * MIN, UNREPORTED = 10 * MIN, ATTEMPTS = 2, DOWN_EVERY = 6 * 60 * MIN;
const QUIET_FROM = 23, QUIET_UNTIL = 8;
// How long a recovery notice's outcome stays on a connected channel's card.
const UP_SHOWN = 24 * 60 * MIN;

/** `first`: the first attempt's error when it failed (null: the host never reported it),
 *  absent when there was no failed first attempt. `at` is an ISO time; `quiet` means it falls
 *  in the quiet hours, so the line names their end (08:00). */
export type ChannelAlertLine =
  | {key: 'none'}
  | {key: 'notLive'; detail: string}
  | {key: 'alerted'; minutes: number; at: string; via: OwnerChannelName}
  | {key: 'toldEarlier'; minutes: number; at: string}
  | {key: 'alerting'; via: OwnerChannelName}
  | {key: 'gaveUp'; error: string | null}
  | {key: 'noVia'}
  | {key: 'unclaimed'}
  | {key: 'waiting'; minutes: number; at: string; quiet: boolean; first?: string | null}
  | {key: 'upSent'; at: string; via: OwnerChannelName}
  | {key: 'upGaveUp'; error: string | null}
  | {key: 'upSending'}
  | {key: 'upNoVia'}
  | {key: 'upUnclaimed'}
  | {key: 'upWaiting'; at: string; quiet: boolean; first?: string | null};

export interface ChannelClock {now: string; owner_tz: string}

const at = (iso: string) => Date.parse(iso);
const lapsed = (row: OwnerChannelAlertRow, now: number) =>
  row.state === 'failed' || (row.state === 'claimed' && now >= at(row.claimed_at) + UNREPORTED);
const gaveUp = (row: OwnerChannelAlertRow | null | undefined, now: number) => !!row && row.attempt >= ATTEMPTS && lapsed(row, now);
const firstMissed = (row: OwnerChannelAlertRow | null | undefined, now: number) => !!row && row.attempt < ATTEMPTS && lapsed(row, now);
const claiming = (row: OwnerChannelAlertRow | null | undefined, now: number) =>
  !!row && row.state === 'claimed' && now < at(row.claimed_at) + UNREPORTED;

function hourIn(ms: number, zone: string): number {
  return Number(new Intl.DateTimeFormat('en-GB', {timeZone: zone, hour: '2-digit', hourCycle: 'h23'}).format(new Date(ms)));
}

/** The later of `times` and `now`, and whether it falls in the owner's quiet hours. */
function when(times: number[], now: number, zone: string): {at: string; quiet: boolean} {
  const moment = Math.max(now, ...times), hour = hourIn(moment, zone);
  return {at: new Date(moment).toISOString(), quiet: hour >= QUIET_FROM || hour < QUIET_UNTIL};
}

export function channelAlertLine(row: OwnerChannel, rows: OwnerChannel[], clock: ChannelClock): ChannelAlertLine {
  const status = row.status;
  if (!status) return {key: 'none'};
  const now = at(clock.now), zone = clock.owner_tz;
  const minutes = status.down_since ? Math.max(0, Math.floor((now - at(status.down_since)) / MIN)) : 0;
  const usable = row.bound && !(status.detail ?? '').startsWith('not configured');
  const via = rows.some(o => o.channel !== row.channel && o.bound && o.status?.live !== false && o.status?.connected);
  const down = status.alert_down, up = status.alert_up, told = status.last_told;
  if (!status.connected) {
    if (!usable) return {key: 'none'};
    if (status.live === false || !status.down_since) return {key: 'notLive', detail: status.detail ?? ''};
    if (down?.state === 'sent') return {key: 'alerted', minutes, at: down.finished_at!, via: down.via};
    if (told?.kind === 'down' && told.down_since !== status.down_since) return {key: 'toldEarlier', minutes, at: told.at};
    if (claiming(down, now)) return {key: 'alerting', via: down!.via};
    if (gaveUp(down, now)) return {key: 'gaveUp', error: down!.error};
    if (!via) return {key: 'noVia'};
    if (status.alert_due) return {key: 'unclaimed'};
    const times = [at(status.down_since) + ALERT_AFTER];
    if (status.last_down_sent_at) times.push(at(status.last_down_sent_at) + DOWN_EVERY);
    const missed = firstMissed(down, now);
    if (missed) times.push(at(down!.claimed_at) + RETRY_AFTER);
    return {key: 'waiting', minutes, ...when(times, now, zone), ...(missed ? {first: down!.error} : {})};
  }
  if (!usable) return {key: 'none'};
  if (status.live === false) return {key: 'notLive', detail: status.detail ?? ''};
  const recent = !!status.up_since && now - at(status.up_since) < UP_SHOWN;
  if (up?.state === 'sent' && recent) return {key: 'upSent', at: up.finished_at!, via: up.via};
  if (gaveUp(up, now) && recent) return {key: 'upGaveUp', error: up!.error};
  if (told?.kind !== 'down') return {key: 'none'};
  if (claiming(up, now)) return {key: 'upSending'};
  if (!via) return {key: 'upNoVia'};
  if (status.alert_due) return {key: 'upUnclaimed'};
  const missed = firstMissed(up, now);
  return {key: 'upWaiting', ...when(missed ? [at(up!.claimed_at) + RETRY_AFTER] : [], now, zone),
    ...(missed ? {first: up!.error} : {})};
}

/** HH:MM of `iso` in the owner's zone (never the browser's). */
export function ownerTime(iso: string, zone: string): string {
  return new Intl.DateTimeFormat('en-GB', {timeZone: zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(new Date(iso));
}

/** The line in words, or '' for none: `tr` is the page's t(). */
export function channelAlertText(line: ChannelAlertLine, clock: ChannelClock, tr: (key: string) => string): string {
  if (line.key === 'none') return '';
  const reason = (error: string | null | undefined) => error ?? tr('steward.chanAlert.noReport');
  const fill = (key: string, values: Record<string, string | number>) =>
    Object.entries(values).reduce((text, [name, value]) => text.split(`{${name}}`).join(String(value)), tr('steward.chanAlert.' + key));
  const time = 'at' in line ? ('quiet' in line && line.quiet ? '08:00' : ownerTime(line.at, clock.owner_tz)) : '';
  const values: Record<string, string | number> = {
    minutes: 'minutes' in line ? line.minutes : '', time,
    via: 'via' in line ? tr('steward.chan.' + line.via) : '',
    detail: 'detail' in line ? line.detail : '', reason: 'error' in line ? reason(line.error) : ''};
  const text = fill(line.key, values);
  if (!('first' in line)) return text;
  return fill(line.key === 'waiting' ? 'firstFailed' : 'upFirstFailed', {reason: reason(line.first)}) + text;
}
