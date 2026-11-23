# Group media: kept without a mention

Status: design delta for the owner's ruling of 2026-11-22, answering
`q-group-media-confirm`. Implementation: issue `iss-68dac765c59c4dbe`. Revised by the
owner's rulings of 2026-11-23 (section "Owner rulings 2026-11-23" below): storage is now
behind a per-group switch that is off by default, an un-mentioned medium is kept one day,
and the owner's own turns cannot use it. Where the older text below disagrees with that
section, the section wins; the older text is marked where it does. The rulings are built on
branch `claude/group-media-switch` (not merged); where the build differs from what this
section first said, the text below says what was built, and section 16 lists those points.

The ruling, in the owner's words: media that arrives in a group **without** calling the
steward must still be kept, so a later question can read it. The use case he gave is an
image arriving first, answered by nothing, and then a second message saying
"@管家 这个图上是什么" - the steward has to be able to read the image that came before.

The two questions the ruling separates are not the same gate:

* *storage* is not gated on the mention - every image, voice or file any member sends in a
  group the steward serves is fetched and kept; **(2026-11-23: only in a group whose
  switch the owner turned on, and only after the group was told; see R2 below)**
* *handling* is gated on the mention - only the `@`-message opens a turn and gets a reply.

## Owner rulings 2026-11-23

The owner answered the open questions on `iss-68dac765c59c4dbe` on 2026-11-23 (~09:30Z).
The platform director's requirements for building them are his note 640. Line numbers in
this section are measured on main `f48efd7126`.

### R. The rulings, as rules

