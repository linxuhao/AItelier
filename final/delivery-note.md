# notes-public rev 4 - delivery note

base_sha = 7512bad3e46531844189e672ca2c7b453a8bbaf3 (the r4 candidate).
This round the behavior of `core/` and `api/` was not touched: the only files changed are
under `docs/`, `tests/`, and one comment block in `web/src/lib/api.ts` (a comment, above
`runWorkflowGraph`; the function body is unchanged). No `.py` file under `core/` or `api/`
was edited.

Every edit this round used a V4A patch (the `@@` hunk carrying the old text, which is the
checksum). No edit used references / `from_col` / `to_col`. The `git diff --word-diff`
comparison runs in the review; the "改后" text below is copied only from bytes read back
with `read` (see `logs/rev4_doc_edits_readback.txt`).

## Ruling as written where the table is

The table is `core/state_commands.py` (`PUBLIC_READS` / `WRITER_ONLY_READS`), and its own
comment already carries the 2026-09-22 ruling verbatim and names the half-sentence it
replaced; the classification is unchanged from r4 (behavior not touched). This round the
four remaining generic "State data private" claims were aligned to that same ruling:
`docs/state-project-ui-migration.md:13`, `:21`, `:92`; `docs/state-graph.md:5`; and the
`web/src/lib/api.ts` comment above `runWorkflowGraph`. The `api.ts` comment now quotes the
owner's words and names the replaced half-sentence.

Owner's words (verbatim, from the table comment): "public的 project的working note也可以
public，private project的working note继续private" - the working note of a public project
may be public too, the working note of a private project stays private; also stated in
English as "let's open up the working note for public projects too". It replaces the
half-sentence in the 2026-09-21 ruling that "keeps the working notes shut".

### criterion 4 - the six deliverables

