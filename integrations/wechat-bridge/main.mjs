/** Starts the bridge against real WeChat: Wechaty with puppet-neo, the token from
 *  ~/.aitelier-secrets/wechat-bridge-token, port WECHAT_BRIDGE_PORT (default 18731),
 *  address WECHAT_BRIDGE_BIND (bridge.mjs bindHost: loopback, or 0.0.0.0 in the
 *  container). It runs in its own container (Dockerfile, run.sh).
 *
 *  The puppet is not started here: the bridge starts `idle` and asks WeChat for a QR
 *  only when POST /login asks it to (bridge.mjs). A QR nobody scanned in time, a logout
 *  or a puppet failure before a login leave it `idle` again; the process ends only on
 *  SIGINT/SIGTERM (0), a missing token or a refused bind address (78), or a crash.
 *
 *  WECHAT_BRIDGE_SKIP_LOGIN=1 serves the HTTP API and refuses every POST /login
 *  (`login_disabled`), so the puppet is never started: the start check of the image,
 *  which must not log in. */
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { WechatyBuilder, types } from 'wechaty';
import { PuppetNeo } from 'wechaty-puppet-neo';
import { bindHost, createBridge, DEFAULT_PORT } from './bridge.mjs';
import { qrSvg } from './qr-svg.mjs';

const tokenFile = join(homedir(), '.aitelier-secrets', 'wechat-bridge-token');
const port = Number(process.env.WECHAT_BRIDGE_PORT || DEFAULT_PORT);
const log = line => console.error(`wechat-bridge: ${line}`);

let token;
try { token = readFileSync(tokenFile, 'utf8'); } catch { token = ''; }
if (!token.trim()) { log(`no token in ${tokenFile}; not starting`); process.exit(78); }

let host;
try { host = bindHost(process.env.WECHAT_BRIDGE_BIND, existsSync('/.dockerenv') || existsSync('/run/.containerenv')); }
catch (error) { log(error.message); process.exit(78); }

// No `name`: Wechaty then keeps its memory card in memory only and writes no file
// (the container's root filesystem is read-only; the login is in memory anyway).
const bot = WechatyBuilder.build({ puppet: new PuppetNeo() });
const skipLogin = process.env.WECHAT_BRIDGE_SKIP_LOGIN === '1';
const bridge = createBridge({ bot, types, token, log, renderQr: qrSvg, loginEnabled: !skipLogin });

let stopping = false;
async function stop(reason, code) {
  if (stopping) return;
  stopping = true;
  log(reason);
  await bridge.close().catch(() => {});
  await bot.stop().catch(() => {});
  process.exit(code);
}

process.once('SIGINT', () => void stop('SIGINT', 0));
process.once('SIGTERM', () => void stop('SIGTERM', 0));

const address = await bridge.listen(port, host);
log(`listening on ${address.address}:${address.port}`);
log(skipLogin ? 'WECHAT_BRIDGE_SKIP_LOGIN=1: the puppet is never started' : 'idle: no QR until POST /login asks for one');
