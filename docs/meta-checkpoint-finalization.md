# Meta checkpoint finalization

Both ordinary REST approval routes complete the meta graph's deterministic
`finalize` tool before submitting its reviewed brief to normal scheduling.
The real emitter reads the SDK-retained completed gather payload for run-scoped
execution. Standalone utility calls continue to read their declared workspace.

Submission shares the scheduler's existing project lock. The SDK validates the
original checkpoint identity and decision on retries, including completed runs.
A later active run, or the project's primary historical run, keeps its cache,
brief and progress; a failed owner still requires explicit retry. The butler
uses the same submission logic before its ordinary build driver.

The current baseline regression demonstrated HTTP 200 with meta still running
at finalize and no project selected by the ordinary scheduler. Tests observe
both routes, real artifact emission, paused-cache synchronization and selection
before constructing one isolated build. Ordinary concurrent requests retain
actual transport observations. Failed emission remains incomplete and cannot
start an unbriefed build. Current failure observation preserves original cause
and existing HTTP translation; no historical unexplained failure is resolved by
these tests.

This is a Source change. Installation and independent review require separate
evidence; the current public SkillFlow 1.5.99 pin is unchanged.
