# Strict code patch editing

## Effective contract

`apply_patch(patch, references)` is a native SkillFlow code mutation tool with two ways to address an edit: a V4A patch envelope, and reference hunks that cite a digest the `read` issued. SkillFlow 1.5.78 owns its grammar, exact matching, repository path checks, preflight, per-file atomic writes, mutation receipts, schema, and user-facing lifecycle wording. AItelier does not carry a second parser or filesystem implementation.

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

A reference hunk quotes the digest and supplies only the new text:

```json
{"file": "src/example.py", "sha": "…", "from_line": 131, "from_col": 11,
 "to_line": 131, "to_col": 12, "new_text": "2"}
```

Lines are 1-based and must lie inside the cited window; columns are 0-based
character offsets and `to_col` is exclusive, so `(L,0)..(M,len(line M))`
replaces whole lines and `from == to` inserts at a point. Every range in one
call resolves against the same file snapshot and the engine orders them, so the
caller neither sorts by descending line number nor compensates for drift inside
the batch. Ranges must not overlap. Changing one word cites one line: a
reference is a hunk, not a file rewrite. A cited window whose text has changed
since the digest was issued is refused outright — no fuzzy match, no fallback,
no partial write. The V4A limits apply unchanged: 128 files, 1024 references
per file, 2 MiB of references.

A file may appear in `patch` or in `references` for a given call, not both.

Each update hunk starts with bare `@@`. Space-prefixed lines are unchanged context, minus removes, and plus adds. Multiple file operations and multiple ordered, non-overlapping hunks are allowed. Each path appears in one operation only. Every hunk matches the original file for that call globally and exactly once. There is no fuzzy matching. Unsupported line-number headers, moves, renames, EOF markers, malformed lines, absolute paths, traversal, `.git`, symlinks, and non-regular targets are rejected.

Before writing, SkillFlow parses and prepares the complete batch, validates paths and snapshots, and checks exact matches. A preflight failure changes no file. Each subsequent file publication is atomic, but the batch is not a transaction: an I/O failure can leave earlier files changed. Results expose `written`, `deleted`, and `partial`; callers must read those paths before repairing the remainder.

Limits are 128 files, 2 MiB UTF-8 patch text, 1024 hunks per updated file, 16 MiB per source/result file, and 64 MiB combined source/result content during preflight.

## Read and lifecycle rules

Every `read` issues a citation for the window it served, so the cheapest correct edit to an existing file is: read the range, cite its `sha`, send the new text. Nothing is copied and nothing has to be re-verified. When a V4A hunk is used instead and comes back stale or ambiguous, the remedy is the same citation — reread that range and cite it — rather than retyping the current text or widening the copied context. `raw=true` remains the way to obtain patch context when a diff is genuinely the right shape; default numbered output is for navigation and must not be copied into a patch. Do not approximate whitespace or line endings.

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
- its exact `skillflow-py==1.5.78` package pin.

Existing saved generated pipelines are historical inputs and are not silently rewritten. New/reloaded definitions receive only the tools their role configuration explicitly grants.

The migration is deployable only after SkillFlow 1.5.78 is published, the AItelier image resolves that exact pin from PyPI, and the fresh image passes the relevant integration and regression tests. This candidate does not publish, push, deploy, record evidence, or verify State.
