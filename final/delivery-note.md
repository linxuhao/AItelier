# notes-public rev 3 - delivery note (documentation, test scripts, delivery only)

Date: 2026-09-23 (UTC).
Base / tree: `79d6f5eb28f090e4954e8d71c02504baab2de794` (the r3 candidate).
Ruling: `note://aitelier/546f3b521eca` - owner 2026-09-22, verbatim
「public的 project的working note也可以public， private project的working note继续private」.
r3 review: `~/.AItelier/director/reports/notes-public-r3-review-20260923/review.md`
sha256 `c4933024c3531845dc367b8b03234e13eb2dfd6c3ac286aaa5a4e9baf97f51b4` - 4 green / 1 red,
red on `the-ruling-is-written-where-the-table-is`, naming `tests/browser/state_project_smoke.py`.

This rev changes **no code behaviour**. On this tree the r2 round's `.py` line (Item 2) is
already clean and compiles. What was still broken, and is fixed here, is
`web/src/__tests__/views/StateProject.test.ts` - and the integrity scan that should have
caught it but reported `hits: 0` anyway (Item 1b below). Three prose sentences the ruling made
false are aligned (Item 5), and the re-run logs are in `logs/`.

## Criteria, one by one (this rev, this tree)

- `an-opened-projects-notes-are-readable-anonymously` - **pass**. `tests/unit/test_state_project_privacy.py::TestTheWorkingNoteReadsAcrossThreeProjectStates::test_the_six_note_reads_over_the_three_project_states` is the cross product **6 note reads × 3 project states = 18 cells** (opened → 200 carrying the body; unopened → 403; absent → 403; the two refusals byte-identical). Re-run on this tree: `logs/rev3_rerun_integrity_and_criteria.txt`, bare exit code 0 (within the 139-passed run).
- `everything-else-private-stays-private` - **pass**. `TestClassification::test_everything_the_ruling_did_not_name_stays_private` (director mailbox, events/long-poll, `project_visibility`) and `TestClassification::test_an_unclassified_read_is_private_by_default` (judges a made-up action name private); the four note writes stay refused in `TestAnonymousHttp::test_writes_stay_refused_anonymously`. Re-run on this tree in the same log, bare exit code 0.
- `no-note-read-leaks-an-unopened-project` - **pass**. `TestListingDoesNotNameRefused::test_no_private_body_surfaces_and_scanner_has_a_positive_control`, `...::test_public_gate_table_unopened_opened_closed`, and `TestExhaustiveDoors::test_every_read_action_is_refused_iff_it_is_private` over every action and every GET route from the mounted app's own OpenAPI document. Re-run on this tree in the same log, bare exit code 0. `logs/criteria_rev3_integration.txt` re-runs the two entrypoint suites, bare exit code 0 (66 passed in 27.78 s).
- `the-ruling-is-written-where-the-table-is` - **pass after the fixes below**; items 1-6 are each shown before → further down, including the scanner gap this rev closes.
- `the-whole-suite-stays-green-in-the-container` - **not re-measured by this step; reported, not claimed.** This step's probe surface caps at 300 s, and the whole-suite attempt carried over from the r2 delivery was killed at that wall with exit status -9 (`logs/full_suite_attempt.txt`). The authoritative whole-suite run is this pipeline's own `run_tests gate`, which executes uncapped after this step. The code behaviour on this tree is the r2 candidate's; the only diffs here are one `.ts` line, one `.py` scan rule + its positive control, prose files, this note, and the re-run log.

No acceptance pin or playtest assertion was loosened.

## Every number with the log that produced it

Every delivery log in `logs/` ends in `.txt`: the repository's `.gitignore` ignores `*.log`,
and the delivery hook refuses a whole round on such a file.

