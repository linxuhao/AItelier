/** Starts the bridge against real WeChat: Wechaty with puppet-neo, the token from
 *  ~/.aitelier-secrets/wechat-bridge-token, port WECHAT_BRIDGE_PORT (default 18731).
 *
 *  A finished login (logged out from the phone, WeChat ended the session) or a QR
 *  nobody scanned in time ends the process: puppet-neo needs a fresh start for a
 *  new QR, and the systemd unit decides whether and how often to start again
 *  (aitelier-wechat-bridge.service bounds that, so an unattended bridge does not
 *  ask WeChat for a QR forever). */
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { WechatyBuilder, types } from 'wechaty';
import { PuppetNeo } from 'wechaty-puppet-neo';
import { createBridge, DEFAULT_PORT } from './bridge.mjs';

const tokenFile = join(homedir(), '.aitelier-secrets', 'wechat-bridge-token');
const port = Number(process.env.WECHAT_BRIDGE_PORT || DEFAULT_PORT);
const log = line => console.error(`wechat-bridge: ${line}`);

let token;
try { token = readFileSync(tokenFile, 'utf8'); } catch { token = ''; }
if (!token.trim()) { log(`no token in ${tokenFile}; not starting`); process.exit(78); }

const bot = WechatyBuilder.build({ name: 'aitelier-wechat-bridge', puppet: new PuppetNeo() });
const bridge = createBridge({ bot, types, token, log });

let stopping = false;
async function stop(reason, code) {
  if (stopping) return;
  stopping = true;
  log(reason);
  await bridge.close().catch(() => {});
  await bot.stop().catch(() => {});
  process.exit(code);
}

bot.on('scan', (_qrcode, status) => {
  if (status === types.ScanStatus.Waiting) log('login QR waiting (GET /status carries it)');
  if (status === types.ScanStatus.Timeout || status === types.ScanStatus.Cancel) void stop('login QR ended without a login', 75);
});
bot.on('login', () => log('logged in'));
bot.on('logout', () => void stop('logged out', 75));
bot.on('error', error => log('error: ' + String(error?.message ?? error).slice(0, 200)));
process.once('SIGINT', () => void stop('SIGINT', 0));
process.once('SIGTERM', () => void stop('SIGTERM', 0));

const address = await bridge.listen(port);
log(`listening on ${address.address}:${address.port}`);
await bot.start();
