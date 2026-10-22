# Normal game gate Source ownership and installation

`tools/gate_run.sh` is the canonical AItelier launcher. Its original installed
body was adopted from `/home/linxuhao/.AItelier/bin/gate_run.sh` (6046 bytes,
SHA256 `fa88aaf3957abc2a7cbb65bedaf613cc10a8eb6a75769060b956fa49ec74d13c`).
The normal queue and `GATE_RUN_SH` dispatch still invoke that launcher, which
snapshots the game and calls its existing `run_tests.sh` four-stage gate.

An authorized operator installs an accepted exact Source with
`sh tools/install_gate_run.sh`. This replaces the normal bin launcher and its
identity helper; installation is separate from authoring or CPU validation.
No installation or activation is performed by the Source batch.

For an authorized update of only an existing launcher, use the same installer:

```sh
sh tools/install_gate_run.sh --launcher-only "$EXPECTED_INSTALLED_SHA256" "$EXPECTED_SOURCE_SHA256" "$DEST"
```

Both hashes must be explicit lowercase SHA256 values derived from the installed
preimage and the accepted Source's `tools/gate_run.sh`. They identify bytes;
they do not grant maintenance authority. This mode requires an existing regular
launcher and destination directory, checks both hashes, writes a same-directory
pending file, preserves installed mode/uid/gid, fsyncs it and atomically renames
it over `gate_run.sh`. An already-open old descriptor keeps the old bytes.
`gate_binding.py` and unrelated files are untouched. Stale hashes, malformed
arguments, missing files and symlink launchers refuse before replacement. A
failure after rename is an error, with no automatic rollback or retry.

Both installation modes take the same nonblocking advisory lock at
`DEST/.gate_run.install.lock`; a competing participant refuses. The lock file
stays in place and must not be removed during installation. It coordinates
participating installers only: other writers can ignore it, so authorization
must also exclude competing writers of this launcher. The ordinary mode still
installs both files with mode755; it is not a two-file atomic transaction.
Python3 and the existing Unix install tools are required, with no AItelier/SDK
imports, initialization, API or service operation.

Updating an unused launcher pathname is a file-only operation. Unrelated CPU
work that does not consume it need not stop. The operator must establish the
actual consumer/owner scope before installation; Source review does not prove
that a production file is unused. Backend restarts and deployment operations
retain their existing guards and physical vetoes. This installer neither calls
nor changes those guards and does not perform a backend restart or deployment.

The queue's inherited `GATE_BINDING_FILE` must name a regular JSON file:

```json
{"source":"/absolute/frozen/AItelier","head":"<40-hex commit>","tree":"<40-hex tree>","image":"sha256:<64-hex immutable backend image>","sdk":"1.5.99","files":{"docker/godot/godot_harness.py":"<64-hex SHA256>","aitelier/tools/godot_playtest/impl.py":"<64-hex SHA256>"},"engine_sha256":"<64-hex SHA256>"}
```

Freeze a clean accepted Source containing these tools. Before dispatch, the
operator must choose and freeze `sdk` from an independently reviewed exact
immutable image. It is a required exact `X.Y.Z` release string; the example
above does not approve an image. Source dependency declarations and currently
observed metadata cannot select or update this expectation. Missing pins and
extra fields refuse without migration. Actual `skillflow-py` metadata must
equal the explicit pin. Unit controls use 1.5.85 and 1.5.99 pins; those controls
do not establish runtime compatibility or approve either image.

Manifest parsing and SHA256 consume the same single read from an opened regular
non-symlink file. The launcher freezes that hash before dispatch and requires
it for Source/image selection, execution and receipt authorization. Capture
compares observations with the caller's original binding and hash. A receipt
can authorize cleanup only against that original hash, SDK pin and image;
newly observed or rebuilt expectations cannot authorize it. Each invocation
requires fresh report paths; unknown or mismatched terminal identity retains
its snapshot and preserves the observed raw exit.

The same Source is mounted readonly at
`/app` and `/home/linxuhao/AItelier`, with explicit `PYTHONPATH=/app`.
The actual imports, both fixture coordinates, manifest hash, actual pinned SDK and
actual engine `/srv/godot_harness.py` loaded/disk hash and process identity are
recorded before and after `run_tests.sh` in `*.platform-identity.json`.
Named Docker sidecar CID/image/PID/start identity is recorded separately before
and after. Incomplete observations remain unknown; observed differences remain
mismatches. Both refuse acceptance and retain the snapshot while preserving the
raw stage exit in the receipt. Source movement, snapshot poisoning, queue ticket and
normal stage exits retain their existing meaning. No absent-path skip is used.

`aitelier recreate-godot` is a closed operation on the existing initialized
runtime. It accepts `--quota-override`, `--source-binding`, exact old
`--expected-cid`, `--expected-pid`, `--expected-image`, an explicit built
immutable target `--image`, and a new absolute
`--report` path. The override may contain only godot-builder's four values:
4096 files, 536870912 bytes, 16 patterns and 4096 search entries. It acquires the
existing deployment fence, calls the initialized admin observation through the
normal local authenticated client, measures all owners, and authorizes the
existing redeploy journal action. Refusals preserve foreign owners.

The operator first builds and independently checks the normal Godot image from
the accepted Source. Only the derived Godot service override is applied: that
explicit immutable image and those quotas. The baked `/srv/godot_harness.py`
must match the frozen Source; no compatibility overlay shadows it. The
command is `up -d --no-deps --force-recreate --no-build godot-builder`.
The source, owned override and exact old process are rechecked under the fence;
the new CID/image, loaded harness hash and effective numeric quotas are checked
before successful journal finalization. Backend restart remains backend only.
A partial effect or failed health observation aborts the journal and is never
automatically replayed. Recovery requires a separately authorized fresh guarded
operation. Old paused Native/Coop owners, semantic owners and GPU work are not
cleared or claimed quiet.

The engine `/health` exposes only named source identity and effective numeric
limits. It reveals no environment or secrets. Retention defaults and omitted
retain behavior remain unchanged. The finite quotas are configured capacity;
future real artifact sufficiency is measured by the normal native run.


The ordinary launcher does not clean shared sidecar scratch: it never invokes
the godot tmp janitor, on launch, on useful execution or on any refusal, and the
absence of its own gate container proves no exclusivity over the sidecar.
Shared scratch maintenance is a separate operation that an operator must run
with the actual owner and evidence checked, never as a gate side effect.

Source review and disposable CPU validation do not establish installed launcher,
active sidecar configuration, game/native/render/browser/package or public
readiness. Each remains separately measured and accepted.
