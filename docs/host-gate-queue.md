# Host gate queue source

`scripts/gate_queue.py` adopts the host FIFO queue as repository Source. The
first adoption commit preserves the frozen script byte for byte; the next
change separates its lifetime `daemon.lock` from its short `mutation.lock`.
There is no dependency upgrade for this repository-owned host script.

Submit, selection, admission, cancellation, finalization and restart requeue
use `mutation.lock`. Admission reads the selected ticket again under that lock
before marking it running or creating a report directory. Cancellation can
therefore withdraw a never-started queued ticket while another gate and the
daemon remain alive. Once admission wins, cancellation refuses and the gate
continues. Finalization reads disk again and only finishes a running record.
The daemon's existing lifetime lock still excludes a second daemon.

Use the existing cancel CLI with the full exact lowercase SHA and a reason.
Return codes remain 0 for cancelled/already cancelled, 3 for unknown, 4 for a
busy short mutation, and 5 for refusal. A busy transition may be retried; a
running daemon alone is no longer a reason for refusal. The first cancellation
retains its exact preimage, hash, actor and reason. Never-started cancellation
creates no gate report, exit or verdict. Cancelled tickets are terminal; use
submit for new work. Do not restore old ticket bytes while a daemon can run.

This source change does not install or activate the queue. Upgrade only during
a separately authorized maintenance window with the old daemon stopped and
its current gate settled. Old daemons know only `daemon.lock`, so mixing a new
cancel CLI with an old running daemon is unsafe. Keep the launcher and its
snapshot/platform checks unchanged. State-linked failure disposition remains
a separate, unaccepted integration.

The focused tests use only temporary owned queues, a temporary Git repository
and harmless gate children synchronized by named pipes. They exercise the real
CLI and transition functions, including both sides of cancel/admission and a
stale-finalization attempt. `QUEUE_TIMELINE` records actual child PIDs, start and
finish events, CLI output and process exits. No engine or Docker command is
used by these cases. Tests are authored, **UNRUN by the source author**.
A different author must review and run the final exact commit in a disposable
container with `--rm --init --network none --cpus 2 --memory 2g --memory-swap 2g`,
with at most four such containers globally. Never run these on the host or in
the production backend. Inside that owned container, use:

```sh
python3 -m pytest -c tests/host_gate_queue/pytest.ini --confcutdir=tests/host_gate_queue -s tests/host_gate_queue
```

Retain the first RED by running the main live-daemon withdrawal test with
`GATE_QUEUE_SOURCE` pointing at the frozen script from the first adoption
commit (or the retained matching original), then run against the final Source.
The frozen baseline SHA256 is
`283b4bbf1c39c1dd899e34cad8b2dce77426ef32d5ae38da31fa5b7c76b44ee4`.
Do not replace the original failure with later passing output.
