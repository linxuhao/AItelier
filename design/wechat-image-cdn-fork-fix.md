# WeChat image CDN: fork fix (both ids, one retry, two redacted diagnostic lines)

Issue `iss-4ca5248eaf7e4e24`. Companion to `design/wechat-image-cdn-diagnosis.md`. Every
number below is a line of the delivered patch
`integrations/wechat-bridge/neo-fork/0001-incoming-message-types.patch` — the file whose
digest `pin.json` pins, so its numbering is the one `git apply` reads. Where a number is
meant for the applied source instead, the sentence says so.

## Where the two ids are read

`src/puppet-neo.ts`, `imageMedia()` (patch line 1232): `cdnbigimgurl` is read first as
`primaryId` (1234); `cdnmidimgurl` is read second as `secondaryId` (1235). `fileId` stays
`primaryId || secondaryId` (1236), so a payload carrying only the mid id still works. The
second candidate is carried as `fallbackFileId` only when it is present and different from
the primary (`...(secondaryId && secondaryId !== primaryId ? { fallbackFileId: secondaryId } : {})`,
1244), and crosses the napi boundary as an optional last argument: the `downloadMedia` type
(1055), its implementation (1079), and `messageImage()`'s call (1442).

## How the order is decided

`cdnbigimgurl` is the id the image branch already sent (diagnosis §2), so it remains the
first attempt (patch lines 1234, 1236). `cdnmidimgurl` is the payload's second candidate
and is tried only after the first attempt is refused (465-468). This tree measures that
order and the single retry; which of the two ids the CDN serves is not measurable here
(see the last section). No third candidate and no reordering.

## Where the retry happens

`native/src/lib.rs`, `Operation::DownloadMedia` handler (patch lines 405-491): the napi
`download_media` takes `fallback_file_id: Option<String>` as its last argument (patch lines
190-196) and threads it into the operation (variant at 105-111, constructed at 198-204).
The task runs the CDN download in a loop guarded by `attempted_fallback` (the flag at 435,
the guard at 465-468), so the second id is tried at most once and only for an image
(`quality == MediaQuality::Original`, 429). A refused first attempt (`download` returns
`Err`, 464-468) triggers exactly one retry with the fallback id (469-477: the flag is set,
`mid_size` is set, the request's quality switches to `MediaQuality::Standard` — the crate's
mid-size profile — `file_id` is reassigned to the fallback id, and `continue` runs the loop
again); any other outcome, or a non-image, or no distinct fallback, returns the error
immediately (479). The quality switch is the point of the retry: resending the same pair —
the full-size id under `Original`'s `filetype` 1 — would repeat the request the CDN just
refused, so the second attempt asks for the mid-size id under the crate's `Standard`
profile, whose `filetype` is 2 (475, with that reasoning at 471-474). The descriptor
the download is built from names `file_id` (446), so the second download uses the id 476
assigned. No third candidate, no unbounded loop.

## Diagnostic lines

Two lines, both emitted with `eprintln!` from inside the attempt loop. The first is one
per download attempt, before the request is made: patch lines 441-444, which are applied
`native/src/lib.rs` lines 646-649.

```
neo cdn download: variant={variant} quality={quality:?} filetype={filetype} downpicformat=1 size_class={size_class} file_id_len={} has_slash={shape_slash} has_query={shape_query}
```

The second is emitted only once a download has been answered successfully: patch lines
458-461, applied lines 663-666.

```
neo cdn download served: variant={variant} filetype={filetype} file_id_len={} has_slash={shape_slash} has_query={shape_query}
```

The `variant` field is the string literal the handler assigned (438): `"primary"` for the
full-size candidate and `"mid"` for the mid-size one, so the second attempt's line and its
served line both say `variant=mid`. Between them the lines name the request fields
(variant, quality, filetype, downpicformat, media size class) and the id's shape (its
length, 443 and 460; whether it contains `/` or `?`, 439-440), and the served line names
which of the two ids answered. A refused image attempt therefore writes the first line
twice — `variant=primary` with `filetype` 1, then `variant=mid` with `filetype` 2 — and the
second line once, when the mid-size download answers.

The `filetype` is not a `match` the patch writes over `MediaQuality` arms: the handler
asks the crate for it (`let filetype = quality.filetype();`, 437), and the crate's own
mapping is `Original` 1, `Standard` 2, `Thumbnail` 3, `Video` 4, `File` 5, `LargeFile` 7,
so a variant added upstream cannot silently escape it. What this handler can send is 1 on
a full-size image attempt, 2 on the mid-size retry, and 5 or 7 for a file; it never asks
for 3 or 4. Neither line ever contains the id value, the AES key, any message/file bytes, or
a full URL.

## Surfaces touched

- TypeScript: `src/puppet-neo.ts` `imageMedia()` and `messageImage()` (patch lines 1232, 1439).
- napi signature: `src/native.ts` `downloadMedia` type and implementation (patch lines 1055, 1079).
- Rust task: `native/src/lib.rs` `download_media`, `Operation::DownloadMedia`, handler (patch lines 190, 105, 405).

## What this environment cannot measure

Whether the CDN accepts either id. That needs a rebuilt native module and a fresh login
(QR scan) after this change; the next live attempt after the rebuild and the login decides.
