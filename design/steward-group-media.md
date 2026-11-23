# Group media: kept without a mention

Status: design delta for the owner's ruling of 2026-11-22, answering
`q-group-media-confirm`. Implementation: issue `iss-68dac765c59c4dbe`.

The ruling, in the owner's words: media that arrives in a group **without** calling the
steward must still be kept, so a later question can read it. The use case he gave is an
image arriving first, answered by nothing, and then a second message saying
"@管家 这个图上是什么" - the steward has to be able to read the image that came before.

The two questions the ruling separates are not the same gate:

* *storage* is not gated on the mention - every image, voice or file any member sends in a
  group the steward serves is fetched and kept;
* *handling* is gated on the mention - only the `@`-message opens a turn and gets a reply.

## What the shipped code already settles

Measured on `f7a325a1fb` (the merge of the bridge half), and deliberately not re-decided
here:

* **Audience, retention and refs are already the library's**
  (`integrations/dsh-director/lib/media.mjs`): a row's audience is `owner`,
  `group:<channel>:<group_ref>` or `project:<id>`, and `covers(turnAudience, row)`
  (`:115-119`) is the one rule `media_call` and `send_media` apply - an owner turn covers
  every ref, a group turn only its own group's. Rows are kept 30 days and at most
  `limits.total_bytes` in all, oldest first, deduplicated by sha256; refs are `m_` + 16 hex
  characters (`integrations/dsh-director/lib/media.mjs:12-45`).
* **The consumer side of a group attachment already exists**
  (`integrations/dsh-director/lib/channels.mjs`): `withAttachments` (`:758-785`) accepts a
  group message's ref only when the row's audience is exactly
  `group:<channel>:<conversationRef>`, so an owner's file can never become a group message's
  attachment.
* **The group record already has a place for attachments**: `state_group_messages.attachments_json`
  (`core/state_groups.py:257`, written at `:746-751`) holds kind, mime, size, duration and
  the ref - never a byte and never a name. Measured, not assumed: `relay_group_message`
  writes it for the inbound row itself (`keep("question", accepted=1, …, text=body)` at
  `:805`), not only for an answered question, and the body it needs is the placeholder line
  `integrations/dsh-director/lib/channels.mjs` composes (`[图片]` …), so the record path
  needs no backend change.
* **The 1:1 path already does the whole shape**: the bridge *describes* a medium on arrival
  and fetches only on demand (`integrations/wechat-bridge/bridge.mjs:539-556`,
  `live.set(mediaId, ...)` at `:553`), the channel plugin stores it through `storeMedia`
  before handing the message in (`integrations/dsh-channel-wechat/index.mjs:111-141`),
  and the host writes the placeholder lines itself
  (`integrations/dsh-director/lib/channels.mjs:758-785`).

## The five seams, and what is implemented now

1. **The bridge: implemented** (`integrations/wechat-bridge/bridge.mjs:484-526`, merged at
   `73089294f7`). It used to return false for every group message whose type is not text,
   and for a bound room's text unless it called the steward; a bound room's image, voice or
   file therefore never became an event. Now a bound room's non-text message that is one of
   the three kinds is described exactly as a 1:1 one is (`mediaOf`, the bridge's own
   `mediaLimits`), registered in `live` under its own `media_id`, and pushed as ONE
   `group_message` event: `text` is the room's prefix-stripped caption (`""` when the
   message does not call the steward), `asked` is true exactly when it does (text group
   messages carry `asked: true` too), and an over-bound medium keeps the 1:1 shape
   (`[{kind, size, too_large: true}]`, no `media_id`). An unbound room still passes only the
   exact "@prefix group-code". The group ring stays separate from the 1:1 ring.
2. **The channel plugin: implemented** (`integrations/dsh-channel-wechat/index.mjs:157-193`).
   `deliver` builds `items` from a `group_message`'s attachments as well as a `message`'s,
   and `storeAttachments` (`:111-141`) hands the room along as `conversationRef`; `asked`
   is passed on for a `group_message` and never invented when the bridge omits it.
