# Strict edit contract audit

Audit date: 2026-09-12. Production base: `4861b6ff869927abb4a0622c05c04c4e7cf24125`; SkillFlow base: `b7eb1bb17005be3f5efaceb8634a941f53d6f655` (1.5.74).

| Surface | Before migration | Resolution |
|---|---|---|
| `skillflow.write_tools` generated `create/edit/write` | Current implementation, but schema did not clearly distinguish direct code mutation from staged artifact output | Keep; clarify both lifecycles in authoritative dynamic schemas |
| `skillflow.read_tools` `read(raw=true)` | Current exact-source recovery path | Keep as authoritative reread mechanism |
| AItelier `.cache/strict-apply-patch/core/strict_patch.py` and custom tool | Candidate duplicated generic parsing, matching, path, preflight, and publication logic in the host | Migrate to `skillflow.strict_patch` and native `tools/apply_patch`; remove host copies |
| AItelier `core/write_scope.py` | Host-specific task ownership | Keep; import the shared parser so scope and execution interpret paths identically |
| AItelier `core/dpe_pipeline.py` | Host-specific grants, root injection, JSON/native loop accounting | Keep; select the native tool and retain complete-batch authorization and partial-result repair |
| DPE, coding offload, fix, and subagent role configs/templates | Some active prompts still taught numbered `read` plus `edit(old_str=...)` | Grant native `apply_patch`; require `read(raw=true)` and exact recovery |
| Game designer and novel designer | Generic code outputs still taught `create/edit`; missed by the earlier four-role migration | Migrate to the same native tool contract |
| Pipeline forge | New generic pipelines need the same semantics | Keep AItelier guidance; generated roles opt in explicitly |
| Fixed-slot and artifact steps | Their staged contract is narrower and distinct | Keep `create/edit`; document staged reread and promotion boundary in SkillFlow |
| Coding-mode `edit_file` | Interactive host editor with its own read guard | Keep separate; it is not a SkillFlow step tool |
| Existing saved/generated YAML | Historical configuration may still grant older tools | Do not mutate silently; update/reload explicitly when desired |

## Observed failure

The production trace database `/home/linxuhao/.AItelier/workspaces/sg-8bcb7ab578634806997f1e259a47c587/trace.db` records at sequence 1749/1750 an `edit` call failing with `old_str not found` for `tests/test_growth_storage.gd`. At sequence 1753 the agent reread lines 112-118 after reasoning that indentation differed. The older prompt exposed numbered `read` text without the current `raw=true` recovery instruction. This is retained as observed historical evidence; it does not by itself prove every mismatch has the same cause.

The migrated fixtures reproduce the relevant contract deterministically: numbered text is not exact patch material, raw reread supplies exact text, duplicate contexts fail, indentation/newline differences fail rather than fuzz, and failed preflight leaves every file unchanged. Native AItelier integration exercises both a non-DPE code role and DPE/generated-style tool selection with multiline patches and auditable mutation receipts.
