# Writing Bench v2

`novel_writing_bench_v2` is a file-first workflow for a direct author. It freezes
the accepted novel and submitted files, builds editorial context, obtains an
independent literary review and a separate ledger audit, prepares an isolated
candidate, and waits for **one manual acceptance checkpoint**. Approval accepts
that exact candidate and attempts an authenticated private backup.

Waiting at `stage` is normal. Neither the author nor the platform needs to click
until the director has actually read the preview. There is no timeout approval,
no background rewrite, and no escalation merely because the checkpoint waits.
The generic auto-driver respects this graph's `manual_checkpoints_only` contract.

## Operator setup

This builtin graph, its tools and role configurations travel with the platform
repository. Loading them does not change any generated v1 graph, tool, run, or
pending chapter checkpoint. Projects must be enabled explicitly in
`$AITELIER_HOME/writing-bench/projects.json`:

```json
{
  "version": 2,
  "projects": {
    "my-novel": {
      "repo": "/absolute/path/to/accepted-novel",
      "branch": "master",
      "genesis": "40-character-genesis-commit",
      "submission_root": "/absolute/path/to/author-inputs",
      "max_context_bytes": 180000,
      "review_revision": 1,
      "backup": {
        "tool": "operator-approved-backup-tool",
        "config": "operator-approved-backup-config",
        "source_sha256": "64-character-reviewed-tool-source-hash",
        "seed": {
          "schema_version": 1,
          "state_project_id": "my-novel",
          "repo_path": "/absolute/path/to/accepted-novel",
          "owner": "authorized-owner",
          "repo_name": "private-novel",
          "remote_url": "https://github.com/authorized-owner/private-novel.git",
          "branch": "master",
          "tag": "novel-genesis"
        }
      }
    }
  }
}
```

Replace the illustrative identities with exact values. Do not put credentials
in this file. The backup adapter invokes the pinned, already authorized private
backup engine; it does not obtain tokens, create public repositories, force-push
or change the destination. A missing or changed engine fails closed.

Repository, author input and artifact storage roots must be distinct. The novel
is UTF-8 text under `novel/`; symlinks, submodules, path traversal and oversized
files are refused. Author refs are resolved component-by-component with
`O_NOFOLLOW`. Artifacts live below the injected data directory, never inside the
accepted source checkout. The default maximum file size is 2 MB and frozen
novel tree limit is 40 MB. Limits fail explicitly, not by trimming evidence.

The version uses `novel_alt` and `flash` model aliases already provided by the
host. Actual registered role settings, template contents, policy and relevant
input hashes bind the review. `review_revision` is an operator salt for changes
outside those tracked settings, such as a materially changed model-alias policy.

## Direct-author submission

Register the work in State first. A request does not invent a State owner or mark
a goal accepted. Write the prose and optional proposed ledger under the enabled
author root, then call the existing pipeline runner with `checkpoints=ask` and a
small JSON `seed_text`:

```json
{
  "version": 2,
  "project_id": "my-novel",
  "submission_id": "chapter-23-a",
  "base_commit": "40-character-current-accepted-commit",
  "mode": "new",
  "brief_file": "chapter-23/intent.md",
  "chapters": [
    {
      "chapter": 23,
      "title": "渡口",
      "prose_file": "chapter-23/prose.md",
      "proposed_events_file": "chapter-23/events.json"
    }
  ],
  "context_paths": ["novel/chapters/ch0014/prose.md"]
}
```

The heading is `# 第23章：渡口` followed by a newline. The file itself is copied
once and remains byte-identical throughout. Supplied ledgers are parsed strictly
and retain their exact keys, values and array order; whitespace and object-key
order have no semantic identity. Duplicate JSON keys, nonfinite values and a
duplicated nested state field at its parent are errors.

`proposed_events_file` is optional. Only a missing ledger calls the extractor.
Existing author ledgers never pass through a copying model. An extractor cannot
replace a supplied chapter's ledger, even in a mixed historical revision.

The ledger contains `chapter`, `title`, a **complete** `summary`, `events`,
`appearances`, `locations`, `thread_updates` and `arc_updates`. Optional provenance
fields are `submission_id`, `mode`, `proposal_only`, `revision_impacts`. Events
use the native `entity_type`, `entity_name`, `changes`, `reason` and optional
boolean `create` contract. The mechanical checks cannot establish literary
truth: the separate audit compares every proposed fact with the manuscript.

For coordinated historical revisions use `mode=revision` and an ordered list of
all changed chapters. They become one direct-child commit; they are not accepted
one at a time and do not increase the chapter count. The context includes full
accepted prose from immediately before the earliest revised chapter through the
latest chapter, so consequential contradictions can be reviewed. An oversized
review fails rather than silently omitting later chapters.

## Context and editorial responsibilities

