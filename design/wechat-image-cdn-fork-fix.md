# WeChat image CDN: fork fix (standard profile first, both ids, two redacted diagnostic lines)

Issue `iss-4ca5248eaf7e4e24`. Companion to `design/wechat-image-cdn-diagnosis.md`. Every
number below is a line of the delivered patch
`integrations/wechat-bridge/neo-fork/0001-incoming-message-types.patch` — the file whose
digest `pin.json` pins, so its numbering is the one `git apply` reads. Where a number is
meant for the applied source instead, the sentence says so.

## Where the two ids are read

`src/puppet-neo.ts`, `imageMedia()` (patch line 1244): `cdnbigimgurl` is read first as
`primaryId` (1246); `cdnmidimgurl` is read second as `secondaryId` (1247). `fileId` stays
`primaryId || secondaryId` (1248), so a payload carrying only the mid id still works. The
second candidate is carried as `fallbackFileId` only when it is present and different from
the primary (`...(secondaryId && secondaryId !== primaryId ? { fallbackFileId: secondaryId } : {})`,
1256), and crosses the napi boundary as an optional last argument: the `downloadMedia` type
(1067), its implementation (1091), and `messageImage()`'s call (1454).

## How the order is decided

Which profile the CDN answers was measured, not assumed. On a live bridge one 194-character
image id was asked for as the full-size picture first: at 09:59:11.53Z the attempt logged
`quality=Original filetype=1` and the CDN refused it after 2.81 s (`CDN HTTP status 400`).
The same id, unchanged, was then asked for as `quality=Standard filetype=2` at 09:59:14.34Z
and served the picture; the served line follows at 09:59:20.31Z, 5.97 s after that request
left. So the standard profile is an image's first attempt (patch line 441) and the full-size
profile follows only a refusal (442). `cdnmidimgurl` is the payload's second candidate and is
tried only when it is present and different from the primary, as the last resort (443-445); the
two attempts that ran both carried the payload's own id, so that arm was never reached, and
whether this payload carried a second id at all is not measured. This tree pins the order
the measurement showed; which of the two ids the CDN serves is not measurable here (see the
last section). No third candidate, no thumbnail profile.

## Where the retry happens

`native/src/lib.rs`, `Operation::DownloadMedia` handler (patch lines 405-503): the napi
`download_media` takes `fallback_file_id: Option<String>` as its last argument (patch lines
190-196) and threads it into the operation (variant at 105-111, constructed at 198-204).
The handler builds the list of attempts (`candidates`, 439-450) and then walks it, one
download per turn (loop at 453-489). The list is an image's ladder or, for anything else,
its one attempt: gated on `is_image` (`quality == MediaQuality::Original`, 438), an image
queues the same id at `MediaQuality::Standard`, labelled `primary-standard` (441), then the
same id at `Original`, labelled `primary-original` (442), and finally the fallback id at
`Standard`, labelled `mid`, when `fallback_file_id` is `Some` and differs from `file_id`
(443-445). Every other profile queues one entry — the id and the quality the caller asked
for, labelled `primary` (449). The loop reads the filetype from the crate for each attempt
(`let filetype = this_quality.filetype();`, 454) and builds each request from that attempt's
own id and profile (descriptor, 467-475; `file_id: this_id.clone()`, 468; `quality:
this_quality`, 470), so the first request really asks for `filetype` 2 and the second for
`filetype` 1. Only an answered download ends the walk (`Ok`, 477-486, `break` at 485); a
refusal is recorded and the next candidate runs (`Err(err) => last_refusal = Some(err)`,
487), which is why the `Original` request can only be sent after the `Standard` one was
refused. When every candidate was refused the last refusal is returned (490-493). No third
candidate, no unbounded loop.

## Diagnostic lines

Two lines, both emitted with `eprintln!` from inside the attempt loop. The first is one
per download attempt, before the request is made: patch lines 463-466, which are applied
`native/src/lib.rs` lines 668-671.

```
neo cdn download: variant={variant} quality={this_quality:?} filetype={filetype} downpicformat=1 size_class={size_class} file_id_len={} has_slash={shape_slash} has_query={shape_query} has_amp={shape_amp} has_equals={shape_equals} all_hex={shape_hex}
```

The second is emitted only once a download has been answered successfully: patch lines
480-483, applied lines 685-688.

```
neo cdn download served: variant={variant} filetype={filetype} file_id_len={} has_slash={shape_slash} has_query={shape_query}
```

The `variant` field is the label the candidate carries (441, 442, 445): `primary-standard`
for an image's first attempt, `primary-original` for its second, `mid` for the fallback id's
last resort, `primary` for a file's one attempt. Between them the lines name the request
fields (variant, quality, filetype, downpicformat, media size class) and the id's shape (its
length, 465 and 482; whether it contains `/`, `?`, `&` or `=`, and whether it is plain
hexadecimal, 455-462), and the served line names which attempt answered. A refused image
attempt therefore writes the first line as `variant=primary-standard` with `filetype` 2 and,
after the refusal, as `variant=primary-original` with `filetype` 1; the second line follows
once a profile answers it. A payload carrying no distinct second id never reaches
`variant=mid`, because that candidate is only queued behind the distinct-id test (443-444).

The `filetype` is not a `match` the patch writes over `MediaQuality` arms: the handler asks
the crate for it (454), and the crate's own mapping is `Original` 1, `Standard` 2,
`Thumbnail` 3, `Video` 4, `File` 5, `LargeFile` 7, so a variant added upstream cannot
silently escape it. What this handler can send is 2 on an image's first attempt and on the
fallback id's last resort, 1 when the full-size profile follows a refusal, and 5 or 7 for a
file; it never asks for 3 or 4. Neither line ever contains the id value, the AES key, any
message/file bytes, or a full URL.

## Surfaces touched

- TypeScript: `src/puppet-neo.ts` `imageMedia()` and `messageImage()` (patch lines 1244, 1451).
- napi signature: `src/native.ts` `downloadMedia` type and implementation (patch lines 1067, 1091).
- Rust task: `native/src/lib.rs` `download_media`, `Operation::DownloadMedia`, handler (patch lines 190, 105, 405).

## What this environment cannot measure

The numbers above were measured on a probe image through a live bridge, not on this artifact.
These bytes have been compiled: the bridge image's native stage applies this patch to the pinned
commit, builds the crate, runs its tests, and the addon it produces carries `primary-standard`
and `primary-original` and no `primary-thumbnail`. What has not happened is a picture fetched
through the rebuilt image, so what the numbers show is the order the CDN answered in that one
session.

The boundary left open is the thumbnail profile: an id that refuses both `Standard` and
`Original` has never been tried against `Thumbnail`, so that candidate is absent rather than
guessed at. Confirming it, or the fallback id's arm, needs a rebuilt native module, a fresh login
(QR scan) and another live image, and the next live attempt after the rebuild decides.
