# Strict code patch editing

## Effective contract

`apply_patch(patch)` is a native SkillFlow code mutation tool. SkillFlow 1.5.75 owns its grammar, exact matching, repository path checks, preflight, per-file atomic writes, mutation receipts, schema, and user-facing lifecycle wording. AItelier does not carry a second parser or filesystem implementation.

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

Each update hunk starts with bare `@@`. Space-prefixed lines are unchanged context, minus removes, and plus adds. Multiple file operations and multiple ordered, non-overlapping hunks are allowed. Each path appears in one operation only. Every hunk matches the original file for that call globally and exactly once. There is no fuzzy matching. Unsupported line-number headers, moves, renames, EOF markers, malformed lines, absolute paths, traversal, `.git`, symlinks, and non-regular targets are rejected.

Before writing, SkillFlow parses and prepares the complete batch, validates paths and snapshots, and checks exact matches. A preflight failure changes no file. Each subsequent file publication is atomic, but the batch is not a transaction: an I/O failure can leave earlier files changed. Results expose `written`, `deleted`, and `partial`; callers must read those paths before repairing the remainder.

Limits are 128 files, 2 MiB UTF-8 patch text, 1024 hunks per updated file, 16 MiB per source/result file, and 64 MiB combined source/result content during preflight.

## Read and lifecycle rules

Use `read(raw=true)` immediately before composing exact patch context. Default `read` output includes numbered lines for navigation and must not be copied into a patch. If a match fails, read the current target range again with `raw=true`, narrow the context until it is unique, and retry. Do not approximate whitespace or line endings.

For `output.target: code`, a successful mutation immediately changes the run's uncommitted worktree. It does not mean validation, review, commit, or delivery passed. Artifact `create`/`edit` is different: it writes the step's staged candidate and is promoted only after confirmation. When reading an artifact step's own staged candidate, use `source="self"`; this is not the code-worktree `apply_patch` lifecycle.

## Ownership and deployment boundary

SkillFlow owns:

- `skillflow.strict_patch` parsing and application;
- native `tools/apply_patch` implementation and schema;
- exact-match, path-jail, snapshot, preflight, write/delete, and receipt semantics;
- generic `read`/`create`/`edit` schema wording for staged artifacts and direct code worktrees.

AItelier owns:

- task-card path authorization and complete-batch refusal;
- host root injection, role grants, tool filtering, loop accounting, and prompt guidance;
- pipeline-forge guidance for newly generated code roles;
- its exact `skillflow-py==1.5.75` package pin.

Existing saved generated pipelines are historical inputs and are not silently rewritten. New/reloaded definitions receive only the tools their role configuration explicitly grants.

The migration is deployable only after SkillFlow 1.5.75 is published, the AItelier image resolves that exact pin from PyPI, and the fresh image passes the relevant integration and regression tests. This candidate does not publish, push, deploy, record evidence, or verify State.