3. **`storeMedia`: implemented** (`integrations/dsh-director/lib/channels.mjs:701-750`). A
   `conversationRef` no longer answers `unsupported` outright. The binding check becomes
   the message's own: a 1:1 sender must be the channel's current binding
   (`host.senderBound`), a group must be one this host serves - `servesGroup`
   (`:840-845`) reads `host.groups.list()`, the same read the wake and the bridge's
   whitelist use, and it is asked BEFORE `open`, so not one byte is fetched for a room
   nobody vouched for. The row is ingested with `audience: 'group:<channel>:<conversationRef>'`.
   A room the host does not list is refused `unsupported` (the closed set has no other
   word for it) with nothing fetched; a host it cannot reach is `unavailable`, so the
   channel holds the message; a host without a media library or without groups stores
   nothing, as before.
4. **A later turn cannot see what it must resolve: still open** (the follow-up card, not
   this one). The group wake's `recent` is still built from answered question/reply pairs
   only (`core/state_groups.py:1219-1233`); the rows to compose an un-answered medium from
   now exist (item 3 and the measurement above), but the wake does not read them yet.
5. **An un-mentioned medium must not answer: implemented**
   (`integrations/dsh-director/lib/channels.mjs:858-905`). The wake is steered by
   `channels.setGroupWake` (`integrations/dsh-director/index.mjs:402-419`); `handleGroup`
   reads `asked` and, when it is false, records the message (the relay above) and returns
   before the wake - no turn, and no line into the room either (no notice, no confirmation
   line).
   A channel that does not send `asked` keeps the old behaviour: every group message it
   hands in is one that called the steward.

## Bounds and refusals this delta keeps

* Size: the bridge's `MEDIA_LIMITS` (`integrations/wechat-bridge/bridge.mjs:67`: image
  20 MiB, file 25 MiB, a voice source 2 MiB, a voice 2 min 5 s) describes an over-bound
  medium as `too_large`, and it is never fetched.
* Rate: the library's `limits.inbound_per_channel_hour` (20), counted from its own index,
  is the per-hour bound for stored media; one past it is refused `too_large`. Measured: it
  counts a channel's inbound `rows` whether they came from a 1:1 message or a group, so a
  busy group shares that hour with the owner's own media - this is the bound the open
  question below is about, and it is reason to change the bound, not the rule that storage
  is not gated on the mention.
* Type: the first bytes' signature against the shared table; a voice container carrying a
  video stream is refused `type`; a host that cannot read a voice's length refuses
  `unsupported`.
* A group the host does not serve, or a host without a media library: nothing is stored and
  nothing is fetched, and the message itself still goes on (the group's relay then decides
  it - an unbound room is refused `group_not_bound` there).
* Group scope: one group's ref is never shown to another group's turn (the audience rule
  above), and the bridge keeps its own group ring separate from the 1:1 ring.

## "Later", measured: bounded, not guaranteed

The owner's word is that a kept medium must be readable later, so this is what the library
actually promises a group row:

* `covers()` (`integrations/dsh-director/lib/media.mjs:115-119`): the group's own turns
  cover it, any owner turn covers every ref, and no other group's turn covers it.
* Retention: `cleanup()` (`integrations/dsh-director/lib/media.mjs:440-451`) drops rows
  older than `limits.retention_days` (**30**, from
  `integrations/dsh-director/media-types.json`, which
  `integrations/dsh-director/lib/media.mjs:53-55` reads) and then evicts oldest-first while
  the library is over `limits.total_bytes` (**5 GiB**); it runs on start and daily while
  the library is up. `protect()` only holds refs owed to a relay that has not committed.
* So "later" is BOUNDED, not enforced: a group's medium is readable for at most 30 days and
  only while the library stays inside its total bound, oldest first. It is not protected
  like an owed relay ref, and there is a small window between `storeMedia` and the group's
  relay in which an eviction under total-bytes pressure could drop it (the message would
  then be refused invalid_inbound rather than stored). Closing either needs a decision
  about protecting a group's rows, not a bug fix, and is left to the follow-up card.

