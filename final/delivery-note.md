# notes-public rev 2 — delivery note (documentation & delivery only)

Date: 2026-09-23 (UTC).
Base / tree: `c037a54fc9281f1ab66f26f3e87f6684ea3914d0` (the r2 candidate).
Ruling: `note://aitelier/546f3b521eca` — owner 2026-09-22.
Review: `~/.AItelier/director/reports/notes-public-r2-review-20260923/review.md`
sha256 `2ce63447de42406cf081df7a453c65437630ea5ca56929afc8d2bce6a61fda18` — 4 green / 1 red.

This rev touches **no code behaviour**. It closes only the red criterion
`the-ruling-is-written-where-the-table-is` (its five fix-items below) and re-runs the four
already-green criteria on this tree.

## Criteria, one by one (this rev, this tree)

- `an-opened-projects-notes-are-readable-anonymously` — **pass**. `tests/unit/test_state_project_privacy.py::TestTheWorkingNoteReadsAcrossThreeProjectStates::test_the_six_note_reads_over_the_three_project_states` is the cross product **6 note reads × 3 project states = 18 cells** (opened → 200 carrying the body; unopened → 403; absent → 403; the two refusals byte-identical). Re-run on this tree: `logs/criteria_named_nodes_13.txt`, bare exit code 0; the same file whole, again, in `logs/criteria_rerun_5files.txt`, bare exit code 0.
- `everything-else-private-stays-private` — **pass**. In the same log: `TestClassification::test_everything_the_ruling_did_not_name_stays_private` (the director mailbox, the events/long-poll plumbing, `project_visibility`) and `TestClassification::test_an_unclassified_read_is_private_by_default`, which judges a made-up action name private. The four note writes stay refused in `TestAnonymousHttp::test_writes_stay_refused_anonymously`, inside `logs/criteria_rerun_5files.txt`. Bare exit code 0 for both logs.
- `no-note-read-leaks-an-unopened-project` — **pass**. `TestListingDoesNotNameRefused::test_no_private_body_surfaces_and_scanner_has_a_positive_control` (the scanner's positive control sits in the same assertion, so a scanner that scans nothing cannot pass it), `test_public_gate_table_unopened_opened_closed` over the whole public-gate set, and `TestExhaustiveDoors::test_every_read_action_is_refused_iff_it_is_private` over every action and every GET route read from the mounted app's own OpenAPI document. Same log, bare exit code 0.
- `the-ruling-is-written-where-the-table-is` — **pass after the fixes below**; its five fix-items are each shown before → after further down.
- `the-whole-suite-stays-green-in-the-container` — **not re-measured by this step; reported, not claimed.** One `pytest tests` run and one `pytest tests/unit` run were both killed at the host probe's 300-second wall with recorded exit status -9 (`logs/full_suite_attempt.txt`), so this step holds no bare whole-suite RC for this tree. The container-wide number for this behaviour is r2's 4773 passed / bare RC 0 on base `c037a54fc9281f1ab66f26f3e87f6684ea3914d0`, and this rev's diff moves no behaviour (markdown, comments, two test docstrings, one unused i18n key). What this tree does measure is the whole State surface: 20 `test_state*.py` files, 447 passed in three chunks, bare exit code 0 in every chunk (`logs/state_surface_chunks.txt`). The authoritative whole-suite run is this pipeline's own `run_tests` gate, which the step's test phase executes uncapped.

The mutation poles for the first three criteria — dropping the project-privacy half, moving one still-private read into the public table, dropping the cross-project filter — were planted and read in r2 and named by the r2 review. This rev changes no code, so it re-runs the named nodes on this tree instead of re-planting them.

## Every number with the log that produced it

Every delivery log in `logs/` ends in `.txt`: the repository's `.gitignore` line 67 is `*.log`, so a `.log` output file is git-ignored and the implement lifecycle hook refuses the whole step.

| number | meaning | log file | bare exit code |
| --- | --- | --- | --- |
| 150 passed in 32.13 s | five criteria/gate files re-run post-edit on this tree | `logs/criteria_rerun_5files.txt` | 0 |
| 188 passed in 34.33 s | eight files carrying the private-read reagents and the leak checks | `logs/criteria_rerun_8files.txt` | 0 |
| 8 passed in 1.40 s | the eight reagent-swapped guard/leak nodes, alone | `logs/criteria_rerun_8files.txt` | 0 |
| 13 passed in 2.43 s | criteria 1/2/3 named nodes: the 6 × 3 = 18-cell cross product, the unknown action, the leak scanner | `logs/criteria_named_nodes_13.txt` | 0 |
| 447 passed (196 + 161 + 90) | every `test_state*.py` file: 20 files in 3 chunks | `logs/state_surface_chunks.txt` | 0 in each chunk |
| exit status -9 at 300 s | `pytest tests`, and `pytest tests/unit`, each past the probe wall | `logs/full_suite_attempt.txt` | none taken (killed) |

Each exit code above is the focused probe's own recorded `exit_status`; no exit code in this
delivery was taken through a pipe. The container command form the r2 review used
(`-w <tree> -e PYTHONPATH=<tree>`) belongs to the `run_tests` gate and is recorded in that
gate's own log, not here.

## Item 1 — broken/duplicated doc sentences (before → after)

- `docs/state-graph.md` 206-209 — before: the sentence
  "(`core.state_commands.PUBLIC_READS`, below) is what decides which of them an anonymous
  REST visitor may read." appeared **twice**; after: single copy.
- `docs/state-graph.md` 431 — before: "...for each transport.**token; use the host's supported
  authenticated channel for each transport.**" (duplicated tail); after: single "...for each transport."
- `docs/state-graph.md` 451 — before (broken concat + contradicted the table):
  `GET .../driver-note/history/search       publicistory/search       writer-only`;
  after (matches the ruling — `search_driver_note_history` is a public read):
  `GET .../driver-note/history/search       public`.
- `docs/state-project-ui-migration.md` 96 — before: "...classification). These extra**assification). These extra**";
  after: "...classification). These extra" (fragment no longer repeated).

## Item 2 — StateProject.svelte comment vs code

- before (25-26): "`canWrite` still decides write affordances and whether the working notes
  may be requested at all." — but the note effect (line 59) gates on `canRead` (`mayReadNotes = canRead`).
- after (25-27): "`canWrite` decides the write affordances, while `canRead` is what decides
  whether the working notes may be requested at all (see the `mayReadNotes` effect below)."

## Item 3 — the ruling recorded in code, verbatim

`core/state_commands.py`, above `PUBLIC_READS` (556-563), now states the ruling lives at
`note://aitelier/546f3b521eca` and quotes the owner's Chinese verbatim
"public的 project的working note也可以public， private project的working note继续private"
with its translation, alongside the pre-existing English line and the named superseded
2026-09-21 half-sentence. (English line and the replaced "keeps the working notes shut"
half-sentence were already present from r2; the note:// pointer and the Chinese verbatim are new.)

## Item 5 — mislabelled docstrings + the unused i18n key

Two test docstrings described a **never-opened** project as "opened"; both fixtures use
`project_read_trusted=True` (the project half is bypassed) and neither ever calls
`open_project`. Corrected to say the note reads are public *actions* and that the project
half passes only because the service is read-trusted here:
- `tests/integration/test_state_graph_entrypoints.py` — `live` fixture's `game` (never opened);
  test `test_anonymous_reader_sees_the_graph_and_the_notebook_but_not_the_mailbox`.
- `tests/integration/test_state_run_summary.py` — `system` fixture's `game` (never opened);
  test `test_full_host_summary_is_a_public_read_while_the_mailbox_and_writes_stay_closed`.

i18n key `noteUnavailable` (`web/src/lib/stateI18n.svelte.ts`) was **unused** and carried a
now-false claim ("Sign in with writer access to read this project") that contradicts the
ruling — deleted. No view asked for it; `noteUnavailable` now appears in this file only, and
the notice the view really renders is the `private` key at `web/src/views/StateProject.svelte:109`.

## Reagent-swapped tests (private-guard reagents now a still-private action)

Where the guard reagent used to be a note read, it is now the director mailbox
`list_director_messages` (or the guide/events), preserving the thing each test proves.
The r2 review rebuilt 8 from the diff; these are those 8:
1. `tests/unit/test_state_read_visibility.py::TestAnonymousHttp::test_the_visitor_reads_the_notebook_but_not_the_mailbox`
2. `tests/unit/test_state_read_visibility.py::TestRefusalWording::test_a_read_refusal_talks_about_reading`
3. `tests/unit/test_state_read_visibility.py::TestRefusalWording::test_a_bad_credential_reads_the_same_as_no_credential`
4. `tests/unit/test_state_read_visibility.py::TestNoLeak::test_the_refusal_bodies_carry_no_still_private_text`
5. `tests/unit/test_state_read_visibility.py::TestEmbedderDefaults::test_an_embedder_without_a_read_verdict_gets_no_MORE_than_the_public_reads`
6. `tests/unit/test_state_project_privacy.py::TestGateHoldsAgainstRouteAuthor::test_HONEST_declares_the_private_read_it_serves`
7. `tests/integration/test_state_graph_entrypoints.py::test_anonymous_reader_sees_the_graph_and_the_notebook_but_not_the_mailbox`
8. `tests/integration/test_state_run_summary.py::test_full_host_summary_is_a_public_read_while_the_mailbox_and_writes_stay_closed`

Also carrying the mailbox reagent (broader portfolio / design-flow refusals, not counted in
the 8): `tests/integration/test_state_portfolio.py`, `tests/integration/test_design_review_flow.py`.

## Out of scope this rev (unchanged behaviour)

- The `PUBLIC_READS` / `WRITER_ONLY_READS` membership is untouched (6 note reads public,
  mailbox/guide/events/visibility writer-only; writes still writer-only).
- `get_driver_guide_section` classification is owned by the guard-shape card, not this one;
  its membership is not touched here.
- No acceptance pin or playtest assertion was loosened.
