# Strict code patch editing

## Effective contract

`apply_patch(patch, references)` is a native SkillFlow code mutation tool with two ways to address an edit: a V4A patch envelope, and reference hunks that cite a digest the `read` issued. SkillFlow 1.5.80 owns its grammar, exact matching, repository path checks, preflight, per-file atomic writes, mutation receipts, schema, and user-facing lifecycle wording. AItelier does not carry a second parser or filesystem implementation.

AItelier explicitly grants the tool to generic code-writing roles. On those steps the host exposes `apply_patch` in place of generic `create`, `edit`, `write`, and `repo_remove_file`; fixed-slot and artifact steps keep their narrower interfaces. AItelier also checks every parsed path against the task card before dispatch, strips caller-supplied roots, accounts all returned paths, and pins the tested SkillFlow version.

```text
*** Begin Patch
*** Update File: src/example.py
@@
 def answer():
-    return 1
+    return 2
*** Add File: tests/new_test.py
+assert answer() == 2
*** Delete File: obsolete.txt
*** End Patch
```

## Reference mode

An editor that makes the caller reproduce the original text turns "is my copy
of this file still accurate?" into a question with no machine answer: the only
way to settle it is to read the file again. Measured on
attempt-c5aae131eaa246d0bc6d7c72864aea0f (2026-09-20): 13 reads, 8 searches, 1
semantic search, zero writes, against a 44,602-character target file and a
24,000-character read window — the agent could never hold the whole file in one
observation, so it could never be sure its context was current. Only 3.9% of
that step's reasoning was original file text, so the cost was never
transcription; it was the doubt.

`references` removes the doubt by making it a precondition the engine checks.
Every `read` result carries a `citation`:

```json
{"path": "src/example.py", "start_line": 120, "end_line": 160,
 "start_byte": 4096, "end_byte": 5312, "start_col": 0, "end_col": 18,
 "sha": "…", "citable": true}
```

The `sha` is issued by the engine over exactly the text that read sent, keyed
with a secret that is generated per process and never emitted. A caller
therefore cannot mint one even while holding the original text, which is what
keeps reference mode from degrading into editing coordinates the agent never
actually read. Issuance is also recorded per run: a digest that is
arithmetically plausible but was never served in this run is refused.

A reference hunk quotes the digest and supplies only the new text. Omit
coordinates to replace exactly the served window:

```json
{"file": "src/example.py", "sha": "…", "new_text": "    return 2"}
```

To narrow a wider read, supply `from_line` and `to_line` using the absolute,
1-based file line numbers printed by the read. Omit columns to replace whole
lines; both line numbers are required together. The read API itself pages with
0-based `start_line`; its returned citation uses 1-based file coordinates.

A cut inside a line adds 0-based character columns with exclusive `to_col`.
The first call writes nothing and returns `spans`: the exact covered text,
`keeps_before`/`keeps_after`, and a span sha for those coordinates. Inspect
that text, then resend with its span sha; incorrect columns must be corrected
before applying. A span sha with only `new_text` also replaces that span.
`from == to` inserts at a point. A multiline point insertion at column 0 of
a nonempty line without a trailing newline is refused; a one-line prefix,
a newline-terminated insertion, an empty-line insertion or a nonzero-column
insertion remains valid after span confirmation.

Every range in a batch resolves against one snapshot. The engine orders the
edits and refuses overlap; callers do not compensate for drift. A sha this run
did not issue is refused. With an intact journal of this run own writes,
the cited range is translated and checked against the served text; changes
elsewhere in the read window may be allowed. Removed ranges, an insertion
inside a window, or a point touching an earlier insertion are refused.
A framed citation is refused when a content change is outside that journal
or its history is unavailable: its position cannot be proved. Legacy citations
issued without a frame retain the strict whole-window comparison. Reread
after refusal. There is no fuzzy matching.

Applied reference and Update edits return `echo`, read back from disk around
the result, with absolute file line numbers. Reference results also retain
`replaced`. The run trace stores the apply_patch echo exactly as returned.

A file may appear in `patch` or in `references` for a given call, not both.

Each update hunk starts with bare `@@`. Space-prefixed lines are unchanged context, minus removes, and plus adds. Multiple file operations and multiple ordered, non-overlapping hunks are allowed. Each path appears in one operation only. Every hunk matches the original file for that call globally and exactly once. There is no fuzzy matching. Unsupported line-number headers, moves, renames, EOF markers, malformed lines, absolute paths, traversal, `.git`, symlinks, and non-regular targets are rejected.

Before writing, SkillFlow parses and prepares the complete batch, validates paths and snapshots, and checks exact matches. A preflight failure changes no file. Each subsequent file publication is atomic, but the batch is not a transaction: an I/O failure can leave earlier files changed. Results expose `written`, `deleted`, and `partial`, and echo Update/reference edits already written; callers must inspect those paths before repairing the remainder. Preflight refusal has no write or echo.

Limits are 128 files, 2 MiB UTF-8 patch text, 1024 hunks per updated file, 16 MiB per source/result file, and 64 MiB combined source/result content during preflight.

## Read and lifecycle rules

Source `read` results also return `file_byte_sha256` and `byte_size` for the
complete raw file bytes, computed from the same buffer used for the served
content. These identify the file even when the response is paged or numbered;
`citation.sha` remains the engine-issued edit authorization for the served
window and is not a raw file hash. No additional read permission is granted.

Every citable `read` issues a citation for the window it served: read the intended range, cite its `sha`, and send the new text. Coordinates may be omitted; explicit columns require the span confirmation above. Inspect the resulting echo before the next edit. When a V4A hunk is used instead and comes back stale or ambiguous, the remedy is the same citation — reread that range and cite it — rather than retyping the current text or widening the copied context. `raw=true` remains the way to obtain patch context when a diff is genuinely the right shape; default numbered output is for navigation and must not be copied into a patch. Do not approximate whitespace or line endings.

For `output.target: code`, a successful mutation immediately changes the run's uncommitted worktree. It does not mean validation, review, commit, or delivery passed. Artifact `create`/`edit` is different: it writes the step's staged candidate and is promoted only after confirmation. When reading an artifact step's own staged candidate, use `source="self"`; this is not the code-worktree `apply_patch` lifecycle.

## Ownership and deployment boundary

SkillFlow owns:

- `skillflow.strict_patch` parsing and application;
- `skillflow.citations`: digest issuance, per-run ledger, and the refusal of any digest this run did not issue;
- native `tools/apply_patch` implementation and schema;
- exact-match, path-jail, snapshot, preflight, write/delete, and receipt semantics;
- generic `read`/`create`/`edit` schema wording for staged artifacts and direct code worktrees.

AItelier owns:

- task-card path authorization and complete-batch refusal;
- host root injection, role grants, tool filtering, loop accounting, and prompt guidance;
- pipeline-forge guidance for newly generated code roles;
- its exact `skillflow-py==1.5.86` package pin.

Existing saved generated pipelines are historical inputs and are not silently rewritten. New/reloaded definitions receive only the tools their role configuration explicitly grants.

Deployment requires the official SkillFlow 1.5.86 package, the AItelier image resolving that exact pin, and observed integration and regression results on the fresh image.
