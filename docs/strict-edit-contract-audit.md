# Strict edit contract audit

Audit date: 2026-09-13. AItelier correction base:
`5dac031e9129c0ff212de3e4fb059076e801107d`; SkillFlow base:
`b7eb1bb17005be3f5efaceb8634a941f53d6f655` (1.5.74).

| Surface | Classification before correction | Resolution |
|---|---|---|
| `skillflow.strict_patch` and native `tools/apply_patch` | Candidate current generic parser, exact matcher, path jail, preflight, atomic per-file publication and receipts | Keep as the only executable implementation; publish as SkillFlow 1.5.75 only after review |
| SkillFlow live schema, README, and skill-runner guide | Contract was current except recovery wording did not distinguish missing/stale from ambiguity | Missing/stale says raw reread and exact copy; ambiguity says add unchanged context until unique |
| `skillflow.write_tools` generated `create/edit/write` | Current staged-artifact and direct-code legacy interfaces | Keep for fixed-slot/artifact contracts; generic code roles use explicit `apply_patch` |
| `skillflow.read_tools.read(raw=true)` | Current exact-source recovery mechanism | Keep as authoritative reread path |
| AItelier `.cache/strict-apply-patch/aitelier/tools/apply_patch` and `core/strict_patch.py` | Stale disposable candidate duplicated generic SkillFlow behavior | Never ship or register; repository candidate contains no AItelier copy |
| AItelier `core/write_scope.py` | Current host-specific task-card authorization | Keep; parse with SkillFlow's shared parser so authorization and execution see the same complete path set |
| AItelier `core/dpe_pipeline.py` | Current host root injection, grant filtering, mutation accounting and retry fencing | Keep host responsibilities; generic mutators are unavailable when `apply_patch` is granted |
| Built-in DPE, coding, fix, subagent, game-designer and novel-designer configs/templates | Some roles taught `create/edit(old_str)`; game and novel ambiguity text incorrectly said to shrink context | Grant native `apply_patch`; use the same two-case recovery contract everywhere |
| `configs/gen_coop_shell_ui60_20260907.yaml` | Checked-in generated config still used implicit write staging plus `repo_apply` | Explicit `output.target: code`, native role grant, engine candidate recording; no copy hook |
| Pipeline-forge authoring guidance | New generated code roles needed an explicit stable contract | Forge emits explicit code target plus `apply_patch`; fixed artifacts retain their narrower editor |
| `core/output_migration.py:migrate_role_prompt` and `_migrate_generated_role_prompts` | Boot migration rewrote prose but neither inferred code roles nor repaired saved tool grants; it could leave generated agents with stale `edit` instructions | Read each companion YAML after output migration, select only explicit generic code roles, replace known stale code sections, remove generic mutator grants, add `apply_patch`, back up and atomically replace the sidecar |
| `core/pipeline_registry.py:load_generated_configs` | Current boot invocation, previously omitted from the audit | Calls `migrate_generated_outputs(generated_configs_dir())` before loading/registering any generated YAML or role sidecar |
| `tests/unit/test_output_migration.py`, `tests/unit/test_final_state_prompts.py` | Existing backup/idempotence tests did not cover saved code-role tool repair | Cover role selection, prompt/tool migration, artifact isolation, backup, idempotence, and checked-in config |
| Active saved `~/.AItelier/configs` | `gen_dpe_state_game` and `gen_gen_media_candidate` contained stale code-role prompts/grants; `gen_dsh_code_review` has artifact/report writers only | A dry copy of the production files migrates exactly the DPE implementer, game designer, and media implementer; DSH roles remain unchanged |
| Archived saved configs | `_archived/gen_roundtrip_probe*.yaml` are excluded from the active root glob and registry | Remain inert and unchanged |
| Historical run graph versions and traces | Immutable provenance | Never repointed or rewritten; migration changes only active saved definitions and writes content-addressed backups |

## Observed failure and deterministic reproduction

The production trace database
`/home/linxuhao/.AItelier/workspaces/sg-8bcb7ab578634806997f1e259a47c587/trace.db`
records at sequence 1749/1750 an `edit` call failing with
`old_str not found` for `tests/test_growth_storage.gd`. At sequence 1753 the
agent reread lines 112-118 after reasoning that indentation differed. The older
prompt exposed numbered `read` text without the current `raw=true` recovery
instruction. This observed trace establishes the stale prompt's practical impact;
it does not prove that every mismatch has the same cause.

Deterministic fixtures separately cover stale/not-found text, duplicate context,
tabs/spaces, LF/CRLF and EOF-newline preservation, staged reread, multi-file and
multi-hunk edits, complete preflight failure, and partial filesystem I/O receipts.
A stale/not-found retry must raw-reread and copy the current bytes exactly.
Ambiguity requires additional unchanged surrounding lines. Neither case permits
fuzzy replacement.

## Ownership and release boundary

Generic patch grammar, matching, path safety, preflight, publication, receipts,
schema and stable lifecycle documentation belong to SkillFlow. AItelier owns
task-card authorization, root injection, role grants, prompt selection, generated
definition migration, package pinning and deployment.

The saved-definition migration is intentionally bounded: it only changes roles
that a companion active YAML explicitly identifies as generic
`output.mode: write`, `output.target: code`, with no fixed slots. Invalid or
unclassified definitions remain for normal registration diagnostics. Original
bytes are retained in content-addressed backups and a second boot is idempotent.

No candidate here publishes SkillFlow, pushes either repository, deploys
AItelier, records evidence, or verifies State. A fresh production image cannot
resolve the exact PyPI pin until the separately reviewed 1.5.75 artifacts are
published; that post-publication image test remains a deployment gate.
