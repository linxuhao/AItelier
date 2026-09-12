# Artifact revision plan

Owner authorized this repair on 2026-09-12. Driver notice precedes code changes.
Branches: fix/artifact-revision-20260912. Bases: AItelier fc80b58, SkillFlow c3c406d.
Shared checkouts and live runs remain untouched.

## Contract
With carry_forward, the candidate is the previous artifact set plus current
additions/replacements minus explicit deletions. Read, edit, validation and
publication use that same set. This is file-level composition, not semantic merging.
Code outputs and carry_forward=false behavior stay outside the change.

## Implementation
1. SkillFlow initializes the complete candidate before host execution. Use the
   existing durable claim inputs to identify run, step instance and loop item.
   Same-instance retries/reclaims preserve edits and deletions, including an empty
   candidate. Fresh revisions use only the matching run/step/item baseline.
2. Merge a recovered partial candidate without overwriting current edits. Keep
   initialization transactional with the claim; tools run only after it completes.
   Refuse competing owners of the same candidate directory.
3. Native and JSON use the engine-prepared candidate. Remove native cleanup and
   re-copy for carry-forward outputs. Read/edit fallbacks must not revive deletions.
4. Preserve strict manifest completeness checks. Invalid candidates remain available
   for repair while published output is unchanged. Carry-forward validation
   exhaustion fails closed. Allow a validated empty set to publish.
5. Describe current behavior in prompts, tool schemas and error feedback. No
   migration narration in agent prompts.

## Tests and review
Preserve first failing results. Test real engine behavior for A/B/C→modify A,
add D, explicit deletion/retry/restart, all deleted, no-change finish, validation
failure, cross-run isolation, loop items and mixed outputs. Host tests use mocked
LLMs but real candidate routing for native/JSON/fallback. Run focused and broad
regressions, review the final diff, build an exact-pinned private wheel, compare
payloads and verify final commits from clean checkouts.

## Delivery boundary
No game source, active artifact set, pinned graph, checkpoint, budget policy,
service restart, remote push or publication changes. Preserve existing shared
recall and forge_tool_impl fixes. Production bind-mounts the main checkout, so
leave commits on isolated branches until the director coordinates merge/rebuild.
