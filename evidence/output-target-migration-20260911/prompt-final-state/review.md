# Final-state prompt review

## Scope
Agent-facing output instructions describe the current destination and operation.
Code uses repo-relative paths in the run worktree. Artifacts use step output
folders and the declared complete-set/carry-forward policy. The storage engine,
artifact revision algorithm, context indexing and run scheduling are unchanged.

Updated shared and role templates, native restart/turn-budget messages, JSON
catalog descriptions, read/write tool descriptions and current developer guidance.
Generic create/edit constraints, exact-match editing, path safety, required JSON
field schemas and fixed-slot archive behavior remain intact. Code-slot create
replaces the declared code file; artifact-slot create retains archive behavior.

Generated-role migration replaces known output-contract passages at boot, before
role registration. A dry run on copies of the real sidecars changed five roles
across two pipelines, preserved other role fields, and was idempotent. Invalid
sidecars remain untouched for normal registration error handling. Original bytes
are backed up and writes use the existing atomic/stale-read-protected helper.
No live sidecars or pinned run graphs were changed during this work.

## Final verification
- AItelier focused prompt, migration, routing, native restart and full-pipeline
  integration batch: 111 passed, zero failures or skips (host-final.xml).
- SkillFlow main tests: 1,073 passed (engine-wheel-final.xml).
- SkillFlow plugin tests: 47 passed (engine-plugins-final.xml).
- The private output2 wheel, installed package and source match for all 80 runtime
  payload files. See runtime-and-role-audit.json for hashes and the source commit.
- Earlier XML files retain the first failures: assertions requiring obsolete
  wording and one missing Git test-template directory. The final batches use the
  current contracts and explicit Git test templates. Repeated batches are not
  added to these totals.

This is a separate self-review pass, not an independent-agent review. The entire
AItelier suite, Docker rebuild and live-run prompt replay were not performed for
this follow-up. Production activation is the owner's rebuild/restart boundary.
No remote push, publication, service restart or active-run mutation was performed.
The existing recall-observation changes are excluded from this prompt commit.
