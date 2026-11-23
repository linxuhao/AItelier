# WeChat image CDN: fork fix (both ids, one retry, one redacted diagnostic)

Issue `iss-4ca5248eaf7e4e24`. Companion to `design/wechat-image-cdn-diagnosis.md`. Every
number below is a line of the delivered patch
`integrations/wechat-bridge/neo-fork/0001-incoming-message-types.patch` — the file whose
digest `pin.json` pins, so its numbering is the one `git apply` reads. Where a number is
meant for the applied source instead, the sentence says so.

## Where the two ids are read

`src/puppet-neo.ts`, `imageMedia()` (patch line 1212): `cdnbigimgurl` is read first as
`primaryId` (1214); `cdnmidimgurl` is read second as `secondaryId` (1215). `fileId` stays
`primaryId || secondaryId` (1216), so a payload carrying only the mid id still works. The
second candidate is carried as `fallbackFileId` only when it is present and different from
the primary (`...(secondaryId && secondaryId !== primaryId ? { fallbackFileId: secondaryId } : {})`,
1224), and crosses the napi boundary as an optional last argument: the `downloadMedia` type
(1035), its implementation (1059), and `messageImage()`'s call (1422).

## How the order is decided

`cdnbigimgurl` is the id the image branch already sent (diagnosis §2), so it remains the
first attempt (patch lines 1214, 1216). `cdnmidimgurl` is the payload's second candidate
and is tried only after the first attempt is refused (451-454). This tree measures that
order and the single retry; which of the two ids the CDN serves is not measurable here
(see the last section). No third candidate and no reordering.

## Where the retry happens

`native/src/lib.rs`, `Operation::DownloadMedia` handler (patch lines 405-471): the napi
`download_media` takes `fallback_file_id: Option<String>` as its last argument (patch lines
190-196) and threads it into the operation (variant at 105-111, constructed at 198-204).
The task runs the CDN download in a loop guarded by `attempted_fallback` (the flag at 431,
the guard at 451-454), so the second id is tried at most once and only for an image
(`quality == MediaQuality::Original`, 429). A refused first attempt (`download` returns
`Err`, 448-450) triggers exactly one retry with the fallback id (455-457: the flag is set,
`file_id` is reassigned to the fallback id, and `continue` runs the loop again); any other
outcome, or a non-image, or no distinct fallback, returns the error immediately (459). The
descriptor the download is built from names `file_id` (440), so the second download uses the
id 456 assigned. No third candidate, no unbounded loop.

## Diagnostic line

One line per attempt, emitted with `eprintln!` before the download is made: patch lines
435-438, which are applied `native/src/lib.rs` lines 640-643.

```
neo cdn download: quality=<MediaQuality> filetype=<1|5|7> downpicformat=1 size_class=<small|large|unknown> file_id_len=<n> has_slash=<bool> has_query=<bool>
```

It names the request fields (quality, filetype, downpicformat, media size class) and the
id's shape (its length, 437; whether it contains `/` or `?`, 433-434). It never contains the
id value, the AES key, or any message/file bytes.

## Surfaces touched

- TypeScript: `src/puppet-neo.ts` `imageMedia()` and `messageImage()` (patch lines 1212, 1419).
- napi signature: `src/native.ts` `downloadMedia` type and implementation (patch lines 1035, 1059).
- Rust task: `native/src/lib.rs` `download_media`, `Operation::DownloadMedia`, handler (patch lines 190, 105, 405).

## What this environment cannot measure

Whether the CDN accepts either id. That needs a rebuilt native module and a fresh login
(QR scan) after this change; the next live attempt after the rebuild and the login decides.
