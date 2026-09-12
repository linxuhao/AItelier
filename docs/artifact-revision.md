# Artifact revisions

An output with `carry_forward: true` is a revision of the complete artifact set:

    candidate = previous published set + additions/replacements - explicit deletions

This is file-set composition. Text and JSON documents are changed by the existing
write/edit tools; their content is not semantically merged by the engine.

## Execution contract
SkillFlow prepares the candidate before exposing the claim. Its identity is the
run, step execution instance and loop item. The claim inputs record readiness; a
small ownership file under `<config>/.artifact-candidates/` identifies the owner
of the candidate directory. Metadata is outside the artifact set and is not
published as an artifact. Another pending/claimed execution cannot take the same
candidate directory. A missing or replaced ready candidate requires restoration
instead of silently rebuilding it from an older version.

New revisions inherit only this run's published output for the matching step and
loop item. Current-attempt edits win when recovering a partially initialized
candidate. A prepared execution's retry/reclaim uses its existing complete set,
including deletions and an empty set. An unrelated run's published output is not
an implicit baseline; explicit project/run continuation remains a separate host
operation. The shared step directory still requires one controller per project.

Artifact read/search/list tools and artifact edits use the current candidate.
Code remains available as the explicit repository source. Mixed steps use default
`artifact` with individually declared code slots. Native and JSON host paths use
the candidate prepared by SkillFlow.

Validation checks the complete candidate. For carry-forward outputs, exhausted
validation fails closed and leaves the published set unchanged. A valid candidate
replaces the published set, including a valid empty set. A task-card deletion must
be explicit and accompanied by its manifest update. The manifest validator still
requires every referenced card to exist and every card to be listed.

Agent-facing instruction:

> Unchanged artifacts are preserved. Write only additions or changes. Use the
> artifact delete tool for removals and update the manifest. Validation and
> publication use this same complete candidate.

## Deployment
This behavior ships in the private `skillflow-py==1.5.72+aitelier.output3` wheel.
Rebuild AItelier with that exact wheel; do not install it into a running service
while an execution is changing artifacts. No database schema migration or graph
rewrite is needed. `carry_forward: false` retains its existing output contract.
Existing pinned graph versions and budget/review checkpoints are not modified.

The repair branches are isolated because production bind-mounts the main checkout.
Merge and rebuild in the director's coordinated window, retaining the existing
recall and tool-implementer-template changes on the main checkout. Verify a new
run/revision first; do not automatically adopt an in-flight pre-upgrade candidate
whose deletion history cannot be established.
