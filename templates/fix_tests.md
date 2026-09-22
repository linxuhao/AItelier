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
Use `apply_patch` for code changes. To edit an existing file, prefer
`references`: read the range, quote the `sha` from its `citation`, and send only
the new text. Use `patch` (Begin/End Patch) for Add/Delete File operations and
diff-shaped edits, reading ranges with `raw=true` first. If a hunk is stale or
ambiguous, reread that range and cite its `sha` instead of copying more of the
file. Group disjoint hunks/files in one call, but keep each call well inside the
output ceiling — a call cut off mid-JSON executes nothing. Split a large change
across several calls on separate turns rather than sending the whole file at once.
Follow the tool's strict format; paths are repo-relative. On partial I/O failure,
reread reported changed paths and repair the remainder rather than replaying.
Call `finish_step` when done — the suite then re-runs to check you.
