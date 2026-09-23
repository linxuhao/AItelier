# Writing Bench review evidence

## Review materials and targets

A review has one immutable `review_key` and an ordered `targets` list. Every target
identifies a chapter by number, title and the SHA-256 of its exact proposed prose.
`review_request.json` is the entry point; `current_prose.md` contains only the
proposed chapter(s). `review_context.md` holds the frozen baseline, applicable
rulings and author intent. A ledger audit also requires `proposed_ledgers.md`.
A multi-chapter revision requires every submitted chapter, not just the last one.

The two reviewers receive separate material sets with distinct phase keys. The
extractor, when needed, does not write either review. Supplied author ledgers are
still preserved exactly. Context and display artifacts are subject to the project
byte budget; an oversized packet is refused rather than silently shortened.

Material wrappers exist only in review artifacts. They never change canonical
prose. Each wrapper identifies its phase-bound source; its exact bytes are checked
against the frozen input. Review findings remain the reviewer's judgment, not
instructions embedded in the manuscript.

## What counts as presented

The native reviewer installs a host-only observation session. After a successful
provider call, the gateway passes the final request messages to that session.
These are **after** native-history projection and request sanitization. A full raw
tool result preserved in a trace is not evidence that its hidden tail reached the
model. A failed provider call does not add coverage.

The session counts exact material text supplied inline, and exact windows returned
by an authorized material read. Native numbered/raw reads are compared with the
frozen file and their actual returned range. The requested `end_line`, a model's
claim, a file hash alone, a filename, or an echoed last sentence grants nothing.
Overlapping reads merge; holes remain visible, including a missing middle between
a read beginning and a read ending. A recalled observation counts only the source
window actually contained in that observation: a complete recall of a partial
source read is still partial coverage.

The bounded reader is:

```
novel_bench_read(path="review/current_prose.md", start=0, length=8000)
```

Use the returned `next_start` for another character window. The same interface
accepts `review/review_context.md` and, for the auditor,
`review/proposed_ledgers.md`. Its complete JSON reply stays below the native
projection threshold. Existing `novel/` paths still refer only to the frozen
baseline. Content already fully presented inline need not be read a second time.

Coverage is a record of presentation, not a claim that software can prove a
model understood prose. Target-first layout reduces confusion; an explicit
`reviewed_chapters` list rejects stale or wrongly identified reports. The reviewer
still has to assess current scenes and causal consequences, and the director
still has to read the candidate before approving it.

## Verdict and certificate

A verdict includes `review_key`, the exact `reviewed_chapters` target list,
`passed`, `read_complete`, `feedback`, and structured `findings`. Positive or
complete verdicts must have complete host-observed coverage. The output tool
refuses an incomplete verdict and gives concrete missing material windows. The
reviewer may instead honestly reject with `passed=false, read_complete=false`.
Correct a report by writing its complete JSON, not by changing a fragment that
would invalidate the report-bound evidence.

The certificate is written by the host into the run's private reading-evidence
store, not by an agent output tool. It binds material identities, observed ranges,
the exact report hash, the run, reviewer step instance and host session. A new
executor starts a new session; a restored conversation contributes only when it
is actually presented again. A late superseded executor cannot issue the active
certificate. An accepted coverage certificate is not an acceptance of the novel.

The domain gates independently require the certificate. Bypassing the native
write guard or emitting schema-valid `passed=true` through another output path
cannot create an approvable stage. The stage hashes both the verdict and its
reading evidence. The two review roles use native mode without an unobserved
JSON fallback.

An explicitly requested literary-review reuse is valid only for identical frozen
literary dependencies and the same observed-material protocol. It preserves the
original report and certificate with their provenance; it does not pretend a new
model review occurred. Ledger changes still require a new ledger audit. Changing
prose, context, policy, or review code invalidates pending review dependencies.

Already accepted commits remain archival facts. Backup-only recovery validates
the original immutable receipt chain, exact accepted tree and files, current
private-remote policy, and authenticated backup result. It does not rerun a
chapter, retroactively certify old self-reported reads, or enable promotion of an
old unaccepted stage under the new review contract.

## Verification and rollout

Tests include a synthetic regression run against the original implementation,
initial full/prefix presentation, numbered and raw pages, middle gaps, false EOF
claims, projected-away tool output, nested recall, source or target changes,
executor replacement, schema-valid unobserved reviews, and the actual gateway
and native output guards. The existing manual-approval, immutable ledger,
multi-chapter revision and backup-recovery cases remain required.

Exercise a real accepted chapter in a private, isolated review-only harness before
rollout. Record exact code/prose/context hashes and actual model inputs. Do not
modify the accepted chapter or run its promotion/backup to test the reader.
Deploy only the reviewed code commit and coordinate process restart with directors
whose work may be in flight. Code test success, actual live reviewer success and
production activation are separate facts.
