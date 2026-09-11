# Final source review — 2026-09-12

Reviewer: ChatGPT, a separate source-review pass in the replacement session. This is not an independently spawned agent review.

## Scope and disposition

The implementation preserves artifact folders and removes the direct-code path's separate source staging, overlay, promotion and deferred deletion. No artifact-revision/fallback redesign, context-index policy change, model change or production restart is included. Source review and final clean-checkout verification are complete. The verified runtime commit is e4577aef47dc24db8ddf86994406a089b5731199; the engine source commit is de0473997df36236343be8abc9febe01c3cf5417. Local integration is recorded separately from this candidate-verification report.

Reviewed boundaries: SkillFlow output_targets.py, graph/core routing, write_tools, validation and read surfaces; AItelier native/JSON output dispatch, resume accounting, runner isolation admission, diagnostic reports, direct deletion/media, playtest roots, code relay, generated-config migration, dependency installation and the accompanying behavioral tests. The original exact-text occurrence inventories and plan review remain in this directory. Semantic search was retried in this session and still failed with connection refused; semantic review is not claimed complete.

## Findings

1. Direct writes are resolver-owned and path-jailed. Source mutation does not choose an artifact baseline. Journal and recovery files contain paths/hashes/commit IDs, not a duplicate source tree.
2. Mixed outputs retain an artifact default with explicit code slots. Diagnostic tool reports are artifacts, do not satisfy code delivery, and do not shadow default code reads. The final native-loop patch invalidates retained report reads after diagnostic mutation; its behavioral test passed in the handoff suite.
3. Validation failure cannot promote a code candidate. Candidate commits are path-scoped; unrelated/ignored dirty changes block delivery. Post-delivery code mutation blocks completion. Generic create remains strict; the rejected-review fixture now edits the existing file instead of weakening create.
4. Same-claim restart and review revision retain the candidate. Direct-code relay requires terminal/quiescent source runs, verifies ownership and hashes, and publishes an explicitly unvalidated Git recovery object without modifying the failed worktree/index.
5. Legacy pinned copy-delivery claims fail explicitly instead of being silently reinterpreted. Generated configuration migration validates before atomic replacement and retains original-byte backups. Existing run graph pins are not changed.
6. PACKAGING FINDING: the exact wheel existed locally but was still ignored by vendor/wheels/.gitignore. It has now been explicitly staged. Delivery must verify the wheel exists in the committed tree and matches build-manifest.json, not merely on disk.
7. Existing uncommitted recall_observation changes predate this migration. They must remain separate and be preserved during local integration. No broad commit or reset is permitted.

## Verification already observed

- Handoff-focused real-dispatch/native/relay/media/migration tests: 33 passed; verification/handoff-targets.xml.
- git diff --check: passed.
- Earlier final integration XML confirms both full_pipeline_real_runner cases passed. Those earlier batches are not substituted for the final clean-checkout run.

## Final acceptance results

The migration was committed without absorbing the pre-existing recall patch. A separate clean worktree at e4577ae was verified against the installed exact wheel. All 192 current unit-test files were enumerated afresh: 2,933 passed, 8 skipped. All other collected AItelier Python tests: 500 passed, 1 skipped, 11 network tests deselected by the existing default marker policy. SkillFlow tests plus the plugin tests outside its top-level tests directory: 1,110 passed, zero skips or failures. No earlier batches were added to these totals. Detailed XML, skip reasons, commands and the test-only subreaper harness are retained in verification/.

The eight unit skips require a Godot binary that is absent from the test environment. The remaining skip is the existing API test-mode write-gate case. Manual browser scripts, Docker build and live gameplay/runtime observation were not performed. The exact wheel SHA-256 matches build-manifest.json; all 80 packaged runtime files match BOTH the installed package and the clean SkillFlow source commit byte for byte.

Disposition: PASS for local source integration and operator rebuild. Preserve the two original dirty recall files and the original untracked configuration during local integration. Service rebuild/restart and observation of a NEW run remain owner actions. No public push or PyPI publication.
