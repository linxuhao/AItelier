# Director brief implementation review

Scope: `aitelier/workflow.novel-director-brief@1`, implementing attempt `attempt-a4a72e9828104dd79f47c43663289d16`. This is the implementer's code/impact review, not independent platform acceptance.

## Inspected paths and conclusions

The change reuses the existing public `seed_text` → ConfigManifest → immutable seed publication → native ContextResolver path. `state_probe`, `novel_state`, worktree provisioning, scheduler gates, review loops, model configurations and checkpoint transitions remain unchanged. Seven role contexts read the same current execution-project input; relevant downstream steps also see the outline/draft checkpoint rulings needed to interpret that input.

A missing optional brief becomes an explicit nonempty default seed, not a scheduler exception. Publication still precedes get_or_create_run, start_run and wake_scheduler. A changed input under the same execution project is refused, including switching from a real brief to the no-direction default. A fresh execution project gets its own input. Only novel_chapter opts into the new default. Meaningful supplied input is preserved byte-for-byte; downstream provider/token budgets are not removed.

The prompts distinguish intent from existing canonical facts and ledger evidence. They preserve chapter identity, reveal locks and bible authority. Outline conflicts can be presented for a director decision rather than provoking an unbounded reviewer loop. Humanize remains constrained to the accepted story. Finalizer/auditor do not manufacture a planned event simply because the brief asked for it.

## Findings corrected during implementation

1. Declaring only seed_file would stall new no-input chapters at the complete-seed gate. The opt-in declarative default fixes this without weakening the gate.
2. Inserting a new positional dataclass field could change existing ConfigManifest callers' argument meanings. seed_default is keyword-only; an explicit positional-compatibility regression test covers the old signature.
3. Initial brief instructions can be superseded by checkpoint decisions. Added the missing downstream feedback sources and updated reviewer wording so explicitly superseded directions and old quoted complaints do not trigger false reverts.
4. Five first-run tests compared a tiny dictionary against the parser's normalized source dictionaries, which contain extra fields. The test now checks the relevant semantic field; a further real ContextResolver test verifies that the changed ruling text actually reaches downstream readers. Initial failed output is retained, not relabelled as a successful run.

## Actual checks

Final selected regression run: **216 passed, 2 warnings**, covering 11 existing/new test files. This includes **43 new director-brief tests** for default/blank/explicit input, invalid defaults, launch publication ordering, named seed precedence, immutable retry/refusal, cross-chapter/book isolation, real probe plus context resolution without mutating the bible, seven role contracts, downstream checkpoint feedback and positional compatibility. Existing novel graph/tool, launcher, registry, seed-publication/concurrency and config-router tests are included.

The two warnings are FastAPI/Starlette deprecation warnings in test dependencies. The environment uses the candidate source with SkillFlow **1.5.74**, installed into this worktree's private test-runtime target; the borrowed interpreter's older package is not selected. AITELIER_HOME and scheduler/instance locks are isolated by the test fixtures. No paid model or production run is launched by these tests.

`git diff --check` passes. The final report binds the exact committed candidate, commands and raw logs.

## Acceptance limits and handoff

No independently verified literary quality, model obedience, unattended Scheduled Task connector access, live deployment behaviour or full-repository regression claim is made. The whole repository test suite was not run; the 216 checks are the selected relevant suite. Existing running graphs remain pinned, so a platform file change alone is not evidence they consumed this input.

The platform director must inspect the exact candidate, review integration against then-current main and the pinned dependency, independently verify the criteria, and decide deployment. The implementing session does not call verify_node, merge/push shared main, restart services or publish. Novel project content is managed separately under novel-lingwu and is not included in this platform commit.
