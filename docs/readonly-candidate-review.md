# Read an exact completed source candidate

`run_pipeline(config="code_review", against_project=producer_project,
against_run=producer_run, against_commit=candidate_sha, seed_text=summary_and_diff,
checkpoints="ask")` selects the complete candidate tree for repository reads.
`against_run` is an exact completed SkillFlow run ID, not a project alias.
`against_commit` is its exact lowercase 40-character Git commit identity.

The producer must already have an immutable `artifact_ref` on its existing State
attempt. The attempt's execution project, completed run, and owned run-isolation
repository must agree with `against_project`. A different repository is refused
even if it contains an equal commit SHA. No branch tip or current code_changes
text is accepted as the artifact declaration. A retained candidate remains valid
when its producer branch advances or is archived, provided its Git object remains
available in that recorded repository. No automatic fetch/materialization occurs.
Producers without a retained State candidate declaration cannot use this exact
binding; an operator must record the candidate through the existing State path.

Exact binding is supported for butler-driven readonly configs (`repo_mode: none`,
`scheduler_owned: false`), including the shipped `code_review`. A scheduler-owned
config is refused: publishing its project/seed could let the poller provision a
source-HEAD snapshot before the explicit launcher pins B. No new scheduling or
identity framework is introduced.

Identity validation occurs before the review project/workspace, immutable seed,
run and driver are created. Missing commits, tag-object identities, incomplete
binding, foreign projects/repositories and unsupported configs fail closed.
The existing detached `read_snapshot` and run-specific resolver serve the selected
candidate; the shared source checkout is never reset or checked out. The reply
includes `review_candidate`, the run context retains the producer/commit identity,
and the run-isolation `base_sha` is the selected SHA.
Repository mutations remain outside the shipped review step's tool grants;
review verdict artifacts remain writable in that review's own output directory.
This preserves the existing tool boundary, not a sandbox against arbitrary host
code that reopens paths outside that boundary.

Omitting both `against_run` and `against_commit` preserves legacy calls:
`against_project` alone selects a source-HEAD snapshot, and no target remains a
diff-only review. The existing relay `request_base` worktree-only rule is unchanged.
Source completion and authored fixtures are not deployment or independent CPU
acceptance; the exact candidate requires a separate isolated evaluation.

## What a readonly snapshot binds, and when it is asked

A readonly review serves the candidate's files to the reviewer BEFORE the first
claim, so the binding is verified at resolution and at the host claim ingress,
which runs before SkillFlow's claim:

* the served tree's **HEAD** must equal the recorded `base_sha` (the candidate's
  commit identity); and
* its **tracked working tree and index** must match that commit — the BYTES and
  MODES the reviewer will read. A clean HEAD with a dirty tracked preview (an
  edited file, a staged change, a mode flip) is downstream or operator work, not
  the retained candidate, and is refused. The check reads only (`git diff-index`,
  no lock writes) and never stages, resets or checks anything out.

* at exact-candidate ingress AND every later resolver/preclaim boundary the served
  tracked lstat entry types, bytes and executable modes are compared directly to the trusted candidate tree
  (`git ls-tree -r B`, pure object reads) with the disk as the served side —
  independent of `assume-unchanged`, `skip-worktree`, the stat cache and
  `core.filemode=false`, none of which `diff-index`-style checks can see
  through. A mismatch refuses the review; no index refresh, reset, stage or
  flag clearing ever repairs it.
* a check that cannot be RUN is refused rather than read as "no differences":
  an unexpected `git` exit is an `IsolationUnavailable`, not a clean tree.

Admission is scoped exactly as the resolver's own answer is. A run WITH an
isolation record is bound to it. A run with NO record is answered by the
deployment's own engine: a run this deployment never created and a run that
predates isolation both keep their project-keyed answer, while a run created
here after isolation began whose record is gone is refused — a missing record
is never a blanket "legacy, admit". The ledger lookup is not wrapped, so an
unreadable table or an unrelated programming error stays distinct and visible
instead of being swallowed as admission. No SDK fork, install or upgrade is
involved; the failure is host source.

The owning run's existing `review_candidate` context is cross-checked against
its snapshot record and the completed producer's retained State declaration.
An exact-bound run cannot become repo-less, lose its base, or replace both the
snapshot and source record with an unrelated clone containing the same commit.
Real committed symlinks are compared by link text; a regular tree entry served
through a symlink is refused even when the target contains identical bytes.
Regular files are opened without following the leaf and checked through that
same descriptor. Verification never clears flags or repairs the index.

A local engine's `created_at` is not an isolation declaration by another engine.
For a record-less unbound run, the engine attached to the deployment ledger must
have created that run before its missing record can trigger the modern refusal.
Exact review context always requires the matching record. SQLite and accessor
errors remain visible; no exception is converted into legacy permission.
