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

Measured on main `25f9ea72e15a`, and deliberately not re-decided here:

* **Audience, retention and refs are already the library's** (`integrations/dsh-director/lib/media.mjs`):
  a row's audience is `owner`, `group:<channel>:<group_ref>` or `project:<id>`, and
  `covers(turnAudience, row)` is the one rule `media_call` and `send_media` apply - an owner
  turn covers every ref, a group turn only its own group's. Rows are kept 30 days and at
  most `limits.total_bytes` in all, oldest first, deduplicated by sha256; refs are `m_` +
  16 hex characters. `lib/media.mjs:12-45`.
* **The consumer side of a group attachment already exists** (`integrations/dsh-director/lib/channels.mjs`):
  `withAttachments` accepts a group message's ref only when the row's audience is exactly
  `group:<channel>:<conversationRef>`, so an owner's file can never become a group
  message's attachment (`lib/channels.mjs:751-760`).
* **The group record already has a place for attachments**: `state_group_messages.attachments_json`
  (`core/state_groups.py:257`, written at `:748`) holds kind, mime, size, duration and the
  ref - never a byte and never a name.
* **The 1:1 path already does the whole shape**: the bridge *describes* a medium on
  arrival and fetches only on demand (`integrations/wechat-bridge/bridge.mjs:519-532`,
  `live.set(mediaId, ...)`), the channel plugin stores it through `storeMedia` before
  handing the message in (`integrations/dsh-channel-wechat/index.mjs:111-136`), and the
  host writes the placeholder lines itself (`lib/channels.mjs:711-745`).

## What is missing, and the three seams this delta fixes

1. **The bridge drops group media before anything can describe it.**
   `relayGroup` returns false for every message whose type is not text
   (`bridge.mjs:488`) and, for a text message in a bound room, unless the text calls the
   steward (`bridge.mjs:492-497`). A bound room's non-text message therefore never becomes
   an event. Decision: a bound room's non-text message is described exactly as a 1:1 one is
   (`mediaOf`, the same `MEDIA_LIMITS`), kept in the bridge's `live` map under its own
   `media_id`, and pushed as one `group_message` event carrying the attachment and the
   text the room's prefix called out - empty when the message does not call the steward.
   The event says which of the two it was (`asked`), because the two have different
   consequences downstream.
2. **The channel plugin stores attachments only for 1:1 events.**
   `deliver` builds `items` from `event.attachments` only when
   `event.type === 'message'` (`index.mjs:156`). Decision: a `group_message` event's
   attachments take the same path, with the room handed in as `conversationRef`.
3. **`storeMedia` refuses group media outright.**
   `lib/channels.mjs:711-716` answers `unsupported` for any meta carrying a
   `conversationRef` and ingests with `audience: 'owner'`. Decision: a `conversationRef`
   is accepted when it names a group this host serves (the same read the wake already
   does), and the row is ingested with `audience: 'group:<channel>:<group_ref>'`; the
   1:1 binding check (`host.senderBound`) stays the 1:1 rule and does not decide a group's.
4. **A later turn cannot see what it must resolve.**
   The group wake's `recent` is built from answered question/reply pairs only
   (`core/state_groups.py:1220-1240`), so an un-answered medium is invisible even after it
   is stored. Decision: the group turn's `recent` additionally carries the group's last
   kept inbound messages that have attachments and no answer, each with its attachment
   line, inside the same `GROUP_LIMITS.recentChars` budget. Un-answered *text* stays out -
   it was never visible either; only media gains a line, so "the picture just now" has a
   ref to resolve.
5. **An un-mentioned medium must not answer.** The wake is steered by
   `channels.setGroupWake` (`index.mjs:402-419`); a message whose `asked` is false is
   recorded and never wakes, never answers, never notices.

## Bounds and refusals this delta keeps

* Size: the bridge's `MEDIA_LIMITS` (image 20 MiB, file 25 MiB, voice 2 MiB SILK) describe
  an over-bound medium as `too_large` and it is never fetched.
* Rate: the library's `limits.inbound_per_channel_hour`, counted from its own index, is
  the per-hour bound for stored media; one past it is refused `too_large`.
* Type: the first bytes' signature against the shared table; a voice container carrying a
  video stream is refused `type`; a host that cannot read a voice's length refuses
  `unsupported`.
* A group the host does not serve, or a host without a media library, keeps today's
  answer: nothing is stored.
* Group scope: one group's ref is never shown to another group's turn (the audience rule
  above), and the bridge keeps its own group ring separate from the 1:1 ring.

## Measured, not assumed

* Fetching on arrival costs one puppet fetch and one library write per group medium; the
  bridge's live copy is evicted with its ring (`bridge.mjs:476-482`) and dies with the
  container, so the bytes have to reach the library for "ask later" to be answerable at
  all.
* WeChat's own copy may be gone by the time the later question arrives, which is why the
  answer path reads the library and never a fresh fetch.

## Deployment precondition (measured 2026-11-23)

The running bridge container `aitelier-wechat-bridge` carries image
`aitelier-wechat-bridge:local`, whose image was created 2026-11-22T20:52:33Z. The fork patch
that widens the native plugin's incoming message types - the one that splits an
`@chatroom` sender into talker and room and supplies the room payloads Wechaty's
`Room.ready()` waits for - was committed 2026-11-22T21:25:04Z (`c55f520bb6`,
`integrations/wechat-bridge/neo-fork/0001-incoming-message-types.patch:658`). The running
image therefore predates it, and whether a bound room's non-text message ever reaches
`relayGroup` in production is **unmeasured**. The unit-level arms below do not need it;
the live witness does.

Making the feature live takes one rebuild of that image and one recreate of its
container, and a recreate loses the WeChat login - so it needs the owner's QR scan, the
same window already owed for the 1:1 image CDN refusal (`iss-c8597b02f70d4640`). Until that
window, this design is measured at the unit level only, and no answer here claims the
feature is live.

## Open questions this delta leaves to measurement

* The group record path (`groupInbound`) has not been read for whether it writes
  `attachments_json` for an un-answered inbound message or only for the reply.
* The cost of one ingest per group medium against `limits.inbound_per_channel_hour` in a
  busy group is unmeasured; if a group's normal traffic brushes the bound, the bound is
  the thing to change, not the rule that storage is not gated on the mention.
