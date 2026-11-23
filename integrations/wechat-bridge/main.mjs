/** Starts the bridge against real WeChat: Wechaty with puppet-neo, the token from
 *  ~/.aitelier-secrets/wechat-bridge-token, port WECHAT_BRIDGE_PORT (default 18731),
 *  address WECHAT_BRIDGE_BIND (bridge.mjs bindHost: loopback, or 0.0.0.0 in the
 *  container). It runs in its own container (Dockerfile, run.sh).
 *
 *  No bot is built here. Step 19.1 (design §7.2.1): when the fork has a saved login (its
 *  `PuppetNeo.savedLoginStatus()`; PUPPET_NEO_STATE_DIR and PUPPET_NEO_STATE_KEY_FILE, set by
 *  run.sh, say where), the bridge resumes it at once with `makeBot({resume: true,
 *  expectUsername})` - no QR, whatever the resume answers;
 *  otherwise it starts `idle` and builds and starts a bot only when POST /login asks for a
 *  QR, a fresh one for each request. A QR nobody scanned in time, a logout or a puppet
 *  failure before a login leave it `idle` again; the process ends only on SIGINT/SIGTERM
 *  (0), a missing token or a refused bind address (78), or a crash. A fork without the
 *  saved-login calls, or with saving off (WECHAT_BRIDGE_SAVE_LOGIN=0 in run.sh), is the
 *  bridge as it was before 19.1.
 *
 *  WECHAT_BRIDGE_SKIP_LOGIN=1 serves the HTTP API, resumes nothing and refuses every POST
 *  /login (`login_disabled`), so no puppet is ever started: the start check of the image,
 *  which must not log in. */
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { WechatyBuilder, types } from 'wechaty';
import { FileBox } from 'file-box';
import { PuppetNeo } from 'wechaty-puppet-neo';
import { accountRecord, bindHost, createBridge, DEFAULT_PORT } from './bridge.mjs';
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

// No `name`: Wechaty then keeps its memory card in memory only and writes no file (the
// container's root filesystem is read-only; the saved login is the fork's, not Wechaty's).
// `options`: none for a QR login, {resume: true, expectUsername?} for a resume (step 19.1).
const makeBot = options => WechatyBuilder.build({ puppet: new PuppetNeo(options) });
// Step 19.1: the fork's saved login - only its status ({present, off?, writes,
// write_failures}, never the login itself; the bridge never deletes it) - and the account the
// last QR login was with, which the bridge records in its own directory outside the fork's
// data/ (WECHAT_BRIDGE_ACCOUNT_DIR, set by run.sh). A fork without savedLoginStatus() has no
// saved login.
const savedLogin = typeof PuppetNeo.savedLoginStatus === 'function' ? {
  status: () => PuppetNeo.savedLoginStatus(),
  ...accountRecord(process.env.WECHAT_BRIDGE_ACCOUNT_DIR),
} : undefined;
const skipLogin = process.env.WECHAT_BRIDGE_SKIP_LOGIN === '1';
// Step 24.2: POST /send-file hands the fork a FileBox whose name says what it goes out as
// (bridge.mjs sendPlan; a voice bar is the SILK the live session's encodeVoice made).
const makeFileBox = (bytes, name) => FileBox.fromBuffer(bytes, name);
const bridge = createBridge({ makeBot, types, token, log, renderQr: qrSvg, loginEnabled: !skipLogin, makeFileBox, savedLogin });

let stopping = false;
async function stop(reason, code) {
  if (stopping) return;
  stopping = true;
  log(reason);
  await bridge.close().catch(() => {});
  process.exit(code);
}

process.once('SIGINT', () => void stop('SIGINT', 0));
process.once('SIGTERM', () => void stop('SIGTERM', 0));

const address = await bridge.listen(port, host);
log(`listening on ${address.address}:${address.port}`);
log(skipLogin ? 'WECHAT_BRIDGE_SKIP_LOGIN=1: the puppet is never started'
  : bridge.state.phase === 'resuming' ? 'resuming the saved login' : 'idle: no QR until POST /login asks for one');
