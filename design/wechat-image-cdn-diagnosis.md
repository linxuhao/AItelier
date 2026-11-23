# Why a 1:1 WeChat image download is refused (CDN HTTP 400) — diagnosis

Status: diagnosis only. Nothing in this document has been fixed. Issue `iss-c8597b02f70d4640`.

Dates are UTC. Every sentence below is meant to be traceable to a line of the repository, a
line of the pinned dependency's source, or a line of the retained log excerpt named in §1; a
claim that rests on neither is marked **inference**.

## 1. The bytes this is written from

- The bridge's own log, taken from the running container with `docker logs -t` for
  `2026-11-22T22:35:00Z … 2026-11-23T00:10:00Z`, retained by the director: 894 bytes, sha256
  `f0974ae531f7075ea851003002599526cd7f113b11beb9832a6d026db07df07b`. The excerpt holds eight
  lines in total: three login lines and five fetch attempts. **It contains no message-arrival
  line** — see §4.
- The code that answers those attempts: `integrations/wechat-bridge/bridge.mjs`
  (`fetchBox`, the `GET /media/:media_id` handler, and the attempt bookkeeping at
  `:690`–`:729`), the fork's own Rust in
  `integrations/wechat-bridge/neo-fork/0001-incoming-message-types.patch`, and the Rust
  dependency `wechat-protocol` — the crate the patch imports (`use wechat_protocol::` at
  `neo-fork/0001-incoming-message-types.patch:28`). **This repository records no version for
  it**: `neo-fork/pin.json` pins the upstream `puppet-neo` commit `ddfa5ecd…` (version 0.1.1),
  the sha256 of seven files — the four the patch touches, plus `LICENSE`, `package.json` and
  `rust-toolchain.toml` — and the patch's own digest; no `Cargo.toml` or lockfile is tracked
  (`git ls-files | grep -c Cargo.toml` = 0, measured 2026-11-23). The dependency is declared in
  the fetched commit's own `native/Cargo.toml`, and `integrations/wechat-bridge/Dockerfile:53`
  builds it with `cargo build --release --locked`, so the version is whatever that commit's
  lockfile names — reproducible from the pin, but not visible in this tree. The source read for
  this document is a copy taken outside the repository that reports `version = "2.2.0"` in its
  own `Cargo.toml`. Cited below as `src/<file>:<line>`, never vendored here.
- The file download that works is **not** in this window: the retained lines are all image
  attempts. The successful file download is the issue's own measurement (107,089 bytes on
  2026-11-23) and is used here only as the contrast the issue states.

## 2. Which code builds the request, and what it carries

For a 1:1 image the chain is:

1. `integrations/wechat-bridge/bridge.mjs` `fetchBox`: `kind === 'image'` goes through
   `message.toImage().hd()`, every other kind through `message.toFileBox()`. This is the first
   place the image path parts company with the file path.
