# Explicit artifact/code output destinations

## Contract

Artifact folders remain. Source-code staging does not: code is written directly
into the run's writable worktree. `output.mode` specifies the tool/output shape;
`output.target` specifies its destination. Neither role names nor `mode: write`
alone imply a code destination.

```yaml
- id: implement
  step_type: agent
  output:
    mode: write
    target: code
  validation:
    - tool: lint
      files: ['*.py']
  validation_on_exhaustion: fail
```

No `repo_apply`, `repo_delete`, `draft_promote`, `carry_forward`, or code `.tmp`
macro is allowed on this code path. The engine preserves candidate changes on
failure; it does not reset/clean the worktree. The host requires a writable,
run-owned worktree; code-less/direct/shared-checkout runs are rejected.

Plans, task cards, verdicts, and reports default to artifact publication:

```yaml
- id: verify
  step_type: agent
  output:
    mode: content
    target: artifact
    fixed:
      readme: {file: README.md, target: code}
      report: {file: final/verify_report.json, target: artifact}
```

Mixed outputs use an artifact default with code slot overrides. Tool steps keep
their own explicitly implemented effects; `output.target: code` is an agent
output contract, not a way to override arbitrary tool-node filesystem behavior.

## Execution and reads

Code create/edit/delete and media output touch the current worktree immediately.
Default read/search, explicit repo reads, and playtests use that same code tree.
Playtests no longer copy the repository and overlay step files. Semantic search
uses the same worktree root, but its external index can still lag uncommitted
changes; this migration does not change index freshness or retrieval policy.

The engine tracks changed path names in artifact metadata, validates the current
candidate, commits only those paths, and publishes `code_changes.json` with the
base and candidate commit. A later reviewer still decides acceptance. A task's
review revision retains its original base: the receipt covers A-to-C2, not only
C1-to-C2. An unrelated dirty file blocks delivery rather than entering the commit.
An ignored code output also blocks delivery instead of disappearing silently.
Mutating checks such as auto-fixing lint execute in validation, before commit.
After-delivery checks must not change code: a changed HEAD or dirty tree blocks
completion and preserves the unexpected changes for diagnosis. Immediate review
revisions preserve the original task base; a new execution after intervening
accepted code steps starts from the current clean HEAD.

Artifact staging/publication and history continue to work. Code source files are
never duplicated into artifact folders. Read a previous code version by commit,
not by interpreting an old step artifact directory as the current code baseline.
Scratch `test_write/read_test_written` is not source delivery and remains scratch.

## Failures, restart and relay

A same-instance restart rebuilds the native conversation from trace and checks
its code output paths in the worktree. Artifact outputs are checked in their own
artifact destination. Failed candidates stay available; no automatic rollback.

Direct-code relay first requires a terminal run with no admitted operations and
verifies file ownership, HEAD and content hashes. It makes an explicitly
UNVALIDATED Git recovery commit using Git objects and a temporary index file.
It does not copy code into an artifact directory or mutate the failed worktree,
branch or real index. The new run starts from that commit; inherited code paths
remain subject to validation even when the next agent makes no additional edit.
Unowned or ignored changes require explicit attention, not silent adoption.

A claim pinned to a legacy agent code-copy graph is refused before the agent
starts on this runtime; it is not silently converted. Finish such runs on their
prior image or explicitly recover them into NEW attempts.

Old code drafts from the pre-migration staging system are retained, but are not
silently fed to a new direct-code step. Their recovery requires an explicit
operator action. Existing pinned runs are NOT repointed to a new graph version.
Do not use resumed legacy runs as evidence that the new storage mode is working.

## Installation and first observation

This change includes a private, exact-pinned SkillFlow wheel
`1.5.72+aitelier.output1` under `vendor/wheels`. It has not been published to PyPI.
Docker installs it using `--find-links`; startup rejects an engine without the
output-target contract. Rebuild the backend image, then recreate/restart it:

```sh
docker compose build aitelier
docker compose up -d aitelier
```

For an editable host installation use `pip install --find-links=vendor/wheels -e .`
from the AItelier repository. Restarting an old image alone is insufficient.

At boot the supporting runtime migrates known generated config copy contracts,
validates before replacement, and backs up original bytes under
`~/.AItelier/migration_backups/output-target-v1/`. Unknown copy contracts fail
explicitly rather than guessing. The migration changes config files and new
registrations, not the graph versions pinned by active or historical runs.
The standalone `scripts/migrate_output_targets.py` supports dry-run and explicit
`--apply --backup-dir ...` for the same transformation.

Observe a NEW run: its claim must show `_output_target: code` for implementation,
code reads and playtest must reference its worktree, no implementation `.tmp`
should be created, and the artifact folder should contain a change receipt rather
than source copies. Keep artifact plan/review publication and all validation
failures visible. Context prefetch, role budgets, models and game logic were not
changed in this goal. Container build and live runtime observation remain the
operator's next step; development tests are not a production rollout claim.

## Diagnostic tools inside code steps

Report-producing tools declare `output: {target: artifact}` in their tool.yaml.
Test/compile/vision reports stay in the step artifact folder, and their result
uses `artifact_written` rather than counting the report as delivered source.
`read(source="self", path="test_report.json")` reads the artifact; default reads
still read the worktree and do not overlay the artifact folder. Reports are
observations, not a substitute for candidate validation: correlate the tool's
trace instance and timestamp with the candidate and rerun checks after edits.

A tool-level code target cannot elevate an artifact-only step into a writer.
Tool nodes continue to use their graph-declared tool_params destinations.
