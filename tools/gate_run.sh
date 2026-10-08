#!/bin/sh
# Restart-proof gate runner. Runs ./run_tests.sh in a THROWAWAY container on the
# aitelier network, so a restart of the `aitelier` container cannot kill the gate.
#
# The exit code is written to a FILE, never piped: a pipe eats it.
WORKTREE="${1:?abs path to the worktree}"
BINDING="${GATE_BINDING_FILE:?frozen platform Source binding required}"
BINDING_TOOL="$(dirname "$0")/gate_binding.py"
PLATFORM_SOURCE="$(python3 "$BINDING_TOOL" source "$BINDING")" || exit 69
PLATFORM_IMAGE="$(python3 "$BINDING_TOOL" image "$BINDING")" || exit 69
REPORTS="${2:?abs path to the gate-report parent dir}"
SUFFIX="${3:-$(basename "$WORKTREE")}"
NAME="wuxia-gate-$SUFFIX-$$"
LOG="$REPORTS/$SUFFIX.gate.log"
EXITF="$REPORTS/$SUFFIX.gate.exit"
HEADF="$REPORTS/$SUFFIX.gate.head"
SNAPROOT="/home/linxuhao/.AItelier/gate-snapshots"
SNAP="$SNAPROOT/$SUFFIX-$$"

# NOTE: a linked worktree's `.git` is a FILE ("gitdir: …"), not a directory, so
# `-d` is wrong here — it refused both real round worktrees. Ask git instead.
if ! git -C "$WORKTREE" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "refusing: $WORKTREE is not inside a git work tree — a copy is not the repository" >&2
  exit 64
fi

# 2026-09-14: run_tests.sh reads the WORKING TREE, not a commit. A round that
# edits the tree while the gate is in flight produces a verdict that belongs to
# no commit at all — and the exit file looks exactly like a real one. So: refuse
# to start on a dirty tree and stamp the HEAD.
#
# 2026-09-16: the boundary checks were blind DURING the run, and two rounds in a
# row edited the gated tree mid-flight (r2 a tools/ file for ~2 min, r3 three
# design/ commits). Both happened to be invisible to every gate stage, so both
# verdicts survived — but "it happened to be harmless" is not a guarantee, and
# the second one only escaped poisoning because the round restored HEAD before
# the end-check looked. Telling implementers to be careful had already failed
# twice, so the tree the gate reads is now one they cannot reach: a DETACHED
# SNAPSHOT worktree at the stamped sha, under $SNAPROOT (inside the bind mount,
# so the Godot sidecar resolves it at the same absolute path). The source
# worktree is released the moment the snapshot exists. run_tests.sh derives
# project_dir from its own location, so the sidecar follows automatically.
if [ -n "$(git -C "$WORKTREE" status --porcelain)" ]; then
  echo "refusing: $WORKTREE is dirty — the gate gates a commit, so a dirty tree gates nothing reproducible" >&2
  exit 65
fi
HEAD_BEFORE="$(git -C "$WORKTREE" rev-parse HEAD)"
echo "$HEAD_BEFORE" > "$HEADF"
rm -f "$EXITF"

# 2026-09-19: the sidecar's /tmp had grown to 21G and took the root volume to
# 90%. One ~700M co-op run dir per gate (host+client logs, two Godot HOMEs, a
# project copy), named <scenario>-gate-<pid>, never removed. The shape of the
# leak is what matters: the path is NAMED by the caller but CREATED by the
# sidecar in its own filesystem, so whatever cleanup the caller does runs in
# the wrong container and the sidecar's copy is nobody's.
#
# Sweeping here rather than at the producer is deliberate: this is the one
# place that knows no gate is in flight (the gate's own container does not
# exist yet at this line), and the sweep is by shape, so it keeps working when
# the producer changes. It can never fail a gate: || true, and its own guard
# skips the sweep whenever a wuxia-gate-* container is alive.
sh /home/linxuhao/.AItelier/bin/godot_tmp_janitor.sh > "$REPORTS/$SUFFIX.janitor.log" 2>&1 || true

mkdir -p "$SNAPROOT"
rm -rf "$SNAP"
if ! git -C "$WORKTREE" worktree add --detach "$SNAP" "$HEAD_BEFORE" >>"$LOG" 2>&1; then
  echo "refusing: could not snapshot $HEAD_BEFORE into $SNAP" >&2
  exit 67
