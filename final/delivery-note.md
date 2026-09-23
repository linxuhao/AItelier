# notes-public rev 2 — delivery note (documentation & delivery only)

Date: 2026-09-23 (UTC).
Base / tree: `c037a54fc9281f1ab66f26f3e87f6684ea3914d0` (the r2 candidate).
Ruling: `note://aitelier/546f3b521eca` — owner 2026-09-22.
Review: `~/.AItelier/director/reports/notes-public-r2-review-20260923/review.md`
sha256 `2ce63447de42406cf081df7a453c65437630ea5ca56929afc8d2bce6a61fda18` — 4 green / 1 red.

This rev touches **no code behaviour**. It closes only the red criterion
`the-ruling-is-written-where-the-table-is` (its five fix-items below) and re-runs the four
already-green criteria on this tree.

## Every number with the log that produced it

| number | meaning | log file | bare exit code |
| --- | --- | --- | --- |
| 143 passed | five criteria/gate test files re-run post-edit | `logs/criteria_rerun_5files.log` | 0 |
| 173 passed | eight reagent-swapped guard + leak tests re-run post-edit | `logs/criteria_rerun_8files.log` | 0 |
| timeout (-9) | full `tests/` single run vs 300s host probe wall | `logs/full_suite_attempt.log` | n/a (killed) |

The full-suite-in-container bare RC (4773 passed, RC 0) belongs to r2 and is unchanged
because this diff is comment/docstring/i18n/markdown only; the authoritative run is the
step's `run_tests` gate, not a probe (see `logs/full_suite_attempt.log`).

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
ruling — deleted (no reference existed; `grep noteUnavailable` → only its definition).

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
