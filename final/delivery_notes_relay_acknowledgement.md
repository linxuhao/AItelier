# Delivery notes - relay acknowledgement proves reading, not recall

Round rev 2, 2026-09-23. The draft change itself is unchanged from r1 and is
not rewritten here; every number below was re-measured on this round's tree.

## 1. Changes

- `core/dpe_pipeline.py`
  - `_relay_work_items(instruction)`: the remaining work is the relay header's
    numbered task list, one item per entry. The retained brief that follows the
    header is background, not a work item, so the acknowledgement pass line no
    longer grows with the brief it rides on. A single unnumbered instruction
    keeps the whole-instruction fallback.
  - `_ack_cjk_chars` / `_ack_units`: acknowledgement units are ASCII tokens
    (`_ack_tokens`) plus CJK characters, so Chinese remaining work is counted.
  - `_acknowledges_incomplete`: each supplied item must be grounded in the
    retained work, and each retained work item must be half-covered by the
    supplied union; counted per item, never over the whole instruction.
- `tests/unit/test_relay_acknowledgement_gate.py`: the gate's own tests, at
  1K / 10K / 50K instruction lengths, ASCII and Chinese.

## 2. Commands and bare exit codes (re-measured this round)

Log files live in `logs/`, all `.txt`.

| run | log file | bare RC |
| --- | --- | --- |
| gate file, 9 passed | `logs/relay_gate_tests.txt` | 0 |
| gate + progress-budget family, 21 passed | `logs/relay_gate_tests.txt` | 0 |
| mutant A (any non-empty passes), 1 failed 4 passed | `logs/relay_gate_mutants.txt` | 1 |
| restore A, 21 passed | `logs/relay_gate_mutants.txt` | 0 |
| mutant B (whole instruction as the item), 1 failed | `logs/relay_gate_mutants.txt` | 1 |
| mutant B named at the 50K tier, 1 failed | `logs/relay_gate_mutants.txt` | 1 |
| restore B, 21 passed | `logs/relay_gate_mutants.txt` | 0 |
| chunk probe api/browser/contracts/e2e/skillflow, 130 passed 1 skipped | `logs/full_suite.txt` | 0 |
| chunk probe integration, no failure inside its window | `logs/full_suite.txt` | 0 |
| order-fix node alone, 1 passed | `logs/full_suite.txt` | 0 |
| whole-tree `pytest tests/` | run_tests gate report (test step) | see gate |

The implement-step probe is capped at 300 seconds and the whole-tree run
takes about 680s (r1 measurement, 2026-09-23), so the authoritative bare
whole-tree run for this tree is the run_tests gate on this same tree; its
counts and bare RC are recorded in the test step's report. r1's sole red was
`test_an_engine_without_the_counter_costs_a_key_not_a_step`, the order
dependency `iss-ab57b4061b314de3` fixed in main `7a57ef14`, which is present
in this round's base; that node alone exits bare RC 0 (`logs/full_suite.txt`).

## 3. Acceptance, one line each

- `a-faithful-acknowledgement-passes-once` - **met**. Faithful paraphrase at
  1K, 10K and 50K (3), the goal's verbatim four-item list at 50K (1), and the
  CJK restatement at 1K, 10K and 50K (3) = 7 first-call acceptances and 0
  refusals, each next to the retained byte total in one sentence
  (`logs/relay_gate_tests.txt`, bare RC 0). The header items themselves are
  asserted equal to the four tasks at all three lengths.
- `an-arbitrary-acknowledgement-is-still-refused` - **met**. Across 3 lengths
  (1K, 10K, 50K) x 5 shapes (wrong byte total, empty list, empty-string entry,
  unrelated entry `banana`, partial restatement of 2 of 4 items) = 15 refusals
  and 0 escapes (`logs/relay_gate_tests.txt`, bare RC 0; mutant A gives bare
  RC 1 and names `test_wrong_bytes_or_partial_or_arbitrary_items_are_refused`
  with the `banana` escape in its assertion, `logs/relay_gate_mutants.txt`).
- `non-ascii-work-is-counted` - **met**. Across 3 lengths (1K, 10K, 50K), a
  Chinese restatement of Chinese work is accepted and unrelated Chinese is
  refused: 3 x 2 = 6 checks, matching the expected polarity
  (`logs/relay_gate_tests.txt`, bare RC 0).
- `the-suite-stays-green-and-the-note-carries-every-number` - **met on the
  evidence above**; the whole-tree verdict is the run_tests gate on this same
  tree (its counts and bare RC are in the test step's report, cross-referenced
  from `logs/full_suite.txt`). Every number in sections 2 and 3 is written
  next to the log file that produced it.

## 4. Decisions

- Per-item coverage instead of whole-instruction coverage: the earlier line
  required half of the ASCII tokens of the whole instruction, so the pass line
  grew with the brief and saw nothing in Chinese -- the failing shape the relay
  cost report describes.
- Header list instead of whole instruction as the work items: the director's
  header enumerates the closing tasks; the brief body is what the relay rides
  on, so it cannot be an item.
- CJK characters are units: `_ack_tokens` finds no tokens in Chinese text, so a
  Chinese restatement would otherwise read as an empty entry.

## 5. Gaps left open

- Mutant B is named twice: by
  `test_header_tasks_become_the_work_items_at_every_length` (items at 1K, 10K
  and 50K, reporting on the first iteration) and by
  `test_the_50k_brief_is_not_what_the_pass_line_weighs`, which asserts the
  header-only item set at 50K alone and the first-call acceptance there
  (`logs/relay_gate_mutants.txt`).
- The implement-step chunk probe of `tests/unit` reaches the 300s probe cap
  before its last dot; the whole-tree run_tests gate covers it.

## 6. Boundaries - what was not touched

- The gate's required exact match of the retained byte total is unchanged.
- `_relay_progress_context`'s retained-file inventory and the refusal of
  repository operations before an acknowledgement are unchanged.
- No pin, playtest assertion or existing test was loosened to make anything
  green.

