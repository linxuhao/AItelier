# Novel final-state writing and humanize contract

The loaded `templates/novel_*.md` files are the runtime instructions. There is
no separate skill loader or extra workflow step for these rules.

## Output boundary
Planning artifacts describe current plans and clearly scoped constraints;
canonical state distinguishes current facts from future plans. Reader prose
presents the story, not checkpoint feedback, rejected alternatives or editing
history. A constraint excludes an unwanted event; it does not require a
character to announce its absence. Meaningful negation, observations,
misconceptions and suspense remain valid story information.

For each scoped decision, the latest explicit authorized ruling replaces a
contradictory earlier one. Unchanged requirements remain active. Revisions
repair affected causal dependencies, not just the one quoted sentence.

## Polishing boundary
Humanize preserves meaning, voice, emotional intensity, viewpoint, uncertainty,
numbers, cause and effect, plot facts and hooks. Removing proven editorial
leakage is not deleting a protected story fact. Deleting a genuine unique fact
is not harmless polishing. An action does not uniquely establish an emotion;
new gestures or props are not authorized by a request to show rather than tell.
Natural wording and ordinary dialogue tags may remain unchanged.

The draft reviewer blocks demonstrated editorial leakage before polish. The
humanize reviewer compares the current draft and final; deletion evidence can
cite surrounding final text instead of inventing a nonexistent matching line.
Ambiguous provenance is not resolved by guesswork. Mechanical title, length and
paragraph checks remain unchanged; no prose is fabricated to bypass them.

The summary/event ledger records story facts. Explicitly approved world-rule
rulings may update canonical rules with their source and scope in the existing
schema. A writing preference is not a world rule; a rule's existence is not a
character's discovery or a completed plot node.

## Verification scope
`tests/unit/test_novel_final_state_prompts.py` exercises real role registration,
template loading and JSON/native message delivery with a mocked provider, plus
static contract checks. These tests do not demonstrate LLM compliance.
`tests/fixtures/novel_final_state_humanize.json` is a balanced, authored semantic
rubric, explicitly not a set of model outputs. To evaluate a deployment, run its
actual configured roles against these cases in an isolated workspace, retain
raw outputs and review them against the rubric. Do not score by banned-word
matching. Record model/version, input/output hashes, false positives and misses.

Prompt edits affect newly constructed agents after their source is integrated
and available to that runtime; an already-built agent need not reload a file.
A candidate commit alone is not deployment, a live chapter test or independent
acceptance. These rules work with existing checkpoint inputs and remain
compatible in intent with the optional run-scoped director brief.
