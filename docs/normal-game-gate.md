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

The queue's inherited `GATE_BINDING_FILE` must name a regular JSON file:

```json
{"source":"/absolute/frozen/AItelier","head":"<40-hex commit>","tree":"<40-hex tree>","image":"sha256:<64-hex immutable backend image>","files":{"docker/godot/godot_harness.py":"<64-hex SHA256>","aitelier/tools/godot_playtest/impl.py":"<64-hex SHA256>"},"engine_sha256":"<64-hex SHA256>"}
```

Freeze a clean accepted Source containing these tools, then derive every value
from that Source and the exact image. The same Source is mounted readonly at
`/app` and `/home/linxuhao/AItelier`, with explicit `PYTHONPATH=/app`.
The actual imports, both fixture coordinates, manifest hash, SDK1.5.85 and
actual engine `/srv/godot_harness.py` loaded/disk hash and process identity are
recorded before and after `run_tests.sh` in `*.platform-identity.json`.
Named Docker sidecar CID/image/PID/start identity is recorded separately before
and after. Missing/changed identity poisons the gate, preserving its raw stage
exit in the receipt. Source movement, snapshot poisoning, queue ticket and
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

Source review and disposable CPU validation do not establish installed launcher,
active sidecar configuration, game/native/render/browser/package or public
readiness. Each remains separately measured and accepted.
