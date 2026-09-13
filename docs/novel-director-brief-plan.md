# Novel director brief — implementation plan

## Scope and ownership

State: `aitelier/workflow.novel-director-brief@1`.
Attempt: `attempt-a4a72e9828104dd79f47c43663289d16`.
Implementer: this ChatGPT session, private worktree `.cache/novel-director-brief`, branch `chatgpt/novel-director-brief-20260913`, base `5dac031e9129c0ff212de3e4fb059076e801107d`.
Deliver a candidate only. Platform verification, integration and deployment belong to the AItelier director. Fictional project content does not belong in this repository.

## Grounded architecture

`core.run_launcher.start_config_run` publishes the manifest's seed_file through `core.seed_publication.publish_seeds`, before the run can execute. Publication is immutable per execution-project/config; a changed input requires a fresh execution project. The MCP `run_pipeline(against_project=...)` already creates a fresh execution project while selecting the existing book repository. `ContextResolver` reads named same-config inputs without a new tool. This is the appropriate delivery path; no new canonical state, bespoke file path lookup or external scheduler is needed.

`core.scheduler.ensure_skillflow_run` requires complete nonempty published seeds for graphs reading their own declared seed. Merely adding `seed_file` would break legacy no-input chapter launches. Existing running/paused runs return before that creation gate and retain their pinned graph. Therefore add a small declarative `seed_default` to the host manifest, consumed only by launch publication. Novel chapters declare a nonempty no-direction default; configs without a default retain existing semantics. Do not relax the scheduler's publication guard or mutate old seed generations.

## Changes

1. Add optional host `seed_default` metadata to ConfigManifest, registration and metadata output. The launcher supplies it when this config's seed is absent/blank, then uses existing immutable publication. A meaningful submitted brief is preserved byte-for-byte. Invalid configured defaults fail explicitly. Existing required-seed configs have no default and are unchanged.
2. Declare `director_brief.md` as novel_chapter's seed file and a clear no-extra-direction default. Use native context sources for outline, outline_review, draft, draft_review, humanize, finalize and finalize_review. Probe remains the canonical bible bundle; the brief remains a separate labelled planning input, not world state. Do not add an LLM step or change graph transitions/checkpoints/models.
3. Update relevant role templates with final-state contracts. Brief is current-chapter intent, never proof of completed events or authority to change bible, chapter number or reveal locks. Current explicit checkpoint rulings supersede corresponding initial brief instructions. Writers implement the approved outline; contradictions require a concrete proposal/review feedback, never invented history. Humanize follows style direction without changing the approved story. Finalizer and journal auditor record actual final prose/authorized canonical rulings, not proposed brief events.
4. Add a compact public usage/precedence/isolation contract and grounded tests. Reuse actual graph parser, ConfigRegistry, ContextResolver, publication and launch path; mock only execution/adapters where necessary to prevent paid LLM/production work.

## Verification

- Focused tests: default/explicit/blank brief, preservation, custom seed_inputs, immutable retry/refusal, fresh chapter/project isolation, actual resolved context for writers/reviewers, unchanged bible/state, optional legacy missing source, and exact checkpoint/graph contract.
- Run existing novel config/tool suites and launcher/publication/registry regressions with SkillFlow 1.5.74 (the candidate's pinned dependency). Keep raw commands/logs and report failures honestly.
- Review actual diff for input races, priority ambiguity, invalid-default handling, stale seed inheritance and overbroad changes. In particular, creation still waits for a complete seed generation; omission is itself an explicit published no-direction input.
- Commit only explicit scoped files. Write immutable report/hash and record per-criterion self-test evidence. Report external attempt as CANDIDATE and quiescent, never VERIFIED. Preserve worktree and exact SHA for independent integration.

## Plan review

The native seed/context route is smaller and safer than teaching state_probe to derive private workspace paths. A declarative default is necessary to preserve omitted-input behaviour without weakening seed readiness. It is intentionally opt-in and shared by public launch paths. No scheduler, novel ledger, reviewer model, artifact staging or worktree lifecycle redesign is included. A creation test must observe publication before wake/start, rather than only check final file existence. Live creative quality and deployed-runtime acceptance remain outside this implementation's evidence.
