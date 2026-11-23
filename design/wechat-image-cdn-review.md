# Review of `design/wechat-image-cdn-diagnosis.md`

Reviewer did not write the diagnosis. Checked every code/log/number citation against the
repository at the bound commit and the retained log excerpt (894 bytes, sha256
`f0974ae531f7075ea851003002599526cd7f113b11beb9832a6d026db07df07b`).

## 1. Verdicts

### c1 — FAIL

Ground: the diagnosis does name the code path (`bridge.mjs` `fetchBox`, fork patch
`download_media`, `wechat-protocol` 2.2.0 `src/media.rs:547`/`:619`/`:635`/`:502`),
names the two differing fields (`filetype` 1 vs 5, and the `fileid` source), and
lists five refused attempts. But its one measured delay is wrong, and the required
arrival-to-fetch delay is not present in the retained bytes.

Counter-measurements:
- Retained excerpt has no message-arrival line. The three "login" lines are
  `login QR requested` (22:43:12.437967519Z), `login QR waiting` (22:43:16.858470428Z),
  `logged in` (22:44:24.711992537Z). The first fetch is at 22:47:51.671749310Z, i.e.
  3 min 26.96 s after `logged in`, not "3 min 27.09 s" as the diagnosis states.
- The five attempts span 22:47:51.671749310Z to 22:49:24.935768712Z = 93.26 s, not
  "92.26 s". The four intermediate spacings (9.44, 14.11, 24.59, 45.13 s) are correct.
- Because no arrival line exists, the delay between arrival and first fetch cannot be
  measured from these bytes; the diagnosis's "3 min 27.09 s after logged in" is the
  nearest available bound and is misstated.

### c2 — PASS

The diagnosis states that the bytes are already fetched on arrival (store-before-hand-on
path), gives the per-medium cost (one CDN POST, the channel's retries while held,
one library write) and the bounds (`MEDIA_LIMITS` per kind, 30-day retention and
`limits.total_bytes`), and lists three observations that would settle the open
question. The open items below are factual errors, not gaps in the c2 content.

## 2. Sentences that are not measured

Each is quoted with what is missing and whether the diagnosis labels it.

1. §1: "The successful file download is the issue's own measurement (107,089 bytes on
   2026-11-23)..." — missing: any file attempt in the retained excerpt (there is none);
   not labelled as an inference; the diagnosis does note it is outside this window.
2. §4: "...and the excerpt records no arrival line between them." — measured against
   the excerpt (true); the following "0 matching lines measured on 2026-11-23" for the
   host journal is not verifiable from the provided bytes and is not labelled as an
   inference.
3. §4: "The later four are the channel's own retries of the same request..." — code
   supports the retry loop with a growing pause, but the log does not show a retry
   reason; not labelled as an inference.
4. §5: "Ruled out ... 'The bridge fetched lazily, long after the message.'" — the
   retained excerpt contains no arrival line, so "long after" cannot be measured; the
   code path (store before hand-on) supports the conclusion, but the sentence is not
   labelled as an inference.
5. §5: "the medium was described and accepted by the channel's `MEDIA_LIMITS`" — the
   size bound is the bridge's `MEDIA_LIMITS`, not the channel plugin's; the acceptance
   happened, but the attribution is wrong (see §3).
6. §6: "moving the fetch to the steward's read would only move the same failing POST
   later (and add a stall to the steward's turn)" — not measured; not labelled as an
   inference.
7. §6: "plus one library write" — the write happens in `media.ingest`, but no line
   or count is cited; not labelled as an inference.
8. §6 observations 1-3 and §7 bullets — by construction not measured; they are
   proposed observations and undetermined items, not facts, and the diagnosis does not
   claim they are measured.

## 3. Factual errors

- Delay from `logged in`: stated "3 min 27.09 s"; measured 3 min 26.96 s (206.96 s).
- Span of the five attempts: stated "92.26 s"; measured 93.26 s (93.264).
- Pin attribution: `integrations/wechat-bridge/neo-fork/pin.json` pins `puppet-neo`
  0.1.1 (upstream commit ddfa5ecd...), not `wechat-protocol` 2.2.0. The crate version
  2.2.0 is declared in `crates/wechat-protocol-2.2.0/Cargo.toml` (and its
  `wechat-media`/`wechat-platform` deps are pinned `=2.2.0` there).
- `MEDIA_LIMITS` attribution: the limits cited as "the channel's" are defined in
  `integrations/wechat-bridge/bridge.mjs` (`MEDIA_LIMITS`, frozen, image 20 MiB etc.).
  The channel plugin does not define `MEDIA_LIMITS`.
- "0 matching lines measured on 2026-11-23" for the host journal: not checkable from
  the provided bytes; should be marked as unverified.

## 4. File-path claim this window cannot support

The retained excerpt contains no file attempt, so the diagnosis correctly states that
the working file download is not in this window and uses the issue's own figure
(107,089 bytes, 2026-11-23) only as the stated contrast. That handling is sound;
the figure itself is not measured here and is listed in §2 item 1.

## 5. What I could not check

- The host journal for 2026-11-22/23 (the "0 matching lines" claim).
- Whether any of the five attempts' CDN response bodies were logged anywhere.
- The actual `cdnbigimgurl`/`cdnmidimgurl` shape for this account (owner content, not
  logged).
- The 2.9.0 crate copy was inspected only to confirm the image path exists in both;
  judgement of the diagnosis is against 2.2.0 as required.
- No tests were run; none are needed for this review.
