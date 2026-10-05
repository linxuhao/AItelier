# Deployment process identity and semantic ledger migration

The deployment observer distinguishes launched measurement code from argument
data using a bounded grammar. A live `/proc/<pid>/cmdline` observation must agree
with the `ps` command, and `/proc/<pid>/comm` must be readable. A corroborated
Python `-m module` or `.py` script supplies the code identity; its configuration
arguments do not name another process. A corroborated native `tail` invocation
reads input paths. A direct native executable whose `/proc/exe` matches argv[0]
may carry serialized JSON settings; those settings are data rather than another
launched program. Unsupported interpreter modes, inline code, opaque shell
launchers, missing observations, malformed argv and changes between observations
keep the conservative full-command scan.

An explicit `--long-gate` signal remains active. Registered external identities
are matched against the full original command before narrowing, and create an
active process owner even when the executable name is neutral. Evaluator module
and script names remain blockers in any container. Neither a uid nor membership
in an unrelated container grants an exemption. Admission rows, operation audits,
semantic and Godot ledgers, manual override validation and its durable journal
are unchanged.

This grammar prevents webserver worker-class configuration and metric log paths
from being treated as separate measurements. Gunicorn documents `-k` and
`--worker-class` as worker-class settings in its
[settings reference](https://gunicorn.org/reference/settings/).

## Historical service observation before activation

At the process-grammar source baseline, `run_resources.enabled()` defaults to false unless
`AITELIER_ZVEC_LIFECYCLE=1` is configured. The current zvec image/entrypoint runs
the legacy filesystem scanner; it does not install or execute
`core.semantic_index_control`. The compose configuration does not configure
the demand-ledger worker. As a result, the existence of a resident zvec service
does not imply that the host control ledger has ever been initialized.

The observer must continue to reject a missing semantic ledger while that
service is resident. Creating an empty database or removing the check would
misrepresent the legacy scanner's outstanding work.

The bounded migration requires a separate service change and controlled runtime
acceptance:

1. Define ownership for the legacy project-root indexes as well as retained
   run-owned indexes. The existing control worker accepts exact managed run
   worktrees; it is not a replacement for project-root ownership.
2. Preserve and audit existing scanner jobs and daemon operations, then settle
   them before changing the writer. An absent ledger cannot attest that they
   stopped. Keep unknown operations blocked or use an explicit audited override.
3. Install the standard-library control worker and its Python runtime in the
   zvec image, share the host control directory, and replace the legacy scanner
   with one ledger-owned writer. Configure the matching host lifecycle flag in
   the same release; do not start both writers.
4. Initialize real retained-owner demand through the owning host lifecycle,
   prove ready/release generation receipts and failure retention, and verify
   actual service recovery after a controlled restart.

This process grammar is a static candidate. It does not establish controlled
restart recovery, interrupted long-gate evidence validity, eventual Godot-owner
reconciliation or a quiescent production deployment.

The combined candidate supplies the service integration described in
[semantic index activation](semantic-index-activation.md). The historical
missing-ledger observation above is not a claim about the combined source
or a claim that production has been activated.

The waiting snapshot wrapper exemption accepts only reader commands separated
by single pipeline or sequence separators. Grouped punctuation such as `||`,
conditional or background controls, redirections, substitutions and incomplete
pipelines remain opaque, even when the currently waiting child is a reader.
A reader child cannot prove that a later conditional runtime command is safe.