2. Wechaty → `puppet-neo` → the fork's `download_media(file_id, aes_key, quality, …)`
   (`0001-incoming-message-types.patch:209`, landing in `native/src/lib.rs`), called with
   `quality = "original"` for an image and `"file"` for a file
   (`0001-incoming-message-types.patch:1194`–`:1214`: an image's id is
   `attribute(image,'cdnbigimgurl') || attribute(image,'cdnmidimgurl')…` with
   `quality:'original'`; a file's is `elementText(content,'cdnattachurl')` with `quality:'file'`).
3. `wechat-protocol` 2.2.0 `src/media.rs:547` `download_with_transport` → `:619` builds the
   fields → `:635` posts to `/download` (`/downloadbig` only for a large file) via
   `src/media.rs:502` `post()`.

That `post()` sends exactly one shape for every medium: `POST http://<config.server_ips[0]>:443/download`
with `Accept: */*`, `Content-Type: application/octet-stream`, `User-Agent: MicroMessenger Client`,
`Connection: close`, a `C-Seq` header, a packed-field body carrying the shared CDN fields
(`ver`, `seq`, `weixinnum`, `nettype`, `clientversion`, `authkey`, `clientostype`, …), then
`wxautostart=0`, `downpicformat=1`, `rangestart`, `rangeend`, `wxmsgflag`, `fileid`. Redirects
are disabled and it runs `no_proxy`. **Any answer other than 200 is turned into the string
`CDN HTTP status {status}`** at `src/media.rs:527` — that literal is what the five retained
lines repeat, so the refusal comes from the HTTP layer, before WeChat's own retry code is even
read.

**What the image request carries that the file request does not**: only two fields differ —
`filetype` (1 for an image, 5 for a file, 7 for a large file: `src/media.rs:285`–`:294`) and the
`fileid` (a `cdnbigimgurl`, else `cdnmidimgurl`, value for an image; a `cdnattachurl` value for a
file). Host, port, path, headers, every other field, and the retry loop are shared.

## 3. The five attempts, as the log states them

```
2026-11-22T22:44:24.711992537Z logged in
2026-11-22T22:47:51.671749310Z media image not fetched (held): … CDN HTTP status 400
2026-11-22T22:48:01.109401157Z media image not fetched (held): … CDN HTTP status 400
2026-11-22T22:48:15.223353652Z media image not fetched (held): … CDN HTTP status 400
2026-11-22T22:48:39.810229316Z media image not fetched (held): … CDN HTTP status 400
2026-11-22T22:49:24.935768712Z media image not fetched (gone): … CDN HTTP status 400
```

Spacings: 9.44 s, 14.11 s, 24.59 s, 45.13 s; the five span 93.26 s (22:47:51.671749310Z to
22:49:24.935768712Z). The words are the bridge's
own (`bridge.mjs:727`): a failed fetch answers a transient `unavailable`, and once a medium's
attempt budget is spent the entry is marked `gone` and later asks get `410`. So the ladder is
one medium being asked five times, each time refused by the CDN, and then no longer offered.

## 4. The delay between arrival and the first attempt

**This window cannot measure it, and the honest bound is what the lines allow**: the first
attempt came 206.96 s (3 min 26.96 s) after `logged in`, and the excerpt records no arrival line
them. **Inference, from the code and not from these lines:** the first attempt was made when the
channel asked for the bytes, not after a timer, and the later four are that channel's retries of
the same request — `integrations/dsh-channel-wechat/index.mjs` retries an `unavailable` answer
with a growing pause, but no line here names a retry reason. The observation that would settle it — a line on the bridge when a media message is
enqueued, or the channel's `inbound … not delivered … retrying` line — is absent from both this
excerpt and the host journal for this window. Measured for this document on 2026-11-23 over
`2026-11-22T22:40:00Z…2026-11-23T00:10:00Z`: the user journal holds 1,837 lines and the system
journal 17,671, and the 18 lines containing `wechat` are **all** `systemd[…]: Started
dsh-subprocess-…scope` records (a process-start record quoting a command line), none of them
channel output; `~/.AItelier/logs` gained no file inside 22:35–00:10Z. Retained with the exact
commands at `journal-1123.txt`, 977 bytes, sha256
`3dc0f2abb61e8ab2f84f36661a5721275ed3edfcf679e0cb258e401a4283a1bc`.

## 5. What that supports, and what it rules out

Ruled out by the retained lines and the pinned source:

- **URL or id expiry.** The first attempt is under 3.5 minutes after login and all five fall
  inside 93.26 s; the crate's own expiry check (`media_expired`, `src/media.rs:561`) never appears
  — the error is an HTTP status, and it is the *same* status five times.
- **The size or kind bound.** The failure happens before any byte reaches the library: the
  medium was described and accepted against the bridge's own `MEDIA_LIMITS`
  (`integrations/wechat-bridge/bridge.mjs:67`: image 20 MiB, file 25 MiB, voice source 2 MiB),
  and the refusal is the CDN's HTTP answer, not a `too_large` refusal. The media library's
  separate `MEDIA_LIMITS` (`integrations/dsh-director/lib/media.mjs:55`) is consumed on ingest,
  after the bytes arrive.
- **A proxy, TLS or network difference.** `post()` builds one client for every medium
  (`no_proxy`, redirects off, same `User-Agent`), and the file path — identical in every field
  but the two named in §2 — is accepted for this account.
- **"The bridge fetched lazily, long after the message."** *Inference from the code path, not a
  measurement:* the bytes are requested on the channel's store-before-hand-on path, so the
  request the log shows is the arrival path, not a later read (§6). These bytes hold no arrival
  line, so "long after" can be neither confirmed nor refuted from them.

Supported, and not separated from each other by these bytes:

- The CDN rejects the **image request shape** — most plausibly the `fileid` the image branch
  sends (`cdnbigimgurl`, else `cdnmidimgurl`) and/or its `filetype = 1` pairing with
  `downpicformat = 1`, because the file branch's POST is accepted with every other field held
  equal. **Inference**: which of the two fields is wrong cannot be told from a status code
  alone; the CDN's body would say, and the crate never reads it (it fails on the status first).

## 6. What changes if the bytes are fetched on arrival instead of on demand

They already are. The channel stores a message's attachments through the host *before* it hands
the message in, and that store asks `GET /media/:media_id`, which is the request in §3 — so the
"on arrival" design is the shipped one and moving the fetch to the steward's read would only
move the same failing POST later (and add a stall to the steward's turn) — an **inference** from
the two call sites, not something this window measured.

What such a fetch costs per medium: one CDN POST, plus the channel's retries while it holds the
message (the ladder in §3 is that cost when the CDN refuses), plus one library write
(`integrations/dsh-director/lib/channels.mjs:711` `storeMedia` calls `media.ingest`
(`:728`), which is the write; no line here counts it). The bounds that hold it are the bridge's
`MEDIA_LIMITS` per kind (size, decided before any fetch — `bridge.mjs:735` answers `too_large`
when the spooled medium passes the limit) and the
media library's retention (`integrations/dsh-director/lib/media.mjs`: a 30-day age bound and
`limits.total_bytes`, with `covers()` protecting what a pending answer owes). Retention is a
bound on how long a kept medium may live, not a guarantee that a refused one is retried later.

The observations that this environment cannot produce without a live WeChat message, and that
would settle the question:

1. The **shape** of this account's `cdnbigimgurl` value (bare media id versus a URL carrying a
   path and query): it lives in the message's own XML payload, which carries owner content and
   is not logged anywhere.
