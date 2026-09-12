# Artifact revision review

This is a separate final self-review pass, not an independent-agent review.
The plan preceded implementation; the director notice preceded both. All runtime
changes were made in isolated fix/artifact-revision-20260912 worktrees.

## Reviewed behavior
- One complete file candidate is prepared by SkillFlow before host execution.
  Each current file overrides its baseline counterpart; untouched bytes are copied
  exactly. This is file-set composition, not semantic text/JSON merging.
- Read, search, list, legacy read_file/list_tree, edit, validation and publication
  agree on the current candidate. Deleted files are not revived from old output.
- Retry/reclaim uses durable readiness for the same run/step instance/item. A small
  directory-ownership stamp is required in addition to DB claim inputs: a released
  claim still owns its files. A second run is refused before notifications can
  commit a claimed state. This was added after an executable concurrency test
  exposed the insufficiency of the initial DB-marker-only plan.
- Loop revisions inherit only the matching item; a fresh item or unrelated run
  does not inherit another one's output. Missing/replaced ready candidates fail
  with a restoration error rather than silently recreating deleted artifacts.
- carry_forward validation exhaustion fails closed; the published set remains
  untouched and the candidate remains available for repair. A valid empty set is
  publishable. Existing non-carry-forward and code contracts retain their behavior.
- Mixed output keeps the existing default-artifact requirement and explicit code
  slots. No configuration rule was relaxed to make the tests pass.
- Native does not recreate the engine-owned candidate when conversation recovery
  is unavailable. Both JSON fallback before writing and fallback after a malformed
  native response following a successful edit preserve the candidate.
- Budget exhaustion/restart and checkpoint boundaries remain unchanged. The real
  runner reads the resolved carry-forward flag from the claim's pinned graph.
- Agent-facing instructions describe current operations and affected missing IDs;
  no instruction requires re-emitting unchanged sibling cards.

## First failures and resolutions
All first-failure XML/logs are retained.
- engine-before.xml used a terminal fixture that never paused at its checkpoint;
  corrected fixture uses an actual downstream step. engine-before-corrected.xml
  then reproduced four real defects (deleted reads, empty-set resurrection,
  cross-run inheritance and invalid publication).
- boundaries-before/fixed exposed ownership of a released claim and premature
  notification side effects. Those runtime paths were corrected and retested.
- The first mixed-code-default fixture violated an existing graph rule. The final
  tests check valid mixed output and explicit rejection of that invalid form.
- A native transport exception after a successful write is handled within the
  native loop. A separate malformed-response case exercises actual fallback;
  the final tests cover both behaviors instead of claiming the first was fallback.
- Existing budget/explicit-revision fixtures wrote artifacts before the first
  claim or enabled carry-forward only on an unnormalized raw-config echo. They
  now produce the baseline through a real confirmed claim, write partial output
  during execution, and assert that the resolved pinned-graph flag wins. Budget,
  restart, prior-transcript and checkpoint assertions are retained.
- The test container delays orphan-process reaping. The test harness adopts and
  reaps only its own test descendants; all nine bash-admission lifetime tests pass
  under that harness. Product process/cancellation code was not changed.
- Web-search unit mocks require a configured URL. The harness supplies a .invalid
  dummy URL; HTTP remains mocked. No production endpoint was reconfigured.
- One baseline failure remains on this isolated AItelier branch: the committed
  forge_tool_impl template lacks its test-runner capability sentence. Reproduced
  on unmodified fc80b58 with the prior installed output2 engine. The director has
  already corrected that file in the shared checkout; their uncommitted change is
  deliberately preserved outside this repair branch.

## Final test scope
- Installed output3 wheel: SkillFlow main tests + plugin tests: 1,137 passed.
- AItelier unit batches: 2,961 passed, 8 skipped, 1 baseline template failure.
- AItelier remaining Python tests: 500 passed, 1 skipped, 11 network tests excluded
  by existing collection policy. Repeated batches are not added to these totals.
- Godot/browsers/live models/production runs were not exercised; no Docker rebuild
  or service restart was performed. This is implementation verification, not a
  measured claim of production token savings.
- Wheel/runtime payload verification is recorded separately; source, wheel and
  isolated installed payload are compared byte for byte.

## Handoff boundary
Keep both isolated branches until the director's coordinated merge/rebuild. Do not
mutate in-flight candidates, pinned graphs, checkpoints, budget policy or game code.
Retain the shared recall-observation and forge-tool-template fixes. No remote push,
public release or production dependency installation was performed.
