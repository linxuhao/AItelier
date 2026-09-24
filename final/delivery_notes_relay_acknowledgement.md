# Delivery notes - relay acknowledgement proves reading, not recall

Round rev 3, 2026-09-24. The header-extraction and CJK-unit decisions from r2
stand and are not rewritten; this round moves the pass line off per-item
half-recall onto a language-neutral per-item test, and re-measures against the
recorded `f835fa35` instruction. Every number below names the log that produced
it; the whole-tree bare `pytest tests/` run is the run_tests gate on this same
tree (the implement probe is capped at 300s and cannot hold a ~590s run).

## 1. Changes

- `core/dpe_pipeline.py`
  - `_relay_work_items(instruction)` (r2, unchanged): the remaining work is the
    relay header's numbered task list, one item per entry. The brief body that
    follows is background, not an item, so the pass line does not grow with the
    brief. A single unnumbered instruction keeps the whole-instruction fallback.
  - `_ack_units` = `_ack_tokens` + `_ack_cjk_chars` (r2, unchanged): ASCII
    identifiers of length >= 3 (plus numbers, plus underscore parts) and CJK
    characters, so Chinese remaining work is counted, not read as empty.
  - `_acknowledges_incomplete` (this round): an item is "named" either when the
    acknowledgement carries a language-neutral anchor that item alone owns - a
    path, command, identifier, number or proper name shared with no sibling -
    or when it recalls two fifths of that item's own units. The r2 line
    (per-item half recall, threshold one half, no anchor rule) rejected every
    faithful restatement of a real relay header, because a compressed item such
    as "回合要省着用... 做完就调用 finish_step" carries its work in one anchor
    (`finish_step`) and a disciplined reader states it in fewer than half the
    characters. A wholly dropped item has no anchor the others can supply and
    no recall, so it stays unnamed; a single unstructured item still needs two
    fifths of all its units so a two-word fragment cannot stand in for it.
- `tests/unit/test_relay_acknowledgement_gate.py` (r2) and
  `tests/unit/test_relay_acknowledgement_real_instruction.py` (this round,
  against `tests/fixtures/relay_ack_f835fa35.json`).

## 2. Commands and bare exit codes (this round)

Logs are `.txt`, in `logs/`. Commands are
`python3.12 -m pytest -q -s --maxfail=1 -p no:cacheprovider <targets>`,
bare exit codes, no pipes.

| run | log file | bare RC |
| --- | --- | --- |
| gate + real-instruction files, 16 passed | `logs/relay_ack_r3_measurement.txt` | 0 |
| BASE (unmutated gate), 16 passed | `logs/relay_ack_r3_mutants.txt` | 0 |
| MUTANT 1 (r2 half-recall line), 1 failed, names must_accept[0] | `logs/relay_ack_r3_mutants.txt` | 1 |
| RESTORE 1, 16 passed | `logs/relay_ack_r3_mutants.txt` | 0 |
| MUTANT 2 (count-matches passes), 1 failed, unrelated-English escape | `logs/relay_ack_r3_mutants.txt` | 1 |
| RESTORE 2, 16 passed | `logs/relay_ack_r3_mutants.txt` | 0 |
| MUTANT 3 (any non-empty passes), 1 failed, "banana" escape | `logs/relay_ack_r3_mutants.txt` | 1 |
| RESTORE 3, 16 passed | `logs/relay_ack_r3_mutants.txt` | 0 |
| MUTANT 4 (CJK chars not counted), 1 failed, faithful Chinese denied | `logs/relay_ack_r3_mutants.txt` | 1 |
| RESTORE 4 (delivered tree), 16 passed | `logs/relay_ack_r3_mutants.txt` | 0 |
| r1/r2 chunk probes on this tree (api/browser/contracts/e2e/skillflow, integration) | `logs/full_suite.txt` | 0 |
| whole-tree `pytest tests/` | run_tests gate report (test step) | see gate |

