# WeChat image CDN: fork fix (both ids, one retry, one redacted diagnostic)

Issue `iss-4ca5248eaf7e4e24`. Companion to `design/wechat-image-cdn-diagnosis.md`.

## Where the two ids are read

`src/puppet-neo.ts`, `imageMedia()` (patch line ~1192): `cdnbigimgurl` is read first as
`primaryId`; `cdnmidimgurl` is read second as `secondaryId`. `fileId` stays
`primaryId || secondaryId`, so a payload carrying only the mid id still works. The second
candidate is carried as `fallbackFileId` only when it is present and different from the
primary (`...(secondaryId && secondaryId !== primaryId ? { fallbackFileId: secondaryId } : {})`).

## How the order is decided

`cdnbigimgurl` is the id the image branch already sent (diagnosis §2), so it remains the
first attempt. `cdnmidimgurl` is the alternate representation's id and is tried only after
the first is refused. No third candidate and no reordering.

## Where the retry happens

`native/src/lib.rs`, `Operation::DownloadMedia` handler (patch line ~422/607): the napi
`download_media` now takes `fallback_file_id: Option<String>` (patch line ~206) and threads
it into the operation. The task runs the CDN download in a loop guarded by
`attempted_fallback`, so the second id is tried at most once and only for an image
(`quality == MediaQuality::Original`). A refused first attempt (`download` returns `Err`)
triggers exactly one retry with the fallback id; any other outcome, or a non-image, or no
distinct fallback, returns immediately. No third candidate, no unbounded loop.

## Diagnostic line

One line per attempt, emitted with `eprintln!` before the POST:

```
neo cdn download: quality=<MediaQuality> filetype=<1|5|7> downpicformat=1 size_class=<small|large|unknown> file_id_len=<n> has_slash=<bool> has_query=<bool>
```

It names the request fields (quality, filetype, downpicformat, media size class) and the
id's shape (length, whether it contains `/` or `?`). It never contains the id value, the
AES key, or any message/file bytes.

## Surfaces touched

- TypeScript: `src/puppet-neo.ts` `imageMedia()` and `messageImage()` (patch lines ~1192, ~1398).
- napi signature: `src/native.ts` `downloadMedia` type and implementation (patch lines ~1017, ~1041).
- Rust task: `native/src/lib.rs` `download_media`, `Operation::DownloadMedia`, handler (patch lines ~206, ~125, ~422).

## What this environment cannot measure

Whether the CDN accepts either id. That needs a rebuilt native module and a fresh login
(QR scan) after this change; the next live attempt decides.
