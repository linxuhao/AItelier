# Fix failing tests

The test suite is failing (or you're asked to make it pass). Read the failure,
fix the code, and make the suite green. The tests run automatically after you
finish — if they still fail, you'll be sent back with the new report.

## Inputs
- **task.md** — optional hint about what to fix.
- **test_report.json** (on a re-run) — the current failures: `summary`,
  `failures`, `returncode`. Read it and target the actual failing tests.
- **the repository** — read the failing code and the tests before editing.

## Your task
1. Read the failure and the code under test. Understand WHY it fails before
   changing anything.
2. Fix the smallest thing that makes the test correct. Prefer fixing the code;
   only change a test if the test itself is wrong (and say so).
3. Do not weaken or delete a test to make it pass — that's a false green.

## Writing files
`edit(file, old_str, new_str)` for existing files (unique `old_str`, rest
preserved verbatim); `create` only for new files. Repo-relative paths. Call
`finish_step` when done — the suite then re-runs to check you.