The baseline is frozen at the exact Git commit. A packet combines current
character/resource projections, full recent prose, nonduplicated recent recaps,
and source hashes. The current-character projection is shared with `state_probe`.
Plans and the compass are labelled as plans. Effective listed directory rulings
are copied with their addresses; temporary handoff text is not duplicated.

Reviewers can call `novel_bench_read` to obtain an earlier full chapter, journal
or history from this frozen baseline. The tool cannot read live files or another
submission. It reports hashes, exact ranges and whether more text remains.
Reading a file is evidence of availability, not automatic proof of understanding:
each reviewer records the material actually considered.

The literary editor first judges scene change, character choice, consequence,
reader interest and repetition across nearby chapters, then causal continuity and
language. A quiet chapter need not invent a fight or level-up to pass. The ledger
auditor independently checks facts, timing, resources, knowledge and preservation
of valid contributions. Neither agent writes an acceptance decision for the
director or rewrites the prose on rejection.

The native index stores a **navigation preview**, not a full chapter recap.
`summary.md` remains complete and may have multiple paragraphs. The preview
bundle links that file and its hash. Consumers needing full context read it or
the prose, rather than forcing summaries into one long paragraph. Existing
novel index semantics are unchanged.

## Preview, manual decision and delivery

Both passing reports bind exact review fingerprints. Before the manual gate,
the deterministic stage checks the unchanged source base, replays the entire
journal history from `novel-genesis`, constructs an isolated candidate, checks
permitted file changes and repeats the exact-commit replay guard. No live novel
file changes during this work.

The stage exposes complete submitted prose, both independent reports, the exact
ledger, `semantic_changes.json` (current before/after state without recopying all
history), the full `candidate.patch`, and an approval manifest containing the
candidate commit and stage hash. Inspect all material needed for the actual
change; unchanged historical bytes are verified mechanically. Full history
remains available in the source and patch.

The only checkpoint is `stage`. An explicit approval in the engine's durable
trace, bound to its completed stage fingerprint, permits `promote`. A seed flag,
reviewer's `passed=true`, or a caller-supplied commit is not approval. Rejection
finishes without acceptance and asks the author for a new submission ID. A source
branch, effective ruling, role, template, policy or reviewed artifact changing
before acceptance refuses promotion. No force merge resolves such a conflict.

Accepted commits are exact children of their reviewed bases and fast-forwarded
only. A retry after a crash accepts the same already-merged commit idempotently.
Source checkouts stay free of bench reports. Retained refs and immutable receipts
keep candidates recoverable instead of depending on a temporary worktree.

## Reuse and backup recovery

For a ledger-only correction a new request may set `reuse_literary_from` to a
prior bench run. Reuse is allowed only when the accepted baseline, full source
hash set, prose, chapter identities, intent, effective rulings, context and
literary contract are identical. Its receipt points to the **original** review;
it does not claim a second read. The corrected ledger is always audited anew.
Changing an actual literary dependency invalidates reuse.

After acceptance, `promote/backup_only_seed.json` contains the recovery request:

```json
{
  "version": 2,
  "operation": "backup_only",
  "project_id": "my-novel",
  "source_run_id": "the-original-bench-run-id",
  "expected_commit": "40-character-accepted-commit"
}
```

First settle or stop the original backup executor; do not start concurrent
backup jobs. Recovery verifies the original accepted receipt and exact current
branch, then only retries the private backup. It never reruns writing, review,
replay or acceptance. If the branch has advanced, do not push the obsolete
recovery request: deliver the newer accepted history through its own receipt.
If implementation/policy artifacts have changed, the current conservative guard
refuses automatic recovery; an operator must restore the reviewed version or
perform a separately verified handoff. It does not bless changed evidence.

`accepted_backup_pending` and `backed_up` are distinct facts. The latter requires
an authenticated receipt proving remote head/tag equality, private repository
status and nonforce/nonmirror operation. Tests with a fake provider do not prove
a live GitHub push.

## Rollout and testing

Deploy this version beside the generated v1 workflows. Finish already-paused
legacy chapters using their original inputs and manual checkpoints; do not hot
retarget them into this graph. No migration of old chapter journals or index
formats is needed.

Run focused tests without production app fixtures:

```bash
python -m pytest -c tests/writing_bench/pytest.ini --confcutdir=tests/writing_bench tests/writing_bench
python -m pytest -c tests/writing_bench/pytest.ini --noconftest tests/unit/test_novel_configs.py tests/unit/test_novel_tools.py
```

The focused suite uses temporary synthetic novels and real Git/SkillFlow routing,
including a paused manual gate, rejection, byte/structure tampering, a simulated
post-accept backup failure and exact backup-only recovery. Model and network
boundaries are deliberately test doubles. Production enablement separately
requires boot discovery of the new tools/roles/graph and the operator policy;
never turn those tests into a claim that a restart or live backup already ran.