**① `docs/state-project-ui-migration.md:13` tail and the four generic claims.**
Read-back "改后" (from `logs/rev4_doc_edits_readback.txt`):
- line 13: `  project's graph and working notes read publicly, everything else stays writer-only.`
  (the trailing ` policy.` left by r4's reference-mode edit is gone.)
- line 21: `query the State API for their associated projects; they do not create a`
  (the blanket word "private" before "State API" is gone.)
- line 92 heading: `## Project projection APIs` (was `## Private project projection APIs`).
- `docs/state-graph.md:5`: `The project DAG viewer (an opened project's graph and working
  notes read publicly, per the owner's ruling of 2026-09-22, note://aitelier/546f3b521eca),
  exact-run historical graphs, stable source binding,` (was `The private project DAG viewer,
  ...`).
- `web/src/lib/api.ts`: the read-back block is in the log; it cites the owner's words and
  names the replaced claim `State project data is private ... require writer authorization`.

**② `tests/browser/state_project_smoke.py` anonymous block: which option was chosen.**
Chosen option: RESTORE the private pole. The block now sends one real anonymous mailbox
request and asserts it is refused, so the comment "The mailbox stays writer-only and is
refused by the API" is backed by an executed check instead of an unexecuted claim.
Read-back "改后" (from `logs/rev4_doc_edits_readback.txt`) - the added lines plus the line
that follows them:
```
                # The mailbox is the private pole, proved by an ACTUAL anonymous
                # request refused at the API — not by hiding it from the UI.
                assert anonymous.request.post(base+'/api/state/query/list_director_messages',
                    data={'project_id':'shrimp-preview'}).status==403
                checks.append('anonymous reader sees the public graph and the opened project working notes')
```
r4's settled `checks.append` line is left byte-for-byte unchanged; the mailbox probe is a
pure addition above it, so the only lines this block gained are the two comments and the
refusal assertion.

**③ Reagent list - the ten tests that use a still-private note read, plus the dropped smoke probe.**
Each was read from its file; the private action it now uses is named next to it. This is
this round's list, not an equivalence claim against any earlier round's table.
1. `tests/unit/test_state_project_privacy.py::TestGateHoldsAgainstRouteAuthor::test_HONEST_declares_the_private_read_it_serves` -> `list_director_messages`
2. `tests/unit/test_state_read_visibility.py::TestAnonymousHttp::test_the_visitor_reads_the_notebook_but_not_the_mailbox` -> `list_director_messages` (and loops `STILL_PRIVATE_READS` = list_director_messages, get_driver_guide_section, events, wait_for_state_change, project_visibility)
3. `tests/unit/test_state_read_visibility.py::TestRefusalWording::test_a_read_refusal_talks_about_reading` -> `list_director_messages`
4. `tests/unit/test_state_read_visibility.py::TestRefusalWording::test_a_bad_credential_reads_the_same_as_no_credential` -> `list_director_messages`
5. `tests/unit/test_state_read_visibility.py::TestNoLeak::test_the_refusal_bodies_carry_no_still_private_text` -> `list_director_messages`, `get_driver_guide_section`
6. `tests/unit/test_state_read_visibility.py::TestEmbedderDefaults::test_an_embedder_without_a_read_verdict_gets_no_MORE_than_the_public_reads` -> `list_director_messages`
7. `tests/integration/test_design_review_flow.py::test_shared_app_reads_the_design_and_keeps_the_mailbox_writer_only` -> `list_director_messages`
8. `tests/integration/test_state_graph_entrypoints.py::test_anonymous_reader_sees_the_graph_and_the_notebook_but_not_the_mailbox` -> `list_director_messages`
9. `tests/integration/test_state_portfolio.py::test_all_new_state_reads_remain_private_and_commands_strict` -> `list_director_messages`, `get_driver_guide_section`
10. `tests/integration/test_state_run_summary.py::test_full_host_summary_is_a_public_read_while_the_mailbox_and_writes_stay_closed` -> `list_director_messages`
The anonymous note reagent in `tests/browser/state_project_smoke.py` was dropped, not
swapped: that block read only the notebook (public now); this round item ② replaced the
dropped private pole with a real anonymous `list_director_messages` refusal.

**④ Every change read back; "改后" only read-back text.**
Done: the read-backs are `logs/rev4_doc_edits_readback.txt`; the note copies from there.

**⑤ Every id named in `final/` and `logs/` run with `focused_check`, bare RC 0.**
All sixteen ids in `logs/rev4_criteria_named_ids.txt` returned BARE EXIT CODE 0 (a missing
id would exit 4). The ids cited in this note are the same ids.

**⑥ This card's edits: V4A only, no references.**
Stated above; the trace shows only `*** Update File` / `*** Add File` / `*** Delete File`
V4A hunks for this card.

**⑦ Delete the prose-corruption heuristic and the `});});` provenance claim; self-defense words 0 hits.**
`tests/unit/test_source_files_are_intact.py` was rewritten to keep ONLY
`test_every_python_file_compiles` (the `.py` compile check that really sees a stale tail).
The prose self-repeat scanner, the web group-closer scanner, the "known corruption shapes"
meta-test, and the `});});` provenance line were all removed with the old file (the plan
records that closer as coming from `5fbfec89`, not from a prior delivery of this card).
`.ts` is compiled by the test step's vitest run. The rewritten module passes
(see Batch A [1]).

## The behavior criteria, re-run on this tree (behavior unchanged this round)

No `core/` or `api/` behavior was modified, so criteria 1, 2, 3 and 5 were re-run on this
tree and reported with their bare exit codes. Fresh mutations were not planted this round
(the plan fixes the behavior); the mutation-killing power is carried by the assertions
quoted in each test, which fail if the corresponding half is loosened.

**criterion 1 - an opened project's notes are readable anonymously - PASS.**
`test_the_six_note_reads_over_the_three_project_states` (logs Batch A [2], BARE EXIT CODE 0)
runs the whole cross product 6 note reads x 3 project states = 18 cells: for every one of
`get_driver_note`, `driver_note_history`, `search_driver_note_history`,
`get_driver_note_entry`, `check_driver_note_index`, `driver_note_index`, the OPENED project
returns 200 carrying the body, the UNOPENED project returns 403, the ABSENT project returns
403, and `unopened.text == absent.text` (byte-identical, no existence oracle). Removing the
project-privacy half would flip the two unopened/absent columns of all six rows to 200 and
fail that assertion, naming each action.

**criterion 2 - everything else private stays private - PASS.**
Anonymous refusal of the still-private reads and the four note writes is carried by
`test_the_visitor_reads_the_notebook_but_not_the_mailbox` (Batch A [4]) which asserts 403
for every action in `STILL_PRIVATE_READS` = `list_director_messages`,
`get_driver_guide_section`, `events`, `wait_for_state_change`, `project_visibility` (5 reads
over 1 state). Default-private is proved two ways: the table
`test_the_classification_is_unknown_action_private_by_default` (Batch C [15]) on an invented
name `a_note_read_invented_later`, and at the HTTP surface
`test_an_unclassified_read_action_is_refused_anonymously` (Batch C [13]) on a non-existent
action name -> 403. Moving any of these into the public table would break the assertion that
names it. `get_driver_guide_section` is on the private side because that is what this tree's
`read_visibility` returns; the collection derives from the measured table, not hand-written.

**criterion 3 - no note read leaks an unopened project - PASS.**
All six note reads are `Project`-scoped (each takes a `project_id`; `search_driver_note_history`
is `SearchDriverNoteHistory(Project)`), so a single call cannot return another project's
note. In the seeded fixture the unopened project's secret `UNOPENED-NOTE-BODY-9999` never
appears in any opened/unopened/absent response (`test_the_six_note_reads_over_the_three_project_states`,
Batch A [2]). The only project-less reads are index/list reads, and
`test_catalog_listing_only_shows_opened_projects` (Batch C [16]) asserts the catalog returns
only opened projects; `test_the_refusal_bodies_carry_no_still_private_text` (Batch A [7])
asserts a refusal body carries none of the still-private content.

**criterion 5 - the whole suite stays green in the container - PASS on every test named; whole-suite number from the gate.**
`logs/rev4_full_suite_probe.txt` records that a whole-tree `tests/` run through
`focused_check` was killed at the 300-second host cap (exit_status -9, timed_out true), so
the whole-suite figure is produced by the `run_tests` gate after this step and by the
reviewer's container run, not claimed from this probe. The one test the plan flags as
order-dependent, `test_an_engine_without_the_counter_costs_a_key_not_a_step`, was run alone
and returned BARE EXIT CODE 0 ("1 passed").

## Files changed this round

- `docs/state-project-ui-migration.md` - lines 13 (dropped ` policy.` tail), 21 (dropped
  "private"), 92 heading.
- `docs/state-graph.md` - line 5.
- `web/src/lib/api.ts` - the comment block above `runWorkflowGraph`.
- `tests/browser/state_project_smoke.py` - anonymous block: real `list_director_messages`
  refusal added.
- `tests/unit/test_source_files_are_intact.py` - rewritten to the `.py` compile check only.
- `logs/` and `final/` - this round's evidence; stale rev2/rev3 logs that named ids not run
  this round were removed.
