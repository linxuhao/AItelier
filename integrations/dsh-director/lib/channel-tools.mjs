/** The steward's one channel tool (step 19, design-steward 7.2 "按需二维码"): ask the WeChat
 *  bridge for a new login QR when the owner wants one (for instance asked for in Slack).
 *  The QR is a live login credential: it is shown on the dashboard's Channels tab only and
 *  never reaches the model - the tool answers {requested} or {refused, retry_after_s?}
 *  and nothing else. Registered only when the host provides the owner's channels. */

const DESCRIPTION = 'Ask the WeChat bridge for a new login QR code, when your owner wants to connect '
  + '(or reconnect) WeChat. The QR is shown ONLY on the dashboard\'s Channels tab and never comes back '
  + 'to you: never send, describe or promise a QR over chat. Answers {requested: true} (tell your owner '
  + 'to open the dashboard\'s Channels tab and scan it with the WeChat account to connect, within about '
  + '2 minutes) or {refused}: qr_waiting (a QR is already on the dashboard: scan that one), '
  + 'already_logged_in (WeChat is connected), login_starting (one is on its way), resuming (the bridge '
  + 'is resuming the saved login without a QR: wait a minute and check again), rate_limited with '
  + 'retry_after_s (try again after that), not_registered / not_configured / bridge_unreachable / '
  + 'not_supported (WeChat is not set up or its bridge is not running or too old: say so). Call it '
  + 'once per request of your owner; never in a loop.';

/** @param login (channel) => Promise<{requested: true} | {refused: string, retry_after_s?: number}> */
export function registerChannelTools(scope, { defineTool, jsonOutput, login }) {
  scope.tools.register(defineTool({ name: 'request_wechat_qr', description: DESCRIPTION, parameters: {}, output: jsonOutput,
    async execute() {
      const answer = await login('wechat');
      // Only the two documented shapes reach the model, whatever the channel answered.
      if (answer?.requested === true) return { requested: true };
      return { refused: String(answer?.refused ?? 'refused').slice(0, 40),
        ...(Number.isInteger(answer?.retry_after_s) ? { retry_after_s: answer.retry_after_s } : {}) };
    },
  }));
}
