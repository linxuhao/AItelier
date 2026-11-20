/** The owner's requests for a new WeChat login QR from the AItelier dashboard (step 19,
 *  design-steward 7.2 "按需二维码"): the backend records each as a row and raises
 *  `owner_channel_login_requested`; this code - not the model - asks the channel for a
 *  login (the director's ownerChannelLogin: plugin -> bridge POST /login) and writes the
 *  answer back. The QR never passes here: it reaches the dashboard through the channel's
 *  status report alone. The backend lists only requests younger than 2 minutes, so a host
 *  that comes back later never asks WeChat for a QR on an old request. */

/** The State events these requests raise: handled here, never a model turn. */
export const CHANNEL_LOGIN_EVENTS = new Set(['owner_channel_login_requested']);

/** Run every pending request, oldest first, and write each answer back. An answer that
 *  could not be written is logged; the row stays pending until it expires (asking again
 *  would only meet the bridge's own refusals). */
export async function processChannelLoginRequests({ request, login, signal, log = () => {} }) {
  const listed = await request('/api/state/query/list_owner_channel_login_requests', {}, signal);
  const pending = (listed.result ?? listed).requests ?? [];
  let done = 0;
  for (const row of pending) {
    let answer;
    try { answer = await login(row.channel); }
    catch (error) { answer = { refused: 'unavailable' }; log(`channel login ${row.request_id}: ${String(error?.message ?? error).slice(0, 200)}`); }
    const result = answer?.requested === true ? { requested: true }
      : { refused: /^[a-z_]{1,40}$/.test(String(answer?.refused)) ? answer.refused : 'refused',
        ...(Number.isInteger(answer?.retry_after_s) && answer.retry_after_s > 0 && answer.retry_after_s <= 86400
          ? { retry_after_s: answer.retry_after_s } : {}) };
    try {
      await request('/api/state/commands/complete_owner_channel_login_request',
        { request_key: `channel-login:${row.request_id}`, request_id: row.request_id, result }, signal);
      done++;
    } catch (error) {
      log(`channel login ${row.request_id}: result not written (${String(error?.message ?? error).slice(0, 200)})`);
    }
  }
  return { pending: pending.length, done };
}
