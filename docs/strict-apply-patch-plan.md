# Strict apply-patch migration plan

State node: `tools.strict-apply-patch-contract-migration@1`
Attempt: `attempt-a08d5b19b4a44ab88134ceaf9614aada`

1. Move the generic parser, exact matcher, path jail, batch preflight, filesystem publication, receipts, native schema, and lifecycle documentation into SkillFlow 1.5.75.
2. Remove AItelier's duplicate parser and custom tool. Import the shared parser only for AItelier's task-card scope decision.
3. Grant the native tool to AItelier's generic code roles and teach every active implementation/design template the same raw-read, exact-match, failure-recovery, and lifecycle contract.
4. Preserve fixed-slot and artifact edit semantics. Do not rewrite historical generated configs.
5. Test SkillFlow independently, then test AItelier against the candidate SkillFlow checkout. Build and inspect a local wheel. Publication, image rebuild, deployment, State evidence, and verification remain separate authorized steps.

The migration deliberately rejects fuzzy replacement. Preflight failure is batch-atomic; an operating-system failure during publication can be partial and must report completed paths.