## Measured, not assumed

* The bridge fetches nothing on arrival: it describes a medium (`mediaOf`) and keeps the
  live message in `live`. The one fetch is the channel's, through `GET /media/:media_id`
  inside `storeMedia`, so one group medium costs one HTTP fetch, one library write, and -
  with item 3 - one `groups.list()` read.
* The bridge's live copy is dropped from `live` when its event leaves the group ring
  (`integrations/wechat-bridge/bridge.mjs:477-482`) and dies with the container; after
  that its `media_id` answers `404` (measured by the bridge half's own suite). A fetch
  later than that window is impossible, which is why the bytes have to reach the library
  for "ask later" to be answerable at all - and why the answer path reads the library,
  never the bridge.

## Deployment precondition (measured 2026-11-23; corrects a claim I first made from timestamps)

The two halves of this delta travel in different carriers, and only one of them costs the
owner's QR scan.

* **The bridge half** (`integrations/wechat-bridge/**` -> the bridge container). The bridge
  container mounts only its token file: `/opt/wechat-bridge` (`bridge.mjs`, `main.mjs`,
  `node_modules`) is baked into the image `aitelier-wechat-bridge:local` (image created
  2026-11-22T20:52:33Z, the files in it dated 17:28). A change to
  `integrations/wechat-bridge/bridge.mjs` therefore does not reach production by merging,
  and not by restarting that container either: it takes one image rebuild and one
  container recreate, and a recreate loses the WeChat login in memory, so it needs the
  owner's QR scan - the same window already owed for the 1:1 image CDN refusal
  (`iss-c8597b02f70d4640`).
* **The plugin and host halves** (`integrations/dsh-director/lib/channels.mjs`,
  `integrations/dsh-channel-wechat/index.mjs`). Both are loaded by the STEWARD DRIVER's own
  process: `core/dsh_driver.py:350-353` declares the channel plugins, and `:749` inserts
  the enabled ones into the profile of the driver whose `role == "steward"`, at that
  driver's start. They are in neither the bridge container nor the
  `aitelier-dsh-host` supervisor. So this half needs the steward driver's process restarted
  (the supervisor relaunches it with the same session); the bridge, the other drivers and
  their in-flight work are untouched, and no QR scan is involved. Whoever deploys this must
  not treat it as a whole-host restart.

Neither carrier has been rebuilt, recreated or restarted for this delta.

I first wrote here that the running bridge image predates the fork patch that widens the
native plugin's incoming message types: the patch landed in this repository at 21:25:04Z
(`c55f520bb6`) and the image was created at 20:52:33Z. That was an inference from
timestamps and it is false. Grepping the running container's addon finds the fork's cause
chain inside it - `CDN HTTP status` matches in
`/opt/wechat-bridge/node_modules/wechaty-puppet-neo/native/linux-x64/neo.node`, while the
text before the fork carried no cause at all - so the native side already has the fork.
Whether a bound room's non-text message reaches `relayGroup` is therefore unmeasured
rather than blocked; only a real group image, after this code lands, can settle it.

## Open questions this delta leaves to measurement

* The cost of one ingest per group medium against `limits.inbound_per_channel_hour` in a
  busy group is unmeasured; if a group's normal traffic brushes the bound, the bound is the
  thing to change, not the rule that storage is not gated on the mention.
* Whether protecting a group's kept rows from retention (the "Later" section above) is
  wanted: it changes what the total-bytes bound evicts, which is an owner's decision, not
  a detail of this seam.
* What a group quota refusal costs a kept medium: past `group_per_hour`/`member_per_hour`
  the relay keeps an `over_quota` row without `attachments_json`
  (`core/state_groups.py:795-796`), so the file is in the library but its history row does
  not name it. A follow-up that reads the history for un-answered media has to know that;
  this card stores the file and records it whenever the relay accepts the message.
