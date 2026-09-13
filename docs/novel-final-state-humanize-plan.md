# Novel final-state writing and humanize — implementation plan

State: aitelier / workflow.novel-final-state-humanize@1
Attempt: attempt-76829de7df884f89adbc7a1d57ceb520
Owner: chatgpt-novel-final-state-humanize-20260913-r1

## Grounding
The runtime uses eleven novel_*.md templates, with role bindings in
agent_configs/novel_init.yaml and novel_chapter.yaml. No separately loadable
final-state or humanize SKILL.md was found in the inspected repository skill
locations; the existing humanize template contains partial anti-instruction-
leakage guidance. test_final_state_prompts.py covers infrastructure output
contracts, not fiction prose behavior.

## Findings to resolve
1. Negative editorial constraints can be echoed into narration through outline,
   draft or repair feedback; checks enforce old quoted wording rather than the
   currently effective meaning.
2. Humanize permits removal of unsupported negations but also unconditionally
   protects every unique sentence. Its reviewer can therefore restore leakage.
3. Humanize examples invent props/actions/emotional meaning; padding to preserve
   length conflicts with factual fidelity. The prompt falsely claims that
   semantic fidelity lacks a dedicated review despite the actual graph.
4. Canonical rule corrections and textual editing instructions need distinct
   ledger provenance. A forbidden fictional entity is not a defeated entity.

## Implementation
1. Update planning, writing and reviewer templates with scoped final-state
   contracts; edits replace superseded requirements and repair affected causal
   dependencies. Narrative prose contains the story, not editing history.
2. Align humanizer and fidelity reviewer: preserve meaningful negation, character
   misconceptions and single-occurrence information; remove demonstrated editor
   leakage without adding facts, changing emotion/POV, or weakening mechanical
   gates. Keep natural voice rather than forced synonym/gesture replacement.
3. Align finalizer/auditor on story facts versus explicitly sourced durable rule
   rulings; current state records contain final values with separate provenance.
4. Preserve existing graph, output schema, model selection and checkpoints. No
   novel content/run mutation; no new prompt loader, skill engine or classifier.

## Validation
- Test actual role-to-template loading and assembled system prompts, not just
  unused markdown. Check obsolete contradictory instructions are removed.
- Supply balanced semantic evaluation cases: zombie-feedback echo, valid
  negative evidence, character uncertainty, numeric/causal/hook fidelity,
  emotion-preserving edits, ledger provenance and superseded feedback.
- Run focused plus existing novel/config/prompt regression suites; report static
  checks separately from model behavior. No claim of guaranteed model adherence.
- Inspect compatibility with director_brief candidate b2d036254834be6d142a19d2c99c311f9b3f2ff7
  in an isolated validation tree. Preserve that candidate and main unchanged.
- Review exact diff, commit explicit files, attach immutable report/log hashes
  and per-criterion implementer evidence. Deliver CANDIDATE; independent
  verification, integration and deployment belong to the platform director.