2. The **CDN's response body** for the 400. A fork build that logs only the status and the body's
   length (never the body) would name which field was rejected without keeping any content.
3. Which pair the CDN accepts for an image: `filetype` 1 with `cdnbigimgurl`, versus `filetype`
   2 with `cdnmidimgurl`. The fork already carries both ids; trying the other one is a one-line
   change in the fork, but it needs a rebuilt native module and a fresh login (a QR scan).

## 7. What is not determined

- Which field the CDN rejected (§5, §6.2).
- Whether the image id WeChat advertised for this account is a bare id at all (§6.1).
- Whether a rebuilt fork with the alternate id would be accepted — untestable without one live
  image message after the rebuild.

The instrument for the next step is therefore not another log reading: it is a fork build that
(a) logs the request's field *names* and the shape of `fileid` (length and whether it contains
`/` or `?`), never the key and never the bytes, and (b) tries `cdnmidimgurl` when
`cdnbigimgurl` is refused for an image. That needs the owner's QR window, which is why this
document stops at the diagnosis.

## 8. Measurements versus inferences in this document

Every number quoted above comes from the retained 894-byte excerpt, the retained journal file, or
`git` in this tree. The sentences below are **not** measurements; each is labelled here so a
reader does not have to infer the split:

1. §1: the 107,089-byte file download is the issue's own figure, not measured here — the excerpt
   holds no file attempt at all. It is used only as the contrast the issue states.
2. §4: the retry ladder and the "store before hand-on" reading of the first attempt are
   inferences from the code; the log names no retry reason.
3. §5: "the bridge fetched lazily, long after the message" is ruled out by the code path, not by
   an arrival timestamp — the excerpt has none.
4. §6: moving the fetch to the steward's read would only move the same failing POST later, and
   the library write happens once per ingested medium; both follow from the call sites, and
   neither is witnessed in the retained bytes.
5. §6 items 1–3 and §7 are proposed observations and undetermined questions, not findings.
