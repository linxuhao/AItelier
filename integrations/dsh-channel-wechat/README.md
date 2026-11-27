# dsh-channel-wechat

The owner's WeChat as an owner channel of the steward: a DSH plugin that registers the
channel `wechat` with the `ownerChannels` service (provided by dsh-director when its host
declares the `channels` capability: the AItelier backend's steward, or dsh-director's built-in
local host with `channels: true`, see its README's "Chat channels"). It
does not speak WeChat itself: the login lives in the wechat-bridge container
(`integrations/wechat-bridge`), and this plugin long-polls the bridge's `/events`, sends
through its `/send`, and shows its login QR on the dashboard.

## Setup

1. Write the token the bridge and this plugin share (one line, any random string),
   then build and start the bridge as `integrations/wechat-bridge/README.md` says:

   ```bash
   ( umask 077; openssl rand -hex 32 > ~/.aitelier-secrets/wechat-bridge-token )
   ```
2. The steward's profile loads this plugin only when that token file exists, checked
   at the steward's start, so **restart the steward** afterwards (stop and start the
   `aitelier-home` driver; see "The steward" in `docs/dsh-driver.md`).
3. Open the Steward card → **Channels**. While the bridge is not logged in, the WeChat
   row shows a login QR (**New QR code** asks for a fresh one); scan it with the WeChat
   account the bot will use.
4. **Bind** the owner's account: press **Bind** on the WeChat row and send the
   six-digit code to the bot from the owner's own WeChat account before it expires.
   Only the bound account's messages reach the steward.

The plugin reaches the bridge at `http://127.0.0.1:18741` unless `WECHAT_BRIDGE_PORT`
names another port. Set that variable in the DSH host service's environment (a systemd
drop-in for `aitelier-dsh-host.service`), not in your shell: the steward is started by
that service.

Tests: `npm test` (no network, no bridge needed).
