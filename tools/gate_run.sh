#!/bin/sh
# Restart-proof gate runner. Runs ./run_tests.sh in a THROWAWAY container on the
# aitelier network, so a restart of the `aitelier` container cannot kill the gate.
#
# The exit code is written to a FILE, never piped: a pipe eats it.
WORKTREE="${1:?abs path to the worktree}"
BINDING="${GATE_BINDING_FILE:?frozen platform Source binding required}"
BINDING_TOOL="$(dirname "$0")/gate_binding.py"
REPORTS="${2:?abs path to the gate-report parent dir}"
SUFFIX="${3:-$(basename "$WORKTREE")}"
NAME="wuxia-gate-$SUFFIX-$$"
LOG="$REPORTS/$SUFFIX.gate.log"
EXITF="$REPORTS/$SUFFIX.gate.exit"
HEADF="$REPORTS/$SUFFIX.gate.head"
SNAPROOT="/home/linxuhao/.AItelier/gate-snapshots"
SNAP="$SNAPROOT/$SUFFIX-$$"

# A suffix names one invocation. Refuse prior evidence without altering it.
# Checked before anything is observed or written, so a second invocation cannot
# overwrite the record of the first.
for report in "$LOG" "$EXITF" "$HEADF" "$REPORTS/$SUFFIX.platform-identity.json" \
  "$REPORTS/$SUFFIX.platform-binding.json" \
  "$REPORTS/$SUFFIX.sidecar-before.json" "$REPORTS/$SUFFIX.sidecar-after.json"; do
  if [ -e "$report" ] || [ -L "$report" ]; then
    echo "refusing: existing gate report $report; prior evidence retained" >&2
    exit 69
  fi
done

# 2026-11-04 (owner ruling, wuxia iss-5e51a56608c64f60): the frozen binding is a
# RECORD OF REVIEW, not a pre-dispatch licence to refuse. This used to `exit 69`
# here -- before running anything -- whenever one literal was stale, so a single
# platform rebuild (2026-10-24 -> 11-03) stopped EVERY project's every gate in
# seconds and made "did not measure" look exactly like "measured and failed".
# So the binding is read, the platform identity is OBSERVED and recorded beside
# the receipt ($SUFFIX.platform-binding.json), and the gate ALWAYS runs.
#
# What is still fatal here is only "there is nothing to dispatch": an unreadable
# or malformed binding names no Source and no image, and inventing either from
# whatever is on disk would measure a platform nobody reviewed.
BINDING_SHA256="$(python3 "$BINDING_TOOL" sha256 "$BINDING")" || exit 69
PLATFORM_SOURCE="$(python3 "$BINDING_TOOL" source "$BINDING" --binding-sha256 "$BINDING_SHA256")" || exit 69
PLATFORM_IMAGE="$(python3 "$BINDING_TOOL" image "$BINDING" --binding-sha256 "$BINDING_SHA256")" || exit 69

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
# Now that this invocation is going to run, record what the platform looked like
# at dispatch: which reviewed fields already differ, observed vs bound. Recorded,
# not refused -- the receipt carries the same comparison from inside the gate.
python3 "$BINDING_TOOL" sha256 "$BINDING" --observe "$REPORTS/$SUFFIX.platform-binding.json" \
  --binding-sha256 "$BINDING_SHA256" > /dev/null 2>> "$LOG" || {
  echo "refusing: the binding could not be observed at dispatch; see $SUFFIX.gate.log" >&2
  exit 69
}
HEAD_BEFORE="$(git -C "$WORKTREE" rev-parse HEAD)"
echo "$HEAD_BEFORE" > "$HEADF"


mkdir -p "$SNAPROOT"
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
  echo "unknown-platform-identity-before" > "$EXITF"
  git -C "$WORKTREE" worktree remove --force "$SNAP" >/dev/null 2>&1
  exit 69
}

docker run --rm --init --name "$NAME" --network aitelier_default -u 1000:1000 \
  --cpus 2 --memory 2g --memory-swap 2g --pids-limit 512 \
  --cap-drop ALL --security-opt no-new-privileges \
  -e HOME=/home/linxuhao -e GATE_REPORT_DIR="$REPORTS" \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 -e GATE_BINDING_FILE="$BINDING" \
  -v "$BINDING:$BINDING:ro" \
  -v "$PLATFORM_SOURCE:/app:ro" \
  -v "$PLATFORM_SOURCE:/home/linxuhao/AItelier:ro" \
  -v /home/linxuhao/.AItelier:/home/linxuhao/.AItelier \
  -w "$SNAP" "$PLATFORM_IMAGE" python3 /app/tools/gate_binding.py --binding-sha256 "$BINDING_SHA256" run "$BINDING" "$REPORTS/$SUFFIX.platform-identity.json" > "$LOG" 2>&1
rc=$?
identity="$(python3 "$BINDING_TOOL" receipt "$REPORTS/$SUFFIX.platform-identity.json" "$rc" --binding-sha256 "$BINDING_SHA256" 2>> "$LOG")" || identity=unknown
python3 "$BINDING_TOOL" sidecar "$REPORTS/$SUFFIX.sidecar-after.json" "$REPORTS/$SUFFIX.sidecar-before.json" >> "$LOG" 2>&1
sidecar_rc=$?
if [ "$sidecar_rc" -eq 70 ]; then
  identity=mismatch
elif [ "$sidecar_rc" -ne 0 ] && [ "$identity" != "mismatch" ]; then
  identity=unknown
fi
if [ "$identity" != "unchanged" ]; then
  [ "$identity" = "mismatch" ] || identity=unknown
  echo "$identity-platform-identity; real rc was $rc; snapshot retained; see $SUFFIX.platform-identity.json" > "$EXITF"
  # A client exit alone does not settle consumers of this exact snapshot.
  exit 69
fi

# `unchanged` only says the instrument did not MOVE while the gate ran. Whether
# it is the platform a human reviewed is a second question, answered from the
# receipt against the hash frozen before dispatch. It is never a reason to stop
# or to relabel the verdict:
#   reviewed            -> the real stage exit is release evidence when green
#   unreviewed/unknown  -> the real stage exit stays the verdict, and the flag
#                          sits on the line after it: a MEASUREMENT, not
#                          release evidence (owner ruling, 2026-11-04).
platform="$(python3 "$BINDING_TOOL" platform "$REPORTS/$SUFFIX.platform-identity.json" --binding-sha256 "$BINDING_SHA256" 2>> "$LOG")" || platform="unknown platform record unavailable"

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
case "$platform" in
  reviewed)
    echo "$rc" > "$EXITF" ;;
  *)
    printf '%s\nplatform_unreviewed %s\n' "$rc" "$platform" > "$EXITF"
    echo "NOTE: identity is not the reviewed binding ($platform); the real stage exit was $rc and it is a MEASUREMENT, not release evidence. See $SUFFIX.platform-binding.json and $SUFFIX.platform-identity.json" >> "$LOG" ;;
esac

# 2026-09-20: for its whole life this script RETURNED the status of the `echo`
# above -- i.e. 0 -- no matter what the gate did. The caller printed GATE_RC=0
# over a red gate; only the exit FILE carried the verdict, and only because
# something happened to read it. Third instance of the same class
# ([[piping-a-gate-eats-its-exit-code]]): the first two were a pipe and a tee,
# this one is a trailing command. The shape is always "the last thing to run is
# not the thing being measured".
exit "$rc"