fi
# The snapshot IS the measured tree. Prove it is the stamped commit and nothing
# else, before spending an hour of the engine's exclusive lock on it.
SNAP_HEAD="$(git -C "$SNAP" rev-parse HEAD)"
SNAP_DIRTY="$(git -C "$SNAP" status --porcelain | wc -l)"
if [ "$SNAP_HEAD" != "$HEAD_BEFORE" ] || [ "$SNAP_DIRTY" -ne 0 ]; then
  echo "refusing: snapshot is not the stamped commit (head $SNAP_HEAD, dirty $SNAP_DIRTY)" >&2
  git -C "$WORKTREE" worktree remove --force "$SNAP" >/dev/null 2>&1
  exit 68
fi

python3 "$BINDING_TOOL" sidecar "$REPORTS/$SUFFIX.sidecar-before.json" >> "$LOG" 2>&1 || {
  echo "poisoned-platform-identity-before" > "$EXITF"
  git -C "$WORKTREE" worktree remove --force "$SNAP" >/dev/null 2>&1
  exit 69
}

docker run --rm --name "$NAME" --network aitelier_default -u 1000:1000 \
  -e HOME=/home/linxuhao -e GATE_REPORT_DIR="$REPORTS" \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e GATE_BINDING_FILE="$BINDING" \
  -v "$BINDING:$BINDING:ro" \
  -v "$PLATFORM_SOURCE:/app:ro" \
  -v "$PLATFORM_SOURCE:/home/linxuhao/AItelier:ro" \
  -v /home/linxuhao/.AItelier:/home/linxuhao/.AItelier \
  -w "$SNAP" "$PLATFORM_IMAGE" python3 /app/tools/gate_binding.py run "$BINDING" "$REPORTS/$SUFFIX.platform-identity.json" > "$LOG" 2>&1
rc=$?
identity="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["identity"])' "$REPORTS/$SUFFIX.platform-identity.json" 2>/dev/null)"
if ! python3 "$BINDING_TOOL" sidecar "$REPORTS/$SUFFIX.sidecar-after.json" "$REPORTS/$SUFFIX.sidecar-before.json" >> "$LOG" 2>&1 || [ "$identity" != "unchanged" ]; then
  echo "poisoned-platform-identity; see $SUFFIX.platform-identity.json" > "$EXITF"
  git -C "$WORKTREE" worktree remove --force "$SNAP" >/dev/null 2>&1
  exit 69
fi

# The snapshot is unreachable by the round, so a change here would mean the gate
# itself moved the tree. Still checked: an unverified assumption is how the last
# two holes got in.
SNAP_HEAD_AFTER="$(git -C "$SNAP" rev-parse HEAD 2>/dev/null)"
if [ "$SNAP_HEAD_AFTER" != "$HEAD_BEFORE" ]; then
  echo "POISONED: the snapshot moved under the gate ($HEAD_BEFORE -> $SNAP_HEAD_AFTER); real rc was $rc" >> "$LOG"
  echo "poisoned-snapshot-moved-under-the-gate" > "$EXITF"
  git -C "$WORKTREE" worktree remove --force "$SNAP" >/dev/null 2>&1
  exit 66
fi

# What the SOURCE worktree did meanwhile no longer changes the verdict — record
# it so a round that edits mid-flight is still visible, and say plainly that the
# measurement was not taken there.
SRC_HEAD_AFTER="$(git -C "$WORKTREE" rev-parse HEAD)"
SRC_DIRTY_AFTER="$(git -C "$WORKTREE" status --porcelain | wc -l)"
if [ "$SRC_HEAD_AFTER" != "$HEAD_BEFORE" ] || [ "$SRC_DIRTY_AFTER" -ne 0 ]; then
  echo "NOTE: the source worktree moved during the gate (head $HEAD_BEFORE -> $SRC_HEAD_AFTER, dirty $SRC_DIRTY_AFTER). The verdict belongs to the snapshot of $HEAD_BEFORE and is unaffected." >> "$LOG"
fi

git -C "$WORKTREE" worktree remove --force "$SNAP" >/dev/null 2>&1
rm -rf "$SNAP"
echo "$rc" > "$EXITF"

# 2026-09-20: for its whole life this script RETURNED the status of the `echo`
# above -- i.e. 0 -- no matter what the gate did. The caller printed GATE_RC=0
# over a red gate; only the exit FILE carried the verdict, and only because
# something happened to read it. Third instance of the same class
# ([[piping-a-gate-eats-its-exit-code]]): the first two were a pipe and a tee,
# this one is a trailing command. The shape is always "the last thing to run is
# not the thing being measured".
exit "$rc"
