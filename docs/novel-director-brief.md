# Novel chapter director input

`novel_chapter` accepts an optional current-chapter brief through the existing public `seed_text` input. Its chapter number still comes from `state_probe` and its canonical facts still come from the book repository. The brief is not a second bible or an event ledger.

```python
run_pipeline(
    config="novel_chapter",
    against_project="EXISTING_BOOK_PROJECT_ID",
    seed_text="""# Director Brief
## Purpose
Show the protagonist beginning to distrust the apparent explanation.
## Reader experience
A quiet opening that gradually becomes unsettling.
## Progress
Advance the current frontier, but do not complete it.
## Boundaries
Do not reveal the locked mystery or introduce an unapproved named character.
## Character and pacing
Give the established supporting character an independent decision. No combat.
""",
    checkpoints="ask",
)
```

The MCP launch creates a fresh execution project sharing the selected book source. It publishes `novel_chapter/_seed/director_brief.md` before execution. Every chapter needs a fresh execution project. Retrying identical input in that execution project is idempotent; replacing its published seed is refused. Resuming a running or paused chapter uses its exact run ID, not another launch.

Omitted, empty or whitespace-only input publishes the configured nonempty no-extra-direction default. This keeps ordinary no-brief chapter launches runnable without relaxing the scheduler's complete-seed gate. A named `seed_inputs["director_brief.md"]` retains the launcher's existing precedence over `seed_text`. Nonblank submitted text is preserved in publication and native resolution; normal downstream model/context limits still apply. Keep production briefs compact rather than treating this as unlimited prompt storage.

The optional host metadata `seed_default` must be nonempty text and requires `seed_file`. Only `novel_chapter` opts in. Configs without this metadata retain their existing required-seed behaviour. Registering a project alone is not a launch: a fresh seed-reading project still waits for publication. Existing in-flight graphs remain pinned; merely updating files does not migrate their seeds or prove a deployed process has loaded the change.

## Readers and authority

Outline, outline review, draft, draft review, humanize, finalizer and ledger auditor read the same native current-execution-project source. The probe bundle stays separate and authoritative for existing facts. The narrow humanize fidelity reviewer continues comparing the approved draft against the polished final version.

The initial brief states current-chapter intent. A later explicit checkpoint ruling supersedes the corresponding initial direction; other effective requirements remain. The writer follows the approved outline. A contradiction with bible rules, chapter identity, character balances or reveal locks must be surfaced for a decision, not silently implemented as changed history. Merely approving a checkpoint carries no feedback channel: binding corrections use reject with feedback, then review the revised artifact.

A planned death, breakthrough, resolved mystery or completed arc node is not an event. The finalizer records what actually occurs in the final prose and explicitly authorized rule-level checkpoint rulings under the existing accounting contract. The ledger auditor checks this boundary. The brief alone does not authorize a canonical rule change.

All review, continuity and accounting steps, loop limits and both checkpoints remain in place. `checkpoints="ask"` preserves deliberate director/human approval. This feature does not auto-approve, publish a book, choose a model or integrate a chapter's private worktree into an accepted source.

## Verification boundary

`tests/unit/test_novel_director_brief.py` exercises the real shipped graph, registry, launcher, seed publisher, native resolver and novel probe with isolated execution adapters. Existing novel, publication, launcher and registry suites protect the surrounding behaviour. These tests validate delivery contracts, not literary quality or model obedience. Independent platform acceptance and deployed-runtime confirmation remain separate.
