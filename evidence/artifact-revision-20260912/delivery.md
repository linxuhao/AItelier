# Artifact revision handoff — 2026-09-12

## Implementation commits and locations
- AItelier runtime/test/package commit: `33598570e63df1f0a4fef1cb69a0a5b551e2eb6f`.
- SkillFlow commit: `f227f74ee00969c3c3960caa07f05a90ebbe8b08`.
- Both branches: `fix/artifact-revision-20260912`.
- AItelier worktree: `/home/linxuhao/AItelier/.claude/worktrees/artifact-revision-20260912`.
- SkillFlow worktree: `/home/linxuhao/stepflow/.claude/worktrees/artifact-revision-20260912`.
- This final handoff/evidence commit changes no runtime code after 3359857.

The tracked dependency is `skillflow-py==1.5.72+aitelier.output3`.
Tracked wheel: `vendor/wheels/skillflow_py-1.5.72+aitelier.output3-py3-none-any.whl`.
SHA-256: `3149c90b09b569f4996b252bb19c3d7b73a31de759ecc64962502d4421712cb1`.
All 81 packaged runtime files match the committed SkillFlow source and the
independently installed test target. The production and shared test environments
were not upgraded; wheel tests use `/tmp/aitelier-artifact-wheel-20260912`.

## Verification
Final installed-wheel results, without adding repeated batches:
- SkillFlow main + plugin suites: 1,137 passed.
- AItelier unit suites: 2,961 passed, 8 skipped, 1 confirmed pre-existing failure.
- Remaining AItelier Python tests: 500 passed, 1 skipped, 11 network tests deselected.
- Post-commit verification of 3359857: 50 relevant native/JSON/restart/budget/revision
  and full-pipeline integration cases passed.
- Separately, the director's existing uncommitted `forge_tool_impl.md` change was
  copied into another isolated checkout of 3359857. All 35 capability/prompt/
  artifact-host tests passed there, including the otherwise failing assertion.
  That file is NOT included in this repair branch and the original was untouched.

The lone baseline failure is `test_the_tool_build_template_promises_no_test_runner`.
It was reproduced on unmodified fc80b58 with the prior installed output2 package.
The director's shared-checkout template already fixes it. Preserve that change
when integrating; do not replace the shared checkout wholesale with this branch.
See `review.md` for first failures, test-fixture changes and environment handling.

## Shared checkout preservation
At handoff, AItelier main still has the original unrelated changes:
- `core/dpe_pipeline.py` (recall fix)
- `templates/forge_tool_impl.md` (director capability fix)
- `tests/unit/test_recall_observation.py`
- untracked `configs/gen_coop_shell_ui60_20260907.yaml`
SkillFlow main remains unchanged and clean. No remote push, release, service
restart, live graph/candidate mutation or run approval/cancellation was performed.

## Director deployment window
Production bind-mounts the main checkout. Save/commit the existing local changes
under their current ownership, integrate the reviewed repair branches and bundled
wheel, then rebuild/restart at a coordinated safe point. Do not change code while
an in-flight host can hot-load only half of the new contract. Do not automatically
resume or repin an old run. Confirm a new revision first: change A among A/B/C,
validate and publish once, and verify B/C remain byte-identical. Task-loop/budget/
review policy and the game's source remain the director's existing responsibility.

The collaboration notice at the top of DRIVER_STATE.md is updated separately.
