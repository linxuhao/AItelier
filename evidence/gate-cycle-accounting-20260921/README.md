# Four recorded `run_tests` reports from one round that was never measured

Taken verbatim out of `~/.AItelier/skillflow.db`,
`skillflow_steps.outputs_json`, on 2026-09-21 by the director.

| file | step id | what the gate actually did |
|---|---|---|
| `step-6730.outputs.json` | 6730 | `rc=1` — python `2630 passed, 6 skipped`, then **GDScript parse FAILED** on two parse errors in a file this round had just written. A real red. |
| `step-6769.outputs.json` | 6769 | `rc=2` — python `2630 passed`, compile `OK (364/364)`, play-test never started: `godot-builder unreachable …: HTTP Error 409: Conflict — gate NOT run.` |
| `step-6773.outputs.json` | 6773 | `rc=2` — same shape, `2630 passed`. |
| `step-6777.outputs.json` | 6777 | `rc=2` — same shape, `2633 passed`. |

Provenance: run `aad5aa9b-6839-4156-8a64-c8c59728e85f`, attempt
`attempt-d3d146325b06482e9325578c25327448`, State DAG project `wuxia-myth`,
node `art.seven-actions-are-static-poses-with-no-motion-in-them` rev 2,
base `ddd8e51b`. The attempt died with `error: "Cycle limit exceeded"` —
`coding_impl` carries `max_loop: 3`, so four implement cycles exhausted it.
Three of those four cycles took no measurement at all.

`new_failures[0]` is 1538 characters in every one of the four: that is the
tool's own bound, so each string is a TRUNCATED tail of the gate output.
The `rc=` token survives the truncation and is present in all four.

These files are evidence, not fixtures anyone may edit. They are committed so
that a test can assert against the bytes that were really recorded, rather
than against a paraphrase, and so the assertion outlives the run that produced
them.
