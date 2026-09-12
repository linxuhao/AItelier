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

## Writing code
Use `apply_patch(patch)` for Add/Update/Delete File operations. Read current ranges
with `raw=true`, include exact unique context, and group disjoint hunks/files in one patch.
Follow the tool's strict format; paths are repo-relative. On partial I/O failure,
reread reported changed paths and repair the remainder rather than replaying.
Call `finish_step` when done — the suite then re-runs to check you.