## 3. Acceptance, one line each (cross-product stated with the number)

- `a-faithful-acknowledgement-passes-once` - this round's measured result,
  replacing r2's claim: on the real 2959-char `f835fa35` instruction all **5
  recorded `must_accept` restatements are accepted on their first call (5 x the
  real instruction)**, including the two English acks (step instances 7743 and
  7714) against the Chinese relay header, and on the synthetic tiers the
  faithful restatements are accepted (**paraphrase and verbatim x 1K/10K/50K = 6;
  Chinese x 1K/10K/50K = 3**), 0 refusals, `logs/relay_ack_r3_measurement.txt`,
  bare RC 0. Under the r2 pass line re-introduced as MUTANT 1 the same
  acceptance test refuses `must_accept[0]`, bare RC 1
  (`logs/relay_ack_r3_mutants.txt`): the acceptance rests on ASCII anchors the
  header itself carries (test paths, `pytest`, `finish_step`), which an honest
  English ack states in its own words.
- `an-arbitrary-acknowledgement-is-still-refused` - met: on the real instruction
  and the 1K/10K/50K tiers, wrong `retained_bytes`, empty list, empty-string
  entry, `banana`, a count-matching set of unrelated English sentences, another
  card's four Chinese closing tasks, a high-frequency-character Chinese sentence,
  and each of the four single dropped items are all refused: **4 tiers x 11
  shapes = 44 refusals, 0 escapes**, plus gate-file **3 x 5 = 15**, plus
  CJK-only dropped **4**, `logs/relay_ack_r3_measurement.txt`, bare RC 0.
  The two poles name themselves: count-matches-alone (MUTANT 2) lets the
  unrelated English set escape, any-non-empty (MUTANT 3) lets `banana` escape,
  each bare RC 1 (`logs/relay_ack_r3_mutants.txt`).
- `non-ascii-work-is-counted` - met: with Chinese-only remaining work, a faithful
  Chinese restatement is accepted and unrelated Chinese refused; the unrelated
  set contains a sentence built from the instruction's own high-frequency
  characters (的、一、个、把、与、在) plus unrelated nouns, which is refused:
  **3 tiers x 3 (faithful, unrelated, partial) = 9 checks** in the gate file plus
  **3 checks** (faithful/another-card/unrelated) on CJK-only work,
  `logs/relay_ack_r3_measurement.txt`, bare RC 0. MUTANT 4, which stops counting
  CJK characters, denies the faithful Chinese restatement (bare RC 1,
  `logs/relay_ack_r3_mutants.txt`).
- `the-suite-stays-green-and-the-note-carries-every-number` - the whole-tree
  `pytest tests/` verdict is the run_tests gate on this same tree (counts and
  bare RC in the test step report, cross-referenced in
  `logs/relay_ack_r3_measurement.txt` and `logs/full_suite.txt`); every other
  number above is written next to the log that produced it. This round's
  criterion-1 line is the measured result, not r2's claim.

## 4. Decisions

- Per-item anchor-or-two-fifths instead of per-item half-recall: half-recall of
  a single item is the same compression that made r2 reject a faithful restating
  ack; one language-neutral anchor the item alone owns is enough to show it was
  read, and a dropped item has no such anchor to borrow.
- Threshold two fifths of the item's own units: enough content to distinguish a
  paraphrase from a sibling's common characters, below what a compressed but
  faithful Chinese restatement of a disciplined-work item can miss.
- English acks against a Chinese header accepted: the header's tasks contain
  ASCII anchors (test paths, `pytest`, `finish_step`), which carry across
  languages, so an honest English ack clears the line.

## 5. Boundaries - what was not touched

- The gate's exact match of the retained byte total is unchanged; repository
  operations remain refused before an acknowledgement.
- `_relay_progress_context`'s retained-file inventory is unchanged.
- No pin, playtest assertion or existing test was loosened to change a result.
- The r2 header-extraction and CJK-unit code was not rewritten.
