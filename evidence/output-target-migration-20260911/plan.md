# Goal: explicit artifact/code output destinations

## Owner contract
Keep artifact folders. Remove CODE staging, overlays, promotion and deferred deletion. Configure output destinations rather than infer them from role names or output.mode. Do not restart services, change game behavior, change models/budgets, or add retrieval prefetch in this goal.

## Baseline and inventory
AItelier HEAD 594a892; SkillFlow HEAD 32e139b (1.5.72). Existing uncommitted recall fixes are carried through unchanged. Work is isolated in fix/output-target-code-20260911 worktrees. Exact recursive inventory excludes third-party/other worktrees: 811 matches in 149 AItelier files, 739 matches in 62 SkillFlow files. Semantic search was attempted; localhost sidecar refused connection. Exact source/config/caller inventory is authoritative, not a semantic top-k completeness claim.

## Contract
- output.target: artifact | code, default artifact, independent of content/write mode.
- A fixed slot may override target, so architecture.md/report.json remain artifacts while linter_manifest.json/README.md go directly to code. No extra pipeline stages solely to copy these files.
- Code writes go to the resolver-owned run worktree immediately. No code .tmp folder, staging overlay, promotion, repo_apply copy, or deletion manifest. Artifact publication remains artifact-specific.
- read/search/semantic_search/playtest see current code. Artifact inputs remain explicitly addressable and never become an implicit stale code baseline.
- Code output metadata (changed paths and candidate commit) is an artifact, not a second code tree. Validation/review/cancellation remain enforced. Failure retains candidate worktree changes; no automatic destructive rollback.
- Code writes are jailed, .git protected, host roots cannot be selected by the agent. New execution steps cannot silently adopt another attempt's dirty worktree.

## Migration tasks
1. SkillFlow: parse/validate/serialize destinations; central output routing; fixed/generic writes; read layers; validation/check roots; output metadata/commit; custom-tool output injection; runner surfaces. Preserve artifact-only tests and state semantics.
2. AItelier: pass explicit graph/target/root metadata (never infer graph from output dir); native and JSON outputs; resume/retry; run-isolation admission; code deletion/media/playtest paths; prompt descriptions; relay handling.
3. Config migration: repository-bearing output steps and mixed fixed slots; remove copy/deferred-delete lifecycle hooks for code. Review all built-in addons and generated config files. Do not alter pinned live runs or silently change their semantics. Produce explicit compatibility/preflight information for the restart.
4. Docs and release: schema fixture, source inventory disposition, migration operator notes, reproducible wheel and dependency alignment. No service restart or unrequested public publication.

## Plan review gates
Read-after-write parity; no stale promoted-code fallback; no code copied to artifact folders; same candidate validated/tested/reviewed; exact-path commit including deletion without unrelated changes; retry/resume retains work; cancellation/fencing unchanged; artifact generation and revision intact; mixed outputs correct; generated configs not omitted; old in-flight state not silently reinterpreted.

## Tests
Run real Python 3.12 tests in a temporary environment, against both edited repositories. Add behavioral tests for output-target round-trip, code writes/reads/search, fixed mixed output, code validation failure, deletion, safe paths, cancellation, promotion absence, exact commit scope, retry/restart, binary/custom output, and artifact invariants. Run existing related suites and broad suites; classify pre-existing failures separately. Code review must inspect final diff and remaining occurrences, not just syntax or source-string counts.

## Not part of this goal
Host prefetch, context-size tuning, role prompt redesign, new rollback/snapshot subsystems, game changes, mainline publication, production restart.