Two words used throughout: a **kept medium** is a file of a group message that did NOT
call the steward (the bridge's `asked: false`), stored while that group's switch is on; an
**addressed medium** is a file of a group message that called the steward (the attachment of
a mention turn's own message).

* **R1 (Q1) Store on arrival, read on a later mention.** Unchanged from the 2026-11-22
  ruling and the shipped code: the file is fetched when it arrives, opens no turn and gets
  no reply, and a later `@` in the same group can read it.
* **R2 (Q2) A switch per group, default OFF.** The owner turns it on in the dashboard. The
  steward then posts one fixed notice line into that group, and the switch is ON only once
  that line was actually sent. Turning it off also posts a line, and deletes what was kept.
  Unbinding and rebinding both return the switch to off; a newly bound group starts off.
* **R3 (Q3) One day, who sent it, twenty.** The owner's words: 「一天， 标出谁发的，20件」.
  A kept medium lives ONE day (not the library's 30). A later mention turn's `recent` lists
  up to 20 of this group's kept media, each with who sent it.
* **R4 (Q6) The owner's private turns cannot use kept media.** An owner turn covers every ref
  except a kept medium. That group's own turns still cover it. Other groups never do.
* **R5 Addressed media keep today's rules.** 30 days, covered by owner turns and by that
  group's turns, and shown by today's `recent` rule.

The director's invariants (note 640), which this section is held to:

* **(c)** "On" is authorized only by the notice being SENT. The notice is posted first, and
  "on" is persisted only after the send succeeded. A failed send leaves the switch off. A
  refusal that can be decided before sending spends no retry.
* **(d)** New groups and rebinds start off. Nothing reuses today's "bound means store"
  shape: `servesGroup` (`integrations/dsh-director/lib/channels.mjs:840-845`) no longer
  authorizes keeping an un-mentioned file.
* **(b)(i), (b)(ii)** are answered in sections 5 and 6.

This section reuses the reasoning of the earlier long draft (design-steward §7.7.25, r44z)
about the switch, the notice, deleting on off, rebind and privacy. It does not reuse that
draft's architecture: there are no `held_media` rows and no `hold_group_media` command. It
builds on what main has: `storeMedia`, the library row's audience, `relay_group_message`
and `list_group_recent`.

### 1. Where the switch lives

On the group's row, `state_owner_groups` (`core/state_groups.py:69-72`). The columns are
added with `add_column_if_missing`, the way `attachments_json` was (`:251`):

| column | type | meaning |
|---|---|---|
| `media_keep` | `TEXT NOT NULL DEFAULT 'off'`, CHECK in `off`, `on_pending`, `on`, `off_notice` | the state |
| `media_keep_at` | `TEXT` | time of the last transition: the EPOCH that notices and purges name |
| `media_keep_attempts` | `INTEGER NOT NULL DEFAULT 0` | failed SENDS for this epoch (refusals made before sending are not counted) |
| `media_keep_note` | `TEXT` | why the switch is waiting or why it fell back, shown on the dashboard (`group_send_budget`, `group_quota_account`, `channel_down`, `purge_owed`, `notice_not_sent`) |
| `media_purged_at` | `TEXT` | the epoch whose purge receipt was last recorded (section 6) |
| `media_purge_reason` | `TEXT` | the purge still owed: `off`, `unbind` or `rebind`, from the transition that made it owed; NULL once its receipt is recorded at the current epoch |
| `media_purge_rows` | `INTEGER NOT NULL DEFAULT 0` | the backend `kept_media` rows those transitions deleted, which the receipt's ledger row reports as `backend_rows` |

The four states. Only `on` stores anything; for the store gate, the other three all mean off:

| state | stores? | what the host owes |
|---|---|---|
| `off` | no | nothing |
| `on_pending` | **no** | the on-notice |
| `on` | yes | nothing |
| `off_notice` | no | the purge receipt, then the off-notice |

The transitions, each in one write transaction that also sets `media_keep_at=now`,
`media_keep_attempts=0` and `media_keep_note=NULL`:

| event | from -> to | also |
|---|---|---|
| owner turns on (`set_group_media_keep on=true`) | `off`, `off_notice` -> `on_pending` | an `off_notice` still owed is dropped: the next line the group reads is the on-notice, which is true |
| owner turns off (`on=false`) | `on` -> `off_notice` | deletes this group's `kind='kept_media'` rows (section 6) |
| owner turns off | `on_pending` -> `off` | nothing was announced and nothing was stored |
| on-notice committed (`group_media_notice phase=sent`) | `on_pending` -> `on` | the outbound row (section 2) |
| off-notice committed | `off_notice` -> `off` | the outbound row |
| third failed send | `on_pending` -> `off`; `off_notice` -> `off` | note `notice_not_sent` (an off already took effect, so only its line is lost) |
| unbind (`unbind_owner_group`, `:467`) | any -> `off` | only when the switch was ever used (`media_keep_at` set): a new epoch and a purge owed (`unbind`); the group's rows are deleted there already (`:485-487`) |
| rebind (`_bind`, after its upsert, `:645-650`) | any -> `off` | the same, with `rebind`; a group whose switch was never used stays as it is (off, no epoch) |
| new bind | (INSERT) -> `off` | the column default |

A turn-on or turn-off that does not change the state (on while `on_pending` or `on`; off
while `off`) is a no-op. It does not move the epoch.

Who reads the switch:

* `list_owner_groups` (`:1214-1225`), which the plugin already calls through
  `host.groups.list()` for the bridge's whitelist, the wake's label and `servesGroup`, adds
  to each live group `media_keep` (`'on'` only in state `on`, otherwise `'off'`) and, when
  the host owes it work, `media_notice: {phase: 'on'|'off', epoch, attempts}`. The answer also
  gains a top-level `media_purges: [{channel, group_ref, epoch}]` for every group, live or
  revoked, that owes a purge (`media_purge_reason` set) and whose state is not `on`; the epoch
  is `media_keep_at`.
* `owner_view` (`:604-636`) adds to each group card `media_keep: {state, since, note,
  attempts, purge_owed, kept_24h, last_purge}` (section 9).

### 2. The notice: post path and the on-after-sent invariant

**The texts** are backend constants next to `BIND_NOTICE` (`core/state_groups.py:170`),
`MEDIA_KEEP_ON_NOTICE` and `MEDIA_KEEP_OFF_NOTICE`. The host never composes them; the check
answer carries the text. Each states honestly that a kept medium is kept for one day, that
only a later `@` in this group can read it, and that switching off deletes it:

> 本群已打开存图：群里没 @ 管家的图片、语音、文件，管家会先存下、不回话，存 1 天后自动删除。只有之后在本群 @ 管家时，管家才会去看（看时会交给外部模型服务处理）；业主私聊和别的群都用不到。业主在面板上看得到谁发过、是什么类型，看不到内容。关闭存图时，已存的一并删除。

> 本群已关闭存图：管家不再存群里没 @ 它的图片、语音、文件，之前存下的已经删除。

The off-notice can only be posted after the purge receipt is recorded (the check below
refuses `purge_owed` until then), so "已经删除" is true when the group reads it.

**The post path** is the existing group-send budget. The new outbound kind `media_notice` is
added to `GROUP_SEND_KINDS` (`:138`). It is therefore judged by `send_refusal`
(`:409-422`), which already holds confirmations and `send_media` files to
`group_sends_per_hour` (20) and every outbound line to `account_out_per_hour` (120). It is
sent by the host's existing `sendGroupLine` (`integrations/dsh-director/lib/channels.mjs:827-837`).
There is one new host-only command, `group_media_notice {request_key, channel, group_ref,
epoch, notice: 'on'|'off', phase, channel_down?}`. It is registered where `post_group_media` is (`core/state_commands.py:1745`,
`:2134`, the steward-only set at `:2293-2311`):

* `phase: 'check'` is a dry run, like `post_group_media`'s `check_only` (`:1025-1072`): no row,
  no budget, nothing recorded for the key (the key is random, `group-media-notice-check:` +
  uuid). It answers `{may_post: true, text}` only when all of these hold: the group is live,
  its state is the one this notice is owed in (`on_pending` for `on`, `off_notice` for `off`),
  `epoch == media_keep_at`, no purge is owed (else `purge_owed` - for either notice, so a
  turn-on during `off_notice` cannot turn on before the earlier kept copies are purged), and
  `send_refusal` is None (else `group_send_budget` or `group_quota_account`). Otherwise it
  answers `{refused}` (`superseded` when the group, state or epoch no longer match). A refusal is written to `media_keep_note` and does NOT touch
  `media_keep_attempts`.
* `phase: 'sent'` is the commit. The host calls it only after `sendGroupLine` returned true.
  The key is `group-media-notice:<channel>:<digest(group_ref)>:<epoch>:sent`, recorded through
  `_replay`/`_record`, so a commit retried after a lost answer gets the first answer and does
  not transition twice. In one transaction:
  * it writes the outbound row (`kind='media_notice'`, the constant as body). The line went
    out, so the record says so, and it counts toward the budget even if the epoch has moved
    on since;
  * if `epoch == media_keep_at`, then `on_pending -> on` (a new epoch: the dashboard's "since")
    or `off_notice -> off` (the epoch kept, so the purge recorded for it stays settled);
  * a group no longer bound writes no row and answers `superseded`;
  * if the epoch moved because the owner turned the switch off while an ON line was being
    sent (the state is now `off`), the group has read "on" and must read "off": the state
    becomes `off_notice` with a new epoch. That purge removes nothing and its receipt says 0;
  * any other mismatch answers `superseded` and changes no state.
* `phase: 'failed'` (key `...:<epoch>:failed:<attempts>`, the attempts the list named) is called only when a send was
  actually attempted and `sendGroupLine` returned false or timed out. It adds one to
  `media_keep_attempts`; the third failure transitions as in the table in section 1.

**The host's pass** runs in `pollStatus` (`channels.mjs:574-581`) right after
`refreshRooms`, on the same `groups.list()` answer, every `statusMs` (30 s), plus once at
`ready`. For each group with `media_notice`, at most one send per group per pass and the
groups one after another:

1. If the epoch has a send that succeeded but whose commit was not answered (an in-memory
   map keyed by group and epoch), retry ONLY the commit. Do not send again.
2. Pre-send refusals the host can decide itself spend no attempt: the channel is not
   registered or has no `sendGroup`, or its last status is not `connected`. The host still
   makes the check call, with `channel_down: true`; it answers `{refused: 'channel_down'}`
   and sets the note, so the dashboard can say why the switch is waiting. No send is
   attempted.
3. `phase: 'check'`. If it is refused, stop here for this group this pass.
4. `sendGroupLine(channel, group_ref, text)`. On true, commit with `phase: 'sent'`; on
   false, `phase: 'failed'`.

The delivery is at least once, never zero. A host that dies between the send and the commit
loses its in-memory map, and after the restart the group may read the same line twice. It
never reads "on" without the switch being on, and the switch is never on without the line
having gone out. Unbinding sends no off line; unbinding sends nothing today either. A
notice that is waiting shows its note on the dashboard and is tried again next pass, with no
attempts limit, until the owner flips the switch back.

### 3. The store gate: channel plugin, `storeMedia`, relay

The gate decides twice, with the same answer expected both times: once before the first
byte is fetched, from the host's list, and once authoritatively, in the transaction that
records the message.

1. **The channel plugin** (`integrations/dsh-channel-wechat/index.mjs:119-147`,
   `storeAttachments`): the `meta` passed to `storeMedia` gains `asked` for a
   `group_message` that carries a boolean `asked`, exactly as `deliver` already forwards it
   to `inbound` (`:174`). It is never invented when the bridge omits it.
2. **`storeMedia`** (`integrations/dsh-director/lib/channels.mjs:719-750`): for a
   `conversationRef` with `asked === false`, `servesGroup` is replaced by `keepsMedia`, the
   same `groups.list()` read, true only when the listed group has `media_keep === 'on'`.
   Otherwise it refuses `unsupported` before `open` (so not one byte is fetched) and logs
   `channel_media_refused {reason: 'group_media_keep_off'}`. When it is true, the ingest
   meta carries `kept: true`. For `asked !== false` (a mention, or a channel that does not
   say), today's `servesGroup` check stays and no `kept` is set: an addressed medium
   (R5).
3. **`handleGroup`** (`:858-909`): for `!mentioned` it calls `groups.relay({..., kept:
   true})`. When the answer is a refusal (`media_keep_off`, `stale`, `undated`,
   `group_not_bound`), every ref of this message that the library marks `kept` is taken
   back at once through `media.withdraw(ref)` (section 4), and the message ends
   `{accepted: false, reason}`. That answer is final for the plugin, because none of those
   reasons is in its `RETRY_REASONS`. A `duplicate` answer takes nothing back. An
   un-mentioned message that carries no file has nothing to keep and is not relayed at all
   (`{accepted: false, reason: 'nothing_kept'}`).
4. **`relay_group_message`** (`core/state_groups.py:708-815`) takes `kept` (request model
   `RelayGroupMessage`, `core/state_commands.py:1743`; the adapter's `relay` in
   `integrations/dsh-driver/steward-host.mjs:860-863` passes it through). With `kept=True`
   it requires nonempty `attachments` (else `invalid_request`). After the group, undated,
   stale and duplicate checks it reads `media_keep`; anything but `on` answers
   `{refused: 'media_keep_off'}` and writes no row. With the switch on, it runs the account-code
   check as every relay does, makes the sender's member row (`_member`, `:360-373`, so the
   sender has a label for R3), and writes ONE row: `kind='kept_media'`, `accepted=0`, the
   placeholder body, `attachments_json`. A kept row is not a question:
   * it is counted by none of the 60/20/120 windows, because those count `accepted=1`
     inbound rows and outbound rows (`:776-803`);
   * it never decides a confirmation;
   * it never yields a quota notice.

   This also removes a side effect of the always-on build: an un-mentioned medium was
   written as an accepted `question`, so it used up the group's 60 questions an hour and
   could write a `quota_notice` row that was never sent.

The races between the two decisions:

| `storeMedia` saw | relay saw | outcome |
|---|---|---|
| on | on | kept: a library row with `kept: true` and a `kept_media` row |
| on | off | the library row is withdrawn at once (step 3). If the host dies first, the sweep in section 6 removes it within one pass |
| off | on | a `kept_media` row whose attachment is `[图片 未收：渠道不支持]`: true, nothing was kept |
| off | off | nothing anywhere |

### 4. One day, for kept media only

* **The marker**: a library row ingested with `kept: true` carries `kept: true` in
  `index.jsonl` (`integrations/dsh-director/lib/media.mjs:352-358`, the row literal).
  `ingest` accepts `kept` only with a `group:` audience and `source: 'in'` (else
  `invalid_media`). An addressed group row and an owner row never carry it.
* **The bound**: `limits.kept_hours: 24` in `integrations/dsh-director/media-types.json`.
  It is one key with two readers: `lib/media.mjs` (`:53-55`) and the backend
  (`core/media_types.limits()`, `core/media_types.py:35-36`), the way `call_body_bytes`
  already has two. `retention_days: 30` stays for every other row.
* **Exact at read time**: `lookup()` (`:238-246`) answers undefined for a kept row older than
  `kept_hours`. `media_call` and `send_media` therefore answer `unknown_ref` from the first
  second past the day, whatever the sweep has done.
* **The sweep**: `cleanup()` (`:451-460`) first drops kept rows older than `kept_hours`, then
  applies the 30-day rule. `start()` (`:479-484`) runs it hourly instead of daily (it is an
  in-memory filter and one index rewrite only when something expired). So a kept file is on
  disk at most 25 hours. A file still named by another row stays (section 5).
* **`withdraw(ref)`** (`:472-476`) takes back a `generated` row (today) or a `kept` row, and
  never an addressed inbound row or an owner row.
* **Never protected**: `protect()` lists refs owed to the owner's 1:1 relay (`channels.mjs:791`).
  A group message never enters `owed` (`:690-691`), so no kept ref is ever protected and
  neither the day nor a purge can be held back. A test pins that.
* **The backend's copy of the day**: `list_group_recent` and `owner_view` read `kept_media`
  rows only with `at > now - kept_hours`, so what the steward and the owner see is exact.
  The rows are deleted by `_retain` (`:386-390`, which every write path already calls) and
  by a new host-only command `expire_group_media {}`, which the host's pass calls once an
  hour, so a quiet group's rows do not linger.
* Addressed media are untouched: 30 days, no marker.

### 5. (b)(i) The same bytes in two groups: one file, one row per ref

Measured in `lib/media.mjs`: the library deduplicates **files** by sha256 and **never rows**.
`store()` (`:312-360`) looks up `existing` by sha256 and reuses its `file`. Every message
still gets its own row: its own `ref`, `audience`, `kept`, `at`, and the idempotency key
`(channel, messageRef, index)` (`find`, `:228-229`). Two groups that receive the same photo
get two rows, `group:wechat:A` and `group:wechat:B`, pointing at one
`<month>/<sha256>.<ext>`. So do an owner's 1:1 photo and a kept copy of it.

Deletion is per row, and the file follows the last row. `dropRows` (`:419-429`) removes the
victim rows, rewrites the index, and unlinks a file only when **no remaining row** names it
(`if (!rows.some(other => other.file === row.file))`). Group A's day therefore expires A's
row and leaves the file while B's kept row, an addressed row or an owner row still names it.
B's row keeps resolving, and A's ref answers `unknown_ref`. Nothing about B is deleted
early, and nothing about A survives under A's ref. Tests pin both directions (section 12).

### 6. (b)(ii) Turning off deletes: the deletion path and its witness

**What "what was kept" is.** For the group being switched off, unbound or rebound, it is
every library row with `kept: true` and that group's audience, plus every backend row of
`kind='kept_media'` for that group. Addressed media are not part of it (R5).

**The references a kept row can have, and what deletion does to each:**

| reference | can it exist? | on deletion |
|---|---|---|
| the backend `kept_media` row naming the ref | yes | deleted in the owner's off transaction (`set_group_media_keep`). Unbind already deletes all the group's rows (`:485-487`). The count goes to the receipt as `backend_rows` |
| another group's row or turn | no: `covers` and `withAttachments` (`channels.mjs:758-789`) refuse a foreign group's ref | - |
| an owner turn | no: R4, `covers` refuses (section 8), and an owner turn never receives the ref (group turns are cut from the conversation when they end) | - |
| the same FILE under another row (an owner 1:1 copy, another group's kept or addressed copy, this group's addressed copy) | yes (section 5) | the kept row goes; the file stays because that other row independently names it. The receipt counts it as `files_shared` and does not name the other rows |
| a ref already in a running group turn's context | possible, for one turn | `lookup` stops resolving at the purge, and `media_call` answers `unknown_ref` |
| text derived from it (a reply, a family-memory note, an OCR result in the session log) | yes | not media and not deleted by the switch. It follows its own retention (group record 30 days). Listed under accepted residuals in the Privacy part of section 13 |

**The path.** The backend cannot delete library files. The library is the steward host's
own directory, and only one writer may touch it (`lib/media.mjs:3-10`). So the deletion is
the host's and the record is the backend's, the way a bundle release is
(`core/state_git_artifacts.py:810-870`). The release writes its ledger row before deleting
and refuses a batch whose counters do not close.

1. **Library `purgeKept(audience)`** (new, exposed through `mediaOps` in
   `integrations/dsh-driver/steward-host.mjs:915-939` and `host.d.ts`). Under the ingest lane
   (so no ingest of that audience is half-done), it:
   1. counts `rows_before` (kept rows of that audience) and the distinct files they name;
   2. drops them with `dropRows`;
   3. counts `rows_after` (must be 0), `files_unlinked` (each one checked absent with
      `existsSync`), `files_shared` (files kept because another row names them) and
      `bytes_freed`;
   4. answers the receipt.

   A receipt that does not close (`rows_after > 0`, or an unlinked file still present) throws
   `purge_incomplete`; the rows of a file that could not be unlinked are put back, so the next
   pass tries again. Nothing is recorded. The library also answers `keptAudiences()`, the
   audiences holding kept rows, which is the sweep's list.
2. **The sweep** runs in the host's pass after a `groups.list()` that SUCCEEDED and is
   well-formed. A failed or empty read is never taken as "every group is off" (the r44z
   lesson about `refreshRooms` swallowing errors, `channels.mjs:846-857`). Every audience
   that has kept rows and is not a listed group in state `on` is purged. This one rule covers
   off, unbind, rebind and late stores (the race in section 3), and it is idempotent. The
   sweep runs before the notices in the same pass, so an owed off-notice can go out in the
   pass that recorded its receipt. A receipt whose record got no answer is kept in memory and
   added to the next purge of the same epoch, so the counts that reach the ledger are what was
   really deleted.
3. **The witness**: for each `media_purges` entry the host calls the new host-only
   `record_group_media_purge {request_key, channel, group_ref, epoch, receipt}`. The backend
   refuses a receipt with `rows_after != 0` (`purge_incomplete`). It APPENDS one row to the new
   table `state_group_media_purges(purge_id, channel, group_ref, epoch, reason, backend_rows,
   rows_before, rows_after, files_unlinked, files_shared, bytes_freed, at)`, with reason `off`,
   `unbind` or `rebind` copied from the transition. It sets `media_purged_at = epoch` when the
   epoch is still current (and clears `media_purge_reason`), and only then does the check stop
   answering `purge_owed`. A receipt for an older epoch is appended with reason `superseded`
   and settles nothing. The ledger rows are kept 30 days (`_retain`). They hold counts only, never a
   ref, a sha or a name.

The dashboard shows the last receipt (section 9). Until it arrives the switch reads 「正在删除」,
which takes at most one pass (30 s) after the owner's click.

### 7. The 20-item `recent`, with who sent each

`list_group_recent` (`core/state_groups.py:1227-1261`) changes only its media part. Today
that part is "inbound rows with attachments that were never answered, sharing the pairs'
`bound`"; it now has two queries:

* **Kept media**: `kind='kept_media' AND at > now - kept_hours`, newest first, at most
  `GROUP_LIMITS["kept_recent_items"] = 20`. This is a backend constant, the only place the
  number lives. It does not depend on the caller's `limit`, so the adapter's `limit: 6`
  (`steward-host.mjs:868-869`) is unchanged. Each entry is `{at, sender, kept: true,
  attachments}`. `sender` comes from `_label_of` (`:376-383`): 业主, the owner's name for
  the member, or 成员甲..; the kept path made the member row (section 3), so it is never
  None. There is no `q`: the body is only the placeholder, and the attachment line already
  says it.
* **Addressed, unanswered media**: today's rule, unchanged (R5): `kind='question' AND
  answered_by IS NULL AND attachments_json IS NOT NULL`, bounded by `limit`.

The answered pairs keep their own `limit` (default 6, at most 20). All three are merged by
time as today. The wake (`integrations/dsh-director/index.mjs:411-412`) trims to
`GROUP_LIMITS.recentChars` by dropping the oldest entry that is NOT `kept`, so a busy
conversation cannot push the twenty out. Kept entries are bounded by their count: one
attachment line each from the bridge, about 90 characters, about 1.8k for twenty. A backstop
`keptChars: 4000` next to `recentChars` (`lib/channels.mjs:264`) drops the oldest kept
entries only past that. `withAttachmentLines` (`lib/media.mjs:140-148`) renders them as it
does today.

### 8. `covers()`, narrowed

`integrations/dsh-director/lib/media.mjs:115-119` becomes:

```js
export function covers(audience, row) {
  const label = audienceLabel(audience);
  const ownGroup = label !== undefined && label.startsWith('group:') && row?.audience === label;
  if (row?.kept === true) return ownGroup;      // R4: a kept medium is its group's alone
  if (audience?.kind === 'owner') return true;  // R5: everything else, as today
  return ownGroup;
}
```

`media_call` and `send_media` (`lib/media-tools.mjs:106`, `:162`) already call it and already
answer `not_visible_in_this_turn`. A kept ref in the same group's turn is usable by both: the
draft's ban on `send_media` is not carried over, because sending a group's own file back
into that group discloses nothing. The library header comment (`:38-45`) and the old
"`covers()`" bullet below are updated to match.

### 9. The dashboard: API and SPA

* **API** (`api/steward_routers.py`, next to the clear route at `:384-387`):
  `PUT /api/steward/groups/{channel}/{group_ref}/media-keep`, body `{on: bool}`,
  `require_writer`, owner session only (`self.steward._owner()`, as `clear_group_messages`).
  It calls `service.groups.set_group_media_keep(channel, group_ref, on)` and answers `{state,
  backend_rows_deleted?}`. An unbound group answers 404 `group_not_bound`.
* **`GET /api/steward/groups`** (`owner_view`): each card gains `media_keep: {state, since,
  note, attempts, purge_owed, kept_24h, last_purge: {at, reason, backend_rows, rows_before,
  files_unlinked, files_shared, bytes_freed} | null}`. In the one-group view, `kept_media`
  rows from the last day appear in `messages` with their `sender_label`, the placeholder
  body and no ref. `messages_30d` does not count them.
* **SPA**: `web/src/views/StewardGroups.svelte`, `web/src/lib/api.ts`
  (`setGroupMediaKeep`, next to `clearGroupRecords`, `:1024`) and
  `web/src/lib/i18n.svelte.ts`. Each group card gets a switch labelled
  「存群里没 @ 管家的图片、语音、文件（存 1 天；关掉即删除已存的）」. Its status text:
  * `off`: 关;
  * `on_pending`: 正在通知群里…, plus the note when it is waiting (群里本小时发送已满 /
    渠道未连接);
  * `on`: 开（自 HH:MM）, with 「今天存了 N 件」;
  * `off_notice`: 已关，正在删除 / 正在通知群里, then 已删除 N 件（M 件文件另有记录引用，未删）
    from `last_purge`.

  A fallback (`notice_not_sent`) reads 关 · 通知没发出去（开关仍是关）- one text for both notices,
  since a failed off-notice leaves the switch off too. The switch is disabled while
  the group is not bound. Changing the SPA means rebuilding the backend image.

### 10. `steward.md`

In `integrations/dsh-director/steward.md:138`, the sentence "A group wake's recent lists
that group's last exchanges, as data." becomes:

> A group wake's recent lists that group's last exchanges, as data; entries marked kept are
> files people posted in that group without addressing you, with who posted each (kept one
> day, at most twenty). Read one with media_call only when the question is about it. Those
> files exist only in that group's turns: with your owner, or in another group, you cannot
> use them - if your owner asks about one, say it was not sent to you and is only readable
> by mentioning you in that group.

That is English, like the rest of the file. The owner-turn half is enforced by `covers()`;
the sentence only stops the model from promising what code refuses. Changing `steward.md`
takes the steward's two-start check (design-steward 7.7.17 item 12).

### 11. What this build does not change

* **The bridge**: it still describes every un-mentioned medium of a bound room. With the
  switch off, the host refuses it before fetching, and the relay refuses `media_keep_off`
  and writes nothing. That costs one `groups.list()` read and one backend command per such
  medium, and no bytes. Passing the switch to the bridge's whitelist would save that, but it
  needs an image rebuild and the owner's QR scan, so it is left out.
* **The library's `inbound_per_channel_hour` (20)** still counts kept rows together with the
  owner's 1:1 rows, so a busy group with the switch on can use up the owner's hour. That is
  the open question already listed at the end of this document. The fix is to exclude
  `kept` rows from that count and give them a per-group bound. It is one filter in `ingest`
  (`lib/media.mjs:394-398`), but it is a bound decision for the director, not part of these
  rulings.
* `withAttachments`, `placeholder`, `attachmentLine`, the group ring, `send_media` budgets.

### 12. Tests (all in disposable containers)

Library, `integrations/dsh-director/test/media.test.mjs`:

* `covers`:
  * a kept row is usable by its group's turn;
  * it is NOT usable by an owner turn or another group's turn;
  * an addressed group row is still usable by an owner turn;
  * an owner row is still usable by an owner turn.
* `ingest` refuses `kept` with an `owner` or `project:` audience (`invalid_media`).
* Fake clock: a kept row resolves at 23 h 59 m and is `undefined` from `lookup` at 24 h + 1 s
  before any sweep; `cleanup()` then drops it; an addressed row of the same age stays.
* (i):
  * the same bytes as an owner 1:1 row, a group A kept row and a group B kept row: A's day
    expires A's row, the file still exists, and B's and the owner's refs still resolve;
  * after B's row and the owner's row go too, the file is unlinked.
* `purgeKept`:
  * it removes only that audience's kept rows (A's addressed row and B's kept row stay);
  * the receipt closes (`rows_after: 0`) with `files_unlinked` and `files_shared` counted
    correctly;
  * a file whose unlink is made to fail throws `purge_incomplete`.
* `withdraw` takes back a kept row and refuses an addressed inbound row.
* The protect set never contains a group ref.

Channels, `integrations/dsh-director/test/channels.test.mjs`:

* With a group bound and its switch `off`, `storeMedia({asked: false})` answers
  `unsupported` and `open` is never called. This pins (d): bound alone stores nothing.
* With the switch `on`, the row is stored with `kept: true`. With `asked: true` and the
  switch off, the row is stored without `kept` (R5).
* `handleGroup({asked: false})` relays with `kept: true`:
  * a `media_keep_off` answer withdraws the stored ref and ends `accepted: false`, with no
    wake and no line;
  * a `duplicate` answer withdraws nothing.
* The notice pass:
  * a `group_send_budget` check refusal makes no send and spends no attempt;
  * a disconnected channel makes no check, no send and spends no attempt;
  * a successful send is committed exactly once, and a lost commit answer is retried without
    a second send;
  * three failed sends answer `failed` three times;
  * an `off_notice` is not checked before its purge receipt.
* The sweep:
  * a failed `groups.list()` purges nothing, and neither does a malformed one;
  * an unlisted group's kept rows are purged;
  * an `on` group's rows stay.
* The wake keeps all twenty kept entries when the pairs overflow `recentChars`.

Plugin, `integrations/dsh-channel-wechat` tests: `asked` reaches `storeMedia`'s meta for a
`group_message`, and is absent for 1:1 and when the bridge omits it.

Adapter: `integrations/dsh-driver/runtime.group.test.mjs` or the adapter's own test
(`steward-host`) checks the request shapes of `relay` with `kept`, `mediaNotice`,
`recordPurge`, `expireMedia` and `purgeKept`. `host-conformance.test.mjs` checks the
`host.d.ts` additions. This build adds no model tool, so `tool-schemas.test.mjs` (the pinned
DSH `defineTool`) is unchanged and must still pass.

Backend, `tests/unit/test_steward_groups.py` (plus `test_steward_media.py` for the send
kinds):

* The switch's starting state:
  * a new bind is `off`;
  * on, then unbind, then rebind gives `off` with a fresh epoch;
  * `set_group_media_keep` is owner-session only, and steward and director tokens are
    refused.
* Every transition in section 1's table, including:
  * off during `on_pending` gives `off` with no notice owed;
  * on during `off_notice` gives `on_pending`.
* `group_media_notice`:
  * check refusals leave `media_keep_attempts` unchanged and set the note;
  * `sent` with the current epoch turns `on` and writes one `media_notice` row counted by
    `send_refusal`;
  * a `sent` replay with the same key does not transition twice;
  * `sent` for an on-notice after the owner turned off gives `off_notice` with a new epoch;
  * the third `failed` gives `off` with `notice_not_sent`;
  * the off check answers `purge_owed` until the receipt is recorded.
* `record_group_media_purge`:
  * `rows_after: 1` is refused;
  * a valid receipt appends one ledger row and sets `media_purged_at`;
  * a stale epoch appends but does not set it;
  * the command is steward-only.
* `relay_group_message(kept=True)`:
  * with the switch off it answers `media_keep_off` and writes no row;
  * with the switch on it writes `kind='kept_media'`, `accepted=0` and a member row;
  * 61 kept rows in an hour still leave the 60-question window open (both arms: a question
    after them is accepted, and the 61st question is still refused);
  * without attachments it answers `invalid_request`.
* `list_group_recent`:
  * 25 kept rows in group A list 20, newest, each with `sender`;
  * a 25-hour-old one is absent;
  * group B's are absent;
  * the pairs are still bounded by `limit`;
  * addressed unanswered media still appear by today's rule.
* Retention:
  * off deletes A's `kept_media` rows in the same transaction and leaves its questions and
    replies;
  * `expire_group_media` deletes only kept rows past 24 h.
* `owner_view` carries `media_keep` and the last receipt, does not count kept rows in
  `messages_30d`, and never answers a kept ref.
* `media_notice` is in `GROUP_SEND_KINDS`.
* `test_steward_groups.py`'s pinned `GROUP_LIMITS` gains `kept_recent_items: 20`.

API and SPA: a route test for `PUT .../media-keep` (owner yes, reader no), and
`web/src/__tests__/views/StewardGroups.test.ts` renders each of the four states, the waiting
note and the last receipt line.

### 13. Acceptance (the owner and one family member, in the family group)

1. The dashboard shows the family group's switch 关. A family member posts an image without
   `@`: no line in the group, nothing in 「今天存了」. The steward log shows
   `group_media_keep_off`, and no media fetch happens.
2. The owner turns the switch on. Within 30 s the group reads the on-notice, and only then
   does the dashboard read 开.
3. The member posts an image without `@`, then a second member writes 「@管家 这个图上是什么」.
   The steward answers from the image, and the wake's recent named the image with the first
   member's label.
4. The owner asks in his private chat 「刚才群里那张图写了什么」. The steward says it cannot
   see it (`media_call` was refused `not_visible_in_this_turn`, or was never attempted).
5. In a second bound group, a mention asking about "the picture" does not see group 1's
   image.
6. The owner turns the switch off. 正在删除 changes to 已删除 N 件, and then the group reads
   the off-notice. A new un-mentioned image is not kept, and the earlier ref answers
   `unknown_ref` in the next mention turn.
7. Turn it on again, keep one image, and wait 24 h: the next mention's recent no longer lists
   it, and the file is gone within the hour after that unless another row names it.
8. Unbind and rebind the group: the switch reads 关, and nothing is kept until it is turned on
   and the notice is sent again.

**Privacy (accepted residuals).** These are not deleted by the switch:

* text the steward derived from a kept file: its reply (in the group record, 30 days), a
  family-memory note a member asked for, and the raw session log;
* the bytes passed to the reading tool when a mention used the file.

The on-notice says the file is handed to an external model service when read.

### 14. Deploy

* **Backend** (`core/state_groups.py`, `core/state_commands.py`, `api/steward_routers.py`,
  `integrations/dsh-director/media-types.json`, which the backend reads too, and the SPA):
  rebuild the backend image and recreate the backend container. The SPA is baked into that
  image. The new columns default to `off`, so every group is off after the recreate (R2).
* **Steward host** (`integrations/dsh-director/{lib/channels.mjs, lib/media.mjs, index.mjs,
  steward.md, media-types.json, host.d.ts}`, `integrations/dsh-channel-wechat/index.mjs`,
  `integrations/dsh-driver/steward-host.mjs`): restart the STEWARD driver's process (the
  supervisor relaunches it with the same session; see the precondition section below). Do
  not restart the whole host. Then do the two-start check for `steward.md`.
* **Not touched**: the bridge image and container. There is no QR scan.
* **Order**: backend first, then the steward restart in the same window.
  * Between the two, the old host relays without `kept`, so the always-on behaviour
    continues for those minutes.
  * The reverse order fails closed: an old backend refuses `kept` as an unknown field, so
    nothing is kept, and the notice command does not exist yet.
* **Precondition to measure, not build**: whether the always-on build of 2026-11-22
  (`3835bf8466`) ever ran in production. If it did, its un-mentioned files are library group
  rows without `kept` (30 days, owner-covered) and backend `question` rows, and the group was
  never told. Before this deploy, measure that by the steward log's
  `group_message_accepted ... asked:false` lines. If there are any, ask the owner whether to
  delete them. The recommended answer is yes, using the same `purgeKept` path after marking
  those rows.

### 15. Files for the build

* `integrations/dsh-channel-wechat/index.mjs`: `asked` in `storeMedia` meta.
* `integrations/dsh-director/lib/channels.mjs`:
  * `keepsMedia` gate in `storeMedia`;
  * `kept` relay and withdraw in `handleGroup`;
  * the notice pass and the purge sweep in `pollStatus`;
  * the hourly `expireMedia` call;
  * `keptChars`.
* `integrations/dsh-director/lib/media.mjs`:
  * the `kept` marker;
  * `covers`;
  * `lookup` expiry;
  * `cleanup` for kept rows, run hourly;
  * `withdraw` for kept rows;
  * `purgeKept`.
* `integrations/dsh-director/media-types.json`: `limits.kept_hours: 24`.
* `integrations/dsh-director/index.mjs`: wake trimming spares kept entries.
* `integrations/dsh-director/host.d.ts`: `groups.relay` `kept`, `groups.mediaNotice`,
  `groups.recordPurge`, `groups.expireMedia`, the `list` additions, `media.purgeKept`.
* `integrations/dsh-director/steward.md`: section 10.
* `integrations/dsh-driver/steward-host.mjs`: `groupOps` (`relay` `kept`, `mediaNotice`,
  `recordPurge`, `expireMedia`) and `mediaOps.purgeKept`.
* `core/state_groups.py`:
  * the columns and the ledger table;
  * the constants, `GROUP_SEND_KINDS` and `GROUP_LIMITS.kept_recent_items`;
  * `set_group_media_keep`, `group_media_notice`, `record_group_media_purge` and
    `expire_group_media`;
  * the `relay_group_message` `kept` branch;
  * `list_group_recent`, `list_owner_groups` and `owner_view`;
  * `unbind_owner_group`, `_bind` and `_retain`.
* `core/state_commands.py`: request models and the steward-only set for the three host
  commands, and `kept` on `RelayGroupMessage`.
* `api/steward_routers.py`: `PUT /groups/{channel}/{group_ref}/media-keep`.
* `web/src/views/StewardGroups.svelte`, `web/src/lib/api.ts`, `web/src/lib/i18n.svelte.ts`.
* Tests:
  * `integrations/dsh-director/test/media.test.mjs`;
  * `integrations/dsh-director/test/channels.test.mjs`;
  * the `integrations/dsh-channel-wechat` test;
  * `integrations/dsh-driver/runtime.group.test.mjs` (or the adapter test);
  * `integrations/dsh-driver/host-conformance.test.mjs`;
  * `tests/unit/test_steward_groups.py`;
  * `tests/unit/test_steward_media.py`;
  * the API route test;
  * `web/src/__tests__/views/StewardGroups.test.ts`.
* This file.

### 16. As built (branch `claude/group-media-switch`)

Where the build differs from what sections 1-15 first said (the text above now says what was
built):

* Two more columns, `media_purge_reason` and `media_purge_rows`: the owed purge is "reason
  set", not "`media_purged_at` differs from `media_keep_at`", and the ledger row's reason and
  `backend_rows` come from them.
* `group_media_notice` names the notice (`on`/`off`); `media_notice` in the list carries
  `attempts` (the failed key). The check refuses `purge_owed` for the on-notice too.
* Unbind and rebind move the switch only when it was ever used.
* `owner_view` adds `purge_owed` (the SPA's 正在删除 / 正在通知群里).
* The library adds `keptAudiences()`; a purge that cannot unlink puts the rows back.
* An un-mentioned message without files is not relayed (`nothing_kept`).
* `messages_30d` and `last_message_at` both leave kept rows out.

## What the shipped code already settles

Measured on `f7a325a1fb` (the merge of the bridge half), and deliberately not re-decided
here:

* **Audience, retention and refs are already the library's**
  (`integrations/dsh-director/lib/media.mjs`): a row's audience is `owner`,
  `group:<channel>:<group_ref>` or `project:<id>`, and `covers(turnAudience, row)`
  (`:115-119`) is the one rule `media_call` and `send_media` apply - an owner turn covers
  every ref, a group turn only its own group's **(2026-11-23: except a kept medium, which only
  its own group's turns cover - Owner rulings section 8)**. Rows are kept 30 days **(a kept
  medium one day - section 4)** and at most
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
  needs no backend change. **(2026-11-23: the kept path does change the backend - a
  `kept_media` row kind, the switch and its commands; Owner rulings sections 1-3.)**
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
   **(2026-11-23: for an un-mentioned message `servesGroup` is replaced by the switch -
   `keepsMedia`, true only in state `on` - and the row carries `kept: true`; a bound group
   with the switch off fetches nothing. Owner rulings section 3.)**
   A room the host does not list is refused `unsupported` (the closed set has no other
   word for it) with nothing fetched; a host it cannot reach is `unavailable`, so the
   channel holds the message; a host without a media library or without groups stores
   nothing, as before.
4. **A later turn sees what it must read: implemented.** The medium an un-mentioned group
   message carries is relayed (item 5) as an un-answered inbound row whose
   `attachments_json` holds its kind, ref and - when the library knew them - mime, size and
   durationMs. The group wake's reader `list_group_recent`
   (`core/state_groups.py:1219-1254`) now returns, for its own channel and `group_ref` alone,
   that group's un-answered inbound rows carrying `attachments_json`, each as an entry of its
   own (`at`, `sender`, `q`, `attachments`) in the place its time gives it among the answered
   pairs; both parts keep their own last `limit` rows. **(2026-11-23: kept media are read from
   their own `kept_media` rows, one day, up to 20 with who sent each, independent of `limit`;
   the relay writes them as `kept_media`, not as accepted questions. Owner rulings sections 3
   and 7.)** The wake renders those attachments
   with the very `attachmentLine` the current message's own use, through
   `withAttachmentLines` (`integrations/dsh-director/lib/media.mjs:140-148`) applied at
   `integrations/dsh-director/index.mjs:414`. Measured by
   `tests/unit/test_steward_groups.py::test_a_later_mention_turn_sees_an_unmentioned_medium_in_its_group_alone`
   (group A's `recent` names the ref, group B's is empty) and by
   `integrations/dsh-director/test/media.test.mjs`'s test `a group wake's recent: a kept
   medium names its ref, a pair without attachments is untouched`.
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
  above), and the bridge keeps its own group ring separate from the 1:1 ring. On the wake
  path it is enforced by the reader's own scope - `list_group_recent` filters both its
  answered pairs and its kept media by one `channel` and one `group_ref`
  (`core/state_groups.py:1219-1254`) - and by the wake being built for that one group
  (`integrations/dsh-director/index.mjs:402-419`). Measured over the same records in two
  groups by
  `tests/unit/test_steward_groups.py::test_a_later_mention_turn_sees_an_unmentioned_medium_in_its_group_alone`:
  group A's `recent` names the kept medium's ref, group B's is empty.

## "Later", measured: bounded, not guaranteed

The owner's word is that a kept medium must be readable later, so this is what the library
actually promises a group row:

**(2026-11-23: superseded for kept media.** A kept medium is covered by its own group's turns
only - not by owner turns - and lives one day, exact at read time (`lookup`) and swept
hourly; the bullets below now describe addressed group media only. Owner rulings sections 4
and 8.)

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
  thing to change, not the rule that storage is not gated on the mention. (Still open after
  2026-11-23, now only for groups whose switch is on; the suggested change is in Owner
  rulings section 11.)
* Whether protecting a group's kept rows from retention (the "Later" section above) is
  wanted: it changes what the total-bytes bound evicts, which is an owner's decision, not
  a detail of this seam. (2026-11-23: with a one-day life the window is a day, not 30; still
  unprotected.)
* What a group quota refusal costs a kept medium: past `group_per_hour`/`member_per_hour`
  the relay keeps an `over_quota` row without `attachments_json`
  (`core/state_groups.py:795-796`), so the file is in the library but its history row does
  not name it, and the reader in item 4 (which returns only rows carrying that metadata)
  does not show it. This card stores the file and records it whenever the relay accepts the
  message. (2026-11-23: closed for kept media - a `kept_media` row is written past the
  question quotas and counted by none of them; Owner rulings section 3. It still holds for an
  addressed medium.)