| number | meaning | log file | bare exit code |
| --- | --- | --- | --- |
| 139 passed in 27.44 s | criteria 1/2/3 behavioral + reagent entrypoints, re-run on THIS rev's tree | `logs/rev3_rerun_integrity_and_criteria.txt` | 0 |
| 4 passed in 1.95 s | integrity scan re-run on THIS rev's tree, AFTER the rule was tightened and the `.ts` file fixed | `logs/rev3_rerun_integrity_and_criteria.txt` | 0 |
| 4 passed in 1.78 s | the integrity scan AS DELIVERED last round (see Item 1b - this was the false green) | `logs/source_integrity_scan.txt` | 0 |
| 1 failed in 1.28 s, naming `tests/browser/state_project_smoke.py:255` | the same scan with the glued tail re-planted | `logs/source_integrity_scan_planted_defect.txt` | 1 |
| 66 passed in 27.78 s | the two entrypoint suites | `logs/criteria_rev3_integration.txt` | 0 |
| 13 passed in 2.12 s | the reagent-swapped guard nodes, run alone | `logs/criteria_rev3_reagent_nodes.txt` | 0 |

## Item 2 - `tests/browser/state_project_smoke.py:255`

Already resolved on this tree (the prior pass's `.py` edit landed): line 255 is
`                checks.append('anonymous reader sees the public graph and the opened project working notes')`
with no ` by the API')` tail, and the file compiles (it is inside `logs/rev3_rerun_integrity_and_criteria.txt`
Run 1's whole-repo `.py` compile pass, bare exit code 0).

## Item 3 - `web/src/__tests__/views/StateProject.test.ts`

The r3 candidate shipped this file uncompilable for vitest. The `it(...)` closer on line 78
had a SECOND `});` glued onto it - `  });});` - which closed the surrounding
`describe('Long-lived project pages')` early; the describe's real closer then had nothing to
close, and esbuild failed the whole suite with `StateProject.test.ts:178: Unexpected "}"`. The
r2 note described the earlier statement+closer glue (`toHaveBeenCalled();  });`) as fixed; the
residue that actually broke the build was the closer-on-closer on the NEXT line, and it is only
now gone.

- before (line 78): `  });});`
- after  (line 78): `  });`

The file's braces are balanced from line 1 to line 368 (every `describe`/`it` opens and closes
on its own line); verified by reading the whole file and confirmed by the gate's `node:test`
surface.

## Item 1b - the scanner gap this rev closes (why "hits: 0" was a false green)

This card's own integrity scan (`tests/unit/test_source_files_are_intact.py`) is the thing that
was supposed to catch a broken `.ts`, and it reported **hits: 0** while Item 3's file was
uncompilable. Its web rule was `;[ \t]+\)` - a semicolon, AT LEAST ONE SPACE, then a `})`
closer. That matches `foo();  });` but NOT `});});`, where the closer is grafted onto another
closer with no space between. That is the exact shape Item 3 shipped. Rev 3 relaxes the rule to
`;[ \t]*\)`, and `test_the_detectors_see_the_known_corruption_shapes` now also asserts
`GRAFTED_CLOSER.search("  });});")` is truthy, so the detector must see the shape or the test
fails. After the fix, `tests/unit/test_source_files_are_intact.py` is 4 passed, bare exit code 0
(`logs/rev3_rerun_integrity_and_criteria.txt`, Run 1) - green now only because the file is fixed
AND the detector sees what it previously missed.

## Item 1 / item 6 - the scan and its positive control

The command, environment and bare exit code are in `logs/source_integrity_scan.txt`; the script
is `tests/unit/test_source_files_are_intact.py`. The scan walks the repository, not the r2
reviewer's list: every `.py` (must compile), every `.md`/`.txt` (no fragment of ≥ 24 characters
that immediately repeats itself while carrying ≥ 2 words), every `.ts`/`.svelte` (no group
closer grafted onto a statement OR onto another closer - see Item 1b). Each walk asserts a
minimum file count, so a walk that finds nothing fails instead of passing. The pole stays the
glued tail edited back into `tests/browser/state_project_smoke.py:255`:
`logs/source_integrity_scan_planted_defect.txt`, bare exit code 1, naming that file and line.

Limits, stated as limits: (a) this step's tool surface has no shell, so base revision
`79d6f5eb` could not itself be checked out and scanned; the base shape is asserted inside the
module's own positive controls instead; (b) the `.ts`/`.svelte` rule is a structural rule for a
grafted closer and does not attempt a TypeScript parse; (c) `final/` and `logs/` sit outside the
scan surface - this note quotes before/after fragments on purpose, and `logs/` holds captured
output.

## Item 4 - the reagent-swapped tests

`git diff 149fe14d HEAD -- tests web/src/__tests__` could not be run here (no shell), so the
table below was rebuilt by reading the test sources for this tree's still-private reagent and
running its nodes (`logs/criteria_rev3_reagent_nodes.txt`, bare exit code 0; pytest errors on
an uncollected node id, so the run also settles that every row exists).

| test | still-private reagent it now uses |
| --- | --- |
| `tests/unit/test_state_read_visibility.py::TestAnonymousHttp::test_the_visitor_reads_the_notebook_but_not_the_mailbox` | `list_director_messages` |
| `...::TestRefusalWording::test_a_read_refusal_talks_about_reading` | `list_director_messages` |
| `...::TestRefusalWording::test_a_bad_credential_reads_the_same_as_no_credential` | `list_director_messages` |
| `...::TestNoLeak::test_the_refusal_bodies_carry_no_still_private_text` | `list_director_messages` |
| `...::TestEmbedderDefaults::test_an_embedder_without_a_read_verdict_gets_no_MORE_than_the_public_reads` | the still-private read set at `tests/unit/test_state_read_visibility.py:51` |
| `tests/unit/test_state_project_privacy.py::TestGateHoldsAgainstRouteAuthor::test_HONEST_declares_the_private_read_it_serves` | `list_director_messages` |
| `tests/integration/test_state_graph_entrypoints.py::test_anonymous_reader_sees_the_graph_and_the_notebook_but_not_the_mailbox` | `list_director_messages` |
| `tests/integration/test_state_run_summary.py::test_full_host_summary_is_a_public_read_while_the_mailbox_and_writes_stay_closed` | `list_director_messages` |

Comparison with the r2 review's table, row by row: the eight rows above are the eight the r2
table already carries, so this rev adds no name to it; this table is rebuilt from the tree's
sources, NOT asserted to equal r2's. The reagent read turned up two further carriers that the r2
table listed apart from the eight: `tests/integration/test_state_portfolio.py` (line 266, and the
pair at line 272) and `tests/integration/test_design_review_flow.py` (lines 99 and 102), both
using `list_director_messages`; `test_state_portfolio.py:272` pairs it with
`get_driver_guide_section`, still writer-only in this tree, whose reclassification belongs to the
guard-shape card. `web/src/__tests__/views/StateProject.test.ts` is in the touched set but is not
a reagent swap (Item 3 above).

## Item 5 - the three generic prose sentences

- `docs/state-graph.md:179-181` - before: "State project data is private to authorized writers; there is not yet a separate per-state-project ACL or a distinct cryptographic verifier role." After: "A state project's graph and its working notes become readable by anyone once the project is opened (the owner's ruling of 2026-09-22, `note://aitelier/546f3b521eca`); every other state project stays private to authorized writers, and an unopened project is refused exactly like an absent one. There is not yet a separate per-state-project ACL or a distinct cryptographic verifier role."
- `docs/state-project-ui-migration.md:12` - before: "- `#/state-projects`: private project catalog with goal counts and dispatch policy." After: "- `#/state-projects`: project catalog with goal counts and dispatch policy; an opened project's graph and working notes read publicly, everything else stays writer-only."
- `docs/state-project-ui-migration.md:122-124` - before: "State data remains administrative-writer scoped; this does not introduce a separate per-project ACL or cryptographic verifier role." After: "An opened state project's graph and working notes are anonymous-readable (the owner's ruling of 2026-09-22); every other state project and every other state read remains administrative-writer scoped. This does not introduce a separate per-project ACL or cryptographic verifier role."

## Out of scope this rev (unchanged behaviour)

- The `PUBLIC_READS` / `WRITER_ONLY_READS` membership is untouched (6 note reads public,
  mailbox/guide/events/visibility writer-only; writes still writer-only).
- `get_driver_guide_section` classification stays with the guard-shape card.
